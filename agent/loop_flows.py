import json
import os
import time
import re as _re
from dataclasses import dataclass
from typing import Callable

from executor import extract_json_blocks
from env_context import collect as collect_env
from file_ops import _read_text
from loop_common import (
    LoopRuntime,
    append_successful_blocks as _append_successful_blocks,
    build_nonterminal_validation_followup as _build_nonterminal_validation_followup,
    build_verify_rewrite_hint as _build_verify_rewrite_hint,
    build_loop_fallback as _build_loop_fallback,
    collect_verify_output as _collect_verify_output,
    execute_with_feedback_override as _execute_with_feedback_override,
    handle_no_block_task_complete as _handle_no_block_task_complete,
    pick_recent_write_path as _pick_recent_write_path,
    register_no_progress as _register_no_progress,
    register_round_signature as _register_round_signature,
    reset_no_progress as _reset_no_progress,
    reset_round_signature as _reset_round_signature,
    session_has_write as _session_has_write,
    validate_completion_for_code as _validate_completion_for_code,
)
from skill_manager import save_skill_from_success


@dataclass(frozen=True)
class ChatContext:
    send: Callable[..., str]
    max_iterations: int
    max_invalid_reply_retries: int


@dataclass(frozen=True)
class IoContext:
    step_pause: Callable[[str, str], None]
    write_replay: Callable[[str, str, str, str], None]
    log_event: Callable[..., None]


@dataclass(frozen=True)
class DiagnosticsContext:
    local_diagnose: Callable[[str, str, dict, int], str]
    cannot_complete_marker: Callable[[str], bool]
    is_valid_reply: Callable[[str, str], bool]
    task_done_marker: Callable[[str], bool]
    has_tool_unavailable_claim: Callable[[str], bool]
    build_tool_correction: Callable[[list], str]
    build_code_block_correction: Callable[[], str]


@dataclass(frozen=True)
class FileOpsContext:
    poll_url: str
    capture_image_url: str
    poll_interval_seconds: float
    poll_timeout_seconds: float
    intercept_large_file_writes: Callable[..., tuple[list[dict], str, bool]]
    run_from_text_with_blocks: Callable[..., tuple[list[dict], str]]
    detect_modify_intent: Callable[[str, dict], tuple[str, str]]
    save_downloaded_file: Callable[[str, str, str], tuple[str, str]]
    is_terminal_file_chat_text_only: Callable[[dict], bool]
    http_get: Callable[[str], dict]


@dataclass(frozen=True)
class FlowContext:
    chat: ChatContext
    io: IoContext
    diagnostics: DiagnosticsContext
    file_ops: FileOpsContext


_INTERMEDIATE_REPLY_PATTERNS = [
    r"\u6b63\u5728\u641c\u7d22",
    r"\u6b63\u5728\u601d\u8003",
    r"\u6b63\u5728\u6d4f\u89c8",
    r"\u6b63\u5728\u67e5\u627e",
    r"\u6b63\u5728\u521b\u5efa",
    r"\u6b63\u5728\u751f\u6210",
    r"\u6b63\u5728\u5904\u7406",
    r"\u6b63\u5728\u7ed8\u5236",
    r"\u6b63\u5728\u6e32\u67d3",
    r"\u6b63\u5728\u4e0a\u4f20",
    r"\u6b63\u5728\u5206\u6790",
    r"\u6b63\u5728\u4fee\u6539",
    r"\u6b63\u5728\u4f18\u5316",
    r"Searching",
    r"Thinking",
    r"Looking up",
    r"Browsing",
    r"Creating",
    r"Generating",
    r"Processing",
    r"Drawing",
    r"Rendering",
    r"Uploading",
    r"Analyzing",
    r"Modifying",
]
_RETRYABLE_CHAT_ERROR_PATTERNS = (
    "send button not found",
    "reply timeout",
    "reply polling stalled",
    "reply polling timed out",
    "timed out",
    "time out",
    "target closed",
    "execution context was destroyed",
    "navigation failed",
    "net::err",
    "not clickable",
    "not an element",
)
_INTERMEDIATE_PREFIX_RE = _re.compile(r"^[\s\S]{0,20}?ChatGPT\s*[^\n:：]*[:：]\s*", _re.IGNORECASE)


def _is_intermediate(text: str) -> bool:
    if not text:
        return True
    cleaned = str(text).strip()
    if len(cleaned) < 5:
        return True
    if "Task complete" in cleaned or "\u4efb\u52a1\u5b8c\u6210" in cleaned:
        return False
    if any(token in cleaned for token in ('"command"', "```json", "```")):
        return False
    cleaned = _INTERMEDIATE_PREFIX_RE.sub("", cleaned).strip()
    return any(_re.search(pattern, cleaned, _re.IGNORECASE) for pattern in _INTERMEDIATE_REPLY_PATTERNS)

def _is_retryable_chat_error(error_text: str) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    return any(pattern in lowered for pattern in _RETRYABLE_CHAT_ERROR_PATTERNS)


def _has_fenced_json_block(text: str) -> bool:
    lowered = str(text or "").lower()
    return '"command"' in lowered and ("```json" in lowered or "```" in lowered)


_SAVE_PATH_SUFFIX_RE = r"\.(?:txt|md|csv|json|html|log|docx|xlsx|pptx)\b"
_WIN_SAVE_PATH_RE = _re.compile(
    rf"(?:[A-Za-z]:[\\/]|\\\\)[^\n\r\"'`<>|*?]+?{_SAVE_PATH_SUFFIX_RE}",
    _re.IGNORECASE,
)
_DESKTOP_ENV_SAVE_PATH_RE = _re.compile(
    rf"%DESKTOP%[\\/][^\n\r\"'`<>|*?]+?{_SAVE_PATH_SUFFIX_RE}",
    _re.IGNORECASE,
)
_DESKTOP_REL_SAVE_PATH_RE = _re.compile(
    rf"(?:Desktop|桌面)[\\/][^\n\r\"'`<>|*?]+?{_SAVE_PATH_SUFFIX_RE}",
    _re.IGNORECASE,
)

_SAVE_VERB_RE = _re.compile(
    r'(save(?:\s+it)?(?:\s+to|\s+as)?|write(?:\s+it)?(?:\s+to)?|export(?:\s+to)?|put(?:\s+it)?\s+on|'
    r'\u4fdd\u5b58|\u5199\u5165|\u5199\u5230|\u653e\u5230|\u653e\u5728|\u843d\u76d8|\u5bfc\u51fa|\u5b58\u5230)',
    _re.IGNORECASE,
)

_SAVE_DEST_RE = _re.compile(
    r'(desktop|documents|downloads|'
    r'\u684c\u9762|\u6587\u6863|\u4e0b\u8f7d|\u8bb0\u4e8b\u672c|notepad|'
    r'\.(?:txt|md|csv|json|html|log|docx|xlsx|pptx)\b|'
    r'\b(?:txt|markdown|md|csv|html|log|docx|xlsx|pptx)\b|'
    r'json\s+file|json\u6587\u4ef6)',
    _re.IGNORECASE,
)

_OFFICE_EXT_FALLBACK_MAP = {
    ".xlsx": ".csv",
    ".xls": ".csv",
    ".docx": ".txt",
    ".doc": ".txt",
}


def _task_suggests_save_to_disk(task_text: str) -> bool:
    if not (task_text or "").strip():
        return False
    text = str(task_text or "")
    if _WIN_SAVE_PATH_RE.search(text):
        return True
    return bool(_SAVE_VERB_RE.search(text) and _SAVE_DEST_RE.search(text))


def _guess_direct_delivery_write_path(task_text: str, ai_text: str, desktop_hint: str) -> str:
    claimed = _extract_claimed_save_paths(ai_text, desktop_hint)
    if claimed:
        path = claimed[0]
        root, ext = os.path.splitext(path)
        fallback_ext = _OFFICE_EXT_FALLBACK_MAP.get(ext.lower())
        return root + fallback_ext if fallback_ext else path
    desktop = str(desktop_hint or "").strip() or r"C:\Users\admin\Desktop"
    lowered = str(task_text or "").lower()
    filename = "result.txt"
    if any(token in lowered for token in ("stock", "stocks", "csv", "table", "list", "news")) or any(
        token in str(task_text or "") for token in ("股票", "表格", "列表", "新闻", "清单", "推荐")
    ):
        filename = "result.csv"
    return os.path.join(desktop, filename)


def _build_direct_delivery_write_example(task_text: str, ai_text: str, desktop_hint: str) -> str:
    sample_path = _guess_direct_delivery_write_path(task_text, ai_text, desktop_hint).replace("\\", "\\\\")
    return (
        "Output exactly one executable JSON object now. No explanation.\n"
        "Example:\n"
        f'{{"command":"file_op","action":"write","path":"{sample_path}","content":"<full final content>"}}'
    )


def _reply_marks_task_complete(ai_text: str) -> bool:
    if not (ai_text or "").strip():
        return False
    lowered = str(ai_text or "").lower()
    return "task complete" in lowered or "\u4efb\u52a1\u5b8c\u6210" in str(ai_text or "")


