"""
file_ops.py — File operations module
AI invokes via JSON: list, find, read, write, append, patch, insert, delete_lines,
mkdir, copy, move, delete, exists, stat, tree, find_program, launch.
"""

import os
import re
import json
import shutil
import fnmatch
from datetime import datetime
from pathlib import Path


# ── 内部工具 ──────────────────────────────────────────────

def _expand(path: str) -> str:
    """展开环境变量和 ~ 并返回绝对路径。"""
    return str(Path(os.path.expandvars(os.path.expanduser(path))).resolve())


def _read_text(path: str) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return Path(path).read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def _write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding=encoding)


# ── action 处理函数 ────────────────────────────────────────

def _op_list(params: dict) -> dict:
    """列出目录内容（非递归）。"""
    path = _expand(params.get("path", "."))
    if not os.path.isdir(path):
        return {"ok": False, "error": f"Not a directory: {path}"}
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        is_dir = os.path.isdir(full)
        entries.append({
            "name": name,
            "type": "dir" if is_dir else "file",
            "size": 0 if is_dir else os.path.getsize(full),
        })
    return {"ok": True, "path": path, "count": len(entries), "entries": entries}


def _op_find(params: dict) -> dict:
    """递归搜索文件，支持通配符和正则。"""
    root     = _expand(params.get("path", "."))
    pattern  = params.get("pattern", "*")          # 通配符，如 *.txt
    regex    = params.get("regex", None)            # 正则，如 .*新闻.*\.txt
    max_depth = params.get("max_depth", 10)
    max_results = params.get("max_results", 200)
    match_type = params.get("type", "any")          # file / dir / any

    results = []
    re_obj = re.compile(regex) if regex else None

    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.replace(root, "").count(os.sep)
        if depth >= max_depth:
            dirnames.clear()
            continue

        candidates = []
        if match_type in ("file", "any"):
            candidates += [(f, os.path.join(dirpath, f)) for f in filenames]
        if match_type in ("dir", "any"):
            candidates += [(d, os.path.join(dirpath, d)) for d in dirnames]

        for name, full in candidates:
            hit = fnmatch.fnmatch(name, pattern) if not re_obj else bool(re_obj.search(name))
            if hit:
                results.append({"name": name, "path": full,
                                 "type": "dir" if os.path.isdir(full) else "file"})
            if len(results) >= max_results:
                return {"ok": True, "count": len(results),
                        "truncated": True, "results": results}

    return {"ok": True, "count": len(results), "truncated": False, "results": results}


def _op_read(params: dict) -> dict:
    """读取文件内容，支持行范围截取。"""
    path = _expand(params.get("path", ""))
    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}

    content = _read_text(path)
    lines   = content.splitlines()

    start = params.get("line_start", 1) - 1       # 1-based → 0-based
    end   = params.get("line_end", len(lines))
    snippet = "\n".join(lines[start:end])

    return {
        "ok": True,
        "path": path,
        "total_lines": len(lines),
        "shown_lines": f"{start+1}-{min(end, len(lines))}",
        "content": snippet,
    }


def _op_write(params: dict) -> dict:
    """写入/覆盖文件，自动创建父目录。"""
    path    = _expand(params.get("path", ""))
    content = params.get("content", "")
    encoding = params.get("encoding", "utf-8")
    if not path:
        return {"ok": False, "error": "path cannot be empty"}
    _write_text(path, content, encoding)
    return {"ok": True, "path": path,
            "bytes": len(content.encode(encoding, errors="replace"))}


def _op_append(params: dict) -> dict:
    """追加内容到文件末尾（不存在则创建）。"""
    path    = _expand(params.get("path", ""))
    content = params.get("content", "")
    newline = params.get("newline", True)    # 是否在追加前插入换行

    if not path:
        return {"ok": False, "error": "path cannot be empty"}

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    if newline and existing and not existing.endswith("\n"):
        content = "\n" + content

    with open(path, "a", encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "path": path, "appended_bytes": len(content.encode())}


def _op_patch(params: dict) -> dict:
    """
    在文件中替换字符串，支持多处替换。
    replacements: [ {"old": "...", "new": "..."}, ... ]
    use_regex: true 时 old 视为正则表达式
    """
    path  = _expand(params.get("path", ""))
    reps  = params.get("replacements", [])
    use_re = params.get("use_regex", False)

    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}
    if not reps:
        return {"ok": False, "error": "replacements cannot be empty"}

    content = _read_text(path)
    original = content
    total_count = 0

    for item in reps:
        old = item.get("old", "")
        new = item.get("new", "")
        count_param = item.get("count", 0)   # 0 = 全部替换

        if use_re:
            flags = re.MULTILINE | (re.IGNORECASE if item.get("ignore_case") else 0)
            new_content, n = re.subn(old, new, content,
                                     count=count_param, flags=flags)
        else:
            n = content.count(old) if not count_param else min(content.count(old), count_param)
            new_content = content.replace(old, new, count_param or -1)

        content = new_content
        total_count += n

    if content == original:
        return {"ok": False, "path": path, "replaced": 0,
                "error": "No match; file unchanged. Try file_op read then file_op write to overwrite."}

    _write_text(path, content)
    return {"ok": True, "path": path, "replaced": total_count}


