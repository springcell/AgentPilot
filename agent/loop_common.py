from dataclasses import dataclass, field
from executor import auto_verify_py, _extract_py_targets


@dataclass
class LoopRuntime:
    task_id: str
    task_text: str
    loop_name: str
    messages_to_send: list[str]
    new_chat: bool
    agent_id_main: str
    executed_blocks: list[dict] = field(default_factory=list)
    iteration: int = 0
    invalid_reply_count: int = 0
    had_intercepted_write: bool = False
    verify_fail_count: int = 0
    error_history: list[str] = field(default_factory=list)
    last_failed_blocks: list[dict] = field(default_factory=list)
    no_progress_count: int = 0
    last_no_progress_signature: str = ""
    chat_error_count: int = 0
    last_chat_error: str = ""
    round_repeat_count: int = 0
    last_round_signature: str = ""
    strategy_reset_count: int = 0


WRITE_ACTIONS = {"write", "write_chunk", "write_web", "patch", "insert", "delete_lines", "append"}


def append_successful_blocks(runtime: LoopRuntime, blocks: list[dict], results: list[dict]) -> None:
    for block, result in zip(blocks, results):
        if result.get("success"):
            runtime.executed_blocks.append(block)


def collect_verify_output(blocks: list[dict], results: list[dict], verbose: bool = False) -> str:
    verify_extra = ""
    py_targets = _extract_py_targets(blocks)
    wrote_files = any(
        b.get("command") == "file_op" and b.get("action") in WRITE_ACTIONS
        for b in blocks
    )
    if any(not r["success"] for r in results) or py_targets:
        for pt in py_targets:
            v = auto_verify_py(pt)
            if v:
                verify_extra += v
                if verbose:
                    print(v)
    elif wrote_files and all(r["success"] for r in results):
        verify_extra = "\n[Validation] Skipped: no supported automatic verifier for the written targets."
        if verbose:
            print(verify_extra)
    return verify_extra


def session_has_write(executed_blocks: list[dict]) -> bool:
    return any(
        b.get("command") == "file_op" and b.get("action") in WRITE_ACTIONS
        for b in executed_blocks
    )


def build_verify_rewrite_hint(current_blocks: list[dict], read_text_fn, attempt: int) -> str:
    py_targets = _extract_py_targets(current_blocks)
    read_hint = ""
    for fp in py_targets:
        try:
            content = read_text_fn(fp)
            lines = content.splitlines()
            preview = "\n".join(lines[:150])
            if len(lines) > 150:
                preview += f"\n...({len(lines)} lines total)"
            read_hint += f"\n\nCurrent file content of {fp}:\n```python\n{preview}\n```"
        except Exception:
            pass
    rewrite_hint = (
        "\n\n[SELF-HEAL] The file was written but has syntax errors. "
        "Rules for writing Python inside JSON:\n"
        "1. Use \\n for newlines (NOT actual newlines in the content string).\n"
        "2. Use \\\\ for backslashes, e.g. 'C:\\\\Users\\\\...' or use r-strings.\n"
        "3. Use double quotes for all JSON strings.\n"
        "Rewrite the ENTIRE file content now, fixing ALL errors at once."
        + read_hint
    )
    if attempt >= 3:
        rewrite_hint += (
            f"\n\nFINAL WARNING: attempt {attempt}. "
            "Output the complete corrected file as a single file_op write block, nothing else."
        )
    return rewrite_hint


def validate_completion_for_code(runtime: LoopRuntime, current_blocks: list[dict], require_verify: bool = True) -> tuple[bool, str]:
    if not session_has_write(runtime.executed_blocks) and not runtime.had_intercepted_write:
        return False, (
            "[REJECTED] You said 'Task complete' but NO file write has been executed yet.\n"
            "The executor has not written any changes to disk.\n"
            "You MUST output the appropriate file_op JSON block to actually perform the modification.\n"
            "Output the write_web (for large files) or write block now."
        )
    py_targets = _extract_py_targets(current_blocks) or _extract_py_targets(runtime.executed_blocks)
    if require_verify and py_targets:
        final_verify = "".join(auto_verify_py(pt) for pt in py_targets)
        if "❌" in final_verify:
            return False, (
                final_verify
                + "\nThe file still has errors after your fix. "
                "Read the file content first, then rewrite it completely with file_op write."
            )
    return True, ""


