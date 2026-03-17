"""
agent_loop.py — AI agent main loop
Flow: env collect -> task -> AI plan -> local exec (shell+file_op) -> result back -> AI continues
Uses AgentPilot web bridge (ChatGPT Web CDP), no API key
"""

import os
import sys
import json
import time
import uuid
import urllib.request
import urllib.error
from env_context import collect as collect_env, to_prompt_block, inject_env_vars
from file_ops import schema_hint, _read_text, _is_binary_file
from loop_common import LoopRuntime
from skill_manager import (
    skills_to_prompt,
    get_identity_prompt,
    get_reviewer_prompt,
    get_skill_runtime_profile,
    match_skill_by_category,
    parse_dispatch_reply,
    infer_category,
    normalize_identity_key,
    get_agent_id_for_identity,
    get_category_for_identity,
)
from loop_flows import (
    ChatContext,
    DiagnosticsContext,
    FileOpsContext,
    FlowContext,
    IoContext,
    _build_code_loop_intro,
    _build_direct_chat_flow_message,
    _build_direct_delivery_intro,
    _build_script_then_run_intro,
    _build_script_then_run_pushback,
    _evaluate_script_then_run_state,
    _format_flow_terminal_feedback,
    _run_code_loop,
    _run_default_loop,
    _run_direct_chat_asset_flow,
    _run_direct_delivery_loop,
    _run_file_chat_first_flow,
    _run_script_then_run_loop,
)

# Config (edit as needed)
CHAT_URL = os.environ.get("AGENTPILOT_URL", "http://127.0.0.1:3000/chat")
MAX_ITERATIONS = 100
EXEC_TIMEOUT = 60

# Prompts directory — all .txt prompt files live here
_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
_LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_REPLAY_DIR = os.path.join(_LOGS_DIR, "replay")

def _load_prompt_file(name: str, fallback: str = "") -> str:
    """Load a prompt file from agent/prompts/<name>.txt; return fallback if missing."""
    path = os.path.join(_PROMPTS_DIR, name if name.endswith(".txt") else name + ".txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return fallback


def _load_json_config(name: str, fallback: dict | None = None) -> dict:
    """Load a JSON config file from agent/config/<name>.json."""
    path = os.path.join(_CONFIG_DIR, name if name.endswith(".json") else name + ".json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"   [config] Failed to load {path}: {e}")
    return dict(fallback or {})


_LOGGING_CFG = _load_json_config(
    "logging",
    {
        "event_log": os.path.join("logs", "agent-events.jsonl"),
        "replay_dir": os.path.join("logs", "replay"),
        "enable_replay": True,
        "mask_paths": True,
        "preview_limit": 240,
    },
)
_DEBUG_MODE = os.environ.get("AGENTPILOT_DEBUG", "").strip() == "1"
_STEP_MODE = os.environ.get("AGENTPILOT_STEP_MODE", "").strip() == "1"


def _event_log_path() -> str:
    raw = _LOGGING_CFG.get("event_log", os.path.join("logs", "agent-events.jsonl"))
    return raw if os.path.isabs(raw) else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), raw)


def _replay_root() -> str:
    raw = _LOGGING_CFG.get("replay_dir", os.path.join("logs", "replay"))
    return raw if os.path.isabs(raw) else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), raw)


def _mask_value(value):
    if isinstance(value, str):
        if _LOGGING_CFG.get("mask_paths", True):
            userprofile = os.environ.get("USERPROFILE", "")
            username = os.environ.get("USERNAME", "")
            if userprofile:
                value = value.replace(userprofile, r"C:\Users\***")
            if username:
                value = value.replace(f"\\{username}\\", "\\***\\")
        limit = int(_LOGGING_CFG.get("preview_limit", 240))
        return value if len(value) <= limit else value[:limit] + "...(truncated)"
    if isinstance(value, list):
        return [_mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _mask_value(v) for k, v in value.items()}
    return value


def _write_event(event: dict) -> None:
    path = _event_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _log_event(level: str, event: str, task_id: str, **extra) -> None:
    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "level": level,
        "event": event,
        "task_id": task_id,
    }
    payload.update(_mask_value(extra))
    try:
        _write_event(payload)
    except Exception as e:
        print(f"   [log] Failed to write event {event}: {e}")