def _reply_claims_local_file_saved(ai_text: str) -> bool:
    if not (ai_text or "").strip():
        return False
    a = ai_text.lower()
    if "[file saved to:" in a:
        return True
    if (
        "saved to" in a
        or "saved as" in a
        or "written to" in a
        or "stored at" in a
        or "\u4fdd\u5b58\u5230" in ai_text
        or "\u5df2\u4fdd\u5b58" in ai_text
        or "\u5199\u5165\u5230" in ai_text
        or "\u5199\u5230" in ai_text
    ):
        return True
    if _WIN_SAVE_PATH_RE.search(ai_text or ""):
        return True
    if _DESKTOP_ENV_SAVE_PATH_RE.search(ai_text or ""):
        return True
    if _DESKTOP_REL_SAVE_PATH_RE.search(ai_text or ""):
        return True
    return False


def _resolve_claimed_save_path(raw_path: str, desktop_hint: str) -> str:
    path = str(raw_path or "").strip().strip("\"'`")
    path = path.rstrip(".,;:!?)，。；：！）】」")
    normalized = path.replace("/", "\\")
    desktop = str(desktop_hint or "").strip()
    upper = normalized.upper()
    if upper.startswith("%DESKTOP%\\"):
        rest = normalized[len("%DESKTOP%\\") :]
        return os.path.normpath(os.path.join(desktop or ".", rest))
    if normalized.lower().startswith("desktop\\") or normalized.startswith("桌面\\"):
        rest = normalized.split("\\", 1)[1] if "\\" in normalized else ""
        return os.path.normpath(os.path.join(desktop or ".", rest))
    return os.path.normpath(normalized)


def _extract_claimed_save_paths(ai_text: str, desktop_hint: str) -> list[str]:
    raw_matches = []
    text = str(ai_text or "")
    for pattern in (_WIN_SAVE_PATH_RE, _DESKTOP_ENV_SAVE_PATH_RE, _DESKTOP_REL_SAVE_PATH_RE):
        raw_matches.extend(match.group(0) for match in pattern.finditer(text))
    resolved = []
    seen = set()
    for raw in raw_matches:
        path = _resolve_claimed_save_path(raw, desktop_hint)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)
    return resolved


def _claimed_save_paths_missing(ai_text: str, desktop_hint: str) -> list[str]:
    paths = _extract_claimed_save_paths(ai_text, desktop_hint)
    return [p for p in paths if not os.path.isfile(p)]


def _direct_delivery_require_real_write(
    *,
    task_text: str,
    ai_text: str,
    wrote_this_session: bool,
    desktop_hint: str,
    last_write_path: str = "",
) -> str | None:
    """If model claims a file was saved but disk write did not happen, return corrective feedback."""
    task_requires_write = _task_suggests_save_to_disk(task_text)
    task_done = _reply_marks_task_complete(ai_text)
    claims_saved = _reply_claims_local_file_saved(ai_text)
    claimed_paths = _extract_claimed_save_paths(ai_text, desktop_hint)
    path_in_reply = bool(claimed_paths)

    if wrote_this_session:
        missing = [p for p in claimed_paths if not os.path.isfile(p)]
        if missing and (claims_saved or task_done):
            return (
                "[LOCAL EXECUTOR] You mentioned these paths but they do not exist on disk: "
                + ", ".join(missing[:5])
                + "\nYou must use file_op write (or write_web) so the executor actually creates the file. "
                "Do not claim Task complete until the file exists.\n"
                + _build_direct_delivery_write_example(task_text, ai_text, desktop_hint)
            )
        if task_requires_write and not path_in_reply:
            saved_path = str(last_write_path or "").strip()
            if saved_path:
                return (
                    "[LOCAL EXECUTOR] This task writes a local file, so your final reply must include the exact saved path.\n"
                    f"Reply again with Task complete and include this path: {saved_path}"
                )
            return (
                "[LOCAL EXECUTOR] This task writes a local file, so your final reply must include the exact saved path.\n"
                "Reply again with Task complete and include the real saved path."
            )
        return None

    if not (task_requires_write or path_in_reply):
        return None

    desk = (desktop_hint or "").strip() or "Desktop path from environment block above"
    if not (claims_saved or task_done):
        return (
            "[LOCAL EXECUTOR] This task requires a real file to be written to disk before it can finish.\n"
            "No successful file_op write / write_web has happened in this session yet.\n"
            "Output one executable JSON block that writes the final file (for example under "
            + desk
            + ").\n"
            + _build_direct_delivery_write_example(task_text, ai_text, desktop_hint)
        )
    return (
        "[LOCAL EXECUTOR] No file was written in this session (no successful file_op write / write_web). "
        "Saying the file is saved is not enough - the local executor never created a file.\n"
        "You MUST output one executable JSON block: file_op write with path (e.g. under "
        + desk
        + ") and full text in \"content\".\n"
        "Do not reply with Task complete until after a write succeeds.\n"
        + _build_direct_delivery_write_example(task_text, ai_text, desktop_hint)
    )


def _build_round_signature(ai_text: str, blocks: list[dict]) -> str:
    if blocks:
        slim_blocks = []
        for block in blocks:
            slim_blocks.append({
                "command": block.get("command"),
                "action": block.get("action"),
                "path": block.get("path"),
                "dst": block.get("dst"),
                "name": block.get("name"),
            })
        return "blocks::" + json.dumps(slim_blocks, sort_keys=True, ensure_ascii=False)
    cleaned = " ".join(str(ai_text or "").split())
    return "text::" + cleaned[:240]


def _build_strategy_reset_feedback(ai_text: str, blocks: list[dict], reset_count: int) -> str:
    if blocks:
        summary = json.dumps(
            [
                {
                    "command": block.get("command"),
                    "action": block.get("action"),
                    "path": block.get("path") or block.get("dst") or "",
                }
                for block in blocks
            ],
            ensure_ascii=False,
        )
        return (
            "[STRATEGY RESET] You are repeating the same executable plan and making no progress.\n"
            "Do not repeat the same command/action/path combination again.\n"
            "Do not ask the user to choose a method. Choose and execute the next method yourself.\n"
            "Choose a different method in the next reply.\n"
            "Examples: if read repeated, move to write/patch; if one tool failed, switch to another tool; "
            "if validation failed, rewrite the file fully or provide an alternative concrete verification step.\n"
            f"Repeated plan: {summary}\n"
            f"Reset count: {reset_count}"
        )
    preview = " ".join(str(ai_text or "").split())[:240]
    return (
        "[STRATEGY RESET] You are repeating non-executable or non-progress text.\n"
        "The next reply must take a different path: output a concrete executable JSON instruction, or explicitly say "
        "'Cannot complete this task' if you are giving up.\n"
        "Do not ask the user what to try next. If you need to check a file or path, do it yourself with JSON.\n"
        "Do not repeat the same explanation.\n"
        f"Repeated text: {preview}\n"
        f"Reset count: {reset_count}"
    )


_READ_ONLY_FILE_ACTIONS = {"read", "list", "exists", "tree", "find", "history"}


def _normalize_path(value: str) -> str:
    return os.path.normcase(str(value or "").strip().replace("/", "\\"))


def _extract_injected_file_content_paths(message: str) -> set[str]:
    matches = _re.findall(r"## File content:\s*([^\r\n(]+?)\s*\(\d+\s+lines\)", str(message or ""))
    return {_normalize_path(item) for item in matches if str(item).strip()}


def _is_desktop_image_cleanup_task(task_text: str) -> bool:
    text = str(task_text or "")
    return (
        bool(_re.search(r"(desktop|\u684c\u9762)", text, _re.IGNORECASE))
        and bool(_re.search(r"(delete|remove|cleanup|clean\s*up|clear|\u5220\u9664|\u5220\u6389|\u79fb\u9664|\u6e05\u7406)", text, _re.IGNORECASE))
        and bool(_re.search(r"(image|images|photo|photos|picture|pictures|png|jpg|jpeg|gif|bmp|webp|\u56fe\u7247|\u7167\u7247|\u622a\u56fe)", text, _re.IGNORECASE))
    )


def _build_desktop_image_cleanup_followup(task_text: str, blocks: list[dict], results: list[dict], feedback: str) -> str:
    if not _is_desktop_image_cleanup_task(task_text):
        return ""
    if len(blocks) != 1 or len(results) != 1:
        return ""
    block = blocks[0]
    result = results[0]
    if block.get("command") != "file_op" or block.get("action") != "list" or not result.get("success"):
        return ""

    desktop = str(block.get("path", "")).strip()
    if not desktop or not os.path.isdir(desktop):
        return ""

    image_names: list[str] = []
    for line in str(feedback or "").splitlines():
        match = _re.match(r"^\[F\]\s+(.+)$", line.strip())
        if not match:
            continue
        name = match.group(1).strip()
        if os.path.splitext(name)[1].lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
            image_names.append(name)

    if not image_names:
        return "No image files were listed on the desktop. If deletion is complete, output: ✅ Task complete: No image files were found on the desktop."

    lines = [
        "[RULE] The desktop listing already succeeded. Do not explain. Do not say the tool is unavailable.",
        "[RULE] Your next reply must contain only executable file_op delete JSON instruction(s) for the listed image files below.",
        "[RULE] After deleting all listed image files, output: ✅ Task complete: Deleted desktop image files.",
        "",
        "Delete these files now:",
    ]
    for name in image_names[:50]:
        full_path = os.path.join(desktop, name).replace("\\", "\\\\")
        lines.append(f'{{"command":"file_op","action":"delete","path":"{full_path}"}}')
    return "\n".join(lines).strip()


