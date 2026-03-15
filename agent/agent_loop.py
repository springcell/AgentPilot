"""
agent_loop.py — AI agent main loop
Flow: env collect -> task -> AI plan -> local exec (shell+file_op) -> result back -> AI continues
Uses AgentPilot web bridge (ChatGPT Web CDP), no API key
"""

import os
import json
import time
import urllib.request
import urllib.error
from executor import run_from_text, extract_json_blocks
from env_context import collect as collect_env, to_prompt_block, inject_env_vars
from file_ops import schema_hint, _read_text
from skill_manager import skills_to_prompt, save_skill_from_success

# Config (edit as needed)
CHAT_URL = os.environ.get("AGENTPILOT_URL", "http://127.0.0.1:3000/chat")
MAX_ITERATIONS = 20
EXEC_TIMEOUT = 60

_PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.txt")

_DEFAULT_PROMPT = """\
You are an AI execution expert; run only in web UI, interact with Windows via JSON blocks.

## Core rules
1. Break task into steps; every step must have a JSON block.
2. For save/write/create file/put on desktop, output a JSON block in the same reply.
3. For Windows operations, output: {"command":"powershell","arguments":["line1","line2"]}
4. command: powershell / cmd / python only.
5. After [Execution result feedback], continue or declare done.
6. When done output: ✅ Task complete: <one-line summary>
7. Be concise; follow format strictly.
"""