def _write_replay(task_id: str, round_name: str, kind: str, content: str) -> None:
    if not _LOGGING_CFG.get("enable_replay", True):
        return
    root = os.path.join(_replay_root(), task_id)
    os.makedirs(root, exist_ok=True)
    safe_content = content if not _LOGGING_CFG.get("mask_paths", True) else _mask_value(content)
    path = os.path.join(root, f"{round_name}_{kind}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(safe_content)


def _step_pause(task_id: str, label: str) -> None:
    if not _STEP_MODE:
        return
    _log_event("INFO", "step_pause", task_id, label=label)
    try:
        input(f"[STEP] {label} — press Enter to continue...")
    except EOFError:
        pass

# 文件超过此行数时，改走 write_web 上传模式（不在 JSON 里传内容）
FILE_WEB_MODE_LINES = 80

# 无 JSON 回复时的重试次数：AI 返回的回复若不含 JSON 执行块、任务完成标记或纯对话内容，
# 则视为无效回复，会进入修复循环。最多重试 MAX_INVALID_REPLY_RETRIES 次后停止。
# 用法：每次无效回复会调用 _local_diagnose 生成分级提示（第1/2/3+次提示强度递增），
# 将提示追加到对话中让 AI 重新输出 JSON。
MAX_INVALID_REPLY_RETRIES = 10

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
    # Priority: agent/prompts/system.txt → legacy agent/system_prompt.txt → hardcoded default
    new_path = os.path.join(_PROMPTS_DIR, "system.txt")
    if os.path.isfile(new_path):
        with open(new_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        print(f"Loaded prompt: {new_path} ({len(content)} chars)")
        return content
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
    r"正在创建", r"正在生成", r"正在处理", r"正在绘制", r"正在渲染",
    r"正在上传", r"正在分析", r"正在修改", r"正在优化",
    r"Searching", r"Thinking", r"Looking up", r"Browsing",
    r"Creating", r"Generating", r"Processing", r"Drawing", r"Rendering",
    r"Uploading", r"Analyzing", r"Modifying",
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


# ── 统一合法回复判定 ────────────────────────────────────────

_CONVERSATIONAL_PATTERNS = [
    r"^\s*(yes|no|sure|ok|okay|done|noted|understood|got it)[.!]?\s*$",
    r"here (is|are) (the |an? )?(answer|explanation|summary|result)",
    r"^(the |a )?(answer|explanation|result) (is|are)\b",
]

_ACTION_TASK_PATTERNS = [
    r"启动|打开|运行|执行|安装|部署|修复|修改|创建|生成|写入|保存|上传|推送|提交|美化",
    r"start|launch|open|run|execute|install|fix|create|write|save|push|commit|deploy|edit|modify|beautify",
    r"git\s+(push|pull|commit|add|checkout)",
    r"\.py|\.exe|\.ps1|\.bat|\.js|\.pptx?|\.xlsx?|\.docx?",
    r"桌面|desktop|文件夹|directory|仓库|repository",
]

def _is_action_task(task_text: str) -> bool:
    for pat in _ACTION_TASK_PATTERNS:
        if _re.search(pat, task_text, _re.IGNORECASE):
            return True
    return False

def _has_json_block(text: str) -> bool:
    return '"command"' in text and any(
        k in text for k in ('```json', '"powershell"', '"cmd"', '"python"', '"file_op"')
    )

def _is_conversational(text: str, task_text: str) -> bool:
    if _is_action_task(task_text):
        return False
    for pat in _CONVERSATIONAL_PATTERNS:
        if _re.search(pat, text.strip(), _re.IGNORECASE):
            return True
    return False

def _is_valid_reply(text: str, task_text: str) -> bool:
    """
    Legitimate reply = has JSON block OR task-done marker OR pure conversational
    OR image was captured (downloaded_b64 injected suffix).
    Everything else is anomalous → enters repair loop.
    """
    if not text or len(text.strip()) < 3:
        return False
    if _task_done_marker(text):
        return True
    if _has_json_block(text):
        return True
    # Image was captured and saved — suffix contains [File saved to: ...]
    if "[File saved to:" in text:
        return True
    if _is_conversational(text, task_text):
        return True
    return False


# ── 本地诊断器 ──────────────────────────────────────────────

def _local_diagnose(task_text: str, ai_text: str, env_info: dict, attempt: int) -> str:
    """
    When AI returns an anomalous reply: auto-diagnose locally,
    read file content summaries, and return a graded retry instruction.

    attempt: 当前无效回复的累计次数（1-based），用于生成分级提示：
      - 1: 温和提醒需输出 JSON
      - 2: 强调必须输出 JSON，不得用纯文本描述
      - 3+: 最终警告，要求立即输出 JSON 或声明无法完成
    """
    desktop = env_info.get("desktop", "")
    desktop_cleanup_task = (
        bool(_re.search("(desktop|\u684c\u9762)", task_text, _re.IGNORECASE))
        and bool(_re.search("(delete|remove|cleanup|clean\\s*up|clear|\u5220\u9664|\u5220\u6389|\u79fb\u9664|\u6e05\u7406)", task_text, _re.IGNORECASE))
        and bool(_re.search("(image|images|photo|photos|picture|pictures|png|jpg|jpeg|gif|bmp|webp|\u56fe\u7247|\u7167\u7247|\u622a\u56fe)", task_text, _re.IGNORECASE))
    )
    desktop_image_candidates = []
    if desktop_cleanup_task and desktop and os.path.isdir(desktop):
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        try:
            for name in sorted(os.listdir(desktop)):
                full_path = os.path.join(desktop, name)
                if os.path.isfile(full_path) and os.path.splitext(name)[1].lower() in image_exts:
                    desktop_image_candidates.append(full_path)
        except Exception:
            desktop_image_candidates = []

    path_re = _re.compile(
        r'[A-Za-z]:\\(?:[^\s\'"<>|*?\r\n\\][^\s\'"<>|*?\r\n]*\\)*[^\s\'"<>|*?\r\n\\]+'
        r'|(?<!\w)[\w\-. ]+\.(?:py|pptx?|xlsx?|docx?|txt|json|js|ts|cs|cpp|java)\b',
        _re.IGNORECASE,
    )
    raw_paths = path_re.findall(task_text) + path_re.findall(ai_text)

    resolved = []
    seen = set()
    for rp in raw_paths:
        if not os.path.isabs(rp) and desktop:
            rp = os.path.join(desktop, rp)
        p = os.path.normpath(rp)
        if p not in seen and os.path.isfile(p):
            seen.add(p)
            resolved.append(p)

    diag_lines = ["[Local diagnosis]"]

    if resolved:
        diag_lines.append(f"Found {len(resolved)} file(s) on this machine:")
        for fp in resolved[:5]:
            size = os.path.getsize(fp)
            diag_lines.append(f"  EXISTS: {fp} ({size} bytes)")
            if _is_binary_file(fp):
                from file_ops import run as _fop_run
                r = _fop_run("read", {"path": fp})
                if r.get("ok"):
                    summary = r.get("content", "")[:500]
                    diag_lines.append(f"  Content summary:\n{summary}")
            else:
                try:
                    lines = _read_text(fp).splitlines()
                    preview = "\n".join(lines[:80])
                    if len(lines) > 80:
                        preview += f"\n... ({len(lines)} lines total)"
                    diag_lines.append(f"  Content ({len(lines)} lines):\n```\n{preview}\n```")
                except Exception as e:
                    diag_lines.append(f"  Read error: {e}")
    elif desktop_cleanup_task and desktop:
        diag_lines.append("No explicit file path was found in the task text, but this is a desktop file-management task.")
        diag_lines.append(f"The desktop path is known locally: {desktop}")
        if desktop_image_candidates:
            diag_lines.append(f"Found {len(desktop_image_candidates)} image file(s) on the desktop:")
            for fp in desktop_image_candidates[:8]:
                diag_lines.append(f"  IMAGE: {fp}")
        else:
            diag_lines.append("No image files were pre-detected on the desktop from local scan.")
        diag_lines.append("Do not explain permissions or environment limits.")
        diag_lines.append("Use file_op list on the desktop first, then delete the matched image files with file_op delete.")
    else:
        diag_lines.append("No local files found from task description.")

    diag_lines.append("")

    # If AI claimed tool unavailable / cannot access — call it out directly first
    if _has_tool_unavailable_claim(ai_text):
        diag_lines.append(
            "[CORRECTION] Your previous reply claimed the tool is unavailable or that you "
            "cannot access/modify files. THIS IS WRONG.\n"
            "FACT: A local executor is running on the Windows PC right now.\n"
            "FACT: file_op read/write/patch execute immediately when you output them.\n"
            "FACT: powershell and python instructions also execute immediately.\n"
            "Do NOT say 'unavailable', 'cannot access', 'please upload' — just output a valid JSON instruction."
        )
        diag_lines.append("")

    retry_prefix = "retry_level"
    try:
        if infer_category(task_text, env_info) == "write_code":
            retry_prefix = "retry_write_code_level"
    except Exception:
        retry_prefix = "retry_level"
    code_block_mode = retry_prefix == "retry_write_code_level"

    # 分级重试提示：从外部文件加载，硬编码内容作为兜底
    if attempt == 1:
        instruction = _load_prompt_file(f"{retry_prefix}_1",
            (
                "Your reply did not contain an executable JSON instruction. "
                "For write_code tasks, every executable step MUST be wrapped in a fenced ```json code block. "
                "Do not output bare JSON or plain text."
                if code_block_mode else
                "Your reply did not contain an executable JSON instruction. "
                "The local executor can execute any valid JSON object; a code fence is optional. It cannot execute plain text instructions. Please output the appropriate JSON instruction now to continue."
            )
        )
    elif attempt == 2:
        instruction = _load_prompt_file(f"{retry_prefix}_2",
            (
                "STILL no executable JSON instruction. For write_code tasks, every action MUST be a fenced ```json code block. "
                "Do NOT output bare JSON. Do NOT describe steps in text. Use the file paths listed above — they exist on this machine right now."
                if code_block_mode else
                "STILL no executable JSON instruction. Every action MUST include a valid JSON object. "
                "Do NOT say 'please upload', 'cannot access', 'try manually', or describe steps in text. "
                "Output the JSON object directly. "
                "Use the file paths listed above — they exist on this machine right now."
            )
        )
    else:
        instruction = _load_prompt_file(f"{retry_prefix}_3",
            (
                "FINAL WARNING: You have now failed to output an executable JSON instruction 3 times. "
                "For write_code tasks, the ONLY valid response is exactly one fenced ```json code block, or: Cannot complete this task."
                if code_block_mode else
                "FINAL WARNING: You have now failed to output an executable JSON instruction 3 times. "
                "Rules: (1) Never say you cannot execute. (2) Never ask for file uploads. "
                "(3) The ONLY valid response is a valid JSON object. "
                "Output one JSON instruction right now or output: Cannot complete this task."
            )
        )

    diag_lines.append(instruction)

    # Append a concrete example JSON using the first found file path (lowers model friction)
    if resolved:
        example_path = resolved[0].replace("\\", "\\\\")
        diag_lines.append(
            "\nExample - start by reading the file:\n"
            f'```json\n{{"command":"file_op","action":"read","path":"{example_path}"}}\n```'
        )
    elif desktop_cleanup_task and desktop:
        desktop_path = desktop.replace("\\", "\\\\")
        diag_lines.append(
            "\nExample 1 - inspect the desktop:\n"
            f'```json\n{{"command":"file_op","action":"list","path":"{desktop_path}"}}\n```'
        )
        if desktop_image_candidates:
            sample_path = desktop_image_candidates[0].replace("\\", "\\\\")
            diag_lines.append(
                "Example 2 - delete one matched image file:\n"
                f'```json\n{{"command":"file_op","action":"delete","path":"{sample_path}"}}\n```'
            )

    return "\n".join(diag_lines)


def _task_done_marker(text: str) -> bool:
    return (
        "✅ Task complete" in text or "Task complete:" in text
        or "✅ 任务完成" in text or "任务完成：" in text
    )

def _cannot_complete_marker(text: str) -> bool:
    return "无法完成此任务" in text or "Cannot complete this task" in text


def _is_desktop_image_cleanup_task(task_text: str) -> bool:
    text = str(task_text or "")
    return (
        bool(_re.search("(desktop|\u684c\u9762)", text, _re.IGNORECASE))
        and bool(_re.search("(delete|remove|cleanup|clean\\s*up|clear|\u5220\u9664|\u5220\u6389|\u79fb\u9664|\u6e05\u7406)", text, _re.IGNORECASE))
        and bool(_re.search("(image|images|photo|photos|picture|pictures|png|jpg|jpeg|gif|bmp|webp|\u56fe\u7247|\u7167\u7247|\u622a\u56fe)", text, _re.IGNORECASE))
    )


_TOOL_UNAVAILABLE_PATTERNS = [
    r"tool[s]?\s+(is|are|may\s+be)?\s*(unavailable|not\s+available)",
    r"cannot\s+(directly\s+)?(modify|access|read|write|use|open|execute|run)\b",
    r"don'?t\s+have\s+(access|ability|the\s+ability|direct\s+access)",
    r"unable\s+to\s+(directly\s+)?(access|modify|read|write|open|execute)",
    r"I('m|\s+am)\s+not\s+able\s+to\s+(directly\s+)?(access|modify|read|write)",
    r"(file|tool)\s+(operations?|ops?)\s+(is|are)\s+(unavailable|not\s+available)",
    r"(not\s+available|unavailable|may\s+(not\s+be|be\s+unavailable))\s+(on|in)\s+this\s+(platform|environment|context)",
    r"may\s+not\s+be\s+available",
    r"please\s+(upload|provide|share)\s+the\s+file",
    r"I\s+cannot\s+directly",
    r"as\s+an\s+AI\s+(language\s+model|assistant)",
    r"I\s+don'?t\s+have\s+the\s+(capability|ability)\s+to\s+(directly\s+)?",
    r"工具[^。\n]*不可用",
    r"无法直接(访问|修改|读取|写入|操作)",
    r"请(上传|提供|分享)(文件|代码)",
    r"无法(访问|修改|读取|写入)本地",
]

_TOOL_UNAVAILABLE_RE = _re.compile(
    "|".join(_TOOL_UNAVAILABLE_PATTERNS), _re.IGNORECASE
)


def _has_tool_unavailable_claim(text: str) -> bool:
    """Return True if AI claims tools/files are inaccessible despite having just used them."""
    return bool(_TOOL_UNAVAILABLE_RE.search(text))


def _build_tool_correction(last_results: list) -> str:
    """
    Build a correction message when AI incorrectly claims tools are unavailable.
    Includes what actually succeeded so AI cannot pretend it failed.
    """
    success_ops = []
    for r in last_results:
        if r.get("success") and r.get("stdout"):
            preview = r["stdout"][:200].replace("\n", " ")
            success_ops.append(f"  • {r['command']}: {preview}")

    lines = [
        "[CORRECTION] The file_op tool IS available and working on this machine.",
        "Your last JSON instruction was executed successfully by the local executor.",
    ]
    if success_ops:
        lines.append("Proof — operations that just succeeded:")
        lines.extend(success_ops)
    lines += [
        "",
        "Rules:",
        "1. NEVER say 'I cannot directly modify/access/read' — you CAN via JSON instructions.",
        "2. NEVER ask the user to upload files — the executor reads local paths directly.",
        "3. Output the next JSON instruction to continue the task.",
        "4. Use the exact local path from the task description.",
    ]
    return "\n".join(lines)


def _build_write_code_code_block_correction() -> str:
    return _load_prompt_file(
        "retry_write_code_code_fence",
        (
            "For write_code tasks, every executable instruction MUST be wrapped in a fenced ```json code block.\n"
            "Do not output bare JSON.\n"
            "Re-output the same executable instruction now, using exactly one fenced ```json block and no extra explanation."
        ),
    )


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


def _resolve_missing_paths(task_text: str) -> str:
    """
    Find paths mentioned in task that don't exist locally, then search for
    same-named files under all user home dirs + Downloads/Desktop/Documents.
    Returns a correction hint string (empty if all paths found or none mentioned).
    """
    # Collect all absolute Windows paths from task text
    abs_re = _re.compile(r'[A-Za-z]:\\(?:[^\s\'"<>|*?\r\n\\][^\s\'"<>|*?\r\n]*\\)*[^\s\'"<>|*?\r\n\\]+')
    all_paths = abs_re.findall(task_text)
    missing = [p for p in all_paths if not os.path.exists(p)]
    if not missing:
        return ""

    # Search dirs: all profiles + common user subdirs
    search_roots = []
    users_root = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")
    if os.path.isdir(users_root):
        for profile in os.listdir(users_root):
            profile_path = os.path.join(users_root, profile)
            if os.path.isdir(profile_path) and profile.lower() not in ("public", "all users", "default", "default user"):
                for sub in ("", "Desktop", "Downloads", "Documents", "Pictures"):
                    d = os.path.join(profile_path, sub) if sub else profile_path
                    if os.path.isdir(d):
                        search_roots.append(d)

    corrections = []
    for mp in missing:
        fname = os.path.basename(mp)
        found = []
        for root in search_roots:
            candidate = os.path.join(root, fname)
            if os.path.isfile(candidate):
                found.append(candidate)
            # Also walk one level deep
            try:
                for sub in os.listdir(root):
                    sub_path = os.path.join(root, sub, fname)
                    if os.path.isfile(sub_path) and sub_path not in found:
                        found.append(sub_path)
            except PermissionError:
                pass

        if found:
            corrections.append(
                f"[!] Path not found: {mp}\n"
                f"   Found same filename at:\n" +
                "\n".join(f"   -> {f}" for f in found[:5]) +
                f"\n   Use the correct path above instead of: {mp}"
            )
        else:
            # Try fuzzy: search for files with same extension in common dirs
            ext = os.path.splitext(fname)[1].lower()
            fuzzy = []
            if ext:
                for root in search_roots[:6]:  # limit search scope
                    try:
                        for f in os.listdir(root):
                            if f.lower().endswith(ext) and os.path.join(root, f) not in fuzzy:
                                fuzzy.append(os.path.join(root, f))
                    except PermissionError:
                        pass
            if fuzzy:
                corrections.append(
                    f"[!] Path not found: {mp}\n"
                    f"   No file named '{fname}' found. Files with same extension ({ext}) nearby:\n" +
                    "\n".join(f"   -> {f}" for f in fuzzy[:5]) +
                    f"\n   Did you mean one of the above?"
                )
            else:
                corrections.append(f"[!] Path not found and not found anywhere on this machine: {mp}")

    if not corrections:
        return ""
    return "\n[PATH CORRECTION]\n" + "\n".join(corrections) + "\n"


_PROJECT_PATH_RE = _re.compile(
    # "项目在 D:\Foo" / "项目路径是 D:\Foo" / "project at D:\Foo" / "in D:\Foo"
    r'(?:项目(?:在|路径是?|目录)?|project(?:\s+at|\s+in|\s+path)?|路径是?|代码(?:在|目录)?)'
    r'\s*[：:是]?\s*([A-Za-z]:\\[^\s\'"<>|*?\r\n,，。；;]+)',
    _re.IGNORECASE,
)
# Also match bare absolute directory paths (with or without trailing backslash)
_ABS_DIR_RE = _re.compile(
    r'([A-Za-z]:\\(?:[^\s\'"<>|*?\r\n\\]+\\)*[^\s\'"<>|*?\r\n\\]+)',
)

# Max depth for injected project tree (keep prompt short)
_PROJECT_TREE_MAX_DEPTH = 3
# Max lines of tree to inject (prevent huge trees from flooding context)
_PROJECT_TREE_MAX_LINES = 80
# Dirs to skip in tree (noise / large)
_PROJECT_TREE_SKIP = {
    '__pycache__', 'node_modules', '.git', '.svn', '.hg',
    'venv', '.venv', 'env', '.env',
    'dist', 'build', 'out', 'target', '.idea', '.vscode',
}


def _extract_project_tree(task_text: str) -> str:
    """
    Detect project/directory paths in the task text and inject a compact
    directory tree into the prompt so the AI sees the project structure
    without having to ask for it first.

    Returns an annotated tree block string, or "" if no directory found.
    """
    candidates: list[str] = []
    seen_dirs: set[str] = set()

    # 1. Explicit "项目在 X" / "project at X" patterns
    for m in _PROJECT_PATH_RE.finditer(task_text):
        raw = m.group(1).strip().rstrip('\\')
        if raw not in seen_dirs:
            candidates.append(raw)
            seen_dirs.add(raw)

    # 2. Any bare absolute path that resolves to a directory
    for m in _ABS_DIR_RE.finditer(task_text):
        raw = m.group(1).strip().rstrip('\\')
        # Skip if it looks like a file (has a dot-extension at the end)
        if _re.search(r'\.[a-zA-Z]{1,5}$', raw):
            continue
        if raw not in seen_dirs:
            candidates.append(raw)
            seen_dirs.add(raw)

    result_blocks: list[str] = []
    for raw_path in candidates:
        path = os.path.normpath(raw_path)
        if not os.path.isdir(path):
            continue

        try:
            lines: list[str] = [path]

            def _walk(p: str, prefix: str, depth: int, _lines=lines) -> None:
                if depth > _PROJECT_TREE_MAX_DEPTH:
                    return
                try:
                    all_items = sorted(os.listdir(p))
                except PermissionError:
                    return
                # Filter skipped dirs from display but keep their position for is_last calc
                visible = [n for n in all_items if n not in _PROJECT_TREE_SKIP]
                for i, name in enumerate(visible):
                    full = os.path.join(p, name)
                    is_last = (i == len(visible) - 1)
                    connector = "└── " if is_last else "├── "
                    _lines.append(prefix + connector + name)
                    if len(_lines) >= _PROJECT_TREE_MAX_LINES:
                        _lines.append(prefix + "    ... (truncated)")
                        return
                    if os.path.isdir(full):
                        extension = "    " if is_last else "│   "
                        _walk(full, prefix + extension, depth + 1, _lines)

            _walk(path, "", 1)

            tree_text = "\n".join(lines[:_PROJECT_TREE_MAX_LINES])
            result_blocks.append(
                f"\n## Project structure: {path}\n"
                f"```\n{tree_text}\n```\n"
                f"Use file_op read/write/patch to work with files in this project."
            )
            print(f"   [preprocess] Injected project tree for: {path} ({len(lines)} lines)")
        except Exception as e:
            result_blocks.append(f"\n## Project directory: {path} (tree failed: {e})")

    return "\n".join(result_blocks)


def _enrich_task(task_text: str, env_info: dict) -> str:
    """
    Scan task for local file paths; inject context so AI can act immediately.

    Small files (≤ FILE_WEB_MODE_LINES lines): inject full content into prompt.
    Large files (> FILE_WEB_MODE_LINES lines) or binary files: do NOT embed
    content — instead instruct AI to use write_web (upload→modify→overwrite),
    which avoids ChatGPT Web length limits and file corruption.
    """
    desktop = env_info.get("desktop", "")
    extras = []
    candidates = _PATH_RE.findall(task_text)
    for mb in _BINARY_PATH_RE.finditer(task_text):
        raw = mb.group(0).strip()
        if raw not in candidates:
            candidates.append(raw)
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

        size = os.path.getsize(path)
        is_bin = _is_binary_file(path)

        if is_bin:
            # Binary: always web mode
            # Check if task mentions saving to a different location
            dst_hint = ""
            if desktop and _re.search(r'(?:存|保存|放|save|put|output|写入).*?(?:桌面|desktop)', task_text, _re.IGNORECASE):
                ts_hint = int(time.time())
                ext = os.path.splitext(path)[1] or ".png"
                dst_hint = f',"dst":"{os.path.join(desktop, os.path.basename(path))}\"'
            extras.append(
                f"\n## File (binary): {path} ({size} bytes)\n"
                f"Use write_web to modify: "
                f'{{"command":"file_op","action":"write_web","path":"{path}"{dst_hint},'
                f'"message":"<your modification instruction>"}}\n'
                f"If the output should go to a different path, add \"dst\":\"<target path>\" to the JSON."
            )
            continue

        try:
            content = _read_text(path)
        except Exception as e:
            extras.append(f"\n## File read failed: {path}\nError: {e}")
            continue

        lines = content.splitlines()
        n = len(lines)

        if n <= FILE_WEB_MODE_LINES:
            # Small file: embed full content
            extras.append(f"\n## File content: {path} ({n} lines)\n```\n{content}\n```")
        else:
            # Large file: web-upload mode — show only head+tail as preview
            head = "\n".join(lines[:30])
            tail = "\n".join(lines[-10:])
            extras.append(
                f"\n## File: {path} ({n} lines, {size} bytes) — TOO LARGE for inline edit\n"
                f"First 30 lines:\n```\n{head}\n```\n"
                f"Last 10 lines:\n```\n{tail}\n```\n"
                f"IMPORTANT: To modify this file use write_web (uploads the full file, "
                f"ChatGPT edits it, result is written back automatically):\n"
                f'{{"command":"file_op","action":"write_web","path":"{path}",'
                f'"message":"<describe exactly what to change>"}}\n'
                f"Do NOT use write or patch for this file."
            )

        if path.endswith('.py'):
            err = _run_py_get_error(path)
            if err:
                extras.append(f"\n## Run error: {path}\n```\n{err}\n```")
            else:
                extras.append(f"\n## Run status: {path} starts without error")

    # Check for broken paths in the task and try to find the actual files
    path_correction = _resolve_missing_paths(task_text)
    if path_correction:
        _pre = path_correction.strip()[:200]
        try:
            print(f"   [preprocess] {_pre}")
        except UnicodeEncodeError:
            print(f"   [preprocess] {_pre.encode('ascii', errors='replace').decode('ascii')}")
        extras.insert(0, path_correction)

    # ── Project path injection ──────────────────────────────────────────────
    # Detect "项目在 X" / "project at X" / standalone directory paths in task.
    # When found: inject a directory tree so AI can see the project structure.
    project_tree_hint = _extract_project_tree(task_text)
    if project_tree_hint:
        extras.insert(0, project_tree_hint)

    if not extras:
        return task_text

    print(f"   [preprocess] Injected {len(seen)} local file context(s)")
    return task_text + "\n" + "\n".join(extras)


def _local_find_files(task_text: str, env_info: dict) -> str:
    """Search desktop/documents/downloads for filenames mentioned in task."""
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

    lines = ["[Local search result]", "Found the following files on this machine:"]
    for p in found_files[:10]:
        lines.append(f"  {p}")
    lines.append("Use the paths above to continue.")
    return "\n".join(lines)


# ── write_web intercept helpers ────────────────────────────────────────────────

_FILE_CHAT_URL = "http://127.0.0.1:4001/file-chat"

def _file_line_count(path: str) -> int:
    try:
        return len(_read_text(path).splitlines())
    except Exception:
        return 0


def _call_write_web(path: str, message: str) -> dict:
    """Call file_ops.write_web directly (which handles its own HTTP fallback chain)."""
    from file_ops import run as _fop
    result = _fop("write_web", {"path": path, "message": message})
    if result.get("ok"):
        return {
            "success": True,
            "stdout": f"write_web OK: {result.get('bytes', '?')} bytes written to {path}"
                      + (f" (backup: {result['backup']})" if result.get("backup") else ""),
            "stderr": "",
            "returncode": 0,
            "command": "file_op",
        }
    return {
        "success": False, "stdout": "",
        "stderr": result.get("error", "write_web failed"),
        "returncode": 1, "command": "file_op",
    }


_ROUTING_REPLY_PATTERNS = [
    _re.compile(r"^[\u4e00-\u9fa5A-Za-z\s,，]+需要\s+[a-z_]+\s+去做[.!]?$", _re.IGNORECASE),
    _re.compile(r"^[\u4e00-\u9fa5A-Za-z\s,，]+,\s*need\s+[a-z_]+\s+to\s+do\s+it[.!]?$", _re.IGNORECASE),
]


def _looks_like_routing_reply(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    cleaned = _PREFIX_RE.sub("", cleaned).strip()
    return any(p.search(cleaned) for p in _ROUTING_REPLY_PATTERNS)


def _is_terminal_file_chat_text_only(result: dict) -> bool:
    if not result.get("ok"):
        return False
    if result.get("downloaded_b64"):
        return False
    if result.get("generating"):
        return False
    if result.get("terminal_text_only"):
        return True
    text = (result.get("text") or "").strip()
    if not text:
        return False
    if _looks_like_routing_reply(text):
        return True
    return len(text) < 80 and not _is_intermediate(text)


def _intercept_large_file_writes(blocks: list, task_text: str = "", agent_id: str = "default",
                                  ai_text: str = "") -> tuple[list, str, bool]:
    """
    Scan blocks for write/patch actions targeting large/binary/image files.

    - Binary extension + file does NOT exist (new file generation):
        Bypass the AI planning loop — send task_text directly to ChatGPT,
        capture result (e.g. generated image), save it, return done feedback.
    - Binary extension + file EXISTS (modify existing):
        Block and guide AI to use write_web.
    - Large/binary existing text files:
        Run write_web directly.

    Returns (remaining_blocks, feedback_text, had_real_write).
    had_real_write=True when write_web executed or direct-gen succeeded.
    """
    from file_ops import _is_binary_file as _is_bin

    # 二进制/图片扩展名集合（不管文件是否存在，都不允许用 write 写入）
    _BINARY_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff",
        ".pptx", ".ppt", ".xlsx", ".xls", ".docx", ".doc",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
        ".zip", ".rar", ".7z", ".gz", ".exe", ".dll", ".pdf",
    }

    # ChatGPT sandbox path prefixes — these paths exist only inside ChatGPT's VM, not on Windows
    _SANDBOX_PREFIXES = ("/mnt/data/", "/tmp/", "/var/", "/home/", "/root/", "/sandbox/")

    remaining = []
    feedback_parts = []

    for block in blocks:
        if block.get("command") != "file_op":
            remaining.append(block)
            continue
        action = block.get("action", "")

        # ── Intercept move/copy with sandbox src path ──────────────────────────
        # ChatGPT sometimes outputs move {"src":"/mnt/data/image.png","dst":"C:\...\image.png"}
        # These sandbox paths don't exist on Windows — capture via DOM instead.
        if action in ("move", "copy"):
            src = block.get("src", "")
            dst = block.get("dst", "") or block.get("path", "")
            if any(src.startswith(p) for p in _SANDBOX_PREFIXES):
                print(f"   [intercept] BLOCKED sandbox-path {action}: src={src}")
                # Try to capture the generated image from the ChatGPT DOM directly
                # First check poll cache, then actively request a DOM capture
                poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
                b64 = poll.get("downloaded_b64", "")
                b64_ext = poll.get("downloaded_ext", "")
                if not b64:
                    print(f"   [intercept] No cached file, requesting DOM capture...")
                    cap = _http_get(f"{CAPTURE_IMG_URL}?agentId={agent_id}", timeout=30)
                    b64 = cap.get("downloaded_b64", "")
                    b64_ext = cap.get("downloaded_ext", "")

                if b64:
                    save_path = dst if dst else None
                    if save_path:
                        import base64 as _b64mod
                        try:
                            os.makedirs(os.path.dirname(save_path), exist_ok=True)
                            with open(save_path, "wb") as _f:
                                _f.write(_b64mod.b64decode(b64))
                            print(f"   [intercept] Saved captured file to: {save_path}")
                            suffix = (f"\n\n[File saved to: {save_path}]\n"
                                      f"✅ Task complete: File saved to {save_path}")
                            feedback_parts.append("__WRITE_EXECUTED__")
                            feedback_parts.append(suffix)
                            continue
                        except Exception as e:
                            print(f"   [intercept] Failed to save file: {e}")
                    # No dst path — use generic save with ext hint
                    _, suffix = _save_downloaded_file(b64, task_text, ext=b64_ext or ".bin")
                    if suffix:
                        feedback_parts.append("__WRITE_EXECUTED__")
                        feedback_parts.append(suffix)
                        continue

                # No image in cache — ChatGPT reply may still be generating; let poll loop handle it
                feedback_parts.append(
                    f"--- BLOCKED sandbox-path move src={src} ---\n"
                    f"The path '{src}' is a ChatGPT internal sandbox path and does not exist on Windows.\n"
                    f"The system is waiting for the image to be captured from the browser.\n"
                    f"Do NOT output another move/copy block. The image will be saved automatically."
                )
                continue
            remaining.append(block)
            continue

        if action not in ("write", "patch", "write_chunk"):
            remaining.append(block)
            continue

        path = block.get("path", "")
        if not path:
            remaining.append(block)
            continue

        # 对图片/二进制扩展名，即使文件不存在也要拦截
        ext = os.path.splitext(path)[1].lower()
        if ext in _BINARY_EXTS:
            file_exists = os.path.isfile(path)
            print(f"   [intercept] BLOCKED binary-ext write: {path} (ext={ext}, exists={file_exists})")

            # ── 优先检查本轮是否已经捕获并保存过文件 ──────────────────────────
            # 情况1：ai_text 已包含 [File saved to: X]，说明本轮 chat_via_bridge 已经保存了文件
            already_saved_match = _re.search(r'\[File saved to: ([^\]]+)\]', ai_text)
            if already_saved_match:
                already_path = already_saved_match.group(1).strip()
                print(f"   [intercept] File already saved this round: {already_path}")
                if already_path != path and os.path.isfile(already_path):
                    # 重命名/复制到 AI 指定的目标路径
                    try:
                        import shutil as _shutil
                        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                        _shutil.copy2(already_path, path)
                        print(f"   [intercept] Copied {already_path} → {path}")
                        suffix = (f"\n\n[File saved to: {path}]\n"
                                  f"✅ Task complete: File saved to {path}")
                        feedback_parts.append("__WRITE_EXECUTED__")
                        feedback_parts.append(suffix)
                        continue
                    except Exception as e:
                        print(f"   [intercept] Copy failed: {e}")
                else:
                    # 路径相同或文件不存在 → 直接用已保存的结果
                    suffix = (f"\n\n[File saved to: {already_path}]\n"
                              f"✅ Task complete: File saved to {already_path}")
                    feedback_parts.append("__WRITE_EXECUTED__")
                    feedback_parts.append(suffix)
                    continue

            # 情况2：poll 缓存里已有 downloaded_b64（本轮网络捕获到了文件但还没保存到 path）
            poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
            cached_b64 = poll.get("downloaded_b64", "")
            cached_ext = poll.get("downloaded_ext", ext or ".bin")
            if cached_b64:
                print(f"   [intercept] Found cached downloaded_b64, saving to: {path}")
                import base64 as _b64mod
                try:
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "wb") as _f:
                        _f.write(_b64mod.b64decode(cached_b64))
                    print(f"   [intercept] Saved cached file to: {path} ({os.path.getsize(path)} bytes)")
                    suffix = (f"\n\n[File saved to: {path}]\n"
                              f"✅ Task complete: File saved to {path}")
                    feedback_parts.append("__WRITE_EXECUTED__")
                    feedback_parts.append(suffix)
                    continue
                except Exception as e:
                    print(f"   [intercept] Failed to save cached file: {e}")

            # ── 没有已有捕获 → 需要触发生成/修改 ──────────────────────────────
            if file_exists:
                # 文件已存在 → 直接用 file-chat 上传文件让 ChatGPT 修改
                # message: AI 块里的指令优先，否则用原始 task_text
                modify_msg = block.get("message", "") or task_text or f"Modify this file as needed."
                print(f"   [intercept] Existing binary — uploading via file-chat: {path}")
                try:
                    from file_ops import _call_file_chat as _fchat_intercept
                    fc = _fchat_intercept(path, modify_msg, agent_id)
                    if fc.get("ok"):
                        dl_b64 = fc.get("downloaded_b64", "")
                        dl_ext = fc.get("downloaded_ext", ext or ".bin")
                        if dl_b64:
                            import base64 as _b64mod
                            dst_path = block.get("dst", "") or path
                            os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
                            with open(dst_path, "wb") as _f:
                                _f.write(_b64mod.b64decode(dl_b64))
                            print(f"   [intercept] Saved modified file: {dst_path} ({os.path.getsize(dst_path)} bytes)")
                            suffix = (f"\n\n[File saved to: {dst_path}]\n"
                                      f"✅ Task complete: File modified and saved to {dst_path}")
                            feedback_parts.append("__WRITE_EXECUTED__")
                            feedback_parts.append(suffix)
                            continue
                        else:
                            reply_preview = fc.get("text", "")[:200]
                            print(f"   [intercept] file-chat OK but no download captured. Reply: {reply_preview}")
                            if _is_terminal_file_chat_text_only(fc):
                                feedback_parts.append(
                                    f"file-chat uploaded {path}, but ChatGPT returned text only and no modified file was captured.\n"
                                    f"Reply: {reply_preview}"
                                )
                                feedback_parts.append("__FILE_CHAT_TERMINAL__")
                            else:
                                feedback_parts.append(
                                    f"--- file-chat uploaded {path} ---\n"
                                    f"ChatGPT replied but no modified file was captured.\n"
                                    f"Reply: {reply_preview}\n"
                                    f"Please try again or use write_web."
                                )
                    else:
                        print(f"   [intercept] file-chat failed: {fc.get('error')}")
                        feedback_parts.append(
                            f"--- BLOCKED file_op:write path={path} ---\n"
                            f"Cannot write binary data using 'write'. Use write_web instead:\n"
                            f'{{"command":"file_op","action":"write_web","path":"{path}",'
                            f'"message":"<describe what changes to make>"}}'
                        )
                except Exception as e:
                    print(f"   [intercept] file-chat exception: {e}")
                    feedback_parts.append(f"--- file-chat error: {e} ---")
            else:
                # 文件不存在 → 直接把原始任务发给 ChatGPT，要求生成并展示（不输出 JSON）
                print(f"   [intercept] New binary file — sending task directly to ChatGPT")
                direct_prompt = (
                    f"IMPORTANT: Do NOT output any JSON block. Do NOT output file_op commands. "
                    f"Do NOT describe the result in text. "
                    f"Just generate and display the result directly in this chat. "
                    f"User request: {task_text if task_text else f'generate {path}'}"
                )
                try:
                    direct_reply = chat_via_bridge(direct_prompt, new_chat=False, agent_id=agent_id)
                    # chat_via_bridge saves any captured file and appends [File saved to:]
                    # But the saved path may be chatgpt_generated_xxx.png, not the target path.
                    # Try to copy/rename to target path.
                    saved_match = _re.search(r'\[File saved to: ([^\]]+)\]', direct_reply)
                    if saved_match:
                        saved_path = saved_match.group(1).strip()
                        if saved_path != path and os.path.isfile(saved_path):
                            try:
                                import shutil as _shutil
                                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                                _shutil.copy2(saved_path, path)
                                direct_reply = direct_reply.replace(
                                    f"[File saved to: {saved_path}]",
                                    f"[File saved to: {path}]"
                                )
                                print(f"   [intercept] Copied {saved_path} → {path}")
                            except Exception as e:
                                print(f"   [intercept] Copy to target failed: {e}")
                        feedback_parts.append("__WRITE_EXECUTED__")
                        feedback_parts.append(direct_reply)
                    elif _task_done_marker(direct_reply):
                        feedback_parts.append("__WRITE_EXECUTED__")
                        feedback_parts.append(direct_reply)
                    else:
                        feedback_parts.append(
                            f"--- Direct generation attempt for {path} ---\n"
                            f"ChatGPT reply: {direct_reply[:300]}\n"
                            f"No file was saved."
                        )
                except Exception as e:
                    feedback_parts.append(f"--- Direct generation failed: {e} ---")
            continue  # Don't add to remaining

        if not os.path.isfile(path):
            remaining.append(block)
            continue

        n_lines = _file_line_count(path)
        is_bin = _is_bin(path)
        print(f"   [intercept] {action} path={path} n_lines={n_lines} is_bin={is_bin} threshold={FILE_WEB_MODE_LINES}")

        if n_lines <= FILE_WEB_MODE_LINES and not is_bin:
            print(f"   [intercept] → pass-through (small file)")
            remaining.append(block)
            continue

        # Build a modification instruction from the block content
        if action == "write":
            new_content = block.get("content", "")
            # If AI already has the new content in the block, just write it directly
            # (no need to upload — content is available).  Only intercept if content
            # looks truncated (ends abruptly without closing structure).
            if new_content and len(new_content) > 50:
                is_truncated = _looks_truncated(new_content)
                print(f"   [intercept] write content len={len(new_content)} is_truncated={is_truncated}")
                if not is_truncated:
                    # Content looks complete → write directly, no upload needed
                    print(f"   [intercept] → pass-through (complete content)")
                    remaining.append(block)
                    continue
            # Content missing or truncated → fall through to write_web
            instruction = block.get("_instruction", "") or (
                f"Rewrite this file as instructed. "
                f"New content provided (may be incomplete): {new_content[:200]}"
                if new_content else "Fix and rewrite this file completely."
            )
        elif action == "patch":
            reps = block.get("replacements", [])
            parts = []
            for r in reps[:5]:
                parts.append(f"Replace: {str(r.get('old',''))[:80]} → {str(r.get('new',''))[:80]}")
            instruction = "Apply these changes:\n" + "\n".join(parts) if parts else "Fix errors in this file."
        else:  # write_chunk
            instruction = (
                f"Replace lines {block.get('line_start',1)}-{block.get('line_end','end')} "
                f"with: {block.get('content','')[:200]}"
            )

        print(f"   [web-mode] {action} on {path} ({n_lines} lines) → write_web")
        r = _call_write_web(path, instruction)
        cmd_label = f"file_op:write_web path={path}"
        if r["success"]:
            feedback_parts.append(f"--- {cmd_label} OK ---\nOutput:\n{r['stdout']}")
            feedback_parts.append("__WRITE_EXECUTED__")  # 标记：真正执行了写入
        else:
            feedback_parts.append(f"--- {cmd_label} Failed ---\nError:\n{r['stderr']}")
        # Don't add to remaining — already handled

    # 检查是否有真正的写入（不是只有 BLOCKED 消息）
    had_real_write = "__WRITE_EXECUTED__" in feedback_parts
    had_terminal_file_chat = "__FILE_CHAT_TERMINAL__" in feedback_parts
    feedback_parts = [p for p in feedback_parts if p not in ("__WRITE_EXECUTED__", "__FILE_CHAT_TERMINAL__")]
    feedback = "\n\n".join(feedback_parts)
    if had_terminal_file_chat:
        feedback = "[FILE_CHAT_TERMINAL]\n" + feedback
    return remaining, feedback, had_real_write


def _looks_truncated(content: str) -> bool:
    """Heuristic: content looks cut off if it ends without a proper closing token."""
    s = content.rstrip()
    if not s:
        return True
    # Common truncation signs: ends mid-word, ends with comma/open-bracket
    if s[-1] in (',', '(', '[', '{', '+', '\\', '=', ':'):
        return True
    # Very short compared to typical file
    if len(s) < 30:
        return True
    return False


def run_from_text_with_blocks(blocks: list, task_id: str = "") -> tuple[list, str]:
    """Execute a pre-parsed block list (skips re-parsing); returns (results, feedback)."""
    return execute_blocks(blocks, task_id=task_id)


def _format_request_help_feedback(block: dict, response_text: str, success: bool, target_identity: str) -> str:
    request_id = block.get("request_id", "")
    task = block.get("task", "")
    header = f"[协作结果 request_id={request_id or 'n/a'}]"
    status = "成功" if success else "失败"
    return (
        f"{header}\n"
        f"目标身份：{target_identity or 'unknown'}\n"
        f"任务：{task}\n"
        f"状态：{status}\n"
        f"结果：\n{response_text.strip() or '(empty)'}\n"
        f"请继续主任务。"
    )


def _handle_request_help_block(block: dict, task_id: str = "") -> dict:
    """Route request_help to a dedicated identity slot and return executor-shaped output."""
    target_raw = str(block.get("target_identity", "")).strip()
    target_identity = normalize_identity_key(target_raw)
    if not target_identity:
        if task_id:
            _log_event("WARN", "request_help_invalid_target", task_id, target_identity=target_raw, block=block)
        return {
            "command": "request_help",
            "success": False,
            "stdout": "",
            "stderr": f"Unknown target_identity: {target_raw}",
            "returncode": 1,
        }

    target_agent_id = get_agent_id_for_identity(target_identity)
    target_category = get_category_for_identity(target_identity, block.get("task", ""))
    target_prompt = get_identity_prompt(target_category, block.get("task", ""))
    target_language = str(block.get("language", "")).strip()
    params = block.get("params", {})
    params_text = json.dumps(params, ensure_ascii=False, indent=2) if isinstance(params, dict) and params else ""
    forwarded_task = block.get("task", "").strip() or "请协助完成当前子任务"
    forwarded_message = (
        (f"{target_prompt}\n\n" if target_prompt else "")
        + (f"输出语言：{target_language}\n\n" if target_language else "")
        + f"你正在协助主任务。请直接完成以下子任务并给出可执行结果。\n\n{forwarded_task}"
        + (f"\n\n补充参数：\n{params_text}" if params_text else "")
    )

    try:
        if task_id:
            _log_event(
                "INFO",
                "request_help_sent",
                task_id,
                target_identity=target_identity,
                agent_id=target_agent_id,
                task=forwarded_task,
            )
        response_text = chat_via_bridge(forwarded_message, new_chat=True, agent_id=target_agent_id)
        if task_id:
            _log_event(
                "INFO",
                "request_help_received",
                task_id,
                target_identity=target_identity,
                response_preview=response_text,
            )
        stdout = _format_request_help_feedback(block, response_text, True, target_identity)
        return {
            "command": "request_help",
            "success": True,
            "stdout": stdout,
            "stderr": "",
            "returncode": 0,
        }
    except Exception as e:
        if task_id:
            _log_event("ERROR", "request_help_failed", task_id, target_identity=target_identity, error=str(e))
        stdout = _format_request_help_feedback(block, str(e), False, target_identity)
        return {
            "command": "request_help",
            "success": False,
            "stdout": stdout,
            "stderr": str(e),
            "returncode": 1,
        }


def execute_blocks(blocks: list[dict], task_id: str = "") -> tuple[list[dict], str]:
    """Execute parsed blocks, handling request_help locally and file/shell ops via executor."""
    from executor import execute_block, format_result_for_ai

    results: list[dict] = []
    for block in blocks:
        if block.get("command") == "request_help":
            results.append(_handle_request_help_block(block, task_id=task_id))
        else:
            results.append(execute_block(block))
    return results, format_result_for_ai(results)




POLL_URL         = CHAT_URL.replace("/chat", "/poll")
CAPTURE_IMG_URL  = CHAT_URL.replace("/chat", "/capture-image")
CLOSE_AGENT_URL  = CHAT_URL.replace("/chat", "/close-agent")
_TIMEOUTS = _load_json_config(
    "timeouts",
    {
        "reply_poll_interval_seconds": 3,
        "reply_poll_timeout_seconds": 660,
        "reply_stall_seconds": 20,
    },
)
POLL_INTERVAL = int(_TIMEOUTS.get("reply_poll_interval_seconds", 3))
POLL_TIMEOUT  = int(_TIMEOUTS.get("reply_poll_timeout_seconds", 660))
STALL_SEC     = int(_TIMEOUTS.get("reply_stall_seconds", 20))


def _http_get(url: str, timeout: int = 10) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _save_downloaded_file(b64: str, task_text: str = "", ext: str = "") -> tuple[str, str]:
    """
    Save base64-encoded file. Works for any file type (images, PDFs, docs, etc.)
    ext: file extension hint from network capture (e.g. '.png', '.pdf').

    Save path priority:
      1. Explicit absolute path in task text (e.g. "保存到 D:\\Projects\\out.png")
      2. Explicit filename (no path) in task text → save to desktop
      3. Fallback: chatgpt_generated_<ts><ext> on desktop

    Returns (saved_path, result_suffix).
    """
    import base64 as _b64
    desktop = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "Desktop")
    ts = int(time.time())

    if not ext:
        ext = ".bin"
    ext_pat = _re.escape(ext.lstrip("."))

    save_path = ""

    # Priority 1: explicit absolute path with matching extension
    abs_path_match = _re.search(
        r'([A-Za-z]:\\[^\s\'"<>|*?\r\n,，。；;]*\.' + ext_pat + r')',
        task_text, _re.IGNORECASE
    )
    if abs_path_match:
        save_path = os.path.normpath(abs_path_match.group(1).strip())

    # Priority 2: explicit save-as filename (no path separators)
    if not save_path:
        save_match = _re.search(
            r'(?:存|保存|save|named?|output|as)\s*(?:as\s*|为\s*)?'
            r'([\w\u4e00-\u9fff][\w\u4e00-\u9fff\-. ]*\.' + ext_pat + r')',
            task_text, _re.IGNORECASE
        )
        if not save_match:
            save_match = _re.search(
                r'([\w\u4e00-\u9fff][\w\u4e00-\u9fff\-. ]*\.' + ext_pat + r')',
                task_text, _re.IGNORECASE
            )
        if save_match:
            fname = save_match.group(1)
            save_path = os.path.join(desktop, fname)

    # Priority 3: fallback
    if not save_path:
        fname = f"chatgpt_generated_{ts}{ext}"
        save_path = os.path.join(desktop, fname)

    try:
        os.makedirs(os.path.dirname(save_path) or desktop, exist_ok=True)
        with open(save_path, "wb") as _f:
            _f.write(_b64.b64decode(b64))
        fname = os.path.basename(save_path)
        print(f"   [download] Saved generated file to: {save_path}")
        suffix = (f"\n\n[File saved to: {save_path}]\n"
                  f"✅ Task complete: File saved as {fname}")
        return save_path, suffix
    except Exception as _e:
        print(f"   [download] Failed to save file: {_e}")
        return "", ""