def _completed_write_web_response(blocks: list[dict], results: list[dict]) -> str:
    """Return a terminal response when an explicit write_web block succeeds."""
    for block, result in zip(blocks, results):
        if block.get("command") != "file_op" or block.get("action") != "write_web":
            continue
        if not result.get("success"):
            continue

        target_path = block.get("dst") or block.get("path") or ""
        if not target_path:
            stdout = result.get("stdout", "")
            match = _re.search(r'"path":\s*"([^"]+)"', stdout)
            if match:
                target_path = match.group(1)

        if target_path:
            return (
                f"[Execution result feedback]\n"
                f"{result.get('stdout', '').strip()}\n\n"
                f"[File saved to: {target_path}]\n"
                f"✅ Task complete: File saved to {target_path}"
            ).strip()

        return (
            f"[Execution result feedback]\n"
            f"{result.get('stdout', '').strip()}\n\n"
            f"✅ Task complete: write_web completed successfully"
        ).strip()
    return ""


def _completed_plain_write_response(blocks: list[dict], results: list[dict], verify_extra: str) -> str:
    """Return a terminal response when a normal text file write succeeds and no verifier exists."""
    if "[Validation] Skipped:" not in (verify_extra or ""):
        return ""
    write_actions = {"write", "write_chunk", "patch", "insert", "append", "delete_lines"}
    for block, result in zip(blocks, results):
        if block.get("command") != "file_op" or block.get("action") not in write_actions:
            continue
        if not result.get("success"):
            continue
        target_path = str(block.get("path") or block.get("dst") or "").strip()
        stdout = str(result.get("stdout", "")).strip()
        parts = ["[Execution result feedback]"]
        if stdout:
            parts.append(stdout)
        parts.append(verify_extra.strip())
        if target_path:
            parts.append(f"[File saved to: {target_path}]")
            parts.append(f"✅ Task complete: File updated at {target_path}")
        else:
            parts.append("✅ Task complete: File update completed")
        return "\n".join(part for part in parts if part).strip()
    return ""


def _completed_direct_delivery_write_response(blocks: list[dict], results: list[dict]) -> str:
    write_actions = {"write", "write_chunk", "patch", "insert", "append", "delete_lines"}
    for block, result in zip(blocks, results):
        if block.get("command") != "file_op" or block.get("action") not in write_actions:
            continue
        if not result.get("success"):
            continue
        target_path = str(block.get("path") or block.get("dst") or "").strip()
        stdout = str(result.get("stdout", "")).strip()
        parts = ["[Execution result feedback]"]
        if stdout:
            parts.append(stdout)
        if target_path:
            parts.append(f"[File saved to: {target_path}]")
            parts.append(f"✅ Task complete: File saved to {target_path}")
        else:
            parts.append("✅ Task complete: File saved successfully")
        return "\n".join(part for part in parts if part).strip()
    return ""


def _format_loop_policy_hint(loop_policy: dict, key: str, fallback: str = "") -> str:
    if not isinstance(loop_policy, dict):
        return fallback
    lines = [str(x).strip() for x in loop_policy.get(key, []) if str(x).strip()]
    if not lines:
        return fallback
    return "；".join(lines[:2])


def _format_flow_terminal_feedback(base_text: str, loop_policy: dict, default_stop: str = "", default_fallback: str = "") -> str:
    stop_hint = _format_loop_policy_hint(loop_policy, "stop_conditions", default_stop)
    fallback_hint = _format_loop_policy_hint(loop_policy, "fallback", default_fallback)
    text = base_text.rstrip()
    if stop_hint:
        text += f"\nStop condition: {stop_hint}"
    if fallback_hint:
        text += f"\nFallback: {fallback_hint}"
    return text


def _collect_candidate_output_paths(blocks: list[dict], results: list[dict], task_text: str = "") -> list[str]:
    paths: list[str] = []

    def _add(path_value: str) -> None:
        raw = str(path_value or "").strip().strip('"')
        if not raw:
            return
        norm = os.path.normpath(raw)
        if norm not in paths:
            paths.append(norm)

    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        _add(block.get("dst", ""))
        _add(block.get("path", ""))
    for result in results or []:
        if not isinstance(result, dict):
            continue
        _add(result.get("path", ""))
    for match in _re.finditer(r'([A-Za-z]:\\[^\s\'"<>|*?\r\n,，。；;]+)', task_text or ""):
        _add(match.group(1))
    return paths


def _evaluate_script_then_run_state(blocks: list[dict], results: list[dict], executed_blocks: list[dict],
                                    task_text: str, loop_policy: dict) -> dict:
    artifact_exts = {
        str(ext).lower() for ext in loop_policy.get("artifact_extensions", [])
        if str(ext).strip()
    }
    script_exts = {
        str(ext).lower() for ext in loop_policy.get("script_extensions", [".py"])
        if str(ext).strip()
    }
    require_artifact = bool(loop_policy.get("require_artifact", False))
    require_script_execution = bool(loop_policy.get("require_script_execution", False))

    all_blocks = list(executed_blocks or []) + list(blocks or [])
    script_written = any(
        b.get("command") == "file_op" and str(b.get("path", "")).lower().endswith(tuple(script_exts))
        for b in all_blocks if isinstance(b, dict)
    )
    script_executed = any(
        (
            b.get("command") == "python"
            and str(b.get("path", "")).lower().endswith(tuple(script_exts))
        ) or (
            b.get("command") in {"powershell", "cmd"}
            and any(ext in json.dumps(b, ensure_ascii=False).lower() for ext in script_exts)
        )
        for b in all_blocks if isinstance(b, dict)
    )

    candidate_paths = _collect_candidate_output_paths(all_blocks, results, task_text)
    artifact_paths = [
        path for path in candidate_paths
        if os.path.isfile(path)
        and (not artifact_exts or os.path.splitext(path)[1].lower() in artifact_exts)
        and os.path.splitext(path)[1].lower() not in script_exts
    ]
    return {
        "script_written": script_written,
        "script_executed": script_executed,
        "artifact_paths": artifact_paths,
        "artifact_ok": bool(artifact_paths) if require_artifact else True,
        "execution_ok": script_executed if require_script_execution else True,
    }


def _build_script_then_run_pushback(state: dict, loop_policy: dict) -> str:
    done_hint = _format_loop_policy_hint(loop_policy, "done_conditions", "最终交付物必须落盘。")
    fallback_hint = _format_loop_policy_hint(loop_policy, "fallback", "请返回当前诊断和建议下一步。")
    missing: list[str] = []
    if not state.get("execution_ok", True):
        missing.append("脚本尚未执行成功")
    if not state.get("artifact_ok", True):
        missing.append("最终交付物尚未生成")
    missing_text = "；".join(missing) if missing else "未满足 script_then_run 完成条件"
    return (
        "[REJECTED] script_then_run flow cannot finish yet.\n"
        f"Missing: {missing_text}\n"
        f"Done condition: {done_hint}\n"
        "You must execute the script and produce the final artifact file before declaring completion.\n"
        f"Fallback: {fallback_hint}"
    )


def _build_script_then_run_intro(loop_policy: dict) -> str:
    done_hint = _format_loop_policy_hint(loop_policy, "done_conditions", "脚本执行成功且最终产物落盘。")
    stop_hint = _format_loop_policy_hint(loop_policy, "stop_conditions", "若无法生成最终产物则停止并返回诊断。")
    return (
        "[script_then_run loop]\n"
        "本任务必须先产出脚本，再执行脚本，最后交付最终文件。\n"
        f"完成条件：{done_hint}\n"
        f"停止条件：{stop_hint}\n"
        "脚本本身不是最终交付物。"
    )


def _build_code_loop_intro(loop_policy: dict) -> str:
    done_hint = _format_loop_policy_hint(loop_policy, "done_conditions", "代码修改落盘且至少完成一次验证。")
    stop_hint = _format_loop_policy_hint(loop_policy, "stop_conditions", "连续验证失败超过阈值后停止并返回诊断。")
    return (
        "[write_code loop]\n"
        "本任务必须围绕读取代码、修改代码、执行验证展开。\n"
        f"完成条件：{done_hint}\n"
        f"停止条件：{stop_hint}\n"
        "不要只给解释，必须真正写入并验证。"
    )


def _build_direct_delivery_intro(loop_policy: dict) -> str:
    done_hint = _format_loop_policy_hint(loop_policy, "done_conditions", "内容完整并可直接交付。")
    fallback_hint = _format_loop_policy_hint(loop_policy, "fallback", "无法完成时给出诊断与下一步。")
    return (
        "[direct_delivery loop]\n"
        "本任务优先直接交付最终内容或保存后的最终文件。\n"
        "如果任务要求保存到桌面或本地文件，必须先输出可执行 JSON（例如 file_op write 或 python），"
        "真实写入成功后才能说 Task complete。\n"
        "不要说工具不可用，不要只用自然语言声称“已保存”。\n"
        f"完成条件：{done_hint}\n"
        f"失败回退：{fallback_hint}"
    )


