"""
env_context.py — Local environment collection
Collect real paths at startup and inject for AI so it does not guess paths.
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
        "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][datetime.now().weekday()],
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
    """Inject collected paths into os.environ for file_ops expandvars."""
    os.environ["DESKTOP"] = info.get("desktop", "")
    os.environ["DOCUMENTS"] = info.get("documents", "")


def to_prompt_block(info: dict) -> str:
    dirs = ", ".join(info["desktop_dirs"]) if info["desktop_dirs"] else "(none)"
    od = f"\n- OneDrive: {info['onedrive']}" if info["onedrive"] else ""
    return f"""
## Local environment (use these paths; do not guess)

- Username:   {info['username']}
- Desktop:    {info['desktop']}
- Documents:  {info['documents']}
- Downloads:  {info['downloads']}
- TEMP:       {info['temp']}{od}
- Date:       {info['date']} {info['weekday']}  {info['datetime']}
- OS:         {info['os']}
- Desktop dirs: {dirs}
"""
