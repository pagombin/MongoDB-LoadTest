"""HTTP(S) server: JSON API, server-sent events, static front end, security middleware."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import platform
import secrets
import shutil
import signal
import ssl
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pymongo
from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pymongo import AsyncMongoClient

from ballast import buildinfo, logs, product, report, targets, tls
from ballast.config import Loaded, effective_with_sources, split_listen
from ballast.engine.profile import Profile
from ballast.engine.runs import ACTIVE, Hub, RunError, RunManager
from ballast.store import Store, intersect_gaps

log = logging.getLogger("ballast.server")
WEB_DIR = Path(__file__).resolve().parent / "web"
CSRF_HEADER = "X-Ballast-Request"
RETENTION_TASK = web.AppKey("retention", asyncio.Task)
COOKIE = "ballast_session"
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str = "", fix: str = "", extra: dict | None = None):
        super().__init__(title)
        self.status, self.title, self.detail, self.fix, self.extra = status, title, detail, fix, extra or {}


def error_body(title: str, detail: str = "", fix: str = "", **extra) -> dict:
    return {"error": {"title": title, "detail": detail, "fix": fix, **extra}}


def json_response(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


async def read_model(request: web.Request, model: type[BaseModel]) -> BaseModel:
    if request.content_type != "application/json":
        raise ApiError(
            415, "Expected a JSON body", f"Content-Type was {request.content_type or 'missing'}.", "Send Content-Type: application/json."
        )
    try:
        data = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "The request body is not valid JSON") from None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]), "problem": "unknown field" if e["type"] == "extra_forbidden" else e["msg"]}
            for e in exc.errors()
        ]
        raise ApiError(
            422, "Some values are not valid", "; ".join(f"{p['field']}: {p['problem']}" for p in problems[:8]), "", {"problems": problems}
        ) from None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=64)
    profile_id: str | None = Field(default=None, max_length=64)
    profile: Profile | None = None
    acknowledge_foreign: bool = False


class PreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(min_length=1, max_length=64)
    profile_id: str | None = None
    profile: Profile | None = None


class MarkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)
    phase: str | None = Field(default=None, pattern="^(begin|end)$")
    source: str = Field(default="api", pattern="^(ui|api|cli)$")


class LevelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: float = Field(gt=0, le=10_000_000)


class WindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    label: str = Field(default="Selected window", max_length=120)


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = Field(min_length=1, max_length=63)
    confirm: str = Field(min_length=1, max_length=63)


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str | None = None
    target: targets.TargetIn | None = None


class App:
    def __init__(self, loaded: Loaded):
        self.loaded = loaded
        self.cfg = loaded.config
        self.started = time.time()
        self.store = Store(loaded.data_dir / "ballast.db")
        self.hub = Hub()
        self.runs = RunManager(self.store, self.hub, loaded.data_dir)
        self.token = loaded.access_token()
        self.session_key = self._session_key()
        self.ssl_ctx: ssl.SSLContext | None = None
        self.cert_path = loaded.resolve(self.cfg.tls.cert_file)
        self.key_path = loaded.resolve(self.cfg.tls.key_file)
        if self.token:
            logs.add_secret(self.token)
        for target in self.store.list_targets():
            self._register_target_secrets(target["uri"])

    def _session_key(self) -> bytes:
        path = self.loaded.data_dir / "session.key"
        if path.exists():
            return path.read_bytes()
        key = secrets.token_bytes(32)
        old = os.umask(0o077)
        try:
            path.write_bytes(key)
        finally:
            os.umask(old)
        return key

    def _register_target_secrets(self, uri: str) -> None:
        for secret in targets.secrets_in(uri):
            logs.add_secret(secret)
        self.runs.register_secrets(uri)

    # ---- request context ----

    def scheme_and_host(self, request: web.Request) -> tuple[str, str]:
        scheme = request.scheme
        host = request.host
        if self.cfg.server.trust_proxy_headers:
            scheme = request.headers.get("X-Forwarded-Proto", scheme).split(",")[0].strip() or scheme
            host = request.headers.get("X-Forwarded-Host", host).split(",")[0].strip() or host
        return scheme, host

    def session_value(self) -> str:
        return hmac.new(self.session_key, self.token.encode(), hashlib.sha256).hexdigest()

    def authorized(self, request: web.Request) -> bool:
        if not self.token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip().encode(), self.token.encode()):
            return True
        cookie = request.cookies.get(COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie.encode(), self.session_value().encode())

    # ---- middleware ----

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        try:
            if request.method in STATE_CHANGING:
                self.check_same_origin(request)
            if not self.authorized(request) and not self.gate_exempt(request.path):
                if request.path.startswith("/api/"):
                    raise ApiError(
                        401,
                        "Access token required",
                        "This server has an access gate.",
                        "Open the app in a browser to enter the token, or send Authorization: Bearer <token>.",
                    )
                raise web.HTTPSeeOther("/auth")
            response = await handler(request)
        except ApiError as exc:
            response = json_response(error_body(exc.title, exc.detail, exc.fix, **exc.extra), exc.status)
        except RunError as exc:
            response = json_response({"error": exc.as_dict()}, exc.status)
        except web.HTTPRequestEntityTooLarge:
            response = json_response(
                error_body("Request body too large", f"The limit is {self.cfg.server.max_body_kb} KB.", "Send a smaller body."), 413
            )
        except web.HTTPException as exc:
            if exc.status >= 400 and request.path.startswith("/api/"):
                response = json_response(error_body(exc.reason or "Error"), exc.status)
            else:
                response = web.Response(status=exc.status, headers=exc.headers, body=exc.body)
        except Exception:
            log.exception("unhandled error on %s %s", request.method, request.path)
            response = json_response(
                error_body(
                    "Internal error",
                    "The server hit an unexpected error. It has been logged.",
                    "Download the log bundle from System and contact support.",
                ),
                500,
            )
        self.add_security_headers(request, response)
        return response

    def gate_exempt(self, path: str) -> bool:
        return path in ("/auth", "/api/health", "/favicon.ico", "/favicon.svg") or path.startswith(("/css/", "/fonts/", "/img/"))

    def check_same_origin(self, request: web.Request) -> None:
        if request.path == "/auth":
            origin = request.headers.get("Origin")
            if origin and not self.origin_matches(request, origin):
                raise ApiError(403, "Cross-site request blocked")
            return
        if request.headers.get(CSRF_HEADER) != "1":
            raise ApiError(
                403,
                "Missing request header",
                f"State-changing requests must send {CSRF_HEADER}: 1.",
                "Use the app or ballastctl, which send it.",
            )
        origin = request.headers.get("Origin")
        if origin is not None and not self.origin_matches(request, origin):
            raise ApiError(
                403, "Cross-site request blocked", f"Origin {origin} does not match this server.", "Open the app from its own address."
            )
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise ApiError(403, "Cross-site request blocked", f"Sec-Fetch-Site was {site}.", "")

    def origin_matches(self, request: web.Request, origin: str) -> bool:
        scheme, host = self.scheme_and_host(request)
        parsed = urlsplit(origin)
        allowed = {f"{scheme}://{host}".lower()}
        if self.cfg.server.public_url:
            pu = urlsplit(self.cfg.server.public_url)
            allowed.add(f"{pu.scheme}://{pu.netloc}".lower())
        return f"{parsed.scheme}://{parsed.netloc}".lower() in allowed

    def add_security_headers(self, request: web.Request, response: web.StreamResponse) -> None:
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.path.startswith("/api/") and "Cache-Control" not in h:
            h["Cache-Control"] = "no-store"
        if self.cfg.tls.mode == "files" and self.scheme_and_host(request)[0] == "https":
            h["Strict-Transport-Security"] = "max-age=31536000"

    # ---- static and gate ----

    async def index(self, request: web.Request) -> web.Response:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("{{PRODUCT_NAME}}", _escape(self.cfg.branding.product_name))
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def auth_page(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            form = await request.post()
            supplied = str(form.get("token", ""))
            if self.token and hmac.compare_digest(supplied.encode(), self.token.encode()):
                response = web.Response(status=303, headers={"Location": "/"})
                secure = self.scheme_and_host(request)[0] == "https"
                response.set_cookie(
                    COOKIE, self.session_value(), httponly=True, secure=secure, samesite="Strict", path="/", max_age=30 * 86400
                )
                return response
            await asyncio.sleep(1)
            return web.Response(
                text=_gate_html(self.cfg.branding.product_name, "That token is not correct."), content_type="text/html", status=401
            )
        return web.Response(text=_gate_html(self.cfg.branding.product_name, ""), content_type="text/html")

    async def logo(self, request: web.Request) -> web.StreamResponse:
        if self.cfg.branding.logo_file:
            path = self.loaded.resolve(self.cfg.branding.logo_file)
            if path.exists() and path.suffix.lower() in (".svg", ".png", ".jpg", ".jpeg", ".webp"):
                return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        return web.FileResponse(WEB_DIR / "img" / "wordmark.svg", headers={"Cache-Control": "no-cache"})

    # ---- meta ----

    async def health(self, request: web.Request) -> web.Response:
        active = self.runs.status()
        return json_response(
            {
                "status": "ok",
                "version": product.VERSION,
                "uptime_seconds": int(time.time() - self.started),
                "active_run": {"id": active["id"], "status": active["status"]} if active and self.authorized(request) else None,
            }
        )

    def about_data(self) -> dict:
        return {
            "product": self.cfg.branding.product_name,
            "default_product": product.NAME,
            "description": product.DESCRIPTION,
            "version": product.VERSION,
            "commit": buildinfo.COMMIT,
            "built": buildinfo.BUILT,
            "driver": f"PyMongo {pymongo.version}",
            "python": platform.python_version(),
            "accent_color": self.cfg.branding.accent_color or None,
            "has_logo": bool(self.cfg.branding.logo_file),
            "access_gate": bool(self.token),
        }

    async def about(self, request: web.Request) -> web.Response:
        return json_response(self.about_data())

    async def system(self, request: web.Request) -> web.Response:
        data_dir = self.loaded.data_dir
        disk = shutil.disk_usage(data_dir)
        log_size = sum(p.stat().st_size for p in self.loaded.log_dir.glob("*") if p.is_file()) if self.loaded.log_dir.exists() else 0
        cert = None
        if self.cfg.tls.mode != "off" and self.cert_path.exists():
            try:
                cert = tls.describe(self.cert_path)
            except Exception as exc:
                cert = {"error": str(exc)}
        listen = self.cfg.server.listen
        return json_response(
            {
                "health": "ok",
                "uptime_seconds": int(time.time() - self.started),
                "pid": os.getpid(),
                "listen": listen,
                "scheme": "http" if self.cfg.tls.mode == "off" else "https",
                "about": self.about_data(),
                "home": str(self.loaded.home),
                "config_file": str(self.loaded.path) if self.loaded.path else None,
                "config": effective_with_sources(self.loaded),
                "storage": {
                    "database_bytes": self.store.size_bytes(),
                    "runs": len(self.store.list_runs(100000)),
                    "logs_bytes": log_size,
                    "disk_free_bytes": disk.free,
                    "disk_total_bytes": disk.total,
                    "data_dir": str(data_dir),
                },
                "certificate": cert,
                "tls_mode": self.cfg.tls.mode,
                "warnings": list(dict.fromkeys(_warnings())),
                "logs": logs.tail(self.loaded.log_dir, lines=100, min_level=request.query.get("level", "warning")),
            }
        )

    async def log_bundle(self, request: web.Request) -> web.Response:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            if self.loaded.log_dir.exists():
                for path in sorted(self.loaded.log_dir.glob("*")):
                    if path.is_file() and path.stat().st_size < 200 * 1024 * 1024:
                        zf.writestr(f"logs/{path.name}", logs.redact(path.read_text(encoding="utf-8", errors="replace")))
            zf.writestr("config.json", json.dumps(self.loaded.redacted(), indent=2))
            zf.writestr(
                "about.json", json.dumps({**self.about_data(), "platform": platform.platform(), "cpu_count": os.cpu_count()}, indent=2)
            )
            zf.writestr("runs.json", json.dumps(self.store.list_runs(200), indent=2, default=str))
        name = f"{product.SLUG}-logs-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.zip"
        return web.Response(
            body=buffer.getvalue(), content_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'}
        )

    # ---- targets ----

    def _target(self, target_id: str) -> dict:
        row = self.store.get_target(target_id)
        if row is None:
            raise ApiError(404, "Target not found", f"No target with id {target_id}.", "Pick a target from the Targets page.")
        return row

    async def list_targets(self, request):
        return json_response([targets.public_view(t) for t in self.store.list_targets()])

    async def create_target(self, request):
        body = await read_model(request, targets.TargetIn)
        if targets.MASK in (targets.parse(body.uri)["password"] or ""):
            raise ApiError(422, "Password is masked", "The connection string contains **** as the password.", "Type the real password.")
        if self.store.get_target_by_name(body.name):
            raise ApiError(409, "Name already used", f"A target named {body.name} exists.", "Choose another name.")
        row = self.store.save_target(body.model_dump())
        self._register_target_secrets(row["uri"])
        log.info("target %s created (%s)", row["name"], targets.redact_uri(row["uri"]))
        return json_response(targets.public_view(row), 201)

    async def get_target(self, request):
        return json_response(targets.public_view(self._target(request.match_info["id"])))

    async def update_target(self, request):
        current = self._target(request.match_info["id"])
        body = await read_model(request, targets.TargetIn)
        other = self.store.get_target_by_name(body.name)
        if other and other["id"] != current["id"]:
            raise ApiError(409, "Name already used", f"A target named {body.name} exists.", "Choose another name.")
        data = body.model_dump()
        data["uri"] = targets.merge_password(body.uri, current["uri"])
        row = self.store.save_target(data, current["id"])
        self._register_target_secrets(row["uri"])
        return json_response(targets.public_view(row))

    async def delete_target(self, request):
        target_id = request.match_info["id"]
        active = self.runs.active
        if active and active.target["id"] == target_id:
            raise ApiError(409, "Target is in use", "A run is using this target.", "Stop the run first.")
        if not self.store.delete_target(target_id):
            raise ApiError(404, "Target not found")
        return json_response({"deleted": target_id})

    async def test_target(self, request):
        body = await read_model(request, TestRequest)
        if body.target_id:
            target = self._target(body.target_id)
            if body.target is not None:
                target = {**body.target.model_dump(), "uri": targets.merge_password(body.target.uri, target["uri"]), "id": target["id"]}
        elif body.target is not None:
            target = body.target.model_dump()
        else:
            raise ApiError(422, "Nothing to test", "Send target_id or target.", "")
        self._register_target_secrets(target["uri"])
        result = await targets.test_connection(target)
        if body.target_id and body.target is None:
            self.store.set_target_test(body.target_id, result)
        return json_response(result)

    async def datasets(self, request):
        target = self._target(request.match_info["id"])
        database = request.query.get("database", "")
        profile = Profile.model_validate({"dataset": {"database": database or "ballast_bench"}})
        return json_response(await self.runs.preflight(target, profile))

    async def cleanup(self, request):
        target = self._target(request.match_info["id"])
        body = await read_model(request, CleanupRequest)
        if body.confirm != body.database:
            raise ApiError(422, "Confirmation does not match", "Type the database name exactly to confirm.", "")
        active = self.runs.active
        if active and active.target["id"] == target["id"] and active.database == body.database:
            raise ApiError(409, "Database is in use", "A run is writing to this database.", "Stop the run first.")
        client = AsyncMongoClient(
            target["uri"], serverSelectionTimeoutMS=8000, appname=f"{product.SLUG}-cleanup", **targets.client_options(target)
        )
        dropped = []
        try:
            db = client[body.database]
            names = set(await db.list_collection_names())
            registry = db[product.REGISTRY_COLLECTION]
            if product.REGISTRY_COLLECTION in names:
                async for doc in registry.find({}, {"_id": 1}):
                    if doc["_id"] in names:
                        await db.drop_collection(doc["_id"])
                        dropped.append(doc["_id"])
                await db.drop_collection(product.REGISTRY_COLLECTION)
        except Exception as exc:
            info = targets.explain_failure(exc, logs.redact)
            raise ApiError(502, info["title"], info["detail"], info["fix"]) from None
        finally:
            await client.close()
        log.info("cleanup on %s.%s dropped %s", target["name"], body.database, dropped)
        return json_response({"database": body.database, "dropped": dropped})

    # ---- profiles ----

    async def list_profiles(self, request):
        return json_response(self.store.list_profiles())

    async def create_profile(self, request):
        profile = await read_model(request, Profile)
        if self.store.get_profile_by_name(profile.name):
            raise ApiError(409, "Name already used", f"A profile named {profile.name} exists.", "Choose another name.")
        return json_response(self.store.save_profile(profile.model_dump(mode="json")), 201)

    def _profile(self, profile_id: str) -> dict:
        row = self.store.get_profile(profile_id)
        if row is None:
            raise ApiError(404, "Profile not found")
        return row

    async def get_profile(self, request):
        return json_response(self._profile(request.match_info["id"]))

    async def update_profile(self, request):
        current = self._profile(request.match_info["id"])
        profile = await read_model(request, Profile)
        other = self.store.get_profile_by_name(profile.name)
        if other and other["id"] != current["id"]:
            raise ApiError(409, "Name already used", f"A profile named {profile.name} exists.", "Choose another name.")
        return json_response(self.store.save_profile(profile.model_dump(mode="json"), current["id"]))

    async def delete_profile(self, request):
        if not self.store.delete_profile(request.match_info["id"]):
            raise ApiError(404, "Profile not found")
        return json_response({"deleted": request.match_info["id"]})

    async def duplicate_profile(self, request):
        current = self._profile(request.match_info["id"])
        body = dict(current["body"])
        base = f"{body['name']} copy"
        name, n = base[:80], 2
        while self.store.get_profile_by_name(name):
            name = f"{base} {n}"[:80]
            n += 1
        body["name"] = name
        return json_response(self.store.save_profile(Profile.model_validate(body).model_dump(mode="json")), 201)

    async def export_profile(self, request):
        current = self._profile(request.match_info["id"])
        payload = {"format": f"{product.SLUG}-profile", "version": 1, "profile": current["body"]}
        filename = "".join(c if c.isalnum() or c in "-_." else "-" for c in current["name"]) + ".json"
        return web.Response(
            text=json.dumps(payload, indent=2),
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def import_profile(self, request):
        try:
            data = await request.json()
        except ValueError:
            raise ApiError(400, "The file is not valid JSON") from None
        body = data.get("profile") if isinstance(data, dict) and "profile" in data else data
        try:
            profile = Profile.model_validate(body)
        except ValidationError as exc:
            raise ApiError(
                422,
                "The file is not a valid profile",
                "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:8]),
                "",
            ) from None
        name, n = profile.name, 2
        while self.store.get_profile_by_name(name):
            name = f"{profile.name} {n}"[:80]
            n += 1
        profile.name = name
        return json_response(self.store.save_profile(profile.model_dump(mode="json")), 201)

    # ---- runs ----

    def _resolve_profile(self, profile_id: str | None, profile: Profile | None) -> Profile:
        if profile is not None:
            return profile
        if profile_id:
            return Profile.model_validate(self._profile(profile_id)["body"])
        raise ApiError(422, "No workload", "Send profile_id or profile.", "")

    async def preflight(self, request):
        body = await read_model(request, PreflightRequest)
        target = self._target(body.target_id)
        profile = self._resolve_profile(body.profile_id, body.profile)
        pre = await self.runs.preflight(target, profile)
        pre["summary"] = profile.plain_summary()
        pre["load_documents"] = self.runs.load_target(profile)
        return json_response(pre)

    async def list_runs(self, request):
        return json_response({"runs": self.store.list_runs(), "active": self.runs.status()})

    async def start_run(self, request):
        body = await read_model(request, RunRequest)
        target = self._target(body.target_id)
        profile = self._resolve_profile(body.profile_id, body.profile)
        row = await self.runs.start(name=body.name, target=target, profile=profile, acknowledge_foreign=body.acknowledge_foreign)
        log.info("run %s started: %s", row["id"], body.name)
        return json_response(_run_view(row), 201)

    def _run(self, run_id: str) -> dict:
        if run_id == "active":
            if self.runs.active is None:
                raise ApiError(404, "No active run", "", "Start a run first.")
            run_id = self.runs.active.id
        row = self.store.get_run(run_id)
        if row is None:
            raise ApiError(404, "Run not found", f"No run with id {run_id}.", "")
        return row

    async def get_run(self, request):
        row = self._run(request.match_info["id"])
        view = _run_view(row)
        view["markers"] = self.store.markers(row["id"])
        view["events"] = self.store.events(row["id"])
        processes = (row["profile"] or {}).get("load", {}).get("processes", 1)
        view["gaps"] = intersect_gaps(self.store.gaps(row["id"]), processes)
        if self.runs.active and self.runs.active.id == row["id"]:
            view["live"] = self.runs.status()
        return json_response(view)

    async def run_series(self, request):
        row = self._run(request.match_info["id"])
        q = request.query
        try:
            max_points = min(5000, max(50, int(q.get("max_points", "1500"))))
            start = int(q["start"]) if "start" in q else None
            end = int(q["end"]) if "end" in q else None
        except ValueError:
            raise ApiError(400, "start, end and max_points must be integers") from None
        return json_response(self.runs.series(row["id"], max_points, start, end))

    async def run_analysis(self, request):
        row = self._run(request.match_info["id"])
        if row["status"] in ACTIVE or request.query.get("fresh") == "1" or not row.get("analysis"):
            return json_response(self.runs.analyze(row["id"]))
        return json_response(row["analysis"])

    async def run_window(self, request):
        row = self._run(request.match_info["id"])
        body = await read_model(request, WindowRequest)
        if body.end_ms <= body.start_ms:
            raise ApiError(422, "End must be after start")
        return json_response(
            self.runs.analyze(row["id"], {"label": body.label, "source": "selection", "start_ms": body.start_ms, "end_ms": body.end_ms})
        )

    async def run_samples(self, request):
        row = self._run(request.match_info["id"])
        return json_response(self.store.samples(row["id"], request.query.get("kind")))

    async def pause_run(self, request):
        return json_response(self.runs.pause(self._run(request.match_info["id"])["id"]))

    async def resume_run(self, request):
        return json_response(self.runs.resume(self._run(request.match_info["id"])["id"]))

    async def stop_run(self, request):
        row = self._run(request.match_info["id"])
        result = await self.runs.stop(row["id"])
        log.info("run %s stop requested", row["id"])
        return json_response(result)

    async def set_level(self, request):
        row = self._run(request.match_info["id"])
        body = await read_model(request, LevelRequest)
        return json_response(self.runs.set_level(row["id"], body.level))

    async def add_marker(self, request):
        run_id = request.match_info["id"]
        body = await read_model(request, MarkerRequest)
        marker = self.runs.add_marker(run_id, body.label, body.note, body.phase, body.source)
        log.info("marker %r added to run %s from %s", body.label, marker["run_id"], body.source)
        return json_response(marker, 201)

    async def delete_run(self, request):
        row = self._run(request.match_info["id"])
        if row["status"] in ACTIVE:
            raise ApiError(409, "Run is active", "Stop the run before deleting it.", "")
        self.store.delete_run(row["id"])
        return json_response({"deleted": row["id"]})

    async def export_run(self, request):
        row = self._run(request.match_info["id"])
        fmt = request.match_info["fmt"]
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in row["name"])[:60] or row["id"]
        if fmt == "csv":
            body, ctype = report.csv_export(self.store, row), "text/csv"
        elif fmt == "json":
            body, ctype = (
                report.json_export(self.store, row, self.runs.analyze(row["id"]) if row["status"] in ACTIVE else row.get("analysis")),
                "application/json",
            )
        elif fmt == "html":
            analysis_result = self.runs.analyze(row["id"]) if row["status"] in ACTIVE or not row.get("analysis") else row["analysis"]
            body, ctype = report.html_export(self.store, row, analysis_result, self.about_data(), self.runs), "text/html"
        else:
            raise ApiError(404, "Unknown export format")
        body = logs.redact(body)
        return web.Response(
            text=body,
            content_type=ctype,
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe}-{row["id"]}.{fmt}"'},
        )

    async def compare(self, request):
        ids = [i for i in request.query.get("ids", "").split(",") if i][:6]
        if len(ids) < 2:
            raise ApiError(422, "Pick at least two runs to compare")
        align = request.query.get("align", "start")
        marker_label = request.query.get("marker", "")
        out = []
        for run_id in ids:
            row = self._run(run_id)
            offset_s = (row["started_at"] or row["created_at"]) // 1000
            aligned_on = "start"
            if align == "marker" and marker_label:
                for m in self.store.markers(row["id"]):
                    if m["label"] == marker_label:
                        offset_s = m["t_ms"] // 1000
                        aligned_on = "marker"
                        break
            series = self.runs.series(row["id"], 1500)
            summary = (row.get("analysis") or {}).get("summary") or row.get("summary") or {}
            impacts = (row.get("analysis") or {}).get("impacts", [])
            out.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "status": row["status"],
                    "offset_s": offset_s,
                    "aligned_on": aligned_on,
                    "step": series["step"],
                    "points": series["points"],
                    "summary": summary,
                    "impacts": impacts,
                    "markers": self.store.markers(row["id"]),
                }
            )
        return json_response({"align": align, "marker": marker_label, "runs": out})

    async def marker_labels(self, request):
        ids = [i for i in request.query.get("ids", "").split(",") if i][:6]
        labels: dict[str, int] = {}
        for run_id in ids:
            for m in self.store.markers(run_id):
                labels[m["label"]] = labels.get(m["label"], 0) + 1
        return json_response(sorted(labels, key=lambda k: -labels[k]))

    # ---- live ----

    async def live(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.add_security_headers(request, response)
        await response.prepare(request)
        queue = self.hub.subscribe()
        run_filter = request.query.get("run")
        try:
            hello: dict[str, Any] = {"active": self.runs.status()}
            if self.runs.active and (not run_filter or run_filter in (self.runs.active.id, "active")):
                hello["points"] = list(self.runs.active.live)[-900:]
            await response.write(_sse("hello", hello))
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    await response.write(b": keep-alive\n\n")
                    continue
                if run_filter and run_filter != "active" and kind != "state" and data.get("run") != run_filter:
                    continue
                await response.write(_sse(kind, data))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(queue)
        return response

    # ---- app ----

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.middleware], client_max_size=self.cfg.server.max_body_kb * 1024)
        app.on_response_prepare.append(_strip_server_header)
        r = app.router
        r.add_get("/", self.index)
        r.add_route("*", "/auth", self.auth_page)
        r.add_get("/branding/logo", self.logo)
        r.add_get("/api/health", self.health)
        r.add_get("/api/about", self.about)
        r.add_get("/api/system", self.system)
        r.add_get("/api/system/logs.zip", self.log_bundle)
        r.add_get("/api/live", self.live)
        r.add_get("/api/targets", self.list_targets)
        r.add_post("/api/targets", self.create_target)
        r.add_post("/api/targets/test", self.test_target)
        r.add_get("/api/targets/{id}", self.get_target)
        r.add_put("/api/targets/{id}", self.update_target)
        r.add_delete("/api/targets/{id}", self.delete_target)
        r.add_get("/api/targets/{id}/datasets", self.datasets)
        r.add_post("/api/targets/{id}/cleanup", self.cleanup)
        r.add_get("/api/profiles", self.list_profiles)
        r.add_post("/api/profiles", self.create_profile)
        r.add_post("/api/profiles/import", self.import_profile)
        r.add_get("/api/profiles/{id}", self.get_profile)
        r.add_put("/api/profiles/{id}", self.update_profile)
        r.add_delete("/api/profiles/{id}", self.delete_profile)
        r.add_post("/api/profiles/{id}/duplicate", self.duplicate_profile)
        r.add_get("/api/profiles/{id}/export", self.export_profile)
        r.add_post("/api/preflight", self.preflight)
        r.add_get("/api/runs", self.list_runs)
        r.add_post("/api/runs", self.start_run)
        r.add_get("/api/compare", self.compare)
        r.add_get("/api/compare/markers", self.marker_labels)
        r.add_get("/api/runs/{id}", self.get_run)
        r.add_delete("/api/runs/{id}", self.delete_run)
        r.add_get("/api/runs/{id}/series", self.run_series)
        r.add_get("/api/runs/{id}/analysis", self.run_analysis)
        r.add_post("/api/runs/{id}/window", self.run_window)
        r.add_get("/api/runs/{id}/samples", self.run_samples)
        r.add_post("/api/runs/{id}/pause", self.pause_run)
        r.add_post("/api/runs/{id}/resume", self.resume_run)
        r.add_post("/api/runs/{id}/stop", self.stop_run)
        r.add_post("/api/runs/{id}/level", self.set_level)
        r.add_post("/api/runs/{id}/markers", self.add_marker)
        r.add_get("/api/runs/{id}/export.{fmt}", self.export_run)
        r.add_static("/", WEB_DIR, show_index=False, append_version=False)
        app.on_startup.append(self.on_startup)
        app.on_shutdown.append(self.on_shutdown)
        return app

    async def on_startup(self, app) -> None:
        interrupted = self.store.interrupt_unfinished()
        if interrupted:
            log.warning("marked %d run(s) interrupted after restart: %s", len(interrupted), ", ".join(interrupted))
        for preset in self.loaded.target_presets():
            existing = self.store.get_target_by_name(preset["name"])
            self.store.save_target(preset, existing["id"] if existing else None)
            self._register_target_secrets(preset["uri"])
        self._seed_profiles()
        app[RETENTION_TASK] = asyncio.create_task(self._retention_loop())

    def _seed_profiles(self) -> None:
        if self.store.list_profiles():
            return
        defaults = [
            {
                "name": "OLTP steady 1k",
                "description": "60% reads by key, 30% updates, 10% inserts at 1,000 ops/s. A starting point for scale tests.",
                "load": {"model": "open", "rate": 1000, "concurrency": 64},
                "schedule": {"warmup_seconds": 30, "steady_seconds": None},
                "dataset": {"load": {"documents": 100000}},
            },
            {
                "name": "Write heavy 2k",
                "description": "80% inserts and updates at 2,000 ops/s with majority write concern.",
                "operations": {"find": 20, "insert": 40, "update": 40},
                "load": {"model": "open", "rate": 2000, "concurrency": 128},
                "schedule": {"warmup_seconds": 30, "steady_seconds": None},
            },
            {
                "name": "Closed loop 32 workers",
                "description": "32 workers with no think time; throughput finds its own level.",
                "load": {"model": "closed", "concurrency": 32},
                "schedule": {"warmup_seconds": 15, "steady_seconds": 300},
            },
        ]
        for body in defaults:
            self.store.save_profile(Profile.model_validate(body).model_dump(mode="json"))

    async def _retention_loop(self) -> None:
        while True:
            try:
                removed = self.store.prune(self.cfg.retention.runs_max_age_days, self.cfg.retention.runs_max_count)
                if removed:
                    log.info("retention removed %d run(s)", len(removed))
            except Exception:
                log.exception("retention failed")
            await asyncio.sleep(3600)

    async def on_shutdown(self, app) -> None:
        log.info("shutting down")
        task = app.get(RETENTION_TASK)
        if task:
            task.cancel()
        await self.runs.shutdown()


async def _strip_server_header(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Server"] = product.SLUG


def _warnings() -> list[str]:
    from ballast.config import PERMISSION_WARNINGS

    return PERMISSION_WARNINGS


def _sse(kind: str, data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n".encode()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _gate_html(name: str, message: str) -> str:
    name = _escape(name)
    note = f'<p class="gate-error" role="alert">{_escape(message)}</p>' if message else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name}: access token</title><link rel="stylesheet" href="/css/app.css"><link rel="icon" href="/favicon.svg"></head>
<body class="gate"><main class="gate-box"><img src="/img/wordmark.svg" alt="{name}" class="gate-mark">
<h1>Enter the access token</h1><p>This server requires a token. Ask whoever set it up, or read it from the file named by <code>server.access_token_file</code>.</p>
{note}<form method="post" action="/auth"><label for="token">Access token</label><input id="token" name="token" type="password" autocomplete="current-password" required autofocus>
<button type="submit" class="btn primary">Continue</button></form></main></body></html>"""


