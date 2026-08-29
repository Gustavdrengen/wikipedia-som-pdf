import os
import subprocess
from pathlib import Path


def create_shortcut(target: Path, shortcut: Path) -> None:
    link_path = shortcut.with_suffix(".lnk") if os.name == "nt" else shortcut
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    if os.name == "nt":
        script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link_path).replace("'", "''") + "');$s.TargetPath='" + str(target.resolve()).replace("'", "''") + "';$s.WorkingDirectory='" + str(target.parent.resolve()).replace("'", "''") + "';$s.Save()"
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    else:
        link_path.symlink_to(os.path.relpath(target, link_path.parent))
