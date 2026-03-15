"""
file_ops.py — 文件操作模块
AI 通过 JSON 指令调用，支持遍历、读取、写入、修改、复制、移动等复杂操作。

支持的 action：
  list        列出目录内容
  find        按模式递归搜索文件
  read        读取文件内容
  write       写入/覆盖文件（自动创建目录）
  append      追加内容到文件末尾
  patch       在文件中替换指定字符串（支持多处替换）
  insert      在指定行号前/后插入内容
  delete_lines 删除指定行范围
  mkdir       创建目录
  copy        复制文件或目录
  move        移动/重命名文件或目录
  delete      删除文件或目录
  exists      检查路径是否存在
  stat        获取文件元信息（大小、修改时间等）
  tree        递归输出目录树结构
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
        return {"ok": False, "error": f"不是目录: {path}"}
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
        return {"ok": False, "error": f"文件不存在: {path}"}

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
        return {"ok": False, "error": "path 不能为空"}
    _write_text(path, content, encoding)
    return {"ok": True, "path": path,
            "bytes": len(content.encode(encoding, errors="replace"))}


def _op_append(params: dict) -> dict:
    """追加内容到文件末尾（不存在则创建）。"""
    path    = _expand(params.get("path", ""))
    content = params.get("content", "")
    newline = params.get("newline", True)    # 是否在追加前插入换行

    if not path:
        return {"ok": False, "error": "path 不能为空"}

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
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not reps:
        return {"ok": False, "error": "replacements 不能为空"}

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
                "error": "未匹配到任何内容，文件未修改。可能原因：old 字符串与文件实际内容不符（空格/缩进/换行不一致）。建议改用 file_op read 读取文件后用 file_op write 覆写整个文件。"}

    _write_text(path, content)
    return {"ok": True, "path": path, "replaced": total_count}


def _op_insert(params: dict) -> dict:
    """在指定行号前或后插入内容。"""
    path     = _expand(params.get("path", ""))
    line_num = params.get("line", 1)          # 1-based
    content  = params.get("content", "")
    after    = params.get("after", False)     # False=之前插入, True=之后插入

    if not os.path.isfile(path):
        return {"ok": False, "error": f"文件不存在: {path}"}

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
        return {"ok": False, "error": f"文件不存在: {path}"}

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
        return {"ok": False, "error": f"源不存在: {src}"}
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
        return {"ok": False, "error": f"源不存在: {src}"}
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)
    return {"ok": True, "src": src, "dst": dst}


def _op_delete(params: dict) -> dict:
    path  = _expand(params.get("path", ""))
    force = params.get("force", False)
    if not os.path.exists(path):
        return {"ok": False, "error": f"路径不存在: {path}"}
    if os.path.isdir(path):
        if force:
            shutil.rmtree(path)
        else:
            return {"ok": False,
                    "error": "删除目录需要 force: true（该操作不可恢复）"}
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
        return {"ok": False, "error": f"不存在: {path}"}
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
        return {"ok": False, "error": "name 不能为空"}

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
        return {"ok": False, "error": f"未找到程序: {name}（搜索范围: {search_roots}）",
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
            return {"ok": False, "error": f"找不到程序 '{name}': {found.get('error')}"}
        path = found["best"]

    if not path:
        return {"ok": False, "error": "需要 path 或 name 参数"}

    if not os.path.isfile(path):
        # 尝试 shutil.which
        import shutil
        resolved = shutil.which(path)
        if resolved:
            path = resolved
        else:
            return {"ok": False, "error": f"可执行文件不存在: {path}"}

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
                    "message": f"已启动: {os.path.basename(path)}"}
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {e}", "path": path}

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
    """
    执行一个文件操作。
    返回包含 ok: bool 的结果 dict。
    """
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False,
                "error": f"未知 action: {action}，支持: {list(_ACTIONS)}"}
    try:
        return fn(params)
    except Exception as e:
        return {"ok": False, "error": f"执行 {action} 时异常: {e}"}


def schema_hint() -> str:
    """返回给 AI 的 file_op 指令格式说明（注入 system prompt 用）。"""
    return """
## file_op 文件操作指令

当需要操作本地文件时，使用 command: "file_op"：

```json
{"command":"file_op","action":"<动作>","path":"<路径>","<其他参数>":"<值>"}
```

可用 action 及关键参数：

| action       | 说明           | 必填参数               | 可选参数                          |
|-------------|----------------|----------------------|---------------------------------|
| list        | 列出目录         | path                 |                                 |
| find        | 搜索文件         | path                 | pattern(*.txt) regex max_depth  |
| read        | 读取文件         | path                 | line_start line_end             |
| write       | 写入/覆盖        | path content         | encoding                        |
| append      | 追加内容         | path content         | newline                         |
| patch       | 替换文件内容      | path replacements    | use_regex                       |
| insert      | 插入行           | path line content    | after                           |
| delete_lines| 删除行范围        | path line_start      | line_end                        |
| mkdir       | 创建目录         | path                 |                                 |
| copy        | 复制             | src dst              |                                 |
| move        | 移动/重命名       | src dst              |                                 |
| delete      | 删除             | path                 | force(目录需true)                |
| exists      | 检查是否存在       | path                 |                                 |
| tree        | 目录树           | path                 | max_depth                       |
| find_program| 查找已安装程序     | name                 |                                 |
| launch      | 启动程序/文件      | path 或 name         | args wait                       |

find_program + launch 示例（查找并启动 Photoshop）：
```json
{"command":"file_op","action":"find_program","name":"Photoshop"}
```
收到路径后启动：
```json
{"command":"file_op","action":"launch","path":"C:\\Program Files\\Adobe\\...\\Photoshop.exe"}
```
或一步直接启动（自动查找）：
```json
{"command":"file_op","action":"launch","name":"Photoshop"}
```

patch 示例（多处替换）：
```json
{
  "command": "file_op",
  "action": "patch",
  "path": "%USERPROFILE%\\Desktop\\news.txt",
  "replacements": [
    {"old": "旧内容1", "new": "新内容1"},
    {"old": "旧内容2", "new": "新内容2"}
  ]
}
```
路径支持 %USERPROFILE% %DESKTOP% 等环境变量。
"""
