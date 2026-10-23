"""Host checks: what is installed, what is missing, what each missing piece costs, and how to fix it."""

from __future__ import annotations

import importlib
import importlib.util
import os
import socket
import sys
import tempfile
from dataclasses import asdict, dataclass

MIN_PYTHON = (3, 10)
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""
    feature: str = ""


def pip_command(*packages: str) -> str:
    """The install command that puts packages where this Ballast will find them."""
    import sysconfig

    site = os.environ.get("BALLAST_SITE")
    if site:
        where = f" --target {site}"
    elif os.access(sysconfig.get_paths()["purelib"], os.W_OK):
        where = ""
    else:
        where = " --user"
    return f"{sys.executable} -m pip install{where} " + " ".join(packages)


def _version(module: str) -> str | None:
    try:
        from importlib.metadata import version

        return version({"yaml": "PyYAML", "faker": "Faker", "dns": "dnspython", "snappy": "python-snappy"}.get(module, module))
    except Exception:
        mod = sys.modules.get(module)
        return getattr(mod, "__version__", None) if mod else None


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except Exception:
        return False


REQUIRED = [
    ("aiohttp", "aiohttp", "web server"),
    ("pydantic", "pydantic", "configuration and profile validation"),
    ("pymongo", "pymongo", "MongoDB driver"),
]
OPTIONAL = [
    (
        "yaml",
        "pyyaml",
        "config file reader",
        "PyYAML reads every YAML feature; without it the built-in reader handles plain mappings, lists and scalars",
    ),
    ("faker", "faker", "'schema' document shape", "without it only the 'simple' document shape is available"),
    ("dns", "dnspython", "mongodb+srv:// connection strings", "without it use a mongodb:// connection string that lists the hosts"),
    ("zstandard", "zstandard", "zstd wire compression", "only needed if a profile sets compressors to zstd; zlib works without it"),
    ("snappy", "python-snappy", "snappy wire compression", "only needed if a profile sets compressors to snappy; zlib works without it"),
]
SPEEDUPS = [
    ("bson._cbson", "PyMongo C extension", "BSON encoding is slower in pure Python; expect fewer ops/s per generator process"),
    ("aiohttp._http_parser", "aiohttp C parser", "the web server uses its pure-Python HTTP parser; fine for the UI"),
    ("multidict._multidict", "multidict C extension", "pure-Python mode, slightly slower web server"),
]


def package_checks() -> list[Check]:
    checks: list[Check] = []
    py = sys.version_info
    if py[:2] >= MIN_PYTHON:
        checks.append(Check("python", OK, f"Python {py.major}.{py.minor}.{py.micro} at {sys.executable}"))
    else:
        checks.append(
            Check(
                "python",
                FAIL,
                f"Python {py.major}.{py.minor} is too old; Ballast needs 3.10 or later",
                "use a release bundle, which brings its own Python",
            )
        )
    for module, title, why in (("sqlite3", "sqlite3", "local run storage"), ("ssl", "ssl", "HTTPS and TLS connections to MongoDB")):
        if _importable(module):
            mod = importlib.import_module(module)
            version = getattr(mod, "sqlite_version", None) or getattr(mod, "OPENSSL_VERSION", "")
            checks.append(Check(title, OK, f"{why}: {version}"))
        else:
            checks.append(
                Check(
                    title,
                    FAIL,
                    f"this Python was built without the {module} module ({why})",
                    "install a Python with it, or use a release bundle",
                )
            )
    for module, package, why in REQUIRED:
        if _importable(module):
            checks.append(Check(package, OK, f"{_version(module) or 'installed'} ({why})"))
        else:
            checks.append(Check(package, FAIL, f"missing ({why})", pip_command(package)))
    for module, package, feature, without in OPTIONAL:
        if _importable(module):
            checks.append(Check(package, OK, f"{_version(module) or 'installed'} ({feature})", feature=feature))
        else:
            status = INFO if without.startswith("only needed") else WARN
            checks.append(Check(package, status, f"not installed: {without}", pip_command(package), feature=feature))
    for module, title, without in SPEEDUPS:
        if importlib.util.find_spec(module.split(".")[0]) is None:
            continue
        if _importable(module):
            checks.append(Check(title, OK, "compiled extension in use"))
        else:
            checks.append(Check(title, INFO, without))
    checks.append(
        Check("certificates", OK, "self-signed certificates are generated in pure Python; openssl and cryptography are not needed")
    )
    return checks


