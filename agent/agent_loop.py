"""
agent_loop.py — AI智能体主循环
完整闭环：环境采集 → 任务 → AI规划 → 本地执行(shell+file_op) → 结果回传 → AI继续
使用 AgentPilot 网页桥（ChatGPT Web CDP）替代 API，无需 API Key
"""

import os
import json
import time
import urllib.request
import urllib.error
from executor import run_from_text, extract_json_blocks
from env_context import collect as collect_env, to_prompt_block, inject_env_vars
from file_ops import schema_hint

# ──────────────────────────────────────────
# 配置区（按需修改）
# ──────────────────────────────────────────
CHAT_URL = os.environ.get("AGENTPILOT_URL", "http://127.0.0.1:3000/chat")
MAX_ITERATIONS = 10          # 单任务最大自动执行轮次
EXEC_TIMEOUT = 60            # 每条命令执行超时（秒）

# ── 从外部 txt 文件加载 System Prompt ──────
_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")

_DEFAULT_PROMPT = """\
你是AI执行专家，只能在web端运行，通过JSON代码块与Windows本地环境交互。

## 核心规则

1. 收到任务后立即拆解步骤，每一步都必须有对应的JSON代码块
2. 严禁只输出文字描述后停止 —— 凡是涉及"保存/写入/创建文件/放到桌面"的任务，必须在同一条回复中输出JSON代码块
3. 凡是需要在Windows执行的操作，立即输出以下格式的JSON代码块：

\`\`\`json
{
  "command": "powershell",
  "arguments": [
    "第一行命令",
    "第二行命令"
  ]
}
\`\`\`

4. command 只能是：powershell / cmd / python
5. 收到 [执行结果反馈] 后，根据结果继续下一步或宣布完成
6. 任务完成时输出：✅ 任务完成：<一句话总结>
7. 回答简洁，不解释，严格按格式

## 重要约束

- 搜索类任务：先在web端整理好内容，然后必须紧接着输出写入桌面文件的JSON代码块，不得等待用户再次要求
- 禁止询问"是否需要生成文件" —— 任务里含"放在桌面"就直接生成
- 每次回复最多包含一个JSON代码块，执行完收到反馈再输出下一个\
"""

def _load_prompt() -> str:
    """读取 system_prompt.txt；不存在时自动生成默认文件。"""
    if not os.path.exists(_PROMPT_FILE):
        with open(_PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_PROMPT)
        print(f"📝 已生成默认 prompt 文件: {_PROMPT_FILE}")
    with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    print(f"✅ 已加载 prompt: {_PROMPT_FILE}  ({len(content)} 字符)")
    return content

# 启动时加载一次（run_agent 内会再次热重载）
SYSTEM_PROMPT = _load_prompt()

# 会话状态：是否已开启过对话（保持单窗口，除非 /new）
_session_has_chat = False


# ── 中间状态识别 ───────────────────────────────────────────
import re as _re

# 用 search 而非 match，兼容 "ChatGPT 说：\n正在搜索..." 等前缀变体
_INTERMEDIATE_PATTERNS = [
    r"正在搜索", r"正在思考", r"正在浏览", r"正在查找",
    r"Searching", r"Thinking", r"Looking up", r"Browsing",
]
# AI 包装前缀（去掉后再判断）
_PREFIX_RE = _re.compile(
    r"^[\s\S]{0,20}?ChatGPT\s*[^\n]*[：:]\s*", _re.IGNORECASE
)

def _is_intermediate(text: str) -> bool:
    """
    判断是否为 ChatGPT 中间过渡状态。
    兼容前缀：'ChatGPT 说：\n正在搜索...' / '正在搜索...' 等所有变体。
    有 JSON 特征(command/```json)时一律返回 False，不误判正常回复。
    """
    if not text:
        return True
    t = text.strip()
    if len(t) < 5:
        return True
    # 任务完成标记 → 一定不是中间状态（含 fallback 防 emoji 丢失）
    if "✅ 任务完成" in t or "任务完成：" in t:
        return False
    # 有 JSON 特征 → 肯定是正常回复
    if any(k in t for k in ('"command"', '```json', '```')):
        return False
    # 去掉 "ChatGPT 说：" 等前缀再匹配
    cleaned = _PREFIX_RE.sub("", t).strip()
    for pat in _INTERMEDIATE_PATTERNS:
        if _re.search(pat, cleaned, _re.IGNORECASE):
            return True
    return False


POLL_URL      = CHAT_URL.replace("/chat", "/poll")
POLL_INTERVAL = 3      # 轮询间隔（秒）
POLL_TIMEOUT  = 300    # 最长等待（秒）
STALL_SEC     = 20     # 文本无更新超过此秒数视为卡死，避免长时间等待