# Keep backward-compat alias (used in intercept logic)
def _save_downloaded_image(b64: str, task_text: str = "") -> tuple[str, str]:
    return _save_downloaded_file(b64, task_text, ext=".png")


def _poll_until_final(agent_id: str, start_text: str, message: str) -> str:
    """Shared poll loop: poll /poll until final reply or timeout. Handles downloaded_b64."""
    deadline      = time.time() + POLL_TIMEOUT
    last_text     = start_text
    last_change_t = time.time()
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
        if not poll.get("ok"):
            continue
        current    = poll.get("text", "")
        generating = poll.get("generating", True)
        dl_b64     = poll.get("downloaded_b64", "")
        dl_ext     = poll.get("downloaded_ext", ".bin")
        print(f"   [poll] generating={generating} len={len(current)}: "
              f"{current[:60].replace(chr(10), ' ')}")
        if dl_b64 and not generating:
            _, suffix = _save_downloaded_file(dl_b64, message, ext=dl_ext)
            clean = _re.sub(r'```[^\n]*\n[\s\S]*?```', '', current).strip()
            return clean + suffix if suffix else current
        if _task_done_marker(current):
            return current
        if _cannot_complete_marker(current):
            return current
        if not _is_intermediate(current):
            return current
        if current != last_text:
            last_text     = current
            last_change_t = time.time()
        if not generating and (time.time() - last_change_t) > STALL_SEC:
            print(f"   [poll] Stall {STALL_SEC}s no update, forcing continue")
            return current
    poll = _http_get(f"{POLL_URL}?agentId={agent_id}")
    return poll.get("text", last_text)


