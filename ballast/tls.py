"""HTTPS certificates: self-signed generation and certificate inspection in pure Python, and reload on SIGHUP.

Nothing here needs openssl or the cryptography package. Self-signed certificates use RSA-2048 with
SHA-256, generated and DER-encoded by the small ASN.1 helpers below. Certificates supplied by the user
(tls.mode: files) can use any key type; only their fields are read here, the ssl module loads them.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import os
import re
import secrets
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from ballast import product

VALID_DAYS = 397
RENEW_BEFORE_DAYS = 30
RSA_BITS = 2048
UTC = dt.timezone.utc

OID_RSA = "1.2.840.113549.1.1.1"
OID_SHA256_RSA = "1.2.840.113549.1.1.11"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_CN = "2.5.4.3"
OID_O = "2.5.4.10"
OID_SAN = "2.5.29.17"
OID_BASIC = "2.5.29.19"
OID_KEY_USAGE = "2.5.29.15"
OID_EKU = "2.5.29.37"
OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"
NAME_LABELS = {OID_CN: "CN", OID_O: "O", "2.5.4.6": "C", "2.5.4.7": "L", "2.5.4.8": "ST", "2.5.4.11": "OU"}


# ---- DER encoding ----


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _len(len(value)) + value


def _int(n: int) -> bytes:
    body = n.to_bytes(n.bit_length() // 8 + 1, "big", signed=False) if n else b"\x00"
    return _tlv(0x02, body)


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytearray([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _seq(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _bitstring(data: bytes, unused: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused]) + data)


def _octets(data: bytes) -> bytes:
    return _tlv(0x04, data)


NULL = b"\x05\x00"


def _time(when: dt.datetime) -> bytes:
    when = when.astimezone(UTC)
    if when.year < 2050:
        return _tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode())
    return _tlv(0x18, when.strftime("%Y%m%d%H%M%SZ").encode())


def _name(common_name: str, organization: str) -> bytes:
    return _seq(
        _set(_seq(_oid(OID_CN), _tlv(0x0C, common_name.encode()))),
        _set(_seq(_oid(OID_O), _tlv(0x0C, organization.encode()))),
    )


# ---- DER decoding ----


@dataclass
class Node:
    tag: int
    value: bytes
    raw: bytes = b""

    def children(self) -> list[Node]:
        out, data = [], self.value
        while data:
            node, data = _read(data)
            out.append(node)
        return out


def _read(data: bytes) -> tuple[Node, bytes]:
    if len(data) < 2:
        raise ValueError("truncated DER")
    tag, first = data[0], data[1]
    pos = 2
    if first & 0x80:
        count = first & 0x7F
        if count == 0 or count > 4 or len(data) < 2 + count:
            raise ValueError("bad DER length")
        length = int.from_bytes(data[2 : 2 + count], "big")
        pos += count
    else:
        length = first
    if len(data) < pos + length:
        raise ValueError("truncated DER")
    return Node(tag, data[pos : pos + length], data[: pos + length]), data[pos + length :]


def _decode_oid(value: bytes) -> str:
    parts = [value[0] // 40, value[0] % 40]
    n = 0
    for b in value[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            parts.append(n)
            n = 0
    return ".".join(str(p) for p in parts)


def _decode_time(node: Node) -> dt.datetime:
    text = node.value.decode()
    fmt = "%y%m%d%H%M%SZ" if node.tag == 0x17 else "%Y%m%d%H%M%SZ"
    return dt.datetime.strptime(text, fmt).replace(tzinfo=UTC)


def _decode_name(node: Node) -> str:
    parts = []
    for rdn in node.children():
        for atv in rdn.children():
            oid, val = atv.children()[:2]
            key = NAME_LABELS.get(_decode_oid(oid.value), _decode_oid(oid.value))
            parts.append(f"{key}={val.value.decode(errors='replace')}")
    return ",".join(reversed(parts))


@dataclass
class Certificate:
    der: bytes
    subject: str
    issuer: str
    not_before: dt.datetime
    not_after: dt.datetime
    hosts: set[str] = field(default_factory=set)

    @property
    def not_valid_after_utc(self) -> dt.datetime:
        return self.not_after


def parse_der(der: bytes) -> Certificate:
    cert, _ = _read(der)
    tbs = cert.children()[0].children()
    idx = 1 if tbs[0].tag == 0xA0 else 0
    issuer = _decode_name(tbs[idx + 2])
    not_before, not_after = (_decode_time(n) for n in tbs[idx + 3].children())
    subject = _decode_name(tbs[idx + 4])
    hosts: set[str] = set()
    for node in tbs[idx + 6 :]:
        if node.tag != 0xA3:
            continue
        for ext in node.children()[0].children():
            parts = ext.children()
            if _decode_oid(parts[0].value) != OID_SAN:
                continue
            san, _ = _read(parts[-1].value)
            for name in san.children():
                if name.tag == 0x82:
                    hosts.add(name.value.decode())
                elif name.tag == 0x87:
                    hosts.add(str(ipaddress.ip_address(name.value)))
    return Certificate(der=cert.raw, subject=subject, issuer=issuer, not_before=not_before, not_after=not_after, hosts=hosts)


PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def load_cert(path: Path) -> Certificate:
    match = PEM_CERT.search(Path(path).read_bytes())
    if not match:
        raise ValueError(f"{path} does not contain a PEM certificate")
    return parse_der(base64.b64decode(b"".join(match.group(1).split())))


def cert_hosts(cert: Certificate) -> set[str]:
    return set(cert.hosts)


def fingerprint(cert: Certificate) -> str:
    digest = hashlib.sha256(cert.der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def describe(path: Path) -> dict:
    cert = load_cert(path)
    return {
        "subject": cert.subject,
        "issuer": cert.issuer,
        "hosts": sorted(cert.hosts),
        "not_after": cert.not_after.isoformat(),
        "sha256": fingerprint(cert),
        "self_signed": cert.issuer == cert.subject,
    }


# ---- RSA ----

SMALL_PRIMES = [p for p in range(3, 2000) if all(p % d for d in range(2, int(p**0.5) + 1))]


def _probable_prime(n: int, rounds: int = 12) -> bool:
    for p in SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, e: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if candidate % e != 1 and _probable_prime(candidate):
            return candidate


@dataclass
class RSAKey:
    n: int
    e: int
    d: int
    p: int
    q: int

    def pem(self) -> bytes:
        body = _seq(
            _int(0),
            _int(self.n),
            _int(self.e),
            _int(self.d),
            _int(self.p),
            _int(self.q),
            _int(self.d % (self.p - 1)),
            _int(self.d % (self.q - 1)),
            _int(pow(self.q, -1, self.p)),
        )
        b64 = base64.encodebytes(body).replace(b"\n", b"")
        lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
        return b"-----BEGIN RSA PRIVATE KEY-----\n" + b"\n".join(lines) + b"\n-----END RSA PRIVATE KEY-----\n"

    def public_der(self) -> bytes:
        return _seq(_seq(_oid(OID_RSA), NULL), _bitstring(_seq(_int(self.n), _int(self.e))))

    def sign_sha256(self, data: bytes) -> bytes:
        size = (self.n.bit_length() + 7) // 8
        info = _seq(_seq(_oid(OID_SHA256), NULL), _octets(hashlib.sha256(data).digest()))
        encoded = b"\x00\x01" + b"\xff" * (size - len(info) - 3) + b"\x00" + info
        m = int.from_bytes(encoded, "big")
        dp, dq, qinv = self.d % (self.p - 1), self.d % (self.q - 1), pow(self.q, -1, self.p)
        m1, m2 = pow(m, dp, self.p), pow(m, dq, self.q)
        s = m2 + self.q * ((qinv * (m1 - m2)) % self.p)
        return s.to_bytes(size, "big")


def generate_rsa(bits: int = RSA_BITS, e: int = 65537) -> RSAKey:
    while True:
        p, q = _prime(bits // 2, e), _prime(bits // 2, e)
        n = p * q
        if p != q and n.bit_length() == bits:
            phi = (p - 1) * (q - 1)
            return RSAKey(n=n, e=e, d=pow(e, -1, phi), p=max(p, q), q=min(p, q))


def pem_cert(der: bytes) -> bytes:
    b64 = base64.encodebytes(der).replace(b"\n", b"")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return b"-----BEGIN CERTIFICATE-----\n" + b"\n".join(lines) + b"\n-----END CERTIFICATE-----\n"


def _san(hosts: list[str]) -> bytes:
    names = []
    for host in hosts:
        host = host.strip()
        if not host:
            continue
        try:
            names.append(_tlv(0x87, ipaddress.ip_address(host).packed))
        except ValueError:
            names.append(_tlv(0x82, host.encode("idna")))
    return _seq(*(names or [_tlv(0x82, b"localhost")]))


def build_self_signed(key: RSAKey, hosts: list[str], now: dt.datetime | None = None) -> bytes:
    now = now or dt.datetime.now(UTC)
    name = _name(hosts[0] if hosts else "localhost", f"{product.NAME} self-signed")
    algorithm = _seq(_oid(OID_SHA256_RSA), NULL)
    extensions = _seq(
        _seq(_oid(OID_SAN), _octets(_san(hosts))),
        _seq(_oid(OID_BASIC), _tlv(0x01, b"\xff"), _octets(_seq())),
        _seq(_oid(OID_EKU), _octets(_seq(_oid(OID_SERVER_AUTH)))),
        _seq(_oid(OID_KEY_USAGE), _tlv(0x01, b"\xff"), _octets(_bitstring(b"\xa0", 5))),
    )
    tbs = _seq(
        _tlv(0xA0, _int(2)),
        _int(secrets.randbits(127) | (1 << 126)),
        algorithm,
        name,
        _seq(_time(now - dt.timedelta(minutes=5)), _time(now + dt.timedelta(days=VALID_DAYS))),
        name,
        key.public_der(),
        _tlv(0xA3, extensions),
    )
    return _seq(tbs, algorithm, _bitstring(key.sign_sha256(tbs)))


def generate_self_signed(cert_path: Path, key_path: Path, hosts: list[str]) -> Certificate:
    key = generate_rsa()
    der = build_self_signed(key, hosts)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        key_tmp = key_path.with_name(key_path.name + ".tmp")
        key_tmp.write_bytes(key.pem())
        os.chmod(key_tmp, 0o600)
        cert_tmp = cert_path.with_name(cert_path.name + ".tmp")
        cert_tmp.write_bytes(pem_cert(der))
        os.replace(key_tmp, key_path)
        os.replace(cert_tmp, cert_path)
    finally:
        os.umask(old)
    os.chmod(cert_path, 0o644)
    return parse_der(der)


def ensure_self_signed(cert_path: Path, key_path: Path, hosts: list[str], force: bool = False) -> tuple[Certificate, bool]:
    """Reuse the existing certificate unless hosts changed, it is near expiry, or force is set."""
    wanted = {h.strip() for h in hosts if h.strip()}
    if not force and cert_path.exists() and key_path.exists():
        try:
            cert = load_cert(cert_path)
            if cert.hosts == wanted and cert.not_after - dt.datetime.now(UTC) > dt.timedelta(days=RENEW_BEFORE_DAYS):
                return cert, False
        except (ValueError, IndexError):
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
