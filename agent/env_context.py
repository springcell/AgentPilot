"""
env_context.py — 本地环境采集模块
启动时采集真实路径，注入给 AI，避免猜路径。
"""
import os
import sys
import subprocess
import platform
from datetime import datetime
from pathlib import Path


def _get_encoding() -> str:
    if sys.platform == "win32":
        try:
            import locale
            return locale.getpreferredencoding() or "utf-8"
        except Exception:
            pass
    return "utf-8"


def _ps(script: str, timeout: int = 8) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=_get_encoding(),
            errors="replace",
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _known_folder(name: str) -> str:
    r = _ps(f"[Environment]::GetFolderPath('{name}')")
    return r if r and Path(r).exists() else ""


def collect() -> dict:
    desktop = (
        _known_folder("Desktop")
        or _ps(r"(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders').Desktop")
        or str(Path.home() / "Desktop")
    )
    documents = _known_folder("MyDocuments") or str(Path.home() / "Documents")
    info = {
        "username": os.environ.get("USERNAME", os.environ.get("USER", "user")),
        "userprofile": os.environ.get("USERPROFILE", str(Path.home())),
        "desktop": desktop,
        "documents": documents,
        "downloads": str(Path.home() / "Downloads"),
        "temp": os.environ.get("TEMP", os.environ.get("TMP", "C:\\Temp")),
        "onedrive": os.environ.get("OneDrive", ""),
        "system_drive": os.environ.get("SystemDrive", "C:"),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "datetime": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][datetime.now().weekday()],
        "desktop_dirs": [],
    }
    try:
        info["desktop_dirs"] = [
            d for d in os.listdir(desktop)
            if os.path.isdir(os.path.join(desktop, d))
        ]
    except Exception:
        pass
    return info


def inject_env_vars(info: dict) -> None:
    """将采集的路径注入 os.environ，供 file_ops 的 expandvars 使用"""
    os.environ["DESKTOP"] = info.get("desktop", "")
    os.environ["DOCUMENTS"] = info.get("documents", "")


def to_prompt_block(info: dict) -> str:
    dirs = "、".join(info["desktop_dirs"]) if info["desktop_dirs"] else "（空）"
    od = f"\n- OneDrive路径: {info['onedrive']}" if info["onedrive"] else ""
    return f"""
## 本机环境（已采集，请直接使用这些路径，勿自行猜测）

- 用户名:     {info['username']}
- 桌面路径:   {info['desktop']}
- 文档路径:   {info['documents']}
- 下载路径:   {info['downloads']}
- TEMP路径:   {info['temp']}{od}
- 当前日期:   {info['date']} {info['weekday']}  {info['datetime']}
- 系统:       {info['os']}
- 桌面已有目录: {dirs}
"""
