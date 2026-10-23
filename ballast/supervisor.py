"""Supervisor: keeps the server running without root.

``ballast supervise`` runs the server as a child process and restarts it after
a crash with exponential backoff, giving up after too many restarts in a short
window. With ``--detach`` it double-forks so it survives SSH logout. PID files
live in run/: supervisor.pid for the supervisor and ballast.pid for the server.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ballast import logs
from ballast.config import Loaded

log = logging.getLogger("ballast.supervisor")
NO_RESTART_CODES = {2, 3}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as handle:
            if handle.read().split()[2] == "Z":
                return False
    except (OSError, IndexError):
        pass
    return True


def pid_is_ours(pid: int, home: Path) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        try:
            cmdline = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return True
    return "ballast" in cmdline


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


class PidFiles:
    def __init__(self, loaded: Loaded):
        self.home = loaded.home
        self.dir = loaded.run_dir
        self.supervisor = self.dir / "supervisor.pid"
        self.server = self.dir / "ballast.pid"
        self.ready = self.dir / "ready.json"
        self.state = self.dir / "supervisor.json"

    def live_supervisor(self) -> int | None:
        pid = read_pid(self.supervisor)
        if pid and pid_alive(pid) and pid_is_ours(pid, self.home):
            return pid
        return None

    def live_server(self) -> int | None:
        pid = read_pid(self.server)
        if pid and pid_alive(pid) and pid_is_ours(pid, self.home):
            return pid
        return None

    def clean_stale(self) -> list[str]:
        removed = []
        for path, check in ((self.supervisor, self.live_supervisor), (self.server, self.live_server)):
            if path.exists() and check() is None:
                path.unlink(missing_ok=True)
                removed.append(path.name)
        if self.ready.exists() and self.live_server() is None:
            self.ready.unlink(missing_ok=True)
        return removed


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{pid}\n")
    os.replace(tmp, path)


def detach(console_log: Path) -> None:
    """Classic double fork so the supervisor has no controlling terminal."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    os.umask(0o027)
    devnull = os.open(os.devnull, os.O_RDWR)
    out = os.open(console_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    os.dup2(devnull, 0)
    os.dup2(out, 1)
    os.dup2(out, 2)


def _rotate(path: Path, max_bytes: int) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def supervise(loaded: Loaded, config_args: list[str], detach_first: bool) -> int:
    pids = PidFiles(loaded)
    pids.dir.mkdir(parents=True, exist_ok=True)
    loaded.log_dir.mkdir(parents=True, exist_ok=True)
    pids.clean_stale()
    existing = pids.live_supervisor()
    if existing:
        print(f"already running (supervisor pid {existing})")
        return 0
    console_log = loaded.log_dir / "console.log"
    if detach_first:
        detach(console_log)
    cfg = loaded.config
    logs.setup(
        loaded.log_dir, cfg.logging.level, cfg.logging.format, cfg.logging.max_size_mb, cfg.logging.max_files, filename="supervisor.log"
    )
    _write_pid(pids.supervisor, os.getpid())
    sup = cfg.supervisor
    stopping = False
    child: subprocess.Popen | None = None
    restarts: list[float] = []
    backoff = sup.restart_backoff_initial_seconds

    def on_term(signum, frame):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    def on_hup(signum, frame):
        if child and child.poll() is None:
            child.send_signal(signal.SIGHUP)
            log.info("forwarded SIGHUP to server pid %d", child.pid)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, on_hup)
    exit_code = 0
    log.info("supervisor started (pid %d)", os.getpid())
    try:
        while not stopping:
            _rotate(console_log, cfg.logging.max_size_mb * 1024 * 1024)
            cmd = [sys.executable, *_interpreter_flags(), "-m", "ballast", "serve", "--ready-file", str(pids.ready), *config_args]
            env = dict(os.environ, BALLAST_HOME=str(loaded.home), BALLAST_SUPERVISED="1")
            env.pop("BALLAST_FOREGROUND", None)
            with open(console_log, "ab") as out:
                child = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, cwd=str(loaded.home), start_new_session=False
                )
            _write_pid(pids.server, child.pid)
            started = time.monotonic()
            pids.state.write_text(json.dumps({"server_pid": child.pid, "started": time.time(), "restarts": len(restarts)}))
            log.info("server started (pid %d)", child.pid)
            code = _wait(child, lambda: stopping, sup.stop_timeout_seconds)
            ran = time.monotonic() - started
            pids.server.unlink(missing_ok=True)
            if stopping:
                log.info("server stopped (exit %s)", code)
                break
            if code in NO_RESTART_CODES:
                log.error("server exited with code %d (configuration or startup check failed); not restarting. See logs/console.log.", code)
                exit_code = code
                break
            if code == 0:
                log.info("server exited normally; supervisor stopping")
                break
            now = time.monotonic()
            if ran > sup.restart_window_seconds:
                backoff = sup.restart_backoff_initial_seconds
            restarts = [t for t in restarts if now - t < sup.restart_window_seconds] + [now]
            if len(restarts) > sup.max_restarts:
                log.error("server crashed %d times in %d s; giving up", len(restarts), sup.restart_window_seconds)
                exit_code = 1
                break
            log.warning(
                "server exited with code %s after %.1f s; restarting in %.1f s (restart %d of %d in window)",
                code,
                ran,
                backoff,
                len(restarts),
                sup.max_restarts,
            )
            deadline = time.monotonic() + backoff
            while time.monotonic() < deadline and not stopping:
                time.sleep(0.2)
            backoff = min(backoff * 2, sup.restart_backoff_max_seconds)
    finally:
        pids.supervisor.unlink(missing_ok=True)
        pids.server.unlink(missing_ok=True)
        pids.ready.unlink(missing_ok=True)
        log.info("supervisor exiting with code %d", exit_code)
    return exit_code