def _run_reviewer_if_needed(ctx: FlowContext, task_id: str, reviewer_prompt: str, task_text: str, ai_text: str) -> tuple[bool, str]:
    if not reviewer_prompt:
        return True, ""
    review_msg = (
        f"{reviewer_prompt}\n\n"
        f"--- 原始需求 ---\n{task_text}\n\n"
        f"--- 执行摘要 ---\n{ai_text}\n\n"
        f"请给出「通过」或具体改进意见。"
    )
    try:
        ctx.io.log_event("INFO", "review_start", task_id, request=review_msg[:200])
        ctx.io.write_replay(task_id, "review", "request", review_msg)
        review_reply = ctx.chat.send(review_msg, new_chat=True, agent_id="reviewer")
        ctx.io.write_replay(task_id, "review", "response", review_reply)
    except Exception as e:
        ctx.io.log_event("ERROR", "review_failed", task_id, error=str(e))
        return True, ""
    approved = "通过" in review_reply or "pass" in review_reply.lower() or "approved" in review_reply.lower()
    ctx.io.log_event("INFO" if approved else "WARN", "review_result", task_id, result="pass" if approved else "fail", reply=review_reply)
    return approved, review_reply


def _loop_chat_round(ctx: FlowContext, runtime: LoopRuntime, verbose: bool) -> str:
    runtime.iteration += 1
    if verbose:
        print(f"[{runtime.iteration}] AI thinking... [{runtime.loop_name}]")
    ctx.io.step_pause(runtime.task_id, f"before_round_{runtime.iteration}_chat")
    request_text = runtime.messages_to_send[-1]
    request_new_chat = runtime.new_chat
    max_attempts = 3
    wait_seconds = 8
    for attempt in range(1, max_attempts + 1):
        try:
            request_slot = "request" if attempt == 1 else f"request_retry_{attempt}"
            response_slot = "response" if attempt == 1 else f"response_retry_{attempt}"
            ctx.io.write_replay(runtime.task_id, f"round_{runtime.iteration}", request_slot, request_text)
            ai_text = ctx.chat.send(request_text, new_chat=request_new_chat, agent_id=runtime.agent_id_main)
            ctx.io.write_replay(runtime.task_id, f"round_{runtime.iteration}", response_slot, ai_text)
            runtime.new_chat = False
            return ai_text
        except RuntimeError as e:
            err_str = str(e)
            retryable = _is_retryable_chat_error(err_str)
            ctx.io.log_event(
                "WARN" if retryable and attempt < max_attempts else "ERROR",
                "chat_error",
                runtime.task_id,
                iteration=runtime.iteration,
                attempt=attempt,
                retryable=retryable,
                error=err_str,
                flow=runtime.loop_name,
            )
            if retryable and attempt < max_attempts:
                print(f"\n   [bridge] transient chat error ({attempt}/{max_attempts}): {err_str}")
                print(f"   [bridge] waiting {wait_seconds}s then retrying...")
                time.sleep(wait_seconds)
                request_new_chat = False
                continue
            raise
    raise RuntimeError("chat round failed without a retry result")


def _loop_invalid_reply(ctx: FlowContext, runtime: LoopRuntime, ai_text: str) -> str | None:
    runtime.invalid_reply_count += 1
    if runtime.invalid_reply_count > ctx.chat.max_invalid_reply_retries:
        runtime.strategy_reset_count += 1
        runtime.invalid_reply_count = 0
        runtime.messages_to_send.append(
            ctx.diagnostics.local_diagnose(runtime.task_text, ai_text, collect_env(), ctx.chat.max_invalid_reply_retries)
            + "\n\n"
            + _build_strategy_reset_feedback(ai_text, [], runtime.strategy_reset_count)
        )
        return None
    runtime.messages_to_send.append(ctx.diagnostics.local_diagnose(runtime.task_text, ai_text, collect_env(), runtime.invalid_reply_count))
    return None


def _loop_reset_invalid(runtime: LoopRuntime) -> None:
    runtime.invalid_reply_count = 0


def _handle_write_web_completion(ctx: FlowContext, task_id: str, iteration: int, reply_text: str) -> str:
    ctx.io.log_event("INFO", "task_complete", task_id, iteration=iteration, final_reply=reply_text)
    ctx.io.write_replay(task_id, f"round_{iteration}", "feedback", reply_text)
    return reply_text


def _apply_reviewer_feedback(
    *,
    ctx: FlowContext,
    task_id: str,
    reviewer_prompt: str,
    task_text: str,
    ai_text: str,
    messages_to_send: list[str],
    rejected_prefix: str = "[审核反馈]\n",
) -> bool:
    approved, review_reply = _run_reviewer_if_needed(ctx, task_id, reviewer_prompt, task_text, ai_text)
    if approved:
        return True
    messages_to_send.append(f"{rejected_prefix}{review_reply}")
    return False


def _build_repeated_failure_hint(current_blocks: list[dict], error_count: int) -> str:
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
    return (
        f"The same error has appeared {error_count} time(s) in a row. "
        f"Your patch is not working. Switch strategy:\n"
        f"1. Read the CURRENT file content below carefully.\n"
        f"2. Rewrite the ENTIRE file with file_op write (not patch).\n"
        f"3. Fix ALL errors at once, not just one line.\n"
        f"{read_hint}"
    )


def _parse_blocks_with_logging(
    ctx: FlowContext,
    *,
    task_id: str,
    iteration: int,
    ai_text: str,
    label: str = "loop",
    flow: str = "",
) -> list[dict]:
    blocks = extract_json_blocks(ai_text)
    print(f"   [{label}] extract_json_blocks found {len(blocks)} block(s)")
    extra = {"count": len(blocks)}
    if flow:
        extra["flow"] = flow
    ctx.io.log_event("INFO", "json_blocks_parsed", task_id, iteration=iteration, **extra)
    for block_index, block in enumerate(blocks):
        summary = f"command={block.get('command')} action={block.get('action', '')} path={block.get('path', '')[:60]}"
        print(f"   [{label}]   block[{block_index}]: {summary}")
    return blocks


def _execute_blocks_round(
    ctx: FlowContext,
    *,
    task_id: str,
    runtime: LoopRuntime,
    task_text: str,
    ai_text: str,
    blocks: list[dict],
    verbose: bool,
    flow: str = "",
) -> tuple[list[dict], list[dict], str, str]:
    current_blocks, feedback_override, had_real_write = ctx.file_ops.intercept_large_file_writes(
        blocks,
        task_text=task_text,
        agent_id=runtime.agent_id_main,
        ai_text=ai_text,
    )
    if feedback_override and had_real_write:
        runtime.had_intercepted_write = True
    if not feedback_override:
        print(f"   [loop] executing pre-parsed blocks ({len(current_blocks)})...")
    results, feedback = _execute_with_feedback_override(
        task_id=task_id,
        runtime=runtime,
        blocks=current_blocks,
        feedback_override=feedback_override,
        step_pause_fn=ctx.io.step_pause,
        run_blocks_fn=ctx.file_ops.run_from_text_with_blocks,
        verbose=verbose and not feedback_override,
    )
    extra = {
        "block_count": len(current_blocks),
        "success": [r.get("success") for r in results],
    }
    if flow:
        extra["flow"] = flow
    ctx.io.log_event("INFO", "json_block_executed", task_id, iteration=runtime.iteration, **extra)
    _append_successful_blocks(runtime, current_blocks, results)
    return current_blocks, results, feedback, feedback_override


def _begin_loop_round(
    ctx: FlowContext,
    *,
    runtime: LoopRuntime,
    task_id: str,
    task_text: str,
    verbose: bool,
    label: str,
    flow: str = "",
    allow_cannot_complete: bool = False,
    cannot_complete_formatter: Callable[[str], str] | None = None,
) -> tuple[str, str, list[dict]]:
    try:
        ai_text = _loop_chat_round(ctx, runtime, verbose)
    except RuntimeError as e:
        err_text = str(e).strip()
        retryable = _is_retryable_chat_error(err_text)
        if retryable:
            if err_text == runtime.last_chat_error:
                runtime.chat_error_count += 1
            else:
                runtime.last_chat_error = err_text
                runtime.chat_error_count = 1
            retry_budget = max(2, min(4, ctx.chat.max_invalid_reply_retries + 1))
            if runtime.chat_error_count <= retry_budget:
                print(f"\n   [bridge] chat round incomplete, retrying loop ({runtime.chat_error_count}/{retry_budget})...")
                time.sleep(2)
                return "retry", "", []
        ctx.io.log_event("ERROR", "chat_round_failed", task_id, iteration=runtime.iteration, error=err_text, flow=flow or runtime.loop_name)
        return "terminal", f"[Bridge error] {err_text}", []

    runtime.chat_error_count = 0
    runtime.last_chat_error = ""

    if verbose:
        print(f"\n[AI reply]\n{ai_text}\n")

    if allow_cannot_complete and ctx.diagnostics.cannot_complete_marker(ai_text):
        print(f"\nAI cannot complete this task")
        ctx.io.log_event("WARN", "task_cannot_complete", task_id, iteration=runtime.iteration, reply=ai_text)
        terminal = cannot_complete_formatter(ai_text) if cannot_complete_formatter else ai_text
        return "terminal", terminal, []

    if not ctx.diagnostics.is_valid_reply(ai_text, task_text):
        result = _loop_invalid_reply(ctx, runtime, ai_text)
        if result is not None:
            return "terminal", result, []
        ctx.io.log_event("WARN", "invalid_reply", task_id, iteration=runtime.iteration, attempt=runtime.invalid_reply_count, reply=ai_text)
        return "retry", ai_text, []

    _loop_reset_invalid(runtime)
    blocks = _parse_blocks_with_logging(
        ctx,
        task_id=task_id,
        iteration=runtime.iteration,
        ai_text=ai_text,
        label=label,
        flow=flow,
    )
    repeat_count = _register_round_signature(runtime, _build_round_signature(ai_text, blocks))
    if repeat_count >= 3:
        runtime.strategy_reset_count += 1
        feedback = _build_strategy_reset_feedback(ai_text, blocks, runtime.strategy_reset_count)
        runtime.messages_to_send.append(feedback)
        ctx.io.log_event(
            "WARN",
            "strategy_reset",
            task_id,
            iteration=runtime.iteration,
            repeat_count=repeat_count,
            reset_count=runtime.strategy_reset_count,
            flow=flow or runtime.loop_name,
        )
        return "retry", "", []
    return "ok", ai_text, blocks


