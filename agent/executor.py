"""
executor.py — 指令执行器
支持 powershell / cmd / python / file_op 四种指令类型。
兜底：PowerShell here-string 触发 ParserError 时，改用临时脚本文件执行。
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
    """Windows 下使用系统编码以正确显示中文错误信息"""
    if sys.platform == "win32":
        try:
            import locale
            return locale.getpreferredencoding() or "utf-8"
        except Exception:
            pass
    return "utf-8"


def build_command(block: dict) -> Optional[list[str]]:
    """将JSON块转换为subprocess可执行的命令列表"""
    cmd = block.get("command", "").lower().strip()
    args = block.get("arguments", [])

    if cmd not in SUPPORTED_COMMANDS:
        raise ValueError(f"不支持的命令类型: {cmd}，支持: {SUPPORTED_COMMANDS}")

    if cmd == "powershell":
        script = "\n".join(args) if isinstance(args, list) else str(args)
        return ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", script]

    elif cmd == "cmd":
        script = " & ".join(args) if isinstance(args, list) else args
        return ["cmd", "/c", script]

    elif cmd in ("python", "python3"):
        script = "\n".join(args) if isinstance(args, list) else args
        return [sys.executable, "-c", script]

    return None


def _run_powershell_fallback(script: str, timeout: int) -> subprocess.CompletedProcess:
    """兜底：将脚本写入临时文件执行，避免 -Command 对 here-string 的转义问题"""
    # 修正 here-string：内容中 \"@ 需单独成行，否则 ParserError
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
    """将 file_op 结果格式化为可读文本"""
    if not r.get("ok"):
        return ""
    if action == "read":
        lines = r.get("total_lines", "?")
        shown = r.get("shown_lines", "")
        content = r.get("content", "")
        return f"[文件共 {lines} 行，显示第 {shown} 行]\n{content}"
    if action == "tree":
        return r.get("tree", "")
    if action == "find":
        items = r.get("results", [])
        trunc = "（结果已截断）" if r.get("truncated") else ""
        lines = [f"{i['type']}  {i['path']}" for i in items]
        return f"找到 {r['count']} 项{trunc}:\n" + "\n".join(lines)
    if action == "list":
        entries = r.get("entries", [])
        lines = [f"{'[D]' if e['type']=='dir' else '[F]'} {e['name']}" for e in entries]
        return f"{r['path']} 共 {r['count']} 项:\n" + "\n".join(lines)
    return json.dumps({k: v for k, v in r.items() if k != "ok"}, ensure_ascii=False, indent=2)


def execute_block(block: dict, timeout: int = 60) -> dict:
    """执行单个JSON指令块，返回结构化结果"""
    cmd = block.get("command", "").lower().strip()
    result = {"command": cmd, "success": False, "stdout": "", "stderr": "", "returncode": -1}

    # ── file_op 分支 ──
    if cmd == "file_op":
        action = block.get("action", "")
        params = {k: v for k, v in block.items() if k not in ("command", "action")}
        r = file_op_run(action, params)
        result["success"] = r.get("ok", False)
        result["stdout"] = _fmt_file_op(action, r)
        result["stderr"] = r.get("error", "") if not r.get("ok") else ""
        result["returncode"] = 0 if r.get("ok") else 1
        return result

    # ── shell 分支 ──
    try:
        cmd_list = build_command(block)
        if not cmd_list:
            result["stderr"] = "无法构建命令"
            return result

        proc = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_get_output_encoding(),
            errors="replace",
        )
        # 兜底：PowerShell ParserError（如 here-string 中 \"@ 导致）时，改用脚本文件执行
        if proc.returncode != 0 and proc.stderr and "ParserError" in proc.stderr:
            if block.get("command", "").lower() == "powershell":
                script = "\n".join(block.get("arguments", [])) if isinstance(block.get("arguments"), list) else str(block.get("arguments", ""))
                proc = _run_powershell_fallback(script, timeout)

        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
        result["returncode"] = proc.returncode
        result["success"] = proc.returncode == 0

    except subprocess.TimeoutExpired:
        result["stderr"] = f"执行超时（>{timeout}s）"
    except FileNotFoundError as e:
        result["stderr"] = f"命令未找到: {e}"
    except Exception as e:
        result["stderr"] = f"执行异常: {e}"

    return result


def format_result_for_ai(results: list[dict]) -> str:
    """将执行结果格式化为发送给AI的文本"""
    lines = ["[执行结果反馈]"]
    for i, r in enumerate(results, 1):
        status = "✅ 成功" if r["success"] else "❌ 失败"
        lines.append(f"\n--- 指令 {i} ({r['command']}) {status} ---")
        if r["stdout"]:
            lines.append(f"输出:\n{r['stdout']}")
        if r["stderr"]:
            lines.append(f"错误:\n{r['stderr']}")
        lines.append(f"返回码: {r['returncode']}")
    return "\n".join(lines)


def run_from_text(ai_response: str, verbose: bool = True) -> tuple[list, str]:
    """从AI响应文本提取 → 执行 → 返回结果"""
    pr = parse(ai_response)
    if not pr.blocks:
        msg = "未找到可执行的JSON块"
        if pr.warnings:
            msg += f"（{'; '.join(pr.warnings)}）"
        return [], msg
    if verbose:
        print(f"   [解析器] 命中策略: {pr.strategy}，共 {len(pr.blocks)} 个块")
    results = [execute_block(b) for b in pr.blocks]
    return results, format_result_for_ai(results)


# ── 单独运行时：从stdin读取AI响应并执行 ──
if __name__ == "__main__":
    print("粘贴AI响应内容（输入 END 结束）:")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    text = "\n".join(lines)
    results, summary = run_from_text(text)
    print("\n" + summary)