def _run_view(row: dict) -> dict:
    return {
        k: row.get(k)
        for k in (
            "id",
            "name",
            "status",
            "target_id",
            "target_name",
            "database_name",
            "created_at",
            "started_at",
            "ended_at",
            "profile",
            "snapshot",
            "summary",
            "analysis",
            "error",
        )
    }


def startup_checks(loaded: Loaded) -> list[str]:
    problems = []
    for label, path in (("data directory", loaded.data_dir), ("log directory", loaded.log_dir), ("run directory", loaded.run_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".write-test-{os.getpid()}"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            problems.append(f"The {label} {path} is not writable: {exc.strerror}. Fix the permissions or set paths.{label.split()[0]}_dir.")
    try:
        free_mb = shutil.disk_usage(loaded.data_dir).free // (1024 * 1024)
        if free_mb < loaded.config.paths.min_free_disk_mb:
            problems.append(
                f"Only {free_mb} MB free on the disk holding {loaded.data_dir}; at least {loaded.config.paths.min_free_disk_mb} MB is required (paths.min_free_disk_mb)."
            )
    except OSError:
        pass
    return problems


def certificate_hosts(loaded: Loaded) -> list[str]:
    """Hosts for the self-signed certificate: the configured list plus a specific listen address."""
    hosts = list(loaded.config.tls.self_signed_hosts)
    host, _ = split_listen(loaded.config.server.listen)
    if host not in ("0.0.0.0", "::") and host not in hosts:  # noqa: S104
        hosts.append(host)
    return hosts


def prepare_tls(app: App) -> tuple[Any, dict | None]:
    cfg = app.cfg.tls
    if cfg.mode == "off":
        return None, None
    if cfg.mode == "self-signed":
        hosts = certificate_hosts(app.loaded)
        _, created = tls.ensure_self_signed(app.cert_path, app.key_path, hosts)
        if created:
            log.info("generated self-signed certificate for %s", ", ".join(hosts))
    else:
        for path, key in ((app.cert_path, "tls.cert_file"), (app.key_path, "tls.key_file")):
            if not path.exists():
                raise SystemExit(f"{key} points to {path}, which does not exist. Set the path, or use tls.mode: self-signed.")
    ctx = tls.server_context(app.cert_path, app.key_path, cfg.min_version)
    app.ssl_ctx = ctx
    return ctx, tls.describe(app.cert_path)


async def _redirect_app(loaded: Loaded) -> web.Application:
    public = loaded.config.server.public_url
    _, port = split_listen(loaded.config.server.listen)

    async def handler(request: web.Request):
        if public:
            raise web.HTTPPermanentRedirect(public + request.path_qs)
        host = request.host.split(":")[0]
        raise web.HTTPPermanentRedirect(f"https://{host}:{port}{request.path_qs}")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def serve(loaded: Loaded, ready_file: Path | None = None) -> int:
    problems = startup_checks(loaded)
    if problems:
        for problem in problems:
            print(f"startup check failed: {problem}", flush=True)
        return 3
    cfg = loaded.config
    logs.setup(
        loaded.log_dir,
        cfg.logging.level,
        cfg.logging.format,
        cfg.logging.max_size_mb,
        cfg.logging.max_files,
        console=os.environ.get("BALLAST_FOREGROUND") == "1",
    )
    for warning in _warnings():
        log.warning(warning)
        print(f"warning: {warning}", flush=True)
    app = App(loaded)
    if app.store.migrated_from is not None:
        log.info("data store migrated from schema version %s", app.store.migrated_from)
    ssl_ctx, cert = prepare_tls(app)
    host, port = split_listen(cfg.server.listen)
    scheme = "https" if ssl_ctx else "http"
    web_app = app.build()

    async def main() -> None:
        runner = web.AppRunner(web_app, handle_signals=False, access_log=None, shutdown_timeout=5)
        await runner.setup()
        site = web.TCPSite(runner, host, port, ssl_context=ssl_ctx, reuse_address=True)
        await site.start()
        redirect_runner = None
        if cfg.tls.redirect_http_listen and ssl_ctx:
            rhost, rport = split_listen(cfg.tls.redirect_http_listen)
            redirect_runner = web.AppRunner(await _redirect_app(loaded), handle_signals=False, access_log=None)
            await redirect_runner.setup()
            await web.TCPSite(redirect_runner, rhost, rport).start()
            log.info("redirecting http://%s:%d to https", rhost, rport)
        url = cfg.server.public_url or f"{scheme}://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"  # noqa: S104
        log.info("%s %s listening on %s://%s:%d", product.NAME, product.VERSION, scheme, host, port)
        print(f"{cfg.branding.product_name} {product.VERSION} listening on {scheme}://{host}:{port}", flush=True)
        print(f"Open {url}", flush=True)
        if cert:
            print(f"TLS certificate SHA-256 fingerprint: {cert['sha256']}", flush=True)
            log.info("certificate fingerprint %s, expires %s", cert["sha256"], cert["not_after"])
        pid_file = loaded.run_dir / "ballast.pid"
        pid_file.write_text(f"{os.getpid()}\n")
        if ready_file:
            ready_file.write_text(json.dumps({"pid": os.getpid(), "url": url, "fingerprint": cert["sha256"] if cert else None}))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def reload_cert() -> None:
            if ssl_ctx is None:
                log.info("SIGHUP received; TLS is off, nothing to reload")
                return
            try:
                if cfg.tls.mode == "self-signed":
                    tls.ensure_self_signed(app.cert_path, app.key_path, certificate_hosts(loaded))
                tls.reload_context(ssl_ctx, app.cert_path, app.key_path)
                info = tls.describe(app.cert_path)
                log.info("reloaded TLS certificate, fingerprint %s, expires %s", info["sha256"], info["not_after"])
            except Exception:
                log.exception("certificate reload failed; keeping the previous certificate")

        loop.add_signal_handler(signal.SIGHUP, reload_cert)
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
        await stop.wait()
        log.info("stop signal received")
        try:
            await asyncio.wait_for(app.runs.shutdown(), timeout=cfg.server.shutdown_timeout_seconds)
        except TimeoutError:
            log.warning("run did not stop within %d s", cfg.server.shutdown_timeout_seconds)
        if redirect_runner:
            await redirect_runner.cleanup()
        await runner.cleanup()
        app.store.close()
        if ready_file and ready_file.exists():
            ready_file.unlink()
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()

    asyncio.run(main())
    log.info("stopped")
    return 0
