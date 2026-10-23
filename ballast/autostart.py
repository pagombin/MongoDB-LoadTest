"""Start after reboot without root: systemd user unit, else user crontab, else instructions."""

from __future__ import annotations

import getpass
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

MARK = "# ballast-autostart"


def _run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=20, check=False)


def unit_name(home: Path) -> str:
    return f"ballast-{hashlib.sha256(str(home).encode()).hexdigest()[:8]}.service"


def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return _run(["systemctl", "--user", "show-environment"]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def linger_state() -> str:
    if not shutil.which("loginctl"):
        return "unknown"
    try:
        out = _run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return {"Linger=yes": "yes", "Linger=no": "no"}.get(out, "unknown")


def crontab_available() -> bool:
    if not shutil.which("crontab"):
        return False
    try:
        result = _run(["crontab", "-l"])
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 or "no crontab" in (result.stderr + result.stdout).lower()


def _unit_text(home: Path) -> str:
    return f"""[Unit]
Description=Ballast load testing server ({home})
After=network-online.target

[Service]
Type=simple
Environment=BALLAST_HOME={home}
ExecStart={home}/bin/ballast supervise
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=90

[Install]
WantedBy=default.target
"""


def enable(home: Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    if systemd_available():
        unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        name = unit_name(home)
        (unit_dir / name).write_text(_unit_text(home))
        for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", name]):
            result = _run(cmd)
            if result.returncode != 0:
                lines.append(f"systemctl failed: {' '.join(cmd)}: {result.stderr.strip()}")
                break
        else:
            lines.append(f"Enabled systemd user unit {name} ({unit_dir / name}).")
            lines.append(
                "It runs 'bin/ballast supervise'. If Ballast is already running from ballastctl start, the unit takes over at the next boot."
            )
            linger = linger_state()
            if linger == "yes":
                lines.append("Lingering is on, so the service starts at boot and keeps running after you log out.")
            else:
                lines.append(
                    "Lingering is off (or unknown). Without it, user services start only when you log in and stop when you log out."
                )
                lines.append(f"Turn it on with: loginctl enable-linger {getpass.getuser()}")
                lines.append(
                    "On many hosts this needs an administrator. If it is refused, run 'ballastctl autostart disable' and use the crontab method instead."
                )
            return 0, lines
    if crontab_available():
        current = _run(["crontab", "-l"]).stdout if _run(["crontab", "-l"]).returncode == 0 else ""
        kept = [line for line in current.splitlines() if f"{MARK} {home}" not in line]
        kept.append(f"@reboot {home}/bin/ballastctl start >/dev/null 2>&1 {MARK} {home}")
        result = _run(["crontab", "-"], "\n".join(kept) + "\n")
        if result.returncode == 0:
            lines.append("Added an @reboot entry to your crontab:")
            lines.append(f"  @reboot {home}/bin/ballastctl start")
            lines.append("cron runs it at boot; it needs cron to be running on this host (check with: pgrep cron).")
            return 0, lines
        lines.append(f"crontab update failed: {result.stderr.strip()}")
    lines += [
        "Could not enable autostart: neither 'systemctl --user' nor 'crontab' works for this user.",
        "Options:",
        f"  1. Ask an administrator to add a system service that runs {home}/bin/ballastctl start as your user at boot.",
        f"  2. Add '{home}/bin/ballastctl start' to a login script such as ~/.profile (starts when you log in).",
        f"  3. Start it by hand after a reboot: {home}/bin/ballastctl start",
    ]
    return 1, lines


def disable(home: Path) -> tuple[int, list[str]]:
    lines: list[str] = []
    name = unit_name(home)
    unit_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / name
    if unit_path.exists():
        if systemd_available():
            _run(["systemctl", "--user", "disable", name])
        unit_path.unlink()
        if systemd_available():
            _run(["systemctl", "--user", "daemon-reload"])
        lines.append(f"Removed systemd user unit {name}.")
    if crontab_available():
        listing = _run(["crontab", "-l"])
        if listing.returncode == 0 and f"{MARK} {home}" in listing.stdout:
            kept = [line for line in listing.stdout.splitlines() if f"{MARK} {home}" not in line]
            _run(["crontab", "-"], "\n".join(kept) + ("\n" if kept else ""))
            lines.append("Removed the @reboot crontab entry.")
    if not lines:
        lines.append("Autostart was not enabled for this install.")
    return 0, lines


def describe(home: Path) -> str:
    unit_path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / unit_name(home)
    if unit_path.exists():
        return f"systemd user unit {unit_name(home)} (linger: {linger_state()})"
    if crontab_available():
        listing = _run(["crontab", "-l"])
        if listing.returncode == 0 and f"{MARK} {home}" in listing.stdout:
            return "crontab @reboot entry"
    return "not enabled"