def _load_prompt() -> str:
    if not os.path.exists(_PROMPT_FILE):
        with open(_PROMPT_FILE, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_PROMPT)
        print(f"Generated default prompt: {_PROMPT_FILE}")
    with open(_PROMPT_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    print(f"Loaded prompt: {_PROMPT_FILE} ({len(content)} chars)")
    return content

SYSTEM_PROMPT = _load_prompt()
_session_has_chat = False

import re as _re

_INTERMEDIATE_PATTERNS = [
    r"正在搜索", r"正在思考", r"正在浏览", r"正在查找",
    r"Searching", r"Thinking", r"Looking up", r"Browsing",
]
_PREFIX_RE = _re.compile(
    r"^[\s\S]{0,20}?ChatGPT\s*[^\n]*[：:]\s*", _re.IGNORECASE
)

def _is_intermediate(text: str) -> bool:
    if not text:
        return True
    t = text.strip()
    if len(t) < 5:
        return True
    if "✅ Task complete" in t or "Task complete:" in t or "✅ 任务完成" in t or "任务完成：" in t:
        return False
    if any(k in t for k in ('"command"', '```json', '```')):
        return False
    cleaned = _PREFIX_RE.sub("", t).strip()
    for pat in _INTERMEDIATE_PATTERNS:
        if _re.search(pat, cleaned, _re.IGNORECASE):
            return True
    return False


_REFUSAL_PATTERNS = [
    r"无法直接", r"无法启动", r"无法执行", r"无法运行", r"无法访问",
    r"目前无法", r"暂时无法", r"无法为您",
    r"不能直接", r"不支持直接", r"请手动", r"请您手动", r"建议手动",
    r"你可以手动", r"您可以手动", r"打开命令提示符", r"打开终端",
    r"I(?:'m| am) unable", r"I cannot", r"I can't",
    r"tool.*unavailable", r"unavailable.*tool", r"is unavailable",
    r"not available.*at this time", r"currently unavailable",
    r"assist.*another way", r"cannot.*assist.*with this",
]

# AI 声称已完成操作但没有 JSON 块（"假完成"）
_FAKE_DONE_PATTERNS = [
    r"I have (written|saved|created|placed|put)",
    r"has been (written|saved|created|placed|put)",
    r"file (has been|was) (written|saved|created)",
    r"already (written|saved|created)",
    r"我已经?(写入|保存|创建|放置|写好|整理好).{0,20}(文件|内容)",
    r"文件(已经?|已)??(写入|保存|创建)",
]

def _task_done_marker(text: str) -> bool:
    return (
        "✅ Task complete" in text or "Task complete:" in text
        or "✅ 任务完成" in text or "任务完成：" in text
    )

def _cannot_complete_marker(text: str) -> bool:
    return "无法完成此任务" in text or "Cannot complete this task" in text

def _is_refusal(text: str) -> bool:
    """Whether AI returned a refusal (no JSON)."""
    if '"command"' in text and any(c in text for c in ('```json', '"powershell"', '"cmd"', '"python"', '"file_op"')):
        return False
    for pat in _REFUSAL_PATTERNS:
        if _re.search(pat, text, _re.IGNORECASE):
            return True
    return False


def _is_fake_done(text: str) -> bool:
    """Whether AI claims it already wrote/saved a file but provided no JSON block."""
    if '"command"' in text:
        return False
    for pat in _FAKE_DONE_PATTERNS:
        if _re.search(pat, text, _re.IGNORECASE):
            return True
    return False


_ACTION_TASK_PATTERNS = [
    r"启动|打开|运行|执行|安装|部署|修复|修改|创建|生成|写入|保存|上传|推送|提交",
    r"start|launch|open|run|execute|install|fix|create|write|save|push|commit|deploy",
    r"git\s+(push|pull|commit|add|checkout)",
    r"\.py|\.exe|\.ps1|\.bat|\.js",
    r"桌面|desktop|文件夹|directory|仓库|repository",
]

def _is_action_task(task_text: str) -> bool:
    """判断任务是否是需要本地执行操作的任务（而非纯问答）。"""
    for pat in _ACTION_TASK_PATTERNS:
        if _re.search(pat, task_text, _re.IGNORECASE):
            return True
    return False


_ASKING_WHAT_TASK_PATTERNS = [
    r"what (specific |exact )?task",
    r"what would you like",
    r"what do you want",
    r"please describe",
    r"could you (tell|describe|specify|clarify)",
    r"what (is|are) you (looking|asking|wanting)",
    r"请问.*任务|请说明.*任务|请描述.*任务",
    r"你想(让我|要我)做什么",
]

def _is_asking_what_task(text: str) -> bool:
    """AI 在询问用户想要做什么任务（没有理解任务已经在消息里了）。"""
    if '"command"' in text:
        return False
    for pat in _ASKING_WHAT_TASK_PATTERNS:
        if _re.search(pat, text, _re.IGNORECASE):
            return True
    return False


_ASK_PATH_PATTERNS = [
    r"请.{0,10}(提供|告知|确认|给出).{0,10}(路径|文件|位置)",
    r"(路径|文件).{0,10}(未知|不明|不清楚|找不到|无法确定)",
    r"请上传", r"请提供文件", r"请确认.*文件",
    r"不知道.*路径", r"没有.*路径", r"未找到.*文件",
    r"file.*not found",
    r"please (provide|give).{0,20}(path|file|location)",
    r"please upload (the )?file",
]

def _is_asking_for_path(text: str) -> bool:
    """Whether AI is asking user for file path instead of searching."""
    if '"command"' in text and any(c in text for c in ('```json', '"powershell"', '"cmd"', '"python"', '"file_op"')):
        return False
    for pat in _ASK_PATH_PATTERNS:
        if _re.search(pat, text, _re.IGNORECASE):
            return True
    return False


def _local_find_files(task_text: str, env_info: dict) -> str:
    """Search desktop/documents/downloads for filenames mentioned in task; return formatted result for AI."""
    desktop   = env_info.get("desktop", "")
    documents = env_info.get("documents", "")
    downloads = env_info.get("downloads", "")
    search_dirs = [d for d in [desktop, documents, downloads] if d and os.path.isdir(d)]

    names = _re.findall(r'[\w\-]+\.[a-zA-Z]{2,4}', task_text)
    bare = _re.findall(r'(?<!\w)([a-zA-Z][\w\-]{1,20})(?!\.\w)', task_text)
    for b in bare:
        names.append(b + '.py')

    if not names:
        names = ['*.py']

    found_files = []
    seen = set()
    for name in names:
        for base in search_dirs:
            for root, dirs, files in os.walk(base):
                depth = root[len(base):].count(os.sep)
                if depth >= 4:
                    dirs.clear()
                    continue
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for f in files:
                    match = (f.lower() == name.lower()) if '*' not in name else \
                            _re.match(_re.escape(name).replace(r'\*', '.*'), f, _re.IGNORECASE)
                    if match:
                        full = os.path.join(root, f)
                        if full not in seen:
                            seen.add(full)
                            found_files.append(full)

    if not found_files:
        return ""

    lines = ["[Local search result]", f"Found the following files on this machine; use these paths to continue:"]
    for p in found_files[:10]:
        lines.append(f"  {p}")
    lines.append("Use the paths above to continue (read content, run and fix).")
    return "\n".join(lines)


_PATH_RE = _re.compile(
    r'[A-Za-z]:\\(?:[^\s\'"<>|*?\r\n\\][^\s\'"<>|*?\r\n]*\\)*[^\s\'"<>|*?\r\n\\]+'
    r'|(?<!\w)[\w\-. ]+\.py\b',
)

def _run_py_get_error(path: str) -> str:
    import subprocess, sys
    try:
        r = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            cwd=str(_re.sub(r'[^\\]+$', '', path).rstrip('\\') or '.'),
        )
        if r.returncode != 0 and r.stderr.strip():
            lines = r.stderr.strip().splitlines()
            return "\n".join(lines[:60])
    except Exception as e:
        return str(e)
    return ""


