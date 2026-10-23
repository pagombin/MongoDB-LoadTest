"""Build identity. Release builds overwrite COMMIT and BUILT; a git checkout reads them from git."""

from __future__ import annotations

import subprocess
from pathlib import Path

COMMIT = "unknown"
BUILT = "unknown"

if COMMIT == "unknown":
    _root = Path(__file__).resolve().parent.parent
    if (_root / ".git").exists():
        try:
            COMMIT = (
                subprocess.run(
                    ["git", "-C", str(_root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=2, check=False
                ).stdout.strip()
                or "unknown"
            )
            BUILT = "source checkout"
        except (OSError, subprocess.SubprocessError):
            pass
