"""Command line: ballast <command>. bin/ballastctl passes its arguments here."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import ssl
import sys
import time
from pathlib import Path

from ballast import autostart, buildinfo, product
from ballast.config import ConfigError, Loaded, effective_with_sources, load, split_listen

EXIT_CONFIG = 2


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help="configuration file (default: config/ballast.yaml in the install directory)")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE", help="override one setting; repeatable")
    parser.add_argument("--listen", help="shortcut for --set server.listen=HOST:PORT")


def _loaded(args) -> Loaded:
    flags = list(args.set or [])
    if getattr(args, "listen", None):
        flags.append(f"server.listen={args.listen}")
    try:
        return load(args.config, flags)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(EXIT_CONFIG) from None


def _passthrough(args) -> list[str]:
    out = []
    if args.config:
        out += ["--config", str(Path(args.config).resolve())]
    for item in args.set or []:
        out += ["--set", item]
    if getattr(args, "listen", None):
        out += ["--listen", args.listen]
    return out


# ---- talking to the local server ----


class LocalClient:
    def __init__(self, loaded: Loaded):
        self.loaded = loaded
        host, port = split_listen(loaded.config.server.listen)
        self.host = "127.0.0.1" if host in ("0.0.0.0", "::", "localhost") else host  # noqa: S104
        self.port = port
        self.tls = loaded.config.tls.mode != "off"
        self.token = ""
        try:
            self.token = loaded.access_token()
        except ConfigError:
            pass

    def _pinned_fingerprint(self) -> str | None:
        cert = self.loaded.resolve(self.loaded.config.tls.cert_file)
        if not cert.exists():
            return None
        pem = cert.read_text()
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest()

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> tuple[int, dict | str]:
        headers = {"Accept": "application/json", "User-Agent": f"{product.SLUG}-cli"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["X-Ballast-Request"] = "1"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        conn: http.client.HTTPConnection
        if self.tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=ctx)
            conn.connect()
            expected = self._pinned_fingerprint()
            cert = conn.sock.getpeercert(binary_form=True)
            if cert is None:
                conn.close()
                raise ConnectionError(f"the server at {self.host}:{self.port} presented no certificate")
            actual = hashlib.sha256(cert).hexdigest()
            if expected and actual != expected:
                conn.close()
                raise ConnectionError(
                    f"the server at {self.host}:{self.port} presented a different certificate than {self.loaded.config.tls.cert_file}"
                )
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
        finally:
            conn.close()


def _print_error(payload) -> None:
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        print(f"error: {err.get('title')}", file=sys.stderr)
        if err.get("detail"):
            print(f"  {err['detail']}", file=sys.stderr)
        if err.get("fix"):
            print(f"  fix: {err['fix']}", file=sys.stderr)
    else:
        print(f"error: {payload}", file=sys.stderr)


# ---- commands ----


def cmd_serve(args) -> int:
    loaded = _loaded(args)
    from ballast import server

    if not os.environ.get("BALLAST_SUPERVISED"):
        os.environ["BALLAST_FOREGROUND"] = "1"
    return server.serve(loaded, Path(args.ready_file) if args.ready_file else None)


def cmd_supervise(args) -> int:
    loaded = _loaded(args)
    from ballast import supervisor

    return supervisor.supervise(loaded, _passthrough(args), detach_first=args.detach)


def cmd_start(args) -> int:
    loaded = _loaded(args)
    from ballast import server, supervisor

    pids = supervisor.PidFiles(loaded)
    removed = pids.clean_stale()
    if removed:
        print(f"removed stale pid file(s): {', '.join(removed)}")
    running = pids.live_supervisor()
    if running:
        print(f"already running (supervisor pid {running})")
        return 0
    problems = server.startup_checks(loaded)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 3
    pids.ready.unlink(missing_ok=True)
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            code = supervisor.supervise(loaded, _passthrough(args), detach_first=True)
        finally:
            os._exit(code)
    os.waitpid(pid, 0)
    deadline = time.monotonic() + args.wait
    console = loaded.log_dir / "console.log"
    while time.monotonic() < deadline:
        if pids.ready.exists():
            try:
                info = json.loads(pids.ready.read_text())
            except ValueError:
                time.sleep(0.2)
                continue
            print(f"started: {info.get('url')} (server pid {info.get('pid')}, supervisor pid {pids.live_supervisor()})")
            if info.get("fingerprint"):
                print(f"certificate SHA-256: {info['fingerprint']}")
            return 0
        if pids.live_supervisor() is None and time.monotonic() > deadline - args.wait + 2:
            break
        time.sleep(0.25)
    print("error: the server did not become ready", file=sys.stderr)
    if console.exists():
        tail = console.read_text(errors="replace").splitlines()[-15:]
        print("last lines of logs/console.log:", file=sys.stderr)
        for line in tail:
            print(f"  {line}", file=sys.stderr)
    return 1


def cmd_stop(args) -> int:
    loaded = _loaded(args)
    from ballast import supervisor

    code, message = supervisor.stop(loaded, args.timeout)
    print(message)
    return code


def cmd_restart(args) -> int:
    code = cmd_stop(args)
    if code != 0:
        return code
    return cmd_start(args)


def cmd_status(args) -> int:
    loaded = _loaded(args)
    from ballast import supervisor

    code, info = supervisor.status(loaded)
    if args.json:
        print(json.dumps(info))
    else:
        print(f"state: {info['state']}")
        for key in ("url", "server_pid", "supervisor_pid", "restarts"):
            if info.get(key) is not None:
                print(f"{key.replace('_', ' ')}: {info[key]}")
        print(f"autostart: {autostart.describe(loaded.home)}")
    return code


def cmd_health(args) -> int:
    loaded = _loaded(args)
    try:
        status, payload = LocalClient(loaded).request("GET", "/api/health", timeout=args.timeout)
    except (TimeoutError, OSError, ConnectionError) as exc:
        print(f"unhealthy: cannot reach the server: {exc}")
        return 1
    if status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
        print(f"healthy: version {payload.get('version')}, up {payload.get('uptime_seconds')} s")
        return 0
    print(f"unhealthy: HTTP {status}")
    return 1


def cmd_mark(args) -> int:
    loaded = _loaded(args)
    body = {"label": args.label, "note": args.note or "", "source": "cli"}
    if args.phase:
        body["phase"] = args.phase
    try:
        status, payload = LocalClient(loaded).request("POST", f"/api/runs/{args.run}/markers", body)
    except (OSError, ConnectionError) as exc:
        print(f"error: cannot reach the server: {exc}", file=sys.stderr)
        return 1
    if status == 201 and isinstance(payload, dict):
        when = time.strftime("%H:%M:%S", time.gmtime(payload["t_ms"] / 1000)) + f".{payload['t_ms'] % 1000:03d}Z"
        print(f"marker added: {payload['label']} at {when} (run {payload['run_id']})")
        return 0
    _print_error(payload)
    return 1


def cmd_logs(args) -> int:
    loaded = _loaded(args)
    path = loaded.log_dir / ("supervisor.log" if args.supervisor else "console.log" if args.console else "ballast.log")
    if not path.exists():
        print(f"no log file yet at {path}")
        return 0

    def show(line: str) -> None:
        try:
            item = json.loads(line)
            print(f"{item.get('ts')} {item.get('level', '').upper():7} {item.get('logger')}: {item.get('msg')}")
            if item.get("exc"):
                print(item["exc"])
        except ValueError:
            print(line)

    with path.open(encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()[-args.lines :]
        for line in lines:
            show(line.rstrip("\n"))
        if args.follow:
            try:
                while True:
                    line = handle.readline()
                    if line:
                        show(line.rstrip("\n"))
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                return 0
    return 0


def cmd_gen_cert(args) -> int:
    loaded = _loaded(args)
    from ballast import supervisor, tls

    cfg = loaded.config.tls
    if cfg.mode != "self-signed":
        print(f"error: tls.mode is {cfg.mode}; gen-cert only manages self-signed certificates", file=sys.stderr)
        return 2
    cert_path, key_path = loaded.resolve(cfg.cert_file), loaded.resolve(cfg.key_file)
    from ballast.server import certificate_hosts

    tls.generate_self_signed(cert_path, key_path, certificate_hosts(loaded))
    info = tls.describe(cert_path)
    print(f"wrote {cert_path} and {key_path}")
    print(f"hosts: {', '.join(info['hosts'])}")
    print(f"SHA-256 fingerprint: {info['sha256']}")
    print(f"expires: {info['not_after']}")
    pids = supervisor.PidFiles(loaded)
    target = pids.live_supervisor() or pids.live_server()
    if target:
        import signal as _signal

        os.kill(target, _signal.SIGHUP)
        print("sent SIGHUP; the running server reloads the certificate without dropping connections")
    return 0


def cmd_autostart(args) -> int:
    loaded = _loaded(args)
    code, lines = autostart.enable(loaded.home) if args.action == "enable" else autostart.disable(loaded.home)
    for line in lines:
        print(line)
    return code


def cmd_config(args) -> int:
    if args.action == "validate":
        if args.file:
            args.config = args.file
        loaded = _loaded(args)
        from ballast.config import PERMISSION_WARNINGS

        try:
            loaded.access_token()
            loaded.target_presets()
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CONFIG
        for warning in PERMISSION_WARNINGS:
            print(f"warning: {warning}")
        print(f"configuration is valid ({loaded.path or 'defaults only'})")
        return 0
    loaded = _loaded(args)
    if args.json:
        print(json.dumps(loaded.redacted(), indent=2))
    else:
        print(f"# install directory: {loaded.home}")
        print(f"# configuration file: {loaded.path or 'none (defaults)'}")
        print("# precedence: flags, then BALLAST_* environment variables, then the file, then defaults")
        for row in effective_with_sources(loaded):
            print(f"{row['key']} = {json.dumps(row['value'])}  # {row['source']}")
    return 0


def cmd_version(args) -> int:
    import pymongo

    print(
        f"{product.NAME} {product.VERSION} (commit {buildinfo.COMMIT}, built {buildinfo.BUILT}, PyMongo {pymongo.version}, Python {sys.version.split()[0]})"
    )
    return 0


def cmd_doctor(args) -> int:
    from ballast import doctor

    try:
        loaded = load(args.config, list(args.set or []))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        loaded = None
    checks = doctor.run(loaded, uri=args.uri, skip_process=args.quick)
    if args.json:
        from dataclasses import asdict

        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(doctor.render(checks))
    return 1 if any(c.status == doctor.FAIL for c in checks) else 0


def cmd_run(args) -> int:
    from ballast import headless

    return headless.main(_loaded(args), args)


def cmd_backup(args) -> int:
    import sqlite3

    loaded = _loaded(args)
    db = loaded.data_dir / "ballast.db"
    if not db.exists():
        print(f"error: no data store at {db}", file=sys.stderr)
        return 1
    dest = (
        Path(args.file)
        if args.file
        else loaded.data_dir / "backups" / f"ballast-manual-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        source = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
    finally:
        os.umask(old)
    os.chmod(dest, 0o600)
    print(f"backup written: {dest} ({dest.stat().st_size:,} bytes)")
    return 0


def cmd_restore(args) -> int:
    import shutil
    import sqlite3

    loaded = _loaded(args)
    from ballast import supervisor

    pids = supervisor.PidFiles(loaded)
    if pids.live_supervisor() is not None or pids.live_server() is not None:
        print("error: Ballast is running; stop it first (bin/ballastctl stop)", file=sys.stderr)
        return 1
    source = Path(args.file)
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        version = conn.execute("SELECT version FROM schema_meta").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        print(f"error: {source} is not a Ballast data store: {exc}", file=sys.stderr)
        return 1
    db = loaded.data_dir / "ballast.db"
    if db.exists():
        keep = loaded.data_dir / "backups" / f"ballast-pre-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.db"
        keep.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, keep)
        os.chmod(keep, 0o600)
        print(f"current store kept as {keep}")
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    shutil.copy2(source, db)
    os.chmod(db, 0o600)
    print(f"restored {runs} run(s) from {source} (schema version {version}); start Ballast to use it")
    return 0


def cmd_legacy(args) -> int:
    loaded = _loaded(args)
    if not loaded.config.features.legacy_dashboard:
        print(
            "error: the legacy dashboard is turned off. Set features.legacy_dashboard: true to use it (see docs/DECISIONS.md D-003).",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    from mongo_load_tester.cli import app as legacy_app

    sys.argv = ["mongo-load-tester", *args.rest]
    legacy_app()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ballast", description=product.DESCRIPTION)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("serve", help="run the server in the foreground")
    _common(p)
    p.add_argument("--ready-file", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("supervise", help="run the server under the supervisor (foreground unless --detach)")
    _common(p)
    p.add_argument("--detach", action="store_true")
    p.set_defaults(func=cmd_supervise)

    for name, func, help_text in (
        ("start", cmd_start, "start in the background under the supervisor"),
        ("stop", cmd_stop, "stop gracefully, then force after a timeout"),
        ("restart", cmd_restart, "stop, then start"),
    ):
        p = sub.add_parser(name, help=help_text)
        _common(p)
        p.add_argument("--wait", type=float, default=45, help="seconds to wait for the server to become ready")
        p.add_argument("--timeout", type=int, default=None, help="seconds to wait for a graceful stop")
        p.set_defaults(func=func)

    p = sub.add_parser("status", help="exit 0 running, 1 dead with stale pid file, 3 stopped, 4 restarting")
    _common(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("health", help="exit 0 if the server answers /api/health")
    _common(p)
    p.add_argument("--timeout", type=float, default=5)
    p.set_defaults(func=cmd_health)

    p = sub.add_parser("mark", help="add a marker to the active run")
    _common(p)
    p.add_argument("label")
    p.add_argument("--note", default="")
    p.add_argument("--phase", choices=("begin", "end"))
    p.add_argument("--run", default="active", help="run id (default: the active run)")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("logs", help="show the server log")
    _common(p)
    p.add_argument("-n", "--lines", type=int, default=100)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--supervisor", action="store_true", help="show the supervisor log")
    p.add_argument("--console", action="store_true", help="show the server console output")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("gen-cert", help="generate a new self-signed certificate and reload it")
    _common(p)
    p.set_defaults(func=cmd_gen_cert)

    p = sub.add_parser("autostart", help="start after reboot without root")
    _common(p)
    p.add_argument("action", choices=("enable", "disable"))
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("config", help="validate or print configuration")
    _common(p)
    p.add_argument("action", choices=("validate", "print"))
    p.add_argument("file", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="check this host: Python, packages, optional features, directories, port, and a cluster")
    _common(p)
    p.add_argument("--uri", help="also connect to this cluster")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quick", action="store_true", help="skip the process start check")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("run", help="run one workload from the command line, without the web server (for CI)")
    _common(p)
    p.add_argument("--uri", help="connection string of the cluster to test")
    p.add_argument("--target", help="saved or configured target name (or the name to save --uri under)")
    p.add_argument("--profile", help="profile file (JSON or YAML) or the name of a saved profile; defaults if omitted")
    p.add_argument("--name", help="run name")
    p.add_argument("--rate", type=float, help="open model: operations per second")
    p.add_argument("--concurrency", type=int, help="closed model: workers; open model: maximum in flight")
    p.add_argument("--processes", type=int, help="generator processes")
    p.add_argument("--duration", type=int, help="steady phase length in seconds")
    p.add_argument("--warmup", type=int, help="warm-up length in seconds")
    p.add_argument("--yes", action="store_true", help="confirm writing into a database that has other collections")
    p.add_argument("--json", help="also write the full JSON export to this file")
    p.add_argument("--markdown", help="also write the Markdown summary to this file")
    p.add_argument("--interval", type=float, default=5, help="seconds between progress lines")
    p.add_argument("--quiet", action="store_true", help="no progress lines")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("backup", help="copy the run database (safe while running)")
    _common(p)
    p.add_argument("file", nargs="?")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="replace the run database from a backup (Ballast must be stopped)")
    _common(p)
    p.add_argument("file")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("version", help="print version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("legacy", help="run the legacy Dash dashboard (needs features.legacy_dashboard: true)")
    _common(p)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_legacy)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code or 0)
