"""Listening on all interfaces: certificate hosts, printed URLs and exposure warnings."""

import pytest

from ballast import server
from ballast.config import load


@pytest.fixture(autouse=True)
def fake_addresses(monkeypatch):
    monkeypatch.setattr(server, "local_addresses", lambda: ["db-tools-1", "10.0.0.5", "2001:db8::5"])


def loaded(tmp_path, *flags):
    return load(None, list(flags), environ={}, home=tmp_path)


def test_wildcard_listen_puts_host_addresses_in_certificate(tmp_path):
    hosts = server.certificate_hosts(loaded(tmp_path, "server.listen=0.0.0.0:8443"))
    assert hosts == ["localhost", "127.0.0.1", "db-tools-1", "10.0.0.5", "2001:db8::5"]


def test_specific_listen_address_only_adds_itself(tmp_path):
    hosts = server.certificate_hosts(loaded(tmp_path, "server.listen=10.0.0.5:8443"))
    assert hosts == ["localhost", "127.0.0.1", "10.0.0.5"]


def test_open_urls(tmp_path):
    assert server.open_urls(loaded(tmp_path), "https") == ["https://127.0.0.1:8443"]
    assert server.open_urls(loaded(tmp_path, "server.listen=0.0.0.0:9443"), "https") == [
        "https://127.0.0.1:9443",
        "https://db-tools-1:9443",
        "https://10.0.0.5:9443",
    ]
    assert server.open_urls(loaded(tmp_path, "server.listen=[::]:8443"), "https")[-1] == "https://[2001:db8::5]:8443"
    public = loaded(tmp_path, "server.listen=0.0.0.0:8443", "server.public_url=https://ballast.example.com")
    assert server.open_urls(public, "https") == ["https://ballast.example.com"]


def test_exposure_warnings(tmp_path):
    assert server.exposure_warnings(loaded(tmp_path)) == []
    open_port = server.exposure_warnings(loaded(tmp_path, "server.listen=0.0.0.0:8443"))
    assert len(open_port) == 1 and "no access token" in open_port[0]
    assert server.exposure_warnings(loaded(tmp_path, "server.listen=0.0.0.0:8443", "server.access_token=s3cret-token-value")) == []
    plain = server.exposure_warnings(
        loaded(tmp_path, "server.listen=0.0.0.0:8443", "server.access_token=s3cret-token-value", "tls.mode=off")
    )
    assert len(plain) == 1 and "unencrypted" in plain[0]


def test_real_local_addresses_exclude_loopback(monkeypatch):
    monkeypatch.undo()
    found = server.local_addresses()
    assert all(not a.startswith("127.") and a not in ("localhost", "::1") for a in found)


def test_token_is_generated_only_when_listening_beyond_loopback(tmp_path):
    assert loaded(tmp_path).auto_token_path() is None
    assert loaded(tmp_path, "server.listen=127.0.0.1:8443").ensure_auto_token() is None
    assert loaded(tmp_path, "server.listen=0.0.0.0:8443", "server.auto_access_token=false").auto_token_path() is None
    assert loaded(tmp_path, "server.listen=0.0.0.0:8443", "server.access_token=s3cret-token-value").auto_token_path() is None
    wide = loaded(tmp_path, "server.listen=0.0.0.0:8443")
    assert wide.auto_token_path() == tmp_path / "config" / "token"
    assert wide.access_token() == ""


def test_generated_token_is_private_kept_and_used(tmp_path):
    wide = loaded(tmp_path, "server.listen=10.0.0.5:8443")
    path = wide.ensure_auto_token()
    assert path == tmp_path / "config" / "token"
    assert path.stat().st_mode & 0o777 == 0o600
    token = wide.access_token()
    assert len(token) >= 32
    assert wide.ensure_auto_token() is None, "an existing token is kept across restarts"
    assert loaded(tmp_path, "server.listen=10.0.0.5:8443").access_token() == token
    assert server.exposure_warnings(wide) == []
    path.write_text("\n")
    assert wide.ensure_auto_token() == path, "an empty token file is replaced"
    assert wide.access_token() not in ("", token)


def test_an_existing_token_file_is_reused(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "token").write_text("my-own-token-value\n")
    (tmp_path / "config" / "token").chmod(0o600)
    wide = loaded(tmp_path, "server.listen=0.0.0.0:8443")
    assert wide.ensure_auto_token() is None
    assert wide.access_token() == "my-own-token-value"