def _run_script_then_run_loop(
    *,
    ctx: FlowContext,
    task_id: str,
    task_text: str,
    verbose: bool,
    messages_to_send: list[str],
    new_chat: bool,
    agent_id_main: str,
    loop_policy: dict,
    task_category: str,
    reviewer_prompt: str,
    unmatched_task: bool,
) -> str:
    runtime = LoopRuntime(task_id, task_text, "script_then_run", messages_to_send, new_chat, agent_id_main)
    runtime.messages_to_send[0] = runtime.messages_to_send[0] + "\n\n" + _build_script_then_run_intro(loop_policy)

    while True:
        status, ai_text, blocks = _begin_loop_round(
            ctx,
            runtime=runtime,
            task_id=task_id,
            task_text=task_text,
            verbose=verbose,
            label="script_then_run",
            flow="script_then_run",
            allow_cannot_complete=True,
            cannot_complete_formatter=lambda text: _format_flow_terminal_feedback(text, loop_policy),
        )
        if status == "error":
            return ""
        if status == "terminal":
            return ai_text
        if status == "retry":
            continue
        if not blocks:
            if ctx.diagnostics.task_done_marker(ai_text):
                state = _evaluate_script_then_run_state([], [], runtime.executed_blocks, task_text, loop_policy)
                if not (state.get("artifact_ok", True) and state.get("execution_ok", True)):
                    runtime.messages_to_send.append(_build_script_then_run_pushback(state, loop_policy))
                    continue
                print(f"\nscript_then_run declared task complete")
                ctx.io.log_event("INFO", "task_complete", task_id, iteration=runtime.iteration, final_reply=ai_text)
                return ai_text
            runtime.strategy_reset_count += 1
            runtime.messages_to_send.append(_build_strategy_reset_feedback(ai_text, [], runtime.strategy_reset_count))
            continue

        if verbose:
            print(f"[{runtime.iteration}] Executing {len(blocks)} local instruction(s)... [script_then_run]")

        current_blocks, results, feedback, feedback_override = _execute_blocks_round(
            ctx,
            task_id=task_id,
            runtime=runtime,
            task_text=task_text,
            ai_text=ai_text,
            blocks=blocks,
            verbose=False,
            flow="script_then_run",
        )

        if feedback_override:
            if feedback_override.startswith("[FILE_CHAT_TERMINAL]\n"):
                terminal_feedback = feedback_override.replace("[FILE_CHAT_TERMINAL]\n", "", 1)
                terminal_feedback = _format_flow_terminal_feedback(terminal_feedback, loop_policy)
                ctx.io.write_replay(task_id, f"round_{runtime.iteration}", "feedback", terminal_feedback)
                return terminal_feedback
            if "[File saved to:" in feedback_override or ctx.diagnostics.task_done_marker(feedback_override):
                state = _evaluate_script_then_run_state(current_blocks, [], runtime.executed_blocks, task_text, loop_policy)
                if state.get("artifact_ok", True) and state.get("execution_ok", True):
                    return feedback_override

        state = _evaluate_script_then_run_state(current_blocks, results, runtime.executed_blocks, task_text, loop_policy)

        write_web_done = _completed_write_web_response(current_blocks, results)
        if write_web_done and state.get("artifact_ok", True) and state.get("execution_ok", True):
            runtime.had_intercepted_write = True
            return _handle_write_web_completion(ctx, task_id, runtime.iteration, write_web_done)

        verify_extra = _collect_verify_output(current_blocks, results, verbose)
        full_feedback = feedback + verify_extra
        ctx.io.write_replay(task_id, f"round_{runtime.iteration}", "feedback", full_feedback)
        runtime.messages_to_send.append(full_feedback)

        if "❌" in verify_extra and all(r["success"] for r in results):
            runtime.verify_fail_count += 1
            runtime.messages_to_send[-1] = full_feedback + (
                "\n\n[SELF-HEAL] 脚本已写入但验证失败。请修正脚本并重新执行，直到生成最终交付物。"
            )
            if runtime.verify_fail_count > ctx.chat.max_invalid_reply_retries:
                return _format_flow_terminal_feedback(ai_text, loop_policy)
            continue
        runtime.verify_fail_count = 0

        if ctx.diagnostics.task_done_marker(ai_text):
            session_wrote = _session_has_write(runtime.executed_blocks)
            if not session_wrote and not runtime.had_intercepted_write:
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + (
                    "[REJECTED] You said 'Task complete' but no file write has been executed yet."
                )
                continue
            if not (state.get("artifact_ok", True) and state.get("execution_ok", True)):
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + _build_script_then_run_pushback(state, loop_policy)
                continue
            if runtime.executed_blocks:
                skill_notes = f"explored_category={task_category}" if unmatched_task else ""
                save_skill_from_success(task_text, runtime.executed_blocks, notes=skill_notes, category=task_category)
            if reviewer_prompt:
                if not _apply_reviewer_feedback(
                    ctx=ctx,
                    task_id=task_id,
                    reviewer_prompt=reviewer_prompt,
                    task_text=task_text,
                    ai_text=ai_text,
                    messages_to_send=runtime.messages_to_send,
                ):
                    continue
            ctx.io.log_event("INFO", "task_complete", task_id, iteration=runtime.iteration, final_reply=ai_text)
            return ai_text

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            err_summary = "|".join((r.get("stderr") or "")[:80] for r in failed)
            repeated = err_summary and err_summary in runtime.error_history
            runtime.error_history.append(err_summary)
            same_patch = current_blocks == runtime.last_failed_blocks and any(b.get("action") == "patch" for b in current_blocks)
            runtime.last_failed_blocks = current_blocks
            if repeated or same_patch:
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + (
                    "[SELF-HEAL] 请改为重写完整脚本并再次执行，直到最终文件生成。"
                )

        if state.get("script_written") and state.get("execution_ok") and not state.get("artifact_ok"):
            runtime.messages_to_send[-1] = full_feedback + "\n\n" + _build_script_then_run_pushback(state, loop_policy)

        time.sleep(0.5)

    ctx.io.log_event("ERROR", "task_failed", task_id, error="max_iterations_reached", flow="script_then_run")
    return _build_loop_fallback(runtime, loop_policy, _format_loop_policy_hint, ctx.chat.max_iterations)


def _run_direct_delivery_loop(
    *,
    ctx: FlowContext,
    task_id: str,
    task_text: str,
    verbose: bool,
    messages_to_send: list[str],
    new_chat: bool,
    agent_id_main: str,
    loop_policy: dict,
    reviewer_prompt: str,
) -> str:
    runtime = LoopRuntime(task_id, task_text, "direct_delivery", messages_to_send, new_chat, agent_id_main)
    runtime.messages_to_send[0] = runtime.messages_to_send[0] + "\n\n" + _build_direct_delivery_intro(loop_policy)

    while True:
        status, ai_text, blocks = _begin_loop_round(
            ctx,
            runtime=runtime,
            task_id=task_id,
            task_text=task_text,
            verbose=verbose,
            label="direct_delivery",
            flow="direct_delivery",
            allow_cannot_complete=True,
            cannot_complete_formatter=lambda text: _format_flow_terminal_feedback(text, loop_policy),
        )
        if status == "error":
            return ""
        if status == "terminal":
            return ai_text
        if status == "retry":
            continue

        if not blocks:
            if not ai_text.strip():
                continue
            desk = str(collect_env().get("desktop") or "")
            gate = _direct_delivery_require_real_write(
                task_text=task_text,
                ai_text=ai_text,
                wrote_this_session=_session_has_write(runtime.executed_blocks),
                desktop_hint=desk,
                last_write_path=_pick_recent_write_path(runtime.executed_blocks),
            )
            if gate:
                runtime.messages_to_send.append(gate)
                if verbose:
                    print(f"   [direct_delivery] blocked prose-only completion: require real file_op write")
                continue
            if _apply_reviewer_feedback(
                ctx=ctx,
                task_id=task_id,
                reviewer_prompt=reviewer_prompt,
                task_text=task_text,
                ai_text=ai_text,
                messages_to_send=runtime.messages_to_send,
            ):
                ctx.io.log_event("INFO", "task_complete", task_id, iteration=runtime.iteration, final_reply=ai_text, flow="direct_delivery")
                return ai_text
            continue

        current_blocks, results, feedback, feedback_override = _execute_blocks_round(
            ctx,
            task_id=task_id,
            runtime=runtime,
            task_text=task_text,
            ai_text=ai_text,
            blocks=blocks,
            verbose=False,
            flow="direct_delivery",
        )
        if feedback_override and feedback_override.startswith("[FILE_CHAT_TERMINAL]\n"):
            terminal_feedback = feedback_override.replace("[FILE_CHAT_TERMINAL]\n", "", 1)
            return _format_flow_terminal_feedback(terminal_feedback, loop_policy)
        write_web_done = _completed_write_web_response(current_blocks, results)
        if write_web_done:
            if _apply_reviewer_feedback(
                ctx=ctx,
                task_id=task_id,
                reviewer_prompt=reviewer_prompt,
                task_text=task_text,
                ai_text=write_web_done,
                messages_to_send=runtime.messages_to_send,
                rejected_prefix=f"{write_web_done}\n\n[审核反馈]\n",
            ):
                return write_web_done
            continue
        direct_write_done = _completed_direct_delivery_write_response(current_blocks, results)
        if direct_write_done:
            return _handle_write_web_completion(ctx, task_id, runtime.iteration, direct_write_done)
        runtime.messages_to_send.append(feedback)

    return _build_loop_fallback(runtime, loop_policy, _format_loop_policy_hint, ctx.chat.max_iterations)


