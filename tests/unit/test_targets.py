from pymongo import errors as pe

from ballast.targets import MASK, explain_failure, merge_password, parse, redact_uri, secrets_in


def test_redact_hides_password_and_sensitive_options():
    uri = "mongodb+srv://bench:S3cr%40t@cluster0.example.net/db?retryWrites=true&authMechanismProperties=AWS_SESSION_TOKEN:abc123"
    red = redact_uri(uri)
    assert "S3cr" not in red and "abc123" not in red
    assert red.startswith(f"mongodb+srv://bench:{MASK}@cluster0.example.net/db?")
    assert "retryWrites=true" in red


def test_parse_hosts_and_srv():
    p = parse("mongodb://u:p@h1:1,h2:2/?replicaSet=rs0")
    assert p["hosts"] == ["h1:1", "h2:2"] and p["srv"] is False and p["password"] == "p"


def test_secrets_include_encoded_and_decoded_forms():
    s = secrets_in("mongodb://u:p%40ss@h/")
    assert "p@ss" in s and "p%40ss" in s


def test_merge_password_keeps_stored_when_masked():
    stored = "mongodb://u:real%21pw@h1:1/"
    assert merge_password(f"mongodb://u:{MASK}@h2:2/", stored) == "mongodb://u:real%21pw@h2:2/"
    assert merge_password("mongodb://u:new@h2:2/", stored) == "mongodb://u:new@h2:2/"


def test_failure_explanations_name_the_cause():
    redact = lambda s: s
    assert explain_failure(pe.ConfigurationError("The DNS query name does not exist: _mongodb._tcp.x."), redact)["cause"] == "dns_srv"
    assert explain_failure(pe.OperationFailure("Authentication failed.", code=18), redact)["cause"] == "auth"
    assert (
        explain_failure(pe.ServerSelectionTimeoutError("h:1: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), redact)["cause"]
        == "tls"
    )
    r = explain_failure(pe.ServerSelectionTimeoutError("h:1: [Errno 111] Connection refused"), redact)
    assert r["cause"] == "server_selection" and r["title"] == "Connection refused"
    assert explain_failure(pe.InvalidURI("bad"), redact)["cause"] == "invalid_uri"
