"""Create a Windows desktop shortcut that opens the overlay.

The shortcut runs the project's own Python (`pythonw.exe`, so no console window
appears) with `-m mtghelper.cli overlay`, starting in the project folder. It
is made with PowerShell's WScript.Shell, which ships with every Windows.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import config


def _ps(text: str | Path) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def shortcut_script(name: str, target: Path, arguments: str, workdir: Path, icon: Path | None) -> str:
    lines = [
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$path = Join-Path $desktop ({_ps(name)} + '.lnk')",
        "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)",
        f"$link.TargetPath = {_ps(target)}",
        f"$link.Arguments = {_ps(arguments)}",
        f"$link.WorkingDirectory = {_ps(workdir)}",
        "$link.Description = 'MTG Arena draft overlay'",
    ]
    if icon is not None:
        lines.append(f"$link.IconLocation = {_ps(str(icon) + ',0')}")
    lines += ["$link.Save()", "Write-Output $path"]
    return "; ".join(lines)


def pythonw() -> Path:
    """The windowless interpreter next to the one running now (the project's venv)."""
    here = Path(sys.executable)
    candidate = here.with_name("pythonw.exe")
    return candidate if candidate.exists() else here


def create_desktop_shortcut(name: str = "MTG Draft Helper", run=subprocess.run) -> str:
    if sys.platform != "win32" and run is subprocess.run:
        raise RuntimeError("desktop shortcuts are only made on Windows")
    data_dir = config.DEFAULT_DB_PATH.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    icon_path = data_dir / "mtghelper.ico"
    icon = icon_path if _write_icon(icon_path) else None
    script = shortcut_script(name, pythonw(), "-m mtghelper.cli overlay", config.PROJECT_ROOT, icon)
    result = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                 capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell failed")
    return result.stdout.strip()


def _write_icon(path: Path) -> bool:
    try:
        from PySide6.QtWidgets import QApplication
        from . import overlay
        app = QApplication.instance() or QApplication([])  # noqa: F841  (needed to draw)
        return overlay.save_icon(path)
    except Exception:
        return False