def _run_code_loop(
    *,
    ctx: FlowContext,
    task_id: str,
    task_text: str,
    verbose: bool,
    messages_to_send: list[str],
    new_chat: bool,
    agent_id_main: str,
    loop_policy: dict,
    task_category: str,
    reviewer_prompt: str,
    unmatched_task: bool,
) -> str:
    runtime = LoopRuntime(task_id, task_text, "write_code", messages_to_send, new_chat, agent_id_main)
    runtime.messages_to_send[0] = runtime.messages_to_send[0] + "\n\n" + _build_code_loop_intro(loop_policy)

    while True:
        status, ai_text, blocks = _begin_loop_round(
            ctx,
            runtime=runtime,
            task_id=task_id,
            task_text=task_text,
            verbose=verbose,
            label="write_code",
            flow="write_code",
            allow_cannot_complete=True,
            cannot_complete_formatter=lambda text: _format_flow_terminal_feedback(text, loop_policy),
        )
        if status == "error":
            return ""
        if status == "terminal":
            return ai_text
        if status == "retry":
            continue

        if not blocks:
            if ctx.diagnostics.task_done_marker(ai_text):
                ok, completion_feedback = _validate_completion_for_code(runtime, [], require_verify=True)
                if not ok:
                    runtime.messages_to_send.append(completion_feedback)
                    continue
                approved, review_reply = _run_reviewer_if_needed(ctx, task_id, reviewer_prompt, task_text, ai_text)
                if approved:
                    if runtime.executed_blocks:
                        skill_notes = f"explored_category={task_category}" if unmatched_task else ""
                        save_skill_from_success(task_text, runtime.executed_blocks, notes=skill_notes, category=task_category)
                    return ai_text
                runtime.messages_to_send.append(f"[审核反馈]\n{review_reply}")
                continue
            if runtime.executed_blocks and (
                runtime.verify_fail_count > 0
                or runtime.had_intercepted_write
                or ctx.diagnostics.cannot_complete_marker(ai_text)
                or ctx.diagnostics.has_tool_unavailable_claim(ai_text)
            ):
                followup = _build_nonterminal_validation_followup()
                runtime.messages_to_send.append(f"{ai_text}\n\n{followup}".strip())
                continue
            runtime.strategy_reset_count += 1
            runtime.messages_to_send.append(_build_strategy_reset_feedback(ai_text, [], runtime.strategy_reset_count))
            continue

        format_warning = ""
        if blocks and not _has_fenced_json_block(ai_text):
            format_warning = ctx.diagnostics.build_code_block_correction()

        read_only_round = all(
            block.get("command") == "file_op" and block.get("action") in _READ_ONLY_FILE_ACTIONS
            for block in blocks
        )
        if read_only_round:
            injected_paths = _extract_injected_file_content_paths(runtime.messages_to_send[0]) if runtime.messages_to_send else set()
            round_paths = {
                _normalize_path(block.get("path", ""))
                for block in blocks
                if block.get("command") == "file_op" and block.get("path")
            }
            if injected_paths and round_paths and round_paths.issubset(injected_paths):
                focus_path = next(iter(round_paths), "")
                followup = (
                    "[REJECTED] The full file content is already available in the current chat.\n"
                    "Do NOT read the same file again.\n"
                    "Your next reply must contain exactly one file_op write/patch instruction that fixes the code."
                )
                if focus_path:
                    escaped = focus_path.replace("\\", "\\\\")
                    followup += (
                        "\nUse this target path:\n"
                        f"```json\n{{\"command\":\"file_op\",\"action\":\"write\",\"path\":\"{escaped}\",\"content\":\"...\"}}\n```"
                    )
                runtime.messages_to_send.append(followup)
                continue
            read_signature = json.dumps(blocks, sort_keys=True, ensure_ascii=False)
            repeat_count = _register_no_progress(runtime, f"read_only::{read_signature}")
            if repeat_count >= 2:
                focus_path = _pick_recent_write_path(runtime.executed_blocks) or str(blocks[0].get("path", "")).strip()
                followup = (
                    "[REJECTED] You already issued the same read-only instruction and it does not move the task forward.\n"
                    "Do NOT read the same file again.\n"
                    "Your next reply must contain exactly one file_op write/patch instruction that fixes the code."
                )
                if focus_path:
                    escaped = focus_path.replace("\\", "\\\\")
                    followup += (
                        "\nUse this target path:\n"
                        f"```json\n{{\"command\":\"file_op\",\"action\":\"write\",\"path\":\"{escaped}\",\"content\":\"...\"}}\n```"
                    )
                runtime.messages_to_send.append(followup)
                continue
        else:
            _reset_no_progress(runtime)

        current_blocks, results, feedback, feedback_override = _execute_blocks_round(
            ctx,
            task_id=task_id,
            runtime=runtime,
            task_text=task_text,
            ai_text=ai_text,
            blocks=blocks,
            verbose=False,
            flow="write_code",
        )
        if feedback_override:
            if feedback_override.startswith("[FILE_CHAT_TERMINAL]\n"):
                return _format_flow_terminal_feedback(feedback_override.replace("[FILE_CHAT_TERMINAL]\n", "", 1), loop_policy)
            if "[File saved to:" in feedback_override or ctx.diagnostics.task_done_marker(feedback_override):
                return feedback_override

        verify_extra = _collect_verify_output(current_blocks, results, verbose=False)
        full_feedback = feedback + verify_extra
        if format_warning:
            full_feedback += "\n\n" + format_warning
        runtime.messages_to_send.append(full_feedback)
        plain_write_done = _completed_plain_write_response(current_blocks, results, verify_extra)
        if plain_write_done:
            return _handle_write_web_completion(ctx, task_id, runtime.iteration, plain_write_done)

        if "❌" in verify_extra and all(r["success"] for r in results):
            runtime.verify_fail_count += 1
            runtime.messages_to_send[-1] = full_feedback + "\n\n[SELF-HEAL] 验证失败，请重读当前文件后整体修复，并再次运行验证。"
            if runtime.verify_fail_count > ctx.chat.max_invalid_reply_retries:
                runtime.messages_to_send[-1] += (
                    "\n\n[Validation] Automatic verification is still failing. "
                    "Keep the loop alive: rewrite the full file or provide a concrete alternative verification step."
                )
            continue
        runtime.verify_fail_count = 0

        if ctx.diagnostics.task_done_marker(ai_text):
            ok, completion_feedback = _validate_completion_for_code(runtime, current_blocks, require_verify=True)
            if not ok:
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + completion_feedback
                continue
            approved, review_reply = _run_reviewer_if_needed(ctx, task_id, reviewer_prompt, task_text, ai_text)
            if approved:
                if runtime.executed_blocks:
                    skill_notes = f"explored_category={task_category}" if unmatched_task else ""
                    save_skill_from_success(task_text, runtime.executed_blocks, notes=skill_notes, category=task_category)
                return ai_text
            runtime.messages_to_send.append(f"[审核反馈]\n{review_reply}")
            continue

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            err_summary = "|".join((r.get("stderr") or "")[:80] for r in failed)
            repeated = err_summary and err_summary in runtime.error_history
            runtime.error_history.append(err_summary)
            same_patch = current_blocks == runtime.last_failed_blocks and any(b.get("action") == "patch" for b in current_blocks)
            runtime.last_failed_blocks = current_blocks
            if repeated or same_patch:
                runtime.messages_to_send[-1] = full_feedback + "\n\n[SELF-HEAL] 同类错误重复出现。请改为重读文件并整体重写，不要继续局部 patch。"

        time.sleep(0.5)

    return _build_loop_fallback(runtime, loop_policy, _format_loop_policy_hint, ctx.chat.max_iterations)


def _build_direct_chat_flow_message(identity_line: str, enriched_task: str, task_text: str) -> str:
    return (
        (f"{identity_line}\n\n" if identity_line else "")
        + "直接完成本次交付，不要解释过程，不要转交，不要输出中间方案。\n\n"
        + f"User task: {enriched_task or task_text}"
    )


