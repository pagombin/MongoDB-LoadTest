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