def process_check() -> Check:
    import multiprocessing

    try:
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        proc = ctx.Process(target=_child_ping, args=(child,), daemon=True)
        proc.start()
        ok = parent.poll(30) and parent.recv() == "pong"
        proc.join(5)
        if ok:
            return Check("generator processes", OK, "a spawned process started and answered")
        return Check("generator processes", FAIL, "a spawned process did not answer within 30 s")
    except Exception as exc:
        return Check(
            "generator processes",
            FAIL,
            f"could not start a process: {exc}",
            "multiprocessing needs a writable /dev/shm; check that it is mounted and writable for your user",
        )


def _child_ping(conn) -> None:
    conn.send("pong")


def path_checks(loaded) -> list[Check]:
    checks = []
    for title, path in (("data", loaded.data_dir), ("logs", loaded.log_dir), ("run", loaded.run_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path):
                pass
            checks.append(Check(f"{title} directory", OK, f"{path} is writable"))
        except OSError as exc:
            checks.append(
                Check(f"{title} directory", FAIL, f"{path}: {exc.strerror}", f"set paths.{title}_dir to a directory your user can write")
            )
    return checks


def port_check(listen: str) -> Check:
    host, _, port = listen.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, int(port)))
        except OSError as exc:
            return Check(
                "listen address",
                WARN,
                f"{listen} is not free ({exc.strerror}); fine if Ballast is already running",
                "set server.listen to another port",
            )
    return Check("listen address", OK, f"{listen} is free")


def cluster_check(uri: str, timeout_ms: int = 8000) -> list[Check]:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    from ballast.targets import explain_failure

    checks = []
    client: MongoClient | None = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms, appname="ballast-doctor")
        hello = client.admin.command("hello")
        version = client.server_info().get("version")
        kind = (
            "replica set " + hello["setName"]
            if hello.get("setName")
            else ("sharded cluster (mongos)" if hello.get("msg") == "isdbgrid" else "standalone")
        )
        checks.append(Check("cluster", OK, f"MongoDB {version}, {kind}"))
        try:
            client.admin.command("serverStatus")
            checks.append(Check("server sampling", OK, "serverStatus is allowed"))
        except PyMongoError:
            checks.append(
                Check(
                    "server sampling",
                    WARN,
                    "serverStatus is not allowed; member and replication samples will be skipped",
                    "grant the clusterMonitor role",
                )
            )
    except Exception as exc:
        from ballast.targets import secrets_in

        hidden = secrets_in(uri)

        def redact(text: str) -> str:
            for secret in hidden:
                text = text.replace(secret, "****")
            return text

        why = explain_failure(exc, redact)
        detail = why["detail"].split(" (configured timeouts")[0]
        checks.append(Check("cluster", FAIL, f"{why['title']}: {detail[:300]}", why.get("fix", "")))
    finally:
        if client is not None:
            client.close()
    return checks


def capabilities() -> list[dict]:
    """Optional features and speedups, for the System page."""
    return [asdict(c) for c in package_checks()]


def run(loaded=None, uri: str | None = None, skip_process: bool = False) -> list[Check]:
    checks = package_checks()
    if not skip_process:
        checks.append(process_check())
    if loaded is not None:
        checks += path_checks(loaded)
        checks.append(port_check(loaded.config.server.listen))
    if uri:
        checks += cluster_check(uri)
    return checks


def render(checks: list[Check], color: bool | None = None) -> str:
    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None if color is None else color
    marks = {OK: ("PASS", "32"), WARN: ("WARN", "33"), FAIL: ("FAIL", "31"), INFO: ("INFO", "36")}
    lines = []
    for c in checks:
        label, code = marks[c.status]
        tag = f"\033[{code}m{label}\033[0m" if color else label
        lines.append(f"{tag}  {c.name}: {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"      fix: {c.fix}")
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append(f"{failed} failed, {warned} warnings" + ("" if failed else ". Ballast can run on this host."))
    return "\n".join(lines)
