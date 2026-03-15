"""
executor.py — Instruction executor
Supports powershell / cmd / python / file_op. Fallback: on PowerShell ParserError use temp script file.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Optional

from json_parser import extract_json_blocks, parse
from file_ops import run as file_op_run

SUPPORTED_COMMANDS = {"powershell", "cmd", "python", "python3", "file_op"}


def _get_output_encoding() -> str:
    """Use system encoding on Windows for correct error display."""
    if sys.platform == "win32":
        try:
            import locale
            return locale.getpreferredencoding() or "utf-8"
        except Exception:
            pass
    return "utf-8"


def build_command(block: dict) -> Optional[list[str]]:
    """Convert JSON block to subprocess command list."""
    cmd = block.get("command", "").lower().strip()
    args = block.get("arguments", [])

    if cmd not in SUPPORTED_COMMANDS:
        raise ValueError(f"Unsupported command: {cmd}; supported: {SUPPORTED_COMMANDS}")

    if cmd == "powershell":
        script = "\n".join(args) if isinstance(args, list) else str(args)
        return ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script]

    elif cmd == "cmd":
        script = " & ".join(args) if isinstance(args, list) else args
        return ["cmd", "/c", script]

    elif cmd in ("python", "python3"):
        path = block.get("path", "")
        if path:
            return [sys.executable, path]
        script = "\n".join(args) if isinstance(args, list) else args
        return [sys.executable, "-c", script]

    return None


def _run_powershell_fallback(script: str, timeout: int) -> subprocess.CompletedProcess:
    """Fallback: run script from temp file to avoid -Command here-string escaping."""
    script = re.sub(r'([^\n])"@', r'\1\n"@', script)
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="agent_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\ufeff")  # UTF-8 BOM for PowerShell
            f.write(script)
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_get_output_encoding(),
            errors="replace",
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _fmt_file_op(action: str, r: dict) -> str:
    """Format file_op result as readable text."""
    if not r.get("ok"):
        return ""
    if action == "read":
        lines = r.get("total_lines", "?")
        shown = r.get("shown_lines", "")
        content = r.get("content", "")
        return f"[File {lines} lines, showing {shown}]\n{content}"
    if action == "tree":
        return r.get("tree", "")
    if action == "find":
        items = r.get("results", [])
        trunc = " (truncated)" if r.get("truncated") else ""
        lines = [f"{i['type']}  {i['path']}" for i in items]
        return f"Found {r['count']} item(s){trunc}:\n" + "\n".join(lines)
    if action == "find_program":
        items = r.get("results", [])
        if not items:
            return r.get("message", "Not found")
        lines = [f"{i.get('name','')}  {i['path']}" for i in items]
        return f"Found {r['count']} program(s):\n" + "\n".join(lines)
    if action == "launch":
        return r.get("message", "Started") + f"\nPath: {r.get('path','')}"
    if action == "list":
        entries = r.get("entries", [])
        lines = [f"{'[D]' if e['type']=='dir' else '[F]'} {e['name']}" for e in entries]
        return f"{r['path']} {r['count']} item(s):\n" + "\n".join(lines)
    return json.dumps({k: v for k, v in r.items() if k != "ok"}, ensure_ascii=False, indent=2)


def execute_block(block: dict, timeout: int = 60) -> dict:
    """Execute a single JSON instruction block; return structured result."""
    cmd = block.get("command", "").lower().strip()
    result = {"command": cmd, "success": False, "stdout": "", "stderr": "", "returncode": -1}

    if cmd == "file_op":
        action = block.get("action", "")
        params = {k: v for k, v in block.items() if k not in ("command", "action")}
        summary_parts = [f"file_op:{action}"]
        for key in ("path", "src", "dst", "name", "pattern"):
            if key in params:
                summary_parts.append(f"{key}={params[key]}")
        print(f"   {' | '.join(summary_parts)}")
        r = file_op_run(action, params)
        if r.get("ok"):
            info_parts = []
            for key in ("count", "total_lines", "bytes", "replaced", "inserted_lines",
                        "deleted_lines", "status", "message"):
                if key in r:
                    info_parts.append(f"{key}={r[key]}")
            print(f"   OK" + (f" ({', '.join(info_parts)})" if info_parts else ""))
        else:
            print(f"   Failed: {r.get('error', 'Unknown error')}")
        result["success"] = r.get("ok", False)
        result["stdout"] = _fmt_file_op(action, r)
        result["stderr"] = r.get("error", "") if not r.get("ok") else ""
        result["returncode"] = 0 if r.get("ok") else 1
        return result

    try:
        cmd_list = build_command(block)
        if not cmd_list:
            result["stderr"] = "Could not build command"
            return result

        proc = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_get_output_encoding(),
            errors="replace",
        )
        if proc.returncode != 0 and proc.stderr and "ParserError" in proc.stderr:
            if block.get("command", "").lower() == "powershell":
                script = "\n".join(block.get("arguments", [])) if isinstance(block.get("arguments"), list) else str(block.get("arguments", ""))
                proc = _run_powershell_fallback(script, timeout)

        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
        result["returncode"] = proc.returncode
        result["success"] = proc.returncode == 0

    except subprocess.TimeoutExpired:
        result["stderr"] = f"Timeout (>{timeout}s)"
    except FileNotFoundError as e:
        result["stderr"] = f"Command not found: {e}"
    except Exception as e:
        result["stderr"] = f"Execution error: {e}"

    return result


def format_result_for_ai(results: list[dict]) -> str:
    """Format execution result for AI."""
    lines = ["[Execution result feedback]"]
    for i, r in enumerate(results, 1):
        status = "OK" if r["success"] else "Failed"
        lines.append(f"\n--- Instruction {i} ({r['command']}) {status} ---")
        if r["stdout"]:
            lines.append(f"Output:\n{r['stdout']}")
        if r["stderr"]:
            lines.append(f"Error:\n{r['stderr']}")
        lines.append(f"Return code: {r['returncode']}")
    return "\n".join(lines)


def auto_verify_py(path: str, timeout: int = 15) -> str:
    """
    Re-run a .py file after a fix attempt.
    Returns a feedback string to append to the AI message.
    """
    if not path or not path.endswith(".py") or not os.path.isfile(path):
        return ""
    try:
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, timeout=timeout,
            encoding=_get_output_encoding(), errors="replace",
            cwd=os.path.dirname(path),
        )
        if proc.returncode == 0:
            return f"\n[Auto-verify] Re-run {path} ✅ No errors"
        else:
            err = proc.stderr.strip()
            lines = err.splitlines()[:30]
            return (
                f"\n[Auto-verify] Re-run {path} ❌ Still failing\n"
                + "\n".join(lines)
            )
    except subprocess.TimeoutExpired:
        return f"\n[Auto-verify] Re-run {path} timed out"
    except Exception as e:
        return f"\n[Auto-verify] Re-run {path} error: {e}"


def _extract_py_targets(blocks: list[dict]) -> list[str]:
    """Extract .py file paths that were written or patched (candidates for auto-verify)."""
    targets = []
    seen = set()
    for b in blocks:
        cmd = b.get("command", "")
        action = b.get("action", "")
        path = ""
        if cmd == "file_op" and action in ("write", "patch", "delete_lines", "insert_lines"):
            path = b.get("path", "")
        elif cmd in ("python", "python3"):
            path = b.get("path", "")
        if path and path.endswith(".py") and path not in seen:
            seen.add(path)
            targets.append(path)
    return targets


def run_from_text(ai_response: str, verbose: bool = True) -> tuple[list, str]:
    """Extract from AI response -> execute -> return result."""
    pr = parse(ai_response)
    if not pr.blocks:
        msg = "No executable JSON block found"
        if pr.warnings:
            msg += f" ({'; '.join(pr.warnings)})"
        return [], msg
    if verbose:
        print(f"   [parser] strategy={pr.strategy}, {len(pr.blocks)} block(s)")
    results = [execute_block(b) for b in pr.blocks]
    return results, format_result_for_ai(results)


if __name__ == "__main__":
    print("Paste AI response (type END to finish):")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    text = "\n".join(lines)
    results, summary = run_from_text(text)
    print("\n" + summary)