def _op_insert(params: dict) -> dict:
    """在指定行号前或后插入内容。"""
    path     = _expand(params.get("path", ""))
    line_num = params.get("line", 1)          # 1-based
    content  = params.get("content", "")
    after    = params.get("after", False)     # False=之前插入, True=之后插入

    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}

    lines = _read_text(path).splitlines(keepends=True)
    idx   = max(0, min(line_num - 1, len(lines)))
    if after:
        idx += 1

    insert_lines = [l + "\n" for l in content.splitlines()]
    lines[idx:idx] = insert_lines
    _write_text(path, "".join(lines))
    return {"ok": True, "path": path, "inserted_at_line": idx + 1,
            "inserted_lines": len(insert_lines)}


def _op_delete_lines(params: dict) -> dict:
    """删除指定行范围（包含两端）。"""
    path  = _expand(params.get("path", ""))
    start = params.get("line_start", 1) - 1   # 1-based → 0-based
    end   = params.get("line_end", start + 1)

    if not os.path.isfile(path):
        return {"ok": False, "error": f"File not found: {path}"}

    lines = _read_text(path).splitlines(keepends=True)
    deleted = lines[start:end]
    lines[start:end] = []
    _write_text(path, "".join(lines))
    return {"ok": True, "path": path,
            "deleted_lines": len(deleted), "range": f"{start+1}-{end}"}


def _op_mkdir(params: dict) -> dict:
    path = _expand(params.get("path", ""))
    Path(path).mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": path}


def _op_copy(params: dict) -> dict:
    src  = _expand(params.get("src", ""))
    dst  = _expand(params.get("dst", ""))
    if not os.path.exists(src):
        return {"ok": False, "error": f"Source not found: {src}"}
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return {"ok": True, "src": src, "dst": dst}


def _op_move(params: dict) -> dict:
    src = _expand(params.get("src", ""))
    dst = _expand(params.get("dst", ""))
    if not os.path.exists(src):
        return {"ok": False, "error": f"Source not found: {src}"}
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)
    return {"ok": True, "src": src, "dst": dst}


def _op_delete(params: dict) -> dict:
    path  = _expand(params.get("path", ""))
    force = params.get("force", False)
    if not os.path.exists(path):
        return {"ok": False, "error": f"Path not found: {path}"}
    if os.path.isdir(path):
        if force:
            shutil.rmtree(path)
        else:
            return {"ok": False,
                    "error": "Delete directory requires force: true (irreversible)"}
    else:
        os.remove(path)
    return {"ok": True, "deleted": path}


def _op_exists(params: dict) -> dict:
    path = _expand(params.get("path", ""))
    exists = os.path.exists(path)
    return {"ok": True, "path": path, "exists": exists,
            "type": ("dir" if os.path.isdir(path) else "file") if exists else None}


def _op_stat(params: dict) -> dict:
    path = _expand(params.get("path", ""))
    if not os.path.exists(path):
        return {"ok": False, "error": f"Not found: {path}"}
    s = os.stat(path)
    return {
        "ok": True, "path": path,
        "size_bytes": s.st_size,
        "modified": datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "created":  datetime.fromtimestamp(s.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
        "is_dir":   os.path.isdir(path),
    }


def _op_tree(params: dict) -> dict:
    """输出目录树，返回文本形式。"""
    root      = _expand(params.get("path", "."))
    max_depth = params.get("max_depth", 3)
    lines     = []

    def _walk(path, prefix, depth):
        if depth > max_depth:
            return
        try:
            items = sorted(os.listdir(path))
        except PermissionError:
            return
        for i, name in enumerate(items):
            full  = os.path.join(path, name)
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "
            lines.append(prefix + connector + name)
            if os.path.isdir(full):
                extension = "    " if is_last else "│   "
                _walk(full, prefix + extension, depth + 1)

    lines.append(root)
    _walk(root, "", 1)
    return {"ok": True, "tree": "\n".join(lines)}




def _op_find_program(params: dict) -> dict:
    """
    在 Windows 常见位置搜索已安装程序的可执行文件路径。
    params: name (程序名，如 "Photoshop" / "Unity" / "notepad")
    返回所有匹配到的路径列表。
    """
    import glob
    name = params.get("name", "").strip()
    if not name:
        return {"ok": False, "error": "name cannot be empty"}

    # 搜索范围：常见安装目录 + PATH
    search_roots = [
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        r"D:\Program Files",
        r"D:\Program Files (x86)",
    ]
    # 常见程序名 → 可执行文件名映射
    name_map = {
        "photoshop":  ["Photoshop.exe"],
        "unity":      ["Unity.exe", "UnityHub.exe"],
        "unityhub":   ["UnityHub.exe"],
        "vscode":     ["Code.exe"],
        "vs code":    ["Code.exe"],
        "chrome":     ["chrome.exe"],
        "firefox":    ["firefox.exe"],
        "notepad":    ["notepad.exe"],
        "notepad++":  ["notepad++.exe"],
        "blender":    ["blender.exe"],
        "steam":      ["steam.exe"],
        "obs":        ["obs64.exe", "obs32.exe", "obs.exe"],
    }

    key = name.lower()
    exe_names = name_map.get(key, [f"{name}.exe", f"{name}64.exe"])

    results = []
    seen = set()

    # 1. 在搜索根目录递归查找
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for exe in exe_names:
            pattern = os.path.join(root, "**", exe)
            for match in glob.glob(pattern, recursive=True):
                norm = os.path.normpath(match)
                if norm not in seen:
                    seen.add(norm)
                    results.append({"path": norm, "source": "search"})

    # 2. 从 PATH 环境变量中查找
    import shutil
    for exe in exe_names:
        found = shutil.which(exe)
        if found:
            norm = os.path.normpath(found)
            if norm not in seen:
                seen.add(norm)
                results.append({"path": norm, "source": "PATH"})

    # 3. 查注册表（仅 Windows，非 Windows 静默跳过）
    try:
        import winreg
        if not hasattr(winreg, 'OpenKey'):
            raise ImportError("winreg not available")
        reg_paths = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        ]
        for reg_path in reg_paths:
            for exe in exe_names:
                try:
                    key_path = os.path.join(reg_path, exe)
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as k:
                        val, _ = winreg.QueryValueEx(k, "")
                        norm = os.path.normpath(val.strip('"'))
                        if norm not in seen and os.path.isfile(norm):
                            seen.add(norm)
                            results.append({"path": norm, "source": "registry"})
                except (FileNotFoundError, OSError):
                    continue
    except ImportError:
        pass  # 非 Windows 环境

    if not results:
        return {"ok": False, "error": f"Program not found: {name}",
                "searched_names": exe_names}

    return {"ok": True, "name": name, "count": len(results), "results": results,
            "best": results[0]["path"]}


