"""Configuration: defaults, then the YAML file, then BALLAST_* environment variables, then flags.

Relative paths resolve against the install directory (BALLAST_HOME, set by
bin/ballast), never the current working directory.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo

from ballast import miniyaml, product

SECRET_KEYS = {"access_token", "password"}
RESERVED_ENV = {"home", "config", "supervised", "foreground"}
PERMISSION_WARNINGS: list[str] = []


def contrast_with_white(hex_color: str) -> float:
    """WCAG contrast ratio of a #RRGGBB color against white."""
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 1.05 / (luminance + 0.05)


class ConfigError(Exception):
    pass


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(Section):
    listen: str = "127.0.0.1:8443"
    public_url: str = ""
    trust_proxy_headers: bool = False
    access_token: str = ""
    access_token_env: str = ""
    access_token_file: str = ""
    max_body_kb: int = Field(default=1024, ge=16, le=65536)
    shutdown_timeout_seconds: int = Field(default=30, ge=1, le=600)

    @field_validator("listen")
    @classmethod
    def _listen(cls, value: str) -> str:
        host, port = split_listen(value)
        if port < 1024:
            raise ValueError(
                f"port {port} is below 1024; ports below 1024 need root, which {product.NAME} does not use. Pick a port such as 8443"
            )
        if port > 65535:
            raise ValueError("port must be at most 65535")
        return value

    @field_validator("public_url")
    @classmethod
    def _public(cls, value: str) -> str:
        if value and not re.match(r"^https?://[^/\s]+(/.*)?$", value):
            raise ValueError("public_url must look like https://host[:port][/path]")
        return value.rstrip("/")


class TlsConfig(Section):
    mode: Literal["off", "files", "self-signed"] = "self-signed"
    cert_file: str = "certs/server.crt"
    key_file: str = "certs/server.key"
    self_signed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    min_version: Literal["1.2", "1.3"] = "1.2"
    redirect_http_listen: str = ""

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_off(cls, value):
        return "off" if value is False else value

    @field_validator("redirect_http_listen")
    @classmethod
    def _redirect(cls, value: str) -> str:
        if value:
            _, port = split_listen(value)
            if port < 1024:
                raise ValueError(f"port {port} is below 1024; pick a port such as 8080")
        return value


class PathsConfig(Section):
    data_dir: str = "data"
    log_dir: str = "logs"
    run_dir: str = "run"
    min_free_disk_mb: int = Field(default=200, ge=0)


class LoggingConfig(Section):
    level: Literal["debug", "info", "warning", "error"] = "info"
    format: Literal["json", "text"] = "json"
    max_size_mb: int = Field(default=100, ge=1, le=10_000)
    max_files: int = Field(default=10, ge=1, le=1000)


class RetentionConfig(Section):
    runs_max_age_days: int = Field(default=0, ge=0)
    runs_max_count: int = Field(default=0, ge=0)