def _build_direct_chat_retry_prompt(text_only_rounds: int) -> str:
    if text_only_rounds >= 6:
        return (
            "Continue the same image-generation task in this chat.\n"
            "Previous rounds returned text only and made no progress.\n"
            "Do not explain. Do not route. Do not apologize.\n"
            "Deliver the final downloadable image only, or reply exactly 'Cannot complete this task' if you are giving up."
        )
    if text_only_rounds >= 3:
        return (
            "Continue the same image-generation task in this chat.\n"
            "Previous rounds returned text only.\n"
            "Do not explain. Deliver the final downloadable image only."
        )
    return (
        "Continue the same image-generation task in this chat.\n"
        "Do not explain. Do not route. Deliver the final downloadable image only."
    )

def _try_capture_direct_chat_asset(ctx: FlowContext, task_text: str, agent_id: str, reply_text: str = "", fallback_ext: str = ".bin") -> str:
    poll = ctx.file_ops.http_get(f"{ctx.file_ops.poll_url}?agentId={agent_id}")
    poll_b64 = poll.get("downloaded_b64", "")
    poll_ext = poll.get("downloaded_ext", fallback_ext)
    if poll_b64:
        save_path, suffix = ctx.file_ops.save_downloaded_file(poll_b64, task_text, ext=poll_ext)
        if suffix:
            print("\nDirect asset flow complete (captured via poll)")
            return _confirm_direct_chat_completion(ctx, agent_id, save_path, (reply_text.strip() + suffix).strip())

    cap = ctx.file_ops.http_get(f"{ctx.file_ops.capture_image_url}?agentId={agent_id}", timeout=30)
    cap_b64 = cap.get("downloaded_b64", "")
    cap_ext = cap.get("downloaded_ext", fallback_ext)
    if cap_b64:
        save_path, suffix = ctx.file_ops.save_downloaded_file(cap_b64, task_text, ext=cap_ext)
        if suffix:
            print("\nDirect asset flow complete (captured via explicit image capture)")
            return _confirm_direct_chat_completion(ctx, agent_id, save_path, (reply_text.strip() + suffix).strip())
    return ""


def _wait_for_direct_chat_asset(
    ctx: FlowContext,
    *,
    task_text: str,
    agent_id: str,
    reply_text: str,
    fallback_ext: str,
    round_deadline: float,
) -> tuple[str, str, bool]:
    latest_text = reply_text or ""
    latest_generating = False
    while time.time() < round_deadline:
        captured = _try_capture_direct_chat_asset(
            ctx,
            task_text,
            agent_id,
            reply_text=latest_text,
            fallback_ext=fallback_ext,
        )
        if captured:
            return captured, latest_text, latest_generating

        poll = ctx.file_ops.http_get(f"{ctx.file_ops.poll_url}?agentId={agent_id}")
        if poll.get("ok"):
            poll_text = (poll.get("text") or "").strip()
            if poll_text:
                latest_text = poll_text
            latest_generating = bool(poll.get("generating", False))
            if not latest_generating and latest_text and not _is_intermediate(latest_text):
                captured = _try_capture_direct_chat_asset(
                    ctx,
                    task_text,
                    agent_id,
                    reply_text=latest_text,
                    fallback_ext=fallback_ext,
                )
                if captured:
                    return captured, latest_text, latest_generating
                break

        time.sleep(max(0.5, float(ctx.file_ops.poll_interval_seconds)))

    return "", latest_text, latest_generating


def _confirm_direct_chat_completion(ctx: FlowContext, agent_id: str, saved_path: str, fallback_text: str = "") -> str:
    confirm_prompt = (
        f"The generated image has already been downloaded and saved locally to: {saved_path}\n"
        "Do not generate another image. Reply with task complete and include the saved path."
    )
    try:
        confirmation = (ctx.chat.send(confirm_prompt, new_chat=False, agent_id=agent_id) or "").strip()
    except Exception:
        confirmation = ""

    if not confirmation:
        return fallback_text or f"[File saved to: {saved_path}]\n✅ Task complete: File saved to {saved_path}"

    if f"[File saved to: {saved_path}]" not in confirmation:
        confirmation = confirmation + f"\n\n[File saved to: {saved_path}]"
    if "Task complete" not in confirmation and "任务完成" not in confirmation:
        confirmation = confirmation + f"\n✅ Task complete: File saved to {saved_path}"
    return confirmation.strip()


def _run_file_chat_first_flow(ctx: FlowContext, task_text: str, env_info: dict, loop_policy: dict, agent_id: str = "default") -> str | None:
    _modify_file, _modify_msg = ctx.file_ops.detect_modify_intent(task_text, env_info)
    if not _modify_file:
        return None
    print(f"\n{'='*60}")
    print(f"Task (modify-intent): {task_text}")
    print(f"Uploading file for modification: {_modify_file}")
    print(f"{'='*60}\n")
    from file_ops import _call_file_chat as _fchat

    fc_result = _fchat(_modify_file, _modify_msg, agent_id=agent_id)
    if not fc_result.get("ok"):
        print(f"   [modify-intent] file-chat failed: {fc_result.get('error')} — falling back to normal loop")
        return None

    dl_b64 = fc_result.get("downloaded_b64", "")
    dl_ext = fc_result.get("downloaded_ext", os.path.splitext(_modify_file)[1] or ".bin")
    reply_text = fc_result.get("text", "")
    if dl_b64:
        _, suffix = ctx.file_ops.save_downloaded_file(dl_b64, task_text, ext=dl_ext)
        if suffix:
            print(f"\nModify-intent complete (file captured)")
            return reply_text + suffix
    if ctx.file_ops.is_terminal_file_chat_text_only(fc_result):
        print(f"   [modify-intent] Text-only final reply detected, stopping without retry loop")
        return _format_flow_terminal_feedback(
            "file-chat uploaded successfully, but ChatGPT returned text only and no modified image was captured.\n"
            f"Reply: {reply_text[:240]}",
            loop_policy,
            default_stop="上传成功但未拿到成品图片，停止当前回路。",
            default_fallback="请返回失败原因并说明下一步建议。",
        )

    print(f"   [modify-intent] No download in file-chat response, polling /poll...")
    poll = ctx.file_ops.http_get(f"{ctx.file_ops.poll_url}?agentId={agent_id}")
    poll_b64 = poll.get("downloaded_b64", "")
    poll_ext = poll.get("downloaded_ext", os.path.splitext(_modify_file)[1] or ".bin")
    if poll_b64:
        _, suffix = ctx.file_ops.save_downloaded_file(poll_b64, task_text, ext=poll_ext)
        if suffix:
            print(f"\nModify-intent complete (file captured via poll)")
            return reply_text + suffix
    print(f"   [modify-intent] No download found. Reply: {reply_text[:120]}")
    print(f"   [modify-intent] Falling through to normal loop")
    return None


def _run_direct_chat_asset_flow(ctx: FlowContext, identity_line: str, enriched_task: str, task_text: str, loop_policy: dict,
                                agent_id: str = "default") -> str | None:
    from file_ops import _call_direct_chat

    prompt = _build_direct_chat_flow_message(identity_line, enriched_task, task_text)
    text_only_rounds = 0
    attempt = 0

    while True:
        attempt += 1
        result = _call_direct_chat(prompt, agent_id=agent_id)
        if not result.get("ok"):
            print(f"   [direct-chat] Direct asset flow failed: {result.get('error')} ? falling back to normal loop")
            return None

        downloaded_b64 = result.get("downloaded_b64", "")
        downloaded_ext = result.get("downloaded_ext", ".bin")
        reply_text = (result.get("result", "") or result.get("text", "") or "").strip()
        if downloaded_b64:
            save_path, suffix = ctx.file_ops.save_downloaded_file(downloaded_b64, task_text, ext=downloaded_ext)
            if suffix:
                print(f"\nDirect asset flow complete")
                return _confirm_direct_chat_completion(ctx, agent_id, save_path, (reply_text + suffix).strip())

        round_deadline = time.time() + max(6.0, float(ctx.file_ops.poll_interval_seconds) * 4.0)
        captured, latest_text, latest_generating = _wait_for_direct_chat_asset(
            ctx,
            task_text=task_text,
            agent_id=agent_id,
            reply_text=reply_text,
            fallback_ext=downloaded_ext,
            round_deadline=round_deadline,
        )
        if captured:
            return captured
        if latest_text:
            reply_text = latest_text

        if ctx.diagnostics.cannot_complete_marker(reply_text):
            print("   [direct-chat] Explicit cannot-complete received")
            return _format_flow_terminal_feedback(
                reply_text,
                loop_policy,
                default_stop="Explicit cannot-complete received; stop the current asset loop.",
                default_fallback="Return the failure reason and the next-step suggestion.",
            )

        if result.get("generating", False) or latest_generating:
            print("   [direct-chat] Reply still generating ? continuing asset loop")
            time.sleep(1.0)
            continue

        if reply_text:
            text_only_rounds += 1
            print(f"   [direct-chat] Text-only round {text_only_rounds}, continuing asset loop")
            prompt = _build_direct_chat_retry_prompt(text_only_rounds)
            time.sleep(0.5)
            continue

        print("   [direct-chat] Empty reply without asset ? continuing asset loop")
        prompt = _build_direct_chat_retry_prompt(text_only_rounds)
        time.sleep(0.5)


