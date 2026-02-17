from __future__ import annotations

from mongo_load_tester.config import MongoConnectionConfig
from mongo_load_tester.engine import client as client_module


def test_build_client_does_not_force_tls_when_unset(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(uri: str, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(client_module, "MongoClient", fake_client)
    config = MongoConnectionConfig(uri="mongodb+srv://cluster0.example.mongodb.net")

    client_module.build_client(config)

    assert captured["uri"] == "mongodb+srv://cluster0.example.mongodb.net"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "tls" not in kwargs


def test_build_client_uses_explicit_tls_override(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_client(uri: str, **kwargs):
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(client_module, "MongoClient", fake_client)
    config = MongoConnectionConfig(uri="mongodb://localhost:27017", tls=False)

    client_module.build_client(config)

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs.get("tls") is False
