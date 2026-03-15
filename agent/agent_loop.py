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
from executor import run_from_text, extract_json_blocks, auto_verify_py, _extract_py_targets
from env_context import collect as collect_env, to_prompt_block, inject_env_vars
from file_ops import schema_hint, _read_text, _is_binary_file
from skill_manager import skills_to_prompt, save_skill_from_success

# Config (edit as needed)
CHAT_URL = os.environ.get("AGENTPILOT_URL", "http://127.0.0.1:3000/chat")
MAX_ITERATIONS = 100
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
    Legitimate reply = has JSON block OR task-done marker OR pure conversational.
    Everything else is anomalous → enters repair loop.
    """
    if not text or len(text.strip()) < 3:
        return False
    if _task_done_marker(text):
        return True
    if _has_json_block(text):
        return True
    if _is_conversational(text, task_text):
        return True
    return False


# ── 本地诊断器 ──────────────────────────────────────────────

def _local_diagnose(task_text: str, ai_text: str, env_info: dict, attempt: int) -> str:
    """
    When AI returns an anomalous reply: auto-diagnose locally,
    read file content summaries, and return a graded retry instruction.
    """
    desktop = env_info.get("desktop", "")

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
    else:
        diag_lines.append("No local files found from task description.")

    diag_lines.append("")

    if attempt == 1:
        instruction = (
            "Your reply did not contain a JSON execution block. "
            "The local executor can only act on JSON blocks — it cannot execute plain text instructions. "
            "Please output the appropriate JSON block now to continue."
        )
    elif attempt == 2:
        instruction = (
            "STILL no JSON block. Every action MUST be a JSON block. "
            "Do NOT say 'please upload', 'cannot access', 'try manually', or describe steps in text. "
            "Output the JSON block directly. "
            "Use the file paths listed above — they exist on this machine right now."
        )
    else:
        instruction = (
            "FINAL WARNING: You have now failed to output a JSON block 3 times. "
            "Rules: (1) Never say you cannot execute. (2) Never ask for file uploads. "
            "(3) The ONLY valid response is a JSON block. "
            "Output one JSON block right now or output: Cannot complete this task."
        )

    diag_lines.append(instruction)
    return "\n".join(diag_lines)


def _task_done_marker(text: str) -> bool:
    return (
        "✅ Task complete" in text or "Task complete:" in text
        or "✅ 任务完成" in text or "任务完成：" in text
    )

def _cannot_complete_marker(text: str) -> bool:
    return "无法完成此任务" in text or "Cannot complete this task" in text


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
    """Send message and return reply; poll /poll on intermediate state."""
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
    return poll.get("text", result)


def run_agent(user_task: str, verbose: bool = True) -> str:
    global _session_has_chat

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
    _error_history: list[str] = []
    _last_failed_blocks: list[dict] = []
    _invalid_reply_count = 0

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
            err_str = str(e)
            # Send button busy (AI still generating) → wait and retry once
            if "Send button not found" in err_str and iteration < MAX_ITERATIONS:
                print(f"\n   [bridge] Send button busy, waiting 8s then retrying...")
                time.sleep(8)
                try:
                    ai_text = chat_via_bridge(messages_to_send[-1], new_chat=False, agent_id=agent_id_main)
                except RuntimeError as e2:
                    print(f"\nError: {e2}")
                    return ""
            else:
                print(f"\nError: {e}")
                return ""

        new_chat = False

        if verbose:
            print(f"\n[AI reply]\n{ai_text}\n")

        if _cannot_complete_marker(ai_text):
            print(f"\nAI cannot complete this task")
            return ai_text

        # ── Unified reply validation ──────────────────────────────
        if not _is_valid_reply(ai_text, task_text):
            _invalid_reply_count += 1
            print(f"\n⚠️  Invalid reply (attempt {_invalid_reply_count}/3) — running local diagnosis...")
            if _invalid_reply_count > 10:
                print(f"\nExceeded max invalid reply attempts, stopping")
                return ai_text
            diag = _local_diagnose(task_text, ai_text, env_info, _invalid_reply_count)
            print(f"   [diagnose] {diag[:120].replace(chr(10),' ')}...")
            messages_to_send.append(diag)
            continue

        _invalid_reply_count = 0

        blocks = extract_json_blocks(ai_text)

        if not blocks:
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
        current_blocks = _parse_blocks(ai_text)
        for block, r in zip(current_blocks, results):
            if r.get("success"):
                executed_blocks.append(block)

        # ── Auto-verify: re-run .py files after write/patch ──────
        verify_extra = ""
        if any(not r["success"] for r in results) or any(
            b.get("action") in ("write", "patch") and b.get("path", "").endswith(".py")
            for b in current_blocks
        ):
            py_targets = _extract_py_targets(current_blocks)
            for pt in py_targets:
                v = auto_verify_py(pt)
                if v:
                    verify_extra += v
                    if verbose:
                        print(v)

        if verbose:
            print(f"\n[Execution result]\n{feedback}\n")

        full_feedback = feedback + verify_extra
        messages_to_send.append(full_feedback)

        if _task_done_marker(ai_text):
            final_verify = ""
            for pt in _extract_py_targets(current_blocks):
                final_verify += auto_verify_py(pt)
            if "❌" in final_verify:
                print(f"\n[Self-check] Final verify failed, continuing fix loop...")
                messages_to_send[-1] += final_verify + (
                    "\nThe file still has errors after your fix. "
                    "Read the file content first, then rewrite it completely with file_op write."
                )
                continue
            print(f"\nAgent declared task complete")
            if executed_blocks:
                skill_name = save_skill_from_success(task_text, executed_blocks)
                print(f"   [skills] Saved skill: {skill_name}")
            return ai_text

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            print(f"[{iteration}] {len(failed)} instruction(s) failed, AI will retry...")

            err_summary = "|".join((r.get("stderr") or "")[:80] for r in failed)
            repeated = err_summary and err_summary in _error_history
            _error_history.append(err_summary)

            same_patch = (
                current_blocks == _last_failed_blocks
                and any(b.get("action") == "patch" for b in current_blocks)
            )
            _last_failed_blocks = current_blocks

            if repeated or same_patch:
                failed_paths = list({
                    b.get("path", "") for b in current_blocks
                    if b.get("path", "").endswith(".py")
                })
                read_hint = ""
                for fp in failed_paths:
                    try:
                        content = _read_text(fp)
                        lines = content.splitlines()
                        preview = "\n".join(lines[:150])
                        if len(lines) > 150:
                            preview += f"\n...({len(lines)} lines total, truncated)"
                        read_hint += f"\n\nCurrent file content of {fp}:\n```python\n{preview}\n```"
                    except Exception:
                        pass
                retry_msg = (
                    f"The same error has appeared {len(_error_history)} time(s) in a row. "
                    f"Your patch is not working. Switch strategy:\n"
                    f"1. Read the CURRENT file content below carefully.\n"
                    f"2. Rewrite the ENTIRE file with file_op write (not patch).\n"
                    f"3. Fix ALL errors at once, not just one line.\n"
                    f"{read_hint}"
                )
                messages_to_send[-1] = full_feedback + "\n\n" + retry_msg
                print(f"   [self-heal] Repeated error detected — forcing full rewrite strategy")

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