def chat_via_bridge(message: str, new_chat: bool = False, agent_id: str = "default") -> str:
    """Send message and return reply; poll /poll on intermediate state."""
    body = json.dumps({"message": message, "newChat": new_chat, "agentId": agent_id}).encode("utf-8")
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    data = None
    try:
        with urllib.request.urlopen(req, timeout=660) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8") if e.fp else str(e)
        if e.code == 404:
            raise RuntimeError(
                f"AgentPilot bridge returned 404. Start API first:\n"
                f"  In another terminal: npm run api\n"
                f"Raw error: {err_body}"
            )
        # 500 with "Reply timeout" means ChatGPT is still generating — fall into poll loop
        if e.code == 500 and "Reply timeout" in err_body:
            print(f"   [Python] bridge 500/Reply timeout — polling /poll for final reply...")
            return _poll_until_final(agent_id, "", message)
        raise RuntimeError(f"AgentPilot request failed ({e.code}): {err_body}")
    except urllib.error.URLError as e:
        reason = str(e.reason)
        if "timed out" in reason.lower() or "time out" in reason.lower():
            # Socket-level timeout: server still processing, fall through to poll
            print(f"   [Python] socket timeout — polling /poll for final reply...")
            return _poll_until_final(agent_id, "", message)
        raise RuntimeError(f"Cannot connect to AgentPilot; run npm run api first: {e.reason}")

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Unknown error"))

    result = data.get("result", "")
    # bridge returns generating:true when doChat exited early (e.g. image gen still running)
    still_generating = data.get("generating", False)

    # If ChatGPT generated a file during regular chat, save it and note the path
    downloaded_b64 = data.get("downloaded_b64", "")
    downloaded_ext = data.get("downloaded_ext", ".bin")
    if downloaded_b64:
        _, suffix = _save_downloaded_file(downloaded_b64, message, ext=downloaded_ext)
        if suffix:
            # Strip any JSON blocks from result — file is captured, no file_op needed
            result_clean = _re.sub(r'```[^\n]*\n[\s\S]*?```', '', result).strip()
            result = result_clean + suffix

    if _task_done_marker(result):
        return result
    if _cannot_complete_marker(result):
        return result
    if not still_generating and not _is_intermediate(result):
        return result

    if still_generating:
        print(f"   [Python] Reply still generating (early return), polling /poll ...")
    else:
        print(f"   [Python] Intermediate '{result[:60].replace(chr(10),' ')}', polling /poll for JSON...")
    return _poll_until_final(agent_id, result, message)