def _run_default_loop(
    *,
    ctx: FlowContext,
    task_id: str,
    task_text: str,
    verbose: bool,
    runtime: LoopRuntime,
    loop_policy: dict,
    task_category: str,
    reviewer_prompt: str,
    unmatched_task: bool,
) -> str:
    while True:
        status, ai_text, blocks = _begin_loop_round(
            ctx,
            runtime=runtime,
            task_id=task_id,
            task_text=task_text,
            verbose=verbose,
            label="loop",
            allow_cannot_complete=True,
            cannot_complete_formatter=lambda text: _format_flow_terminal_feedback(text, loop_policy),
        )
        if status == "error":
            return ""
        if status == "terminal":
            if ai_text.startswith("[Bridge error]"):
                print(f"\n{ai_text}")
                return ai_text
            if ctx.diagnostics.cannot_complete_marker(ai_text):
                print(f"\nAI cannot complete this task")
            else:
                print(f"\nTerminal stop condition reached")
            return ai_text
        if status == "retry":
            if runtime.invalid_reply_count > 0 and runtime.messages_to_send:
                print(f"   [diagnose] {runtime.messages_to_send[-1][:120].replace(chr(10), ' ')}...")
            else:
                print(f"   [bridge] retrying current round...")
            continue

        if not blocks:
            ok, no_block_feedback = _handle_no_block_task_complete(runtime, ai_text, ctx.diagnostics.task_done_marker)
            if ctx.diagnostics.task_done_marker(ai_text) and not ok:
                print("\n[!] [false-done/no-blocks] Task complete + no blocks parsed and no prior write - pushing back")
                runtime.messages_to_send.append(no_block_feedback)
                continue
            if ctx.diagnostics.task_done_marker(ai_text):
                print(f"\nAgent declared task complete (no more blocks)")
                return ai_text
            if runtime.executed_blocks and (
                runtime.verify_fail_count > 0
                or runtime.had_intercepted_write
                or ctx.diagnostics.cannot_complete_marker(ai_text)
                or ctx.diagnostics.has_tool_unavailable_claim(ai_text)
            ):
                followup = _build_nonterminal_validation_followup()
                runtime.messages_to_send.append(f"{ai_text}\n\n{followup}".strip())
                continue
            if verbose and ai_text:
                preview = ai_text[:200] + ("..." if len(ai_text) > 200 else "")
                print(f"\nNo exec instruction (received {len(ai_text)} chars)")
                print(f"   Preview: {preview!r}")
            runtime.strategy_reset_count += 1
            runtime.messages_to_send.append(_build_strategy_reset_feedback(ai_text, [], runtime.strategy_reset_count))
            continue

        if verbose:
            print(f"[{runtime.iteration}] Executing {len(blocks)} local instruction(s)...")

        current_blocks, results, feedback, feedback_override = _execute_blocks_round(
            ctx,
            task_id=task_id,
            runtime=runtime,
            task_text=task_text,
            ai_text=ai_text,
            blocks=blocks,
            verbose=verbose,
        )
        print(f"   [loop] after intercept: {len(current_blocks)} remaining block(s), feedback_override={'yes' if feedback_override else 'no'}")
        if feedback_override:
            if feedback_override.startswith("[FILE_CHAT_TERMINAL]\n"):
                terminal_feedback = feedback_override.replace("[FILE_CHAT_TERMINAL]\n", "", 1)
                print(f"\nfile-chat returned text-only terminal reply; stopping loop")
                ctx.io.log_event("WARN", "file_chat_terminal_text_only", task_id, iteration=runtime.iteration, feedback=terminal_feedback)
                stop_hint = _format_loop_policy_hint(loop_policy, "stop_conditions")
                fallback_hint = _format_loop_policy_hint(loop_policy, "fallback")
                if stop_hint or fallback_hint:
                    terminal_feedback = (
                        terminal_feedback
                        + (f"\nStop condition: {stop_hint}" if stop_hint else "")
                        + (f"\nFallback: {fallback_hint}" if fallback_hint else "")
                    )
                ctx.io.write_replay(task_id, f"round_{runtime.iteration}", "feedback", terminal_feedback)
                return terminal_feedback
            if ctx.diagnostics.task_done_marker(feedback_override) or "[File saved to:" in feedback_override:
                print(f"\nDirect generation complete")
                return feedback_override

        write_web_done = _completed_write_web_response(current_blocks, results)
        if write_web_done:
            runtime.had_intercepted_write = True
            print(f"\nwrite_web completed; stopping loop")
            return _handle_write_web_completion(ctx, task_id, runtime.iteration, write_web_done)

        verify_extra = _collect_verify_output(current_blocks, results, verbose)

        control_cleanup_followup = _build_desktop_image_cleanup_followup(task_text, current_blocks, results, feedback)
        if control_cleanup_followup:
            print("\ncontrol cleanup rule generated next-step delete instructions")
            ctx.io.log_event("INFO", "control_cleanup_followup_generated", task_id, iteration=runtime.iteration)
            ctx.io.write_replay(task_id, f"round_{runtime.iteration}", "feedback", control_cleanup_followup)
            runtime.messages_to_send.append(control_cleanup_followup)
            continue

        if verbose:
            print(f"\n[Execution result]\n{feedback}\n")

        full_feedback = feedback + verify_extra
        ctx.io.write_replay(task_id, f"round_{runtime.iteration}", "feedback", full_feedback)
        runtime.messages_to_send.append(full_feedback)

        if "❌" in verify_extra and all(r["success"] for r in results):
            runtime.verify_fail_count += 1
            print(f"\n[!] [verify-fail] auto_verify failed (attempt {runtime.verify_fail_count}) - injecting rewrite hint")
            rewrite_hint = _build_verify_rewrite_hint(current_blocks, _read_text, runtime.verify_fail_count)
            runtime.messages_to_send[-1] = full_feedback + rewrite_hint
            if runtime.verify_fail_count > ctx.chat.max_invalid_reply_retries:
                print(f"\nVerify retries exceeded; continuing loop with stronger guidance")
                runtime.messages_to_send[-1] += (
                    "\n\n[Validation] Automatic verification is still failing. "
                    "Keep the loop alive: rewrite the full file or provide a concrete alternative verification step."
                )
            continue
        runtime.verify_fail_count = 0

        if ctx.diagnostics.has_tool_unavailable_claim(ai_text) and results:
            correction = ctx.diagnostics.build_tool_correction(results)
            print(f"\n[!] [correction] AI falsely claimed tool unavailable - injecting correction")
            runtime.messages_to_send[-1] = full_feedback + "\n\n" + correction

        if ctx.diagnostics.task_done_marker(ai_text):
            ok, completion_feedback = _validate_completion_for_code(runtime, current_blocks, require_verify=True)
            if not ok:
                print(f"\n[Self-check] Completion guard rejected current result, continuing fix loop...")
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + completion_feedback
                continue
            print(f"\nAgent declared task complete")
            if runtime.executed_blocks:
                skill_notes = f"explored_category={task_category}" if unmatched_task else ""
                skill_name = save_skill_from_success(
                    task_text,
                    runtime.executed_blocks,
                    notes=skill_notes,
                    category=task_category,
                )
                print(f"   [skills] Saved skill: {skill_name}")

            if reviewer_prompt:
                print(f"   [reviewer] Sending review request...")
                if not _apply_reviewer_feedback(
                    ctx=ctx,
                    task_id=task_id,
                    reviewer_prompt=reviewer_prompt,
                    task_text=task_text,
                    ai_text=ai_text,
                    messages_to_send=runtime.messages_to_send,
                    rejected_prefix="[审核反馈] 审核专家认为本次结果未达标，具体意见如下，请修改：\n",
                ):
                    print(f"   [reviewer] Rejected - pushing feedback back to AI")
                    continue

            ctx.io.log_event("INFO", "task_complete", task_id, iteration=runtime.iteration, final_reply=ai_text)
            return ai_text

        if not all(r["success"] for r in results):
            failed = [r for r in results if not r["success"]]
            print(f"[{runtime.iteration}] {len(failed)} instruction(s) failed, AI will retry...")

            err_summary = "|".join((r.get("stderr") or "")[:80] for r in failed)
            repeated = err_summary and err_summary in runtime.error_history
            runtime.error_history.append(err_summary)

            same_patch = (
                current_blocks == runtime.last_failed_blocks
                and any(b.get("action") == "patch" for b in current_blocks)
            )
            runtime.last_failed_blocks = current_blocks

            if repeated or same_patch:
                retry_msg = _build_repeated_failure_hint(current_blocks, len(runtime.error_history))
                runtime.messages_to_send[-1] = full_feedback + "\n\n" + retry_msg
                print(f"   [self-heal] Repeated error detected - forcing full rewrite strategy")

        time.sleep(0.5)

    print(f"\nMax iterations ({ctx.chat.max_iterations}) reached, stopping")
    ctx.io.log_event("ERROR", "task_failed", task_id, error="max_iterations_reached")
    return _build_loop_fallback(runtime, loop_policy, _format_loop_policy_hint, ctx.chat.max_iterations)

