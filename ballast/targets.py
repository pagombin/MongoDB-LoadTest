"""Saved targets: connection strings, redaction, and the "Test connection" action."""

from __future__ import annotations

import re
import time
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import AsyncMongoClient
from pymongo import errors as pe

from ballast.product import SLUG

MASK = "****"
URI_RE = re.compile(
    r"^(?P<scheme>mongodb(?:\+srv)?)://(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?(?P<hosts>[^/?]+)(?P<path>/[^?]*)?(?:\?(?P<query>.*))?$"
)
SENSITIVE_QUERY_KEYS = {"authmechanismproperties", "tlscertificatekeyfilepassword", "sslclientcertificatekeypassword"}


class TargetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    uri: str = Field(min_length=10, max_length=8192)
    tls: Literal["auto", "on", "off"] = "auto"
    tls_ca_file: str | None = Field(default=None, max_length=4096)
    tls_certificate_key_file: str | None = Field(default=None, max_length=4096)
    tls_allow_invalid_hostnames: bool = False
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$", value):
            raise ValueError("name may use letters, digits, space, '.', '_' and '-'")
        return value

    @field_validator("uri")
    @classmethod
    def _uri(cls, value: str) -> str:
        value = value.strip()
        if not URI_RE.match(value):
            raise ValueError("connection string must start with mongodb:// or mongodb+srv:// and name at least one host")
        return value


def parse(uri: str) -> dict:
    match = URI_RE.match(uri.strip())
    if not match:
        return {"scheme": None, "user": None, "password": None, "hosts": [], "srv": False, "path": "", "query": ""}
    parts = match.groupdict()
    return {
        "scheme": parts["scheme"],
        "user": unquote(parts["user"]) if parts["user"] else None,
        "password": unquote(parts["password"]) if parts["password"] is not None else None,
        "hosts": parts["hosts"].split(","),
        "srv": parts["scheme"] == "mongodb+srv",
        "path": parts["path"] or "",
        "query": parts["query"] or "",
    }


def _redact_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        pairs.append(f"{key}={MASK if key.lower() in SENSITIVE_QUERY_KEYS else value}")
    return "&".join(pairs)


def redact_uri(uri: str) -> str:
    p = parse(uri)
    if p["scheme"] is None:
        return MASK
    auth = ""
    if p["user"] is not None:
        auth = quote(p["user"], safe="") + (f":{MASK}" if p["password"] is not None else "") + "@"
    query = _redact_query(p["query"])
    return f"{p['scheme']}://{auth}{','.join(p['hosts'])}{p['path']}" + (f"?{query}" if query else "")


def secrets_in(uri: str) -> list[str]:
    p = parse(uri)
    found = []
    if p["password"]:
        found.append(p["password"])
        found.append(quote(p["password"], safe=""))
    for key, value in parse_qsl(p["query"], keep_blank_values=True):
        if key.lower() in SENSITIVE_QUERY_KEYS and value:
            found.append(value)
    return sorted({s for s in found if len(s) >= 3}, key=len, reverse=True)


def merge_password(new_uri: str, stored_uri: str | None) -> str:
    """If the client sent back the masked password, keep the stored one."""
    new = parse(new_uri)
    if stored_uri is None or new["password"] != MASK:
        return new_uri
    stored = parse(stored_uri)
    if stored["password"] is None:
        return new_uri
    return new_uri.replace(f":{MASK}@", f":{quote(stored['password'], safe='')}@", 1)


def client_options(target: dict) -> dict:
    options: dict = {}
    tls = target.get("tls", "auto")
    if tls == "on":
        options["tls"] = True
    elif tls == "off":
        options["tls"] = False
    if target.get("tls_ca_file"):
        options["tlsCAFile"] = target["tls_ca_file"]
    if target.get("tls_certificate_key_file"):
        options["tlsCertificateKeyFile"] = target["tls_certificate_key_file"]
    if target.get("tls_allow_invalid_hostnames"):
        options["tlsAllowInvalidHostnames"] = True
    return options