def build_nonterminal_validation_followup() -> str:
    return (
        "[REJECTED] Validation feedback or unavailable tooling is not a terminal state.\n"
        "Do not stop the task yet.\n"
        "Either output the next executable JSON fix, or give a concrete alternative verification step after the write.\n"
        "Do not reply with only 'cannot complete' or 'tool unavailable'."
    )


def execute_with_feedback_override(
    *,
    task_id: str,
    runtime: LoopRuntime,
    blocks: list[dict],
    feedback_override: str,
    step_pause_fn,
    run_blocks_fn,
    verbose: bool = False,
) -> tuple[list[dict], str]:
    current_blocks = blocks
    step_pause_fn(task_id, f"before_round_{runtime.iteration}_execute")
    if feedback_override:
        results_raw, feedback_normal = run_blocks_fn(current_blocks, task_id=task_id) if current_blocks else ([], "")
        feedback = feedback_override + ("\n\n" + feedback_normal if feedback_normal else "")
        return results_raw, feedback
    results, feedback = run_blocks_fn(current_blocks, task_id=task_id)
    if verbose:
        print(f"   [loop] block execution returned {len(results)} result(s): {[r.get('success') for r in results]}")
    return results, feedback


def build_loop_fallback(runtime: LoopRuntime, loop_policy: dict, format_hint_fn, max_iterations: int) -> str:
    fallback_hint = format_hint_fn(loop_policy, "fallback", "请返回当前诊断、阻塞原因和建议的下一步。")
    if runtime.messages_to_send:
        return runtime.messages_to_send[-1] + f"\n\n[Loop fallback]\n{fallback_hint}"
    return f"Stopped after reaching max iterations ({max_iterations}).\nFallback: {fallback_hint}"


def build_false_done_pushback() -> str:
    return (
        "[REJECTED] You said 'Task complete' but NO file write has been executed yet, "
        "and your JSON instruction could not be parsed (likely malformed or single-quoted).\n"
        "Rules:\n"
        "1. JSON must use double quotes, not single quotes.\n"
        "2. A plain JSON object is enough; a code fence is optional.\n"
        "3. Output the write instruction again, correctly formatted.\n\n"
        "Example format:\n"
        '{"command":"file_op","action":"write","path":"...","content":"..."}'
    )


def handle_no_block_task_complete(runtime: LoopRuntime, ai_text: str, task_done_marker_fn) -> tuple[bool, str]:
    if not task_done_marker_fn(ai_text):
        return True, ai_text
    if not session_has_write(runtime.executed_blocks) and not runtime.had_intercepted_write:
        return False, build_false_done_pushback()
    return True, ai_text


def register_no_progress(runtime: LoopRuntime, signature: str) -> int:
    signature = str(signature or "").strip()
    if not signature:
        signature = "<empty>"
    if signature == runtime.last_no_progress_signature:
        runtime.no_progress_count += 1
    else:
        runtime.last_no_progress_signature = signature
        runtime.no_progress_count = 1
    return runtime.no_progress_count


def reset_no_progress(runtime: LoopRuntime) -> None:
    runtime.no_progress_count = 0
    runtime.last_no_progress_signature = ""


def register_round_signature(runtime: LoopRuntime, signature: str) -> int:
    signature = str(signature or "").strip()
    if not signature:
        signature = "<empty-round>"
    if signature == runtime.last_round_signature:
        runtime.round_repeat_count += 1
    else:
        runtime.last_round_signature = signature
        runtime.round_repeat_count = 1
    return runtime.round_repeat_count


def reset_round_signature(runtime: LoopRuntime) -> None:
    runtime.round_repeat_count = 0
    runtime.last_round_signature = ""


def pick_recent_write_path(blocks: list[dict]) -> str:
    for block in reversed(blocks or []):
        if block.get("command") != "file_op":
            continue
        if block.get("action") not in WRITE_ACTIONS:
            continue
        path = str(block.get("path") or block.get("dst") or "").strip()
        if path:
            return path
    return ""