def _close_agent_window(agent_id: str) -> None:
    if not agent_id or agent_id == "default":
        return
    body = json.dumps({"agentId": agent_id}).encode("utf-8")
    req = urllib.request.Request(
        CLOSE_AGENT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            json.loads(resp.read().decode("utf-8"))
        print(f"   [bridge] closed agent window: {agent_id}")
    except Exception as e:
        print(f"   [bridge] close agent window failed ({agent_id}): {e}")


# ── Modify-intent detection ────────────────────────────────────────────────────

_MODIFY_INTENT_CFG = _load_json_config(
    "modify_intent",
    {
        "verbs": ["修改", "细化", "edit", "modify", "refine"],
        "binary_extensions": [
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".psd",
            ".pdf", ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv"
        ],
        "search_dirs": ["desktop", "documents", "downloads", "cwd"],
        "allow_residual_instruction_without_verb": True,
        "min_residual_chars": 2,
    },
)


def _compile_modify_verb_re() -> _re.Pattern:
    verbs = [str(v).strip() for v in _MODIFY_INTENT_CFG.get("verbs", []) if str(v).strip()]
    if not verbs:
        verbs = ["修改", "细化", "edit", "modify", "refine"]
    escaped = [_re.escape(v) for v in verbs]
    return _re.compile("|".join(escaped), _re.IGNORECASE)


def _compile_binary_path_re() -> _re.Pattern:
    exts = [str(v).strip().lstrip(".").lower() for v in _MODIFY_INTENT_CFG.get("binary_extensions", []) if str(v).strip()]
    if not exts:
        exts = ["png", "jpg", "jpeg", "gif", "bmp", "webp", "tiff", "psd", "pdf", "mp3", "mp4", "wav", "avi", "mov", "mkv"]
    ext_group = "|".join(_re.escape(ext) for ext in exts)
    return _re.compile(
        r'[A-Za-z]:\\(?:[^\s\'"<>|*?\r\n\\][^\s\'"<>|*?\r\n]*\\)*[^\s\'"<>|*?\r\n\\]+'
        rf'(?:\.{ext_group})\b'
        r'|(?<!\w)(?:[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-]*'
        rf'(?:\.{ext_group}))\b',
        _re.IGNORECASE,
    )


_MODIFY_VERBS = _compile_modify_verb_re()
_BINARY_PATH_RE = _compile_binary_path_re()


def _detect_modify_intent(task_text: str, env_info: dict) -> tuple[str, str]:
    """
    Return (file_path, modify_message) when the task is clearly:
      - Mentioning an EXISTING binary/image file by path or filename
      - AND containing modification verbs (refine / 细化 / edit / etc.)
    Returns ("", "") if not a modification task or file doesn't exist.
    """
    has_modify_verb = bool(_MODIFY_VERBS.search(task_text))

    search_dirs = []
    for key in _MODIFY_INTENT_CFG.get("search_dirs", ["desktop", "documents", "downloads", "cwd"]):
        if key == "cwd":
            dir_path = os.getcwd()
        else:
            dir_path = env_info.get(str(key), "")
        if dir_path and os.path.isdir(dir_path) and dir_path not in search_dirs:
            search_dirs.append(dir_path)

    def _resolve(candidate_raw: str) -> str:
        if os.path.isabs(candidate_raw) and os.path.isfile(candidate_raw):
            return os.path.normpath(candidate_raw)
        for base in search_dirs:
            candidate = os.path.normpath(os.path.join(base, candidate_raw))
            if os.path.isfile(candidate):
                return candidate
        return ""

    binary_candidates = [m.group(0).strip() for m in _BINARY_PATH_RE.finditer(task_text)]
    if not binary_candidates:
        return "", ""

    for raw in binary_candidates:
        found = _resolve(raw)
        if not found:
            # Try removing leading "./" or ".\" for relative references
            trimmed = raw.lstrip(".\\/ ")
            if trimmed and trimmed != raw:
                found = _resolve(trimmed)
        if found:
            if not has_modify_verb and _MODIFY_INTENT_CFG.get("allow_residual_instruction_without_verb", True):
                # Path + additional instruction text is usually an image-edit request even
                # when the user phrase is not covered by the explicit modify verb list.
                residual = task_text.replace(raw, " ").strip()
                min_residual_chars = int(_MODIFY_INTENT_CFG.get("min_residual_chars", 2))
                if len(_re.sub(r"\s+", "", residual)) < min_residual_chars:
                    continue
            elif not has_modify_verb:
                continue
            print(f"   [modify-intent] Detected binary file modify intent: {found}")
            return found, task_text

    return "", ""


def run_agent(user_task: str, verbose: bool = True) -> str:
    global _session_has_chat

    task_id = "task_" + uuid.uuid4().hex[:12]
    task_text = user_task.strip()
    force_new = task_text.lower().startswith("/new")
    if force_new:
        task_text = task_text[4:].strip()

    if force_new and not task_text:
        _session_has_chat = False
        print("New conversation window will open on next task.")
        return ""

    if not task_text:
        task_text = user_task

    new_chat = force_new or not _session_has_chat
    if new_chat:
        _session_has_chat = True

    _log_event("INFO", "task_start", task_id, task_text=task_text, new_chat=new_chat, force_new=force_new)

    prompt = _load_prompt()
    env_info = collect_env()
    inject_env_vars(env_info)
    env_block = to_prompt_block(env_info)
    file_ops_hint = schema_hint()
    enriched_task = _enrich_task(task_text, env_info)
    skill_hint = skills_to_prompt(task_text)
    project_traverse_hint = _load_prompt_file("project_traverse_hint") if "## Project structure:" in enriched_task else ""

    # ── Identity dispatch: round-0 AI judgment ───────────────────────────────
    # Send a lightweight dispatch message to let AI decide language + identity.
    # Falls back to code-side infer_category if the dispatch call fails or times out.
    _dispatch_language = ""
    _dispatch_identity = ""
    _task_category     = infer_category(task_text, env_info)   # code-side fallback

    dispatch_prompt_tpl = _load_prompt_file("identity_dispatch")
    if dispatch_prompt_tpl:
        dispatch_msg = dispatch_prompt_tpl + task_text
        try:
            _write_replay(task_id, "dispatch", "request", dispatch_msg)
            dispatch_reply = chat_via_bridge(dispatch_msg, new_chat=True, agent_id="dispatcher")
            _write_replay(task_id, "dispatch", "response", dispatch_reply)
            _lang, _identity_key, _cat = parse_dispatch_reply(dispatch_reply)
            if _identity_key:                        # AI gave a valid role
                _dispatch_language = _lang
                _dispatch_identity = _identity_key
                _task_category = _cat
                print(f"   [dispatch] lang={_lang!r}  identity={_identity_key!r}  category={_task_category}")
                _log_event("INFO", "skill_matched", task_id, category=_task_category, dispatch_language=_lang, identity=_identity_key)
            else:
                print(f"   [dispatch] Could not parse reply: {dispatch_reply[:80]!r} — using code fallback")
                _log_event("WARN", "dispatch_parse_failed", task_id, reply_preview=dispatch_reply)
        except Exception as _e:
            print(f"   [dispatch] Call failed ({_e}) — using code fallback")
            _log_event("WARN", "dispatch_failed", task_id, error=str(_e))

        finally:
            _close_agent_window("dispatcher")

    _matched_skill, _ = match_skill_by_category(task_text, env_info)
    _runtime_profile = get_skill_runtime_profile(_task_category, _matched_skill)
    _loop_policy = _runtime_profile.get("loop_policy", {}) if isinstance(_runtime_profile, dict) else {}
    _flow_name = str(_runtime_profile.get("flow", "default")).strip() or "default"
    _prompt_style = str(_runtime_profile.get("prompt_style", "plain")).strip() or "plain"
    _unmatched_task = not bool(_matched_skill) and not bool(skill_hint)
    _identity_line   = get_identity_prompt(_task_category, task_text[:80], language=_dispatch_language, skill=_matched_skill)
    _reviewer_prompt = get_reviewer_prompt(_task_category, _matched_skill)

    if _identity_line:
        print(f"   [identity] category={_task_category}  identity injected")
        _log_event("INFO", "identity_injected", task_id, category=_task_category)
    if _reviewer_prompt:
        print(f"   [reviewer] reviewer prompt loaded for category={_task_category}")
        _log_event("INFO", "reviewer_loaded", task_id, category=_task_category)
    # ── end identity dispatch ─────────────────────────────────────────────────

    first_message = (
        (f"{_identity_line}\n\n" if _identity_line else "")
        + f"{prompt}\n\n"
        f"{env_block}\n\n"
        f"{file_ops_hint}\n\n"
        + (f"{project_traverse_hint}\n\n" if project_traverse_hint else "")
        + (f"{skill_hint}\n\n" if skill_hint else "")
        + (f"建议执行流：{_runtime_profile.get('flow', _task_category)}\n\n" if (_runtime_profile.get("flow") or _unmatched_task) else "")
        + f"---\n\n"
        + f"User task (execute now, no questions): {enriched_task}"
    )
    messages_to_send = [first_message]
    runtime_identity_name = str(_runtime_profile.get("identity", "")).strip()
    runtime_identity_key = runtime_identity_name.replace("identity_", "") if runtime_identity_name.startswith("identity_") else runtime_identity_name
    effective_identity = _dispatch_identity or runtime_identity_key
    agent_id_main = get_agent_id_for_identity(effective_identity) if effective_identity else "default"
    agent_new_chat = new_chat
    if agent_id_main != "default":
        _close_agent_window(agent_id_main)
        agent_new_chat = True
    runtime = LoopRuntime(task_id, task_text, "default", messages_to_send, agent_new_chat, agent_id_main)

    if skill_hint:
        print(f"   [skills] Matched history skill injected into prompt")

    print(f"\n{'='*60}")
    print(f"Task: {task_text}" + (" (new window)" if new_chat and force_new else ""))
    print(f"Desktop: {env_info.get('desktop', '')}")
    print(f"{'='*60}\n")

    flow_ctx = FlowContext(
        chat=ChatContext(
            send=chat_via_bridge,
            max_iterations=MAX_ITERATIONS,
            max_invalid_reply_retries=MAX_INVALID_REPLY_RETRIES,
        ),
        io=IoContext(
            step_pause=_step_pause,
            write_replay=_write_replay,
            log_event=_log_event,
        ),
        diagnostics=DiagnosticsContext(
            local_diagnose=_local_diagnose,
            cannot_complete_marker=_cannot_complete_marker,
            is_valid_reply=_is_valid_reply,
            task_done_marker=_task_done_marker,
            has_tool_unavailable_claim=_has_tool_unavailable_claim,
            build_tool_correction=_build_tool_correction,
            build_code_block_correction=_build_write_code_code_block_correction,
        ),
        file_ops=FileOpsContext(
            poll_url=POLL_URL,
            capture_image_url=CAPTURE_IMG_URL,
            poll_interval_seconds=POLL_INTERVAL,
            poll_timeout_seconds=POLL_TIMEOUT,
            intercept_large_file_writes=_intercept_large_file_writes,
            run_from_text_with_blocks=run_from_text_with_blocks,
            detect_modify_intent=_detect_modify_intent,
            save_downloaded_file=_save_downloaded_file,
            is_terminal_file_chat_text_only=_is_terminal_file_chat_text_only,
            http_get=_http_get,
        ),
    )

    print(f"   [flow] Using flow={_flow_name} agent_id={agent_id_main}")
    try:
        if _flow_name == "file_chat_first":
            flow_result = _run_file_chat_first_flow(flow_ctx, task_text, env_info, _loop_policy, agent_id=agent_id_main)
            if flow_result is not None:
                return flow_result
        elif _flow_name == "direct_chat":
            flow_result = _run_direct_chat_asset_flow(
                flow_ctx,
                _identity_line,
                enriched_task,
                task_text,
                _loop_policy,
                agent_id=agent_id_main,
            )
            if flow_result is not None:
                return flow_result
        elif _flow_name == "script_then_run":
            return _run_script_then_run_loop(
                ctx=flow_ctx,
                task_id=task_id,
                task_text=task_text,
                verbose=verbose,
                messages_to_send=messages_to_send,
                new_chat=agent_new_chat,
                agent_id_main=agent_id_main,
                loop_policy=_loop_policy,
                task_category=_task_category,
                reviewer_prompt=_reviewer_prompt,
                unmatched_task=_unmatched_task,
            )
        elif _task_category == "write_code":
            return _run_code_loop(
                ctx=flow_ctx,
                task_id=task_id,
                task_text=task_text,
                verbose=verbose,
                messages_to_send=messages_to_send,
                new_chat=agent_new_chat,
                agent_id_main=agent_id_main,
                loop_policy=_loop_policy,
                task_category=_task_category,
                reviewer_prompt=_reviewer_prompt,
                unmatched_task=_unmatched_task,
            )
        elif _prompt_style == "direct_delivery":
            return _run_direct_delivery_loop(
                ctx=flow_ctx,
                task_id=task_id,
                task_text=task_text,
                verbose=verbose,
                messages_to_send=messages_to_send,
                new_chat=agent_new_chat,
                agent_id_main=agent_id_main,
                loop_policy=_loop_policy,
                reviewer_prompt=_reviewer_prompt,
            )

        return _run_default_loop(
            ctx=flow_ctx,
            task_id=task_id,
            task_text=task_text,
            verbose=verbose,
            runtime=runtime,
            loop_policy=_loop_policy,
            task_category=_task_category,
            reviewer_prompt=_reviewer_prompt,
            unmatched_task=_unmatched_task,
        )
    finally:
        if agent_id_main and agent_id_main != "default":
            _close_agent_window(agent_id_main)


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
    print("Multiline: newline to continue, empty line to submit.")
    print("Ensure AgentPilot is running: npm run api\n")

    while True:
        task = _read_multiline("Task (empty line to submit)> ")
        if task.lower() in ("quit", "exit", "q"):
            print("Bye")
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