def _wait(child: subprocess.Popen, stop_requested, stop_timeout: int) -> int:
    signalled_at = None
    while True:
        code = child.poll()
        if code is not None:
            return code
        if stop_requested() and signalled_at is None:
            signalled_at = time.monotonic()
        if signalled_at is not None and time.monotonic() - signalled_at > stop_timeout:
            log.warning("server did not stop within %d s; killing it", stop_timeout)
            child.kill()
            return child.wait()
        time.sleep(0.2)


def _interpreter_flags() -> list[str]:
    flags = []
    if sys.flags.isolated:
        flags.append("-I")
    return flags


def stop(loaded: Loaded, timeout: int | None = None) -> tuple[int, str]:
    pids = PidFiles(loaded)
    timeout = timeout or loaded.config.supervisor.stop_timeout_seconds + 5
    sup_pid = pids.live_supervisor()
    srv_pid = pids.live_server()
    if not sup_pid and not srv_pid:
        removed = pids.clean_stale()
        return 0, "not running" + (f" (removed stale {', '.join(removed)})" if removed else "")
    target = sup_pid or srv_pid
    assert target is not None
    os.kill(target, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(target) and not (srv_pid and pid_alive(srv_pid)):
            pids.clean_stale()
            return 0, f"stopped (sent SIGTERM to pid {target}, exited cleanly)"
        time.sleep(0.2)
    killed = []
    for pid in (srv_pid, sup_pid):
        if pid and pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
            killed.append(str(pid))
    time.sleep(0.5)
    pids.clean_stale()
    return 0, f"forced stop after {timeout} s: sent SIGKILL to pid {', '.join(killed)}"


def status(loaded: Loaded) -> tuple[int, dict]:
    pids = PidFiles(loaded)
    sup = read_pid(pids.supervisor)
    srv = read_pid(pids.server)
    sup_live = pids.live_supervisor()
    srv_live = pids.live_server()
    info: dict = {"supervisor_pid": sup_live, "server_pid": srv_live}
    if pids.ready.exists():
        try:
            info.update(json.loads(pids.ready.read_text()))
        except ValueError:
            pass
    if pids.state.exists():
        try:
            info["restarts"] = json.loads(pids.state.read_text()).get("restarts", 0)
        except ValueError:
            pass
    if sup_live and srv_live:
        return 0, {**info, "state": "running"}
    if sup_live and not srv_live:
        return 4, {**info, "state": "restarting"}
    if srv_live:
        return 0, {**info, "state": "running (not supervised)"}
    if sup or srv:
        return 1, {**info, "state": "dead (stale pid file)"}
    return 3, {**info, "state": "stopped"}
