import datetime as dt
import socket
import ssl
import threading

from ballast import tls


def test_self_signed_certificate_works_for_a_tls_handshake(tmp_path):
    cert_path, key_path = tmp_path / "c.crt", tmp_path / "c.key"
    cert = tls.generate_self_signed(cert_path, key_path, ["localhost", "127.0.0.1", "ballast.example"])
    assert cert.hosts == {"localhost", "127.0.0.1", "ballast.example"}
    assert cert.subject == cert.issuer == "O=Ballast self-signed,CN=localhost"
    assert dt.timedelta(days=396) < cert.not_after - dt.datetime.now(tls.UTC) <= dt.timedelta(days=397)
    assert oct(key_path.stat().st_mode)[-3:] == "600"

    server_ctx = tls.server_context(cert_path, key_path, "1.2")
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as s:
            s.sendall(b"ok")

    thread = threading.Thread(target=serve)
    thread.start()
    with socket.create_connection(("127.0.0.1", port)) as raw, client_ctx.wrap_socket(raw, server_hostname="ballast.example") as s:
        assert s.recv(2) == b"ok"
        assert tls.fingerprint(cert) == ":".join(f"{b:02X}" for b in __import__("hashlib").sha256(s.getpeercert(True)).digest())
    thread.join()
    listener.close()


def test_describe_and_reuse(tmp_path):
    cert_path, key_path = tmp_path / "c.crt", tmp_path / "c.key"
    first, created = tls.ensure_self_signed(cert_path, key_path, ["localhost"])
    assert created
    again, created = tls.ensure_self_signed(cert_path, key_path, ["localhost"])
    assert not created and tls.fingerprint(again) == tls.fingerprint(first)
    _, created = tls.ensure_self_signed(cert_path, key_path, ["localhost", "10.0.0.5"])
    assert created
    info = tls.describe(cert_path)
    assert info["hosts"] == ["10.0.0.5", "localhost"] and info["self_signed"]


def test_parses_certificates_made_elsewhere(tmp_path):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        return
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "db.example")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=3000))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("db.example")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "x.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    info = tls.describe(path)
    assert info["hosts"] == ["db.example"] and info["subject"] == "CN=db.example"
    assert info["not_after"][:10] == cert.not_valid_after_utc.isoformat()[:10]
    ours = tmp_path / "ours.crt"
    tls.generate_self_signed(ours, tmp_path / "ours.key", ["localhost"])
    parsed = x509.load_pem_x509_certificate(ours.read_bytes())
    parsed.public_key().verify(
        parsed.signature,
        parsed.tbs_certificate_bytes,
        __import__("cryptography.hazmat.primitives.asymmetric.padding", fromlist=["PKCS1v15"]).PKCS1v15(),
        hashes.SHA256(),
    )
