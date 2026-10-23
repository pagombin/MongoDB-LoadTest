"""HTTPS certificates: self-signed generation without openssl, loading, and reload on SIGHUP."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ballast import product

VALID_DAYS = 397
RENEW_BEFORE_DAYS = 30


def _san_entries(hosts: list[str]) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    for host in hosts:
        host = host.strip()
        if not host:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    return names


def cert_hosts(cert: x509.Certificate) -> set[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    return {str(v) for v in san.get_values_for_type(x509.DNSName)} | {str(v) for v in san.get_values_for_type(x509.IPAddress)}


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def fingerprint(cert: x509.Certificate) -> str:
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def describe(path: Path) -> dict:
    cert = load_cert(path)
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "hosts": sorted(cert_hosts(cert)),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "sha256": fingerprint(cert),
        "self_signed": cert.issuer == cert.subject,
    }


def generate_self_signed(cert_path: Path, key_path: Path, hosts: list[str]) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hosts[0] if hosts else "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"{product.NAME} self-signed"),
    ])
    now = dt.datetime.now(dt.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(_san_entries(hosts) or [x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False, data_encipherment=False,
                                     key_agreement=True, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
    )
    cert = builder.sign(key, hashes.SHA256())
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        key_tmp = key_path.with_name(key_path.name + ".tmp")
        key_tmp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(key_tmp, 0o600)
        cert_tmp = cert_path.with_name(cert_path.name + ".tmp")
        cert_tmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        os.replace(key_tmp, key_path)
        os.replace(cert_tmp, cert_path)
    finally:
        os.umask(old)
    os.chmod(cert_path, 0o644)
    return cert


def ensure_self_signed(cert_path: Path, key_path: Path, hosts: list[str], force: bool = False) -> tuple[x509.Certificate, bool]:
    """Reuse the existing certificate unless hosts changed, it is near expiry, or force is set."""
    wanted = {h.strip() for h in hosts if h.strip()}
    if not force and cert_path.exists() and key_path.exists():
        try:
            cert = load_cert(cert_path)
            expires = cert.not_valid_after_utc
            if cert_hosts(cert) == wanted and expires - dt.datetime.now(dt.UTC) > dt.timedelta(days=RENEW_BEFORE_DAYS):
                return cert, False
        except ValueError:
            pass
    return generate_self_signed(cert_path, key_path, sorted(wanted, key=lambda h: (h != "localhost", h)) or ["localhost"]), True


def server_context(cert_path: Path, key_path: Path, min_version: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3 if min_version == "1.3" else ssl.TLSVersion.TLSv1_2
    ctx.options |= ssl.OP_NO_COMPRESSION
    ctx.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!DSS")
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx


def reload_context(ctx: ssl.SSLContext, cert_path: Path, key_path: Path) -> None:
    """Load a renewed certificate into the live context; new handshakes use it, open connections are untouched."""
    ctx.load_cert_chain(str(cert_path), str(key_path))