class BrandingConfig(Section):
    product_name: str = product.NAME
    logo_file: str = ""
    accent_color: str = ""

    @field_validator("accent_color")
    @classmethod
    def _color(cls, value: str) -> str:
        if value and not re.match(r"^#[0-9a-fA-F]{6}$", value):
            raise ValueError("accent_color must be a hex color like #0B6E77")
        if value and contrast_with_white(value) < 4.5:
            PERMISSION_WARNINGS.append(
                f"branding.accent_color {value} has a contrast ratio of {contrast_with_white(value):.1f}:1 against white; text on buttons needs at least 4.5:1. Choose a darker color."
            )
        return value

    @field_validator("product_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 40:
            raise ValueError("product_name must be 1-40 characters")
        return value


class SupervisorConfig(Section):
    restart_backoff_initial_seconds: float = Field(default=1, gt=0)
    restart_backoff_max_seconds: float = Field(default=60, gt=0)
    max_restarts: int = Field(default=5, ge=1)
    restart_window_seconds: int = Field(default=120, ge=1)
    stop_timeout_seconds: int = Field(default=45, ge=1)


class FeaturesConfig(Section):
    legacy_dashboard: bool = False


class TargetPreset(Section):
    name: str
    uri: str
    password: str = ""
    password_env: str = ""
    password_file: str = ""
    tls: Literal["auto", "on", "off"] = "auto"
    tls_ca_file: str | None = None
    tls_certificate_key_file: str | None = None
    tls_allow_invalid_hostnames: bool = False


class Config(Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    tls: TlsConfig = Field(default_factory=TlsConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    branding: BrandingConfig = Field(default_factory=BrandingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    targets: list[TargetPreset] = Field(default_factory=list)

    @model_validator(mode="after")
    def _secrets(self) -> Config:
        sources = [bool(self.server.access_token), bool(self.server.access_token_env), bool(self.server.access_token_file)]
        if sum(sources) > 1:
            raise ValueError("set only one of server.access_token, server.access_token_env, server.access_token_file")
        return self


SECTIONS = tuple(Config.model_fields)


def split_listen(value: str) -> tuple[str, int]:
    match = re.match(r"^\[?([^\]]*?)\]?:(\d+)$", value.strip())
    if not match:
        raise ValueError(f"'{value}' must be host:port, for example 127.0.0.1:8443")
    host = match.group(1)
    if host not in ("", "localhost"):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.match(r"^[A-Za-z0-9.-]+$", host):
                raise ValueError(f"'{host}' is not a valid host") from None
    return host or "0.0.0.0", int(match.group(2))  # noqa: S104


def install_home() -> Path:
    env = os.environ.get("BALLAST_HOME")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


try:
    import yaml as _pyyaml

    YAML_ERRORS: tuple[type[Exception], ...] = (_pyyaml.YAMLError, miniyaml.YAMLError)
    yaml_load: Callable[[str], Any] = _pyyaml.safe_load
    YAML_READER = "PyYAML"
except ImportError:
    YAML_ERRORS = (miniyaml.YAMLError,)
    yaml_load = miniyaml.safe_load
    YAML_READER = "built-in"


def _parse_scalar(raw: str) -> Any:
    try:
        return yaml_load(raw)
    except YAML_ERRORS:
        return raw


def _section_fields(section: str) -> dict[str, FieldInfo]:
    model = Config.model_fields[section].annotation
    assert model is not None
    return model.model_fields


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    environ = os.environ if environ is None else environ
    out: dict[str, dict[str, Any]] = {}
    for name, raw in environ.items():
        if not name.startswith(product.ENV_PREFIX):
            continue
        rest = name[len(product.ENV_PREFIX) :].lower()
        if rest in RESERVED_ENV or rest.startswith("test_"):
            continue
        section, _, key = rest.partition("_")
        if section not in SECTIONS or not key or section == "targets":
            raise ConfigError(
                f"unknown environment variable {name}: expected {product.ENV_PREFIX}<SECTION>_<KEY> with section one of {', '.join(s for s in SECTIONS if s != 'targets')}"
            )
        fields = _section_fields(section)
        if key not in fields:
            raise ConfigError(f"unknown environment variable {name}: {section} has no key '{key}'")
        value = _parse_scalar(raw) if fields[key].annotation is not str else raw
        out.setdefault(section, {})[key] = value
    return out


def flag_overrides(pairs: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if "=" not in pair or "." not in pair.split("=", 1)[0]:
            raise ConfigError(f"--set {pair!r} must look like section.key=value")
        dotted, raw = pair.split("=", 1)
        section, key = dotted.split(".", 1)
        if section not in SECTIONS or section == "targets":
            raise ConfigError(f"--set {pair!r}: unknown section '{section}'")
        fields = _section_fields(section)
        if key not in fields:
            raise ConfigError(f"--set {pair!r}: {section} has no key '{key}'")
        out.setdefault(section, {})[key] = _parse_scalar(raw) if fields[key].annotation is not str else raw
    return out


def _format_error(exc: ValidationError, origin: str) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = "unknown key"
        lines.append(f"  {loc}: {msg}")
    return f"invalid configuration ({origin}):\n" + "\n".join(lines)


class Loaded:
    def __init__(self, config: Config, home: Path, path: Path | None, sources: dict[str, str]):
        self.config = config
        self.home = home
        self.path = path
        self.sources = sources

    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.home / p)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.config.paths.data_dir)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.config.paths.log_dir)

    @property
    def run_dir(self) -> Path:
        return self.resolve(self.config.paths.run_dir)

    def access_token(self) -> str:
        s = self.config.server
        if s.access_token:
            return s.access_token
        if s.access_token_env:
            value = os.environ.get(s.access_token_env, "")
            if not value:
                raise ConfigError(f"server.access_token_env names {s.access_token_env}, which is not set")
            return value
        if s.access_token_file:
            return read_secret_file(self.resolve(s.access_token_file), "server.access_token_file").strip()
        return ""

    def target_presets(self) -> list[dict]:
        out = []
        for preset in self.config.targets:
            password = preset.password
            if preset.password_env:
                password = os.environ.get(preset.password_env, "")
                if not password:
                    raise ConfigError(f"targets[{preset.name}].password_env names {preset.password_env}, which is not set")
            elif preset.password_file:
                password = read_secret_file(self.resolve(preset.password_file), f"targets[{preset.name}].password_file").strip()
            uri = preset.uri
            if password:
                from urllib.parse import quote

                encoded = quote(password, safe="")
                uri = re.sub(
                    r"^(mongodb(?:\+srv)?://)([^:@/]+)(?::[^@/]*)?@",
                    rf"\g<1>\g<2>:{encoded}@",
                    uri,
                    count=1,
                )
            out.append(
                {
                    "name": preset.name,
                    "uri": uri,
                    "tls": preset.tls,
                    "tls_ca_file": preset.tls_ca_file,
                    "tls_certificate_key_file": preset.tls_certificate_key_file,
                    "tls_allow_invalid_hostnames": preset.tls_allow_invalid_hostnames,
                }
            )
        return out

    def redacted(self) -> dict:
        data = self.config.model_dump(mode="json")
        if data["server"].get("access_token"):
            data["server"]["access_token"] = "****"  # noqa: S105
        from ballast.targets import redact_uri

        for preset in data.get("targets", []):
            if preset.get("password"):
                preset["password"] = "****"  # noqa: S105
            preset["uri"] = redact_uri(preset["uri"])
        return data


def read_secret_file(path: Path, key: str) -> str:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        raise ConfigError(f"{key}: file {path} does not exist") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        PERMISSION_WARNINGS.append(f"{key}: {path} is readable by group or others (mode {oct(mode & 0o777)}); run chmod 600 {path}")
    return path.read_text(encoding="utf-8")


def load(
    path: str | Path | None = None, flags: list[str] | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None
) -> Loaded:
    home = home or install_home()
    environ = os.environ if environ is None else environ
    config_path: Path | None = None
    if path:
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()
        if not config_path.exists():
            raise ConfigError(f"configuration file {config_path} does not exist")
    elif environ.get("BALLAST_CONFIG"):
        config_path = Path(environ["BALLAST_CONFIG"])
        if not config_path.is_absolute():
            config_path = home / config_path
        if not config_path.exists():
            raise ConfigError(f"BALLAST_CONFIG points to {config_path}, which does not exist")
    elif (home / "config" / "ballast.yaml").exists():
        config_path = home / "config" / "ballast.yaml"
    raw: dict = {}
    sources: dict[str, str] = {}
    if config_path is not None:
        try:
            raw = yaml_load(config_path.read_text(encoding="utf-8")) or {}
        except YAML_ERRORS as exc:
            raise ConfigError(f"{config_path} is not valid YAML: {exc}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{config_path} must contain a mapping of sections")
        try:
            Config.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(_format_error(exc, str(config_path))) from None
        for section, values in raw.items():
            if isinstance(values, dict):
                for key in values:
                    sources[f"{section}.{key}"] = "file"
            else:
                sources[section] = "file"
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    for origin, overrides in (("env", env_overrides(environ)), ("flag", flag_overrides(flags or []))):
        for section, values in overrides.items():
            merged.setdefault(section, {}).update(values)
            for key in values:
                sources[f"{section}.{key}"] = origin
    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_error(exc, "after environment and flag overrides")) from None
    return Loaded(config, home, config_path, sources)


def effective_with_sources(loaded: Loaded) -> list[dict]:
    rows = []
    data = loaded.redacted()
    for section, values in data.items():
        if isinstance(values, dict):
            for key, value in values.items():
                rows.append({"key": f"{section}.{key}", "value": value, "source": loaded.sources.get(f"{section}.{key}", "default")})
        else:
            rows.append({"key": section, "value": values, "source": loaded.sources.get(section, "default")})
    return rows