def _op_launch(params: dict) -> dict:
    """
    启动程序或打开文件。
    params:
      path  — 可执行文件完整路径（优先）
      name  — 程序名（自动查找后启动，需先有 find_program 结果或直接 name）
      args  — 启动参数列表（可选）
      wait  — 是否等待程序退出（默认 False）
    """
    import subprocess as _sp

    path = params.get("path", "").strip()
    name = params.get("name", "").strip()
    args = params.get("args", [])
    wait = params.get("wait", False)

    # 没有 path 时自动 find_program
    if not path and name:
        found = _op_find_program({"name": name})
        if not found.get("ok"):
            return {"ok": False, "error": f"Program not found '{name}': {found.get('error')}"}
        path = found["best"]

    if not path:
        return {"ok": False, "error": "path or name required"}

    if not os.path.isfile(path):
        # 尝试 shutil.which
        import shutil
        resolved = shutil.which(path)
        if resolved:
            path = resolved
        else:
            return {"ok": False, "error": f"Executable not found: {path}"}

    cmd = [path] + (args if isinstance(args, list) else [str(args)])

    try:
        if wait:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=60)
            return {"ok": proc.returncode == 0, "path": path,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}
        else:
            _sp.Popen(cmd, close_fds=True)
            return {"ok": True, "path": path, "status": "launched",
                    "message": f"Started: {os.path.basename(path)}"}
    except Exception as e:
        return {"ok": False, "error": f"Launch failed: {e}", "path": path}

# ── 调度表 ────────────────────────────────────────────────

_ACTIONS = {
    "list":         _op_list,
    "find":         _op_find,
    "read":         _op_read,
    "write":        _op_write,
    "append":       _op_append,
    "patch":        _op_patch,
    "insert":       _op_insert,
    "delete_lines": _op_delete_lines,
    "mkdir":        _op_mkdir,
    "copy":         _op_copy,
    "move":         _op_move,
    "delete":       _op_delete,
    "exists":       _op_exists,
    "stat":         _op_stat,
    "tree":         _op_tree,
    "find_program": _op_find_program,
    "launch":       _op_launch,
}


# ── 公开入口 ──────────────────────────────────────────────

def run(action: str, params: dict) -> dict:
    """Execute one file operation; return dict with ok: bool."""
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False,
                "error": f"Unknown action: {action}; supported: {list(_ACTIONS)}"}
    try:
        return fn(params)
    except Exception as e:
        return {"ok": False, "error": f"Error executing {action}: {e}"}


def schema_hint() -> str:
    """Return file_op format hint for AI (injected into system prompt)."""
    return """
## file_op file operations

Use command: "file_op" for local files:

```json
{"command":"file_op","action":"<action>","path":"<path>","<key>":"<value>"}
```

| action       | Required   | Optional |
| list        | path       | |
| find        | path       | pattern regex max_depth |
| read        | path       | line_start line_end |
| write       | path content | encoding |
| append      | path content | newline |
| patch       | path replacements | use_regex |
| insert      | path line content | after |
| delete_lines| path line_start | line_end |
| mkdir       | path       | |
| copy        | src dst    | |
| move        | src dst    | |
| delete      | path       | force |
| exists      | path       | |
| tree        | path       | max_depth |
| find_program| name       | |
| launch      | path or name | args wait |

Paths support %USERPROFILE% %DESKTOP% etc.
"""