def public_view(row: dict) -> dict:
    p = parse(row["uri"])
    return {
        "id": row["id"],
        "name": row["name"],
        "uri": redact_uri(row["uri"]),
        "hosts": p["hosts"],
        "srv": p["srv"],
        "username": p["user"],
        "has_password": bool(p["password"]),
        "tls": row.get("tls", "auto"),
        "tls_ca_file": row.get("tls_ca_file"),
        "tls_certificate_key_file": row.get("tls_certificate_key_file"),
        "tls_allow_invalid_hostnames": bool(row.get("tls_allow_invalid_hostnames")),
        "notes": row.get("notes", ""),
        "last_test": row.get("last_test"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def explain_failure(exc: BaseException, redact) -> dict:
    """Name the real cause of a failed connection and suggest a fix."""
    text = redact(str(exc))
    lower = text.lower()
    if isinstance(exc, pe.InvalidURI):
        return {
            "cause": "invalid_uri",
            "title": "The connection string is not valid",
            "detail": text,
            "fix": "Check the format: mongodb://user:password@host:port/?options or mongodb+srv://user:password@cluster-host/. Percent-encode special characters in the password.",
        }
    if isinstance(exc, pe.ConfigurationError) and (
        "dns" in lower or "srv" in lower or "nxdomain" in lower or "resolution" in lower or "query name" in lower
    ):
        return {
            "cause": "dns_srv",
            "title": "DNS SRV lookup failed",
            "detail": text,
            "fix": "Check the cluster host name. From this server run: nslookup -type=SRV _mongodb._tcp.<cluster-host>. If DNS is blocked, use the standard mongodb:// connection string with every member listed.",
        }
    if (isinstance(exc, pe.OperationFailure) and getattr(exc, "code", None) == 18) or "authentication failed" in lower:
        return {
            "cause": "auth",
            "title": "Authentication failed",
            "detail": text,
            "fix": "Check the user name and password, and set authSource (usually admin) in the connection string. Managed services also need the user to exist on this cluster.",
        }
    if "certificate" in lower or "ssl" in lower or "tls" in lower:
        return {
            "cause": "tls",
            "title": "TLS handshake failed",
            "detail": text,
            "fix": "If the server uses a private CA, set the CA file path. If TLS is required, set TLS to on. For a host name mismatch, connect using the name on the certificate.",
        }
    if isinstance(exc, pe.ServerSelectionTimeoutError):
        if "connection refused" in lower:
            fix = "Nothing is listening on that host and port. Check the port and that mongod is running."
            title = "Connection refused"
        elif "timed out" in lower or "no route" in lower or "unreachable" in lower:
            fix = "The server did not answer. Check firewalls and the cluster's IP access list for this server's address."
            title = "Server selection timed out"
        elif "replicasetnomprimary" in lower.replace(" ", "") or "no primary" in lower:
            fix = "Members are reachable but none is primary. Check replica set health, or connect with readPreference=secondaryPreferred to test reads."
            title = "No primary available"
        else:
            fix = "Check host names, ports, network access, and the replicaSet name in the connection string."
            title = "Server selection timed out"
        return {"cause": "server_selection", "title": title, "detail": text, "fix": fix}
    if isinstance(exc, pe.ConfigurationError):
        return {
            "cause": "configuration",
            "title": "Driver configuration error",
            "detail": text,
            "fix": "Check the connection string options and TLS file paths.",
        }
    return {"cause": "other", "title": type(exc).__name__, "detail": text, "fix": "See the detail for the driver's message."}


async def test_connection(target: dict, timeout_ms: int = 8000) -> dict:
    secrets = secrets_in(target["uri"])

    def redact(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, MASK)
        return text

    started = time.monotonic()
    client = None
    try:
        client = AsyncMongoClient(
            target["uri"],
            appname=f"{SLUG}-test",
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            **client_options(target),
        )
        hello = await client.admin.command("hello")
        try:
            build = await client.admin.command("buildInfo")
            version = build.get("version")
        except pe.OperationFailure:
            version = None
        description = client.topology_description
        members: list[dict[str, Any]] = []
        for address, sd in description.server_descriptions().items():
            members.append(
                {
                    "address": f"{address[0]}:{address[1]}",
                    "state": sd.server_type_name,
                    "round_trip_ms": round(sd.round_trip_time * 1000, 1) if sd.round_trip_time is not None else None,
                    "node_type": (sd.tags or {}).get("nodeType"),
                    "tags": dict(sd.tags or {}),
                }
            )
        members.sort(key=lambda m: m["address"])
        topology = description.topology_type_name
        if hello.get("msg") == "isdbgrid":
            topology = "Sharded"
        return {
            "ok": True,
            "tested_at": int(time.time() * 1000),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "server_version": version,
            "topology": topology,
            "set_name": hello.get("setName"),
            "primary": hello.get("primary"),
            "members": members,
        }
    except Exception as exc:
        return {
            "ok": False,
            "tested_at": int(time.time() * 1000),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            **explain_failure(exc, redact),
        }
    finally:
        if client is not None:
            await client.close()