def _enrich_task(task_text: str, env_info: dict) -> str:
    """Scan task for local file paths; read content and run errors; append so AI need not ask for uploads."""
    desktop = env_info.get("desktop", "")
    extras = []
    candidates = _PATH_RE.findall(task_text)
    dir_name_re = _re.compile(r'[\u4e00-\u9fff\w]{2,}(?=目录|文件夹|游戏|项目)?')
    if desktop and _re.search(r'桌面.*?(?:中|里|的|目录|文件夹)', task_text):
        for m in dir_name_re.finditer(task_text):
            candidate_dir = os.path.join(desktop, m.group())
            if os.path.isdir(candidate_dir):
                for fname in os.listdir(candidate_dir):
                    if fname.endswith('.py'):
                        candidates.append(os.path.join(candidate_dir, fname))

    seen = set()
    for raw_path in candidates:
        if not os.path.isabs(raw_path) and desktop:
            raw_path = os.path.join(desktop, raw_path)
        path = os.path.normpath(raw_path)
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)

        try:
            content = _read_text(path)
            lines = content.splitlines()
            truncated = len(lines) > 200
            preview = "\n".join(lines[:200])
            if truncated:
                preview += f"\n... ({len(lines)} lines, truncated)"
            extras.append(f"\n## File content: {path}\n```python\n{preview}\n```")
        except Exception as e:
            extras.append(f"\n## File read failed: {path}\nError: {e}")
            continue

        # 尝试运行，获取报错
        if path.endswith('.py'):
            err = _run_py_get_error(path)
            if err:
                extras.append(f"\n## Run error: {path}\n```\n{err}\n```")
            else:
                extras.append(f"\n## Run status: {path} starts without error")

    if not extras:
        return task_text

    print(f"   [preprocess] Injected {len(seen)} local file context(s)")
    return task_text + "\n" + "\n".join(extras)


POLL_URL      = CHAT_URL.replace("/chat", "/poll")
POLL_INTERVAL = 3
POLL_TIMEOUT  = 300
STALL_SEC     = 20


def _http_get(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def chat_via_bridge(message: str, new_chat: bool = False, agent_id: str = "default") -> str:
    """Send message and return reply; on intermediate state poll /poll for JSON or task complete."""
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
                f"AgentPilot bridge returned 404. Start API first:\n"
                f"  In another terminal: npm run api\n"
                f"Raw error: {err_body}"
            )
        raise RuntimeError(f"AgentPilot request failed ({e.code}): {err_body}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot connect to AgentPilot; run npm run api first: {e.reason}")

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Unknown error"))

    result = data.get("result", "")

    if _task_done_marker(result):
        return result
    if _cannot_complete_marker(result):
        return result
    if not _is_intermediate(result):
        return result

    print(f"   [Python] Intermediate '{result[:60].replace(chr(10),' ')}', polling /poll for JSON...")
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
        print(f"   [poll] generating={generating} len={len(current)}: "
              f"{current[:60].replace(chr(10), ' ')}")

        if _task_done_marker(current):
            print(f"   [poll] Task complete")
            return current
        if _cannot_complete_marker(current):
            print(f"   [poll] AI cannot complete this task")
            return current
        if not _is_intermediate(current):
            print(f"   [poll] Full reply len={len(current)}")
            return current

        if current != last_text:
            last_text     = current
            last_change_t = time.time()

        if not generating and (time.time() - last_change_t) > STALL_SEC:
            print(f"   [poll] Stall {STALL_SEC}s no update, forcing continue")
            return current

    poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
    return poll.get("text", result)


