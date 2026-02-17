"""MongoDB load-testing toolkit with live dashboard support."""

from .compat import ensure_supported_python

ensure_supported_python()

from .config import AppConfig, load_config

__all__ = ["AppConfig", "load_config"]