def _http_get(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def chat_via_bridge(message: str, new_chat: bool = False, agent_id: str = "default") -> str:
    """
    发送消息并返回回复。中间状态时轮询 /poll 等待 JSON 或任务完成。
    """
    body = json.dumps({"message": message, "newChat": new_chat, "agentId": agent_id}).encode("utf-8")
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=200) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else str(e)
        if e.code == 404:
            raise RuntimeError(
                f"AgentPilot 桥接返回 404。请先启动 API：\n"
                f"  另开终端运行: npm run api\n"
                f"原始错误: {err_body}"
            )
        raise RuntimeError(f"AgentPilot 请求失败 ({e.code}): {err_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 AgentPilot，请先运行 npm run api: {e.reason}")

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "未知错误"))

    result = data.get("result", "")

    # 任务完成 → 最高优先级，直接返回
    if "✅ 任务完成" in result or "任务完成：" in result:
        return result

    # 无法完成此任务 → 直接返回
    if "无法完成此任务" in result:
        return result

    # 非中间状态 → 直接返回（已有 JSON 或完整回复）
    if not _is_intermediate(result):
        return result

    # 中间状态（正在搜索等）→ 轮询等待 JSON 或任务完成
    print(f"   [Python] ⚠️ 中间状态 '{result[:60].replace(chr(10),' ')}'，轮询 /poll 等待 JSON...")
    deadline      = time.time() + POLL_TIMEOUT
    last_text     = result
    last_change_t = time.time()

    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
        if not poll.get("ok"):
            continue
        current    = poll.get("text", "")
        generating = poll.get("generating", True)
        print(f"   [轮询] generating={generating} len={len(current)}: "
              f"{current[:60].replace(chr(10), ' ')}")

        # 任务完成标记 → 直接返回
        if "✅ 任务完成" in current or "任务完成：" in current:
            print(f"   [轮询] ✅ 任务完成")
            return current

        # 无法完成此任务 → 直接返回
        if "无法完成此任务" in current:
            print(f"   [轮询] ⚠️ 无法完成此任务")
            return current

        # 非中间状态（含 JSON）→ 直接返回
        if not _is_intermediate(current):
            print(f"   [轮询] ✅ 完整回复 len={len(current)}")
            return current

        # 文本有变化 → 重置卡死计时
        if current != last_text:
            last_text     = current
            last_change_t = time.time()

        # 卡死检测：generating=false + 中间状态 + 文本长时间不变
        if not generating and (time.time() - last_change_t) > STALL_SEC:
            print(f"   [轮询] ⚠️ 卡死 {STALL_SEC}s 无更新，强制继续")
            return current

    # 超时兜底
    poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
    return poll.get("text", result)


def run_agent(user_task: str, verbose: bool = True) -> str:
    global _session_has_chat

    # 解析 /new 指令：仅此时开新窗口
    task_text = user_task.strip()
    force_new = task_text.lower().startswith("/new")
    if force_new:
        task_text = task_text[4:].strip() or "继续"
    if not task_text:
        task_text = user_task

    # 仅首次启动或 /new 时开新窗口，其余保持单窗口上下文
    new_chat = force_new or not _session_has_chat
    if new_chat:
        _session_has_chat = True

    prompt = _load_prompt()
    env_info = collect_env()
    inject_env_vars(env_info)
    env_block = to_prompt_block(env_info)
    file_ops_hint = schema_hint()

    first_message = (
        f"{prompt}\n\n"
        f"{env_block}\n\n"
        f"{file_ops_hint}\n\n"
        f"---\n\n用户任务: {task_text}"
    )
    messages_to_send = [first_message]
    iteration     = 0
    agent_id_main = "default"

    print(f"\n{'='*60}")
    print(f"🚀 任务启动: {task_text}" + (" (新窗口)" if new_chat and force_new else ""))
    print(f"📂 桌面: {env_info.get('desktop', '')}")
    print(f"{'='*60}\n")

    while iteration < MAX_ITERATIONS:
        iteration += 1

        if verbose:
            print(f"[{iteration}] 🤖 AI思考中...")

        try:
            ai_text = chat_via_bridge(messages_to_send[-1], new_chat=new_chat, agent_id=agent_id_main)
        except RuntimeError as e:
            print(f"\n❌ 错误: {e}")
            return ""

        new_chat = False

        if verbose:
            print(f"\n[AI响应]\n{ai_text}\n")

        # 无法完成此任务 → 直接打印并返回
        if "无法完成此任务" in ai_text:
            print(f"\n⚠️ AI 无法完成此任务")
            return ai_text

        blocks = extract_json_blocks(ai_text)

        if not blocks:
            if verbose and ai_text:
                preview = ai_text[:200] + ("..." if len(ai_text) > 200 else "")
                print(f"\n⚠️ 未解析到 JSON 块（收到 {len(ai_text)} 字符）")
                print(f"   预览: {preview!r}")
            print(f"\n✅ 智能体结束（无更多指令）")
            return ai_text

        if verbose:
            print(f"[{iteration}] ⚙️  执行 {len(blocks)} 条本地指令...")

        results, feedback = run_from_text(ai_text)

        if verbose:
            print(f"\n[执行结果]\n{feedback}\n")

        messages_to_send.append(feedback)

        if "✅ 任务完成" in ai_text or "任务完成：" in ai_text:
            print(f"\n✅ 智能体宣告任务完成")
            return ai_text

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            print(f"[{iteration}] ⚠️  {len(failed)} 条指令失败，AI将重试...")

        time.sleep(0.5)

    print(f"\n⚠️  达到最大迭代次数 ({MAX_ITERATIONS})，循环终止")
    return messages_to_send[-1] if messages_to_send else ""


def interactive_mode():
    print("╔══════════════════════════════════════╗")
    print("║     Windows AI 智能体执行器 v1.0      ║")
    print("║  输入任务，AI将自动规划并本地执行      ║")
    print("║  (AgentPilot 网页桥，无需 API Key)   ║")
    print("╚══════════════════════════════════════╝")
    print(f"📄 Prompt 文件: {_PROMPT_FILE}")
    print("输入 'quit' 退出，'/new 任务' 开启新窗口")
    print("请确保已启动 AgentPilot: npm run api\n")

    while True:
        task = input("📋 任务> ").strip()
        if task.lower() in ("quit", "exit", "q"):
            print("退出")
            break
        if not task:
            continue
        run_agent(task)
        print()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
        run_agent(task)
    else:
        interactive_mode()