def run_agent(user_task: str, verbose: bool = True) -> str:
    global _session_has_chat

    task_text = user_task.strip()
    force_new = task_text.lower().startswith("/new")
    if force_new:
        task_text = task_text[4:].strip()

    # /new 单独使用（不带任务）：只重置会话窗口，不发任何消息
    if force_new and not task_text:
        _session_has_chat = False
        print("🔄 New conversation window will open on next task.")
        return ""

    if not task_text:
        task_text = user_task

    new_chat = force_new or not _session_has_chat
    if new_chat:
        _session_has_chat = True

    prompt = _load_prompt()
    env_info = collect_env()
    inject_env_vars(env_info)
    env_block = to_prompt_block(env_info)
    file_ops_hint = schema_hint()
    enriched_task = _enrich_task(task_text, env_info)
    skill_hint = skills_to_prompt(task_text)

    first_message = (
        f"{prompt}\n\n"
        f"{env_block}\n\n"
        f"{file_ops_hint}\n\n"
        + (f"{skill_hint}\n\n" if skill_hint else "")
        + f"---\n\n"
        + f"User task (execute now, no questions): {enriched_task}"
    )
    messages_to_send = [first_message]
    executed_blocks: list[dict] = []
    iteration     = 0
    agent_id_main = "default"

    if skill_hint:
        print(f"   [skills] Matched history skill injected into prompt")

    print(f"\n{'='*60}")
    print(f"Task: {task_text}" + (" (new window)" if new_chat and force_new else ""))
    print(f"Desktop: {env_info.get('desktop', '')}")
    print(f"{'='*60}\n")

    while iteration < MAX_ITERATIONS:
        iteration += 1

        if verbose:
            print(f"[{iteration}] AI thinking...")

        try:
            ai_text = chat_via_bridge(messages_to_send[-1], new_chat=new_chat, agent_id=agent_id_main)
        except RuntimeError as e:
            print(f"\nError: {e}")
            return ""

        new_chat = False

        if verbose:
            print(f"\n[AI reply]\n{ai_text}\n")

        if _cannot_complete_marker(ai_text):
            print(f"\nAI cannot complete this task")
            return ai_text

        blocks = extract_json_blocks(ai_text)

        if not blocks:
            if _is_asking_for_path(ai_text):
                find_result = _local_find_files(task_text, env_info)
                if find_result:
                    print(f"\nAI asked for path; local search done, sending result back...")
                    messages_to_send.append(find_result)
                    continue

            if _is_refusal(ai_text) and iteration <= 2 and _is_action_task(task_text):
                print(f"\nAI refused direct action; retrying with JSON command request...")
                messages_to_send.append(
                    "You said you cannot execute directly, but you can output a powershell or python JSON block "
                    "for the local program to run. Do not refuse; output the JSON block to complete the task."
                )
                continue

            # 情况D：AI 声称已写入/保存文件但没有 JSON 块（"假完成"）
            if _is_fake_done(ai_text) and iteration <= 2 and _is_action_task(task_text):
                print(f"\nAI claimed file was written but produced no JSON block; requesting actual JSON...")
                messages_to_send.append(
                    "You described writing a file but did not output any JSON block. "
                    "No file was actually written—the local program only executes JSON blocks. "
                    "Output a file_op write JSON block now with the full file content."
                )
                continue

            # 情况E：AI 在询问"要做什么任务"——直接把原始任务重发一遍
            if _is_asking_what_task(ai_text) and iteration == 1:
                print(f"\nAI asked what to do; re-sending task directly...")
                messages_to_send.append(
                    f"The task is already stated. Execute it now without asking questions:\n\n{task_text}"
                )
                continue

            if verbose and ai_text:
                preview = ai_text[:200] + ("..." if len(ai_text) > 200 else "")
                print(f"\nNo exec instruction (received {len(ai_text)} chars)")
                print(f"   Preview: {preview!r}")
            print(f"\nAgent done (no more instructions)")
            return ai_text

        if verbose:
            print(f"[{iteration}] Executing {len(blocks)} local instruction(s)...")

        results, feedback = run_from_text(ai_text)

        from json_parser import extract_json_blocks as _parse_blocks
        for block, r in zip(_parse_blocks(ai_text), results):
            if r.get("success"):
                executed_blocks.append(block)

        if verbose:
            print(f"\n[Execution result]\n{feedback}\n")

        messages_to_send.append(feedback)

        if _task_done_marker(ai_text):
            print(f"\nAgent declared task complete")
            if executed_blocks:
                skill_name = save_skill_from_success(task_text, executed_blocks)
                print(f"   [skills] Saved skill: {skill_name}")
            return ai_text

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            print(f"[{iteration}] {len(failed)} instruction(s) failed, AI will retry...")

        time.sleep(0.5)

    print(f"\nMax iterations ({MAX_ITERATIONS}) reached, stopping")
    return messages_to_send[-1] if messages_to_send else ""


def _read_multiline(prompt: str) -> str:
    """Read multiline input; empty line submits; 'quit'/'exit'/'q' to exit."""
    print(prompt)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not lines and line.strip().lower() in ("quit", "exit", "q"):
            return line.strip()
        if line == "":
            if lines:
                break
            continue
        lines.append(line)
    return "\n".join(lines)


def interactive_mode():
    print("╔══════════════════════════════════════╗")
    print("║   Windows AI Agent Executor v1.0     ║")
    print("║  Enter task; AI plans and runs it    ║")
    print("║  (AgentPilot web bridge, no API key) ║")
    print("╚══════════════════════════════════════╝")
    print(f"Prompt file: {_PROMPT_FILE}")
    print("Type 'quit' to exit, '/new task' for new window")
    print("Multiline: newline to continue, empty line to submit. Single line: Enter to submit.")
    print("Ensure AgentPilot is running: npm run api\n")

    while True:
        task = _read_multiline("Task (empty line to submit)> ")
        if task.lower() in ("quit", "exit", "q"):
            print("退出")
            break
        if not task.strip():
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