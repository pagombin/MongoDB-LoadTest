"""Checks that `ballast gen-cert` (new certificate + SIGHUP) keeps open connections and serves the new certificate to new ones."""

import hashlib
import http.client
import ssl
import subprocess
import sys

host, port = "127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8443
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def fingerprint(conn):
    return hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()[:16]


held = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
held.request("GET", "/api/health")
print("held connection, first request:", held.getresponse().read()[:30], "cert", fingerprint(held))
before = fingerprint(held)
out = subprocess.run(["bin/ballast", "gen-cert"], capture_output=True, text=True)
print(out.stdout.strip().splitlines()[-1])
import time

time.sleep(1)
held.request("GET", "/api/health")
print("held connection after reload:", held.getresponse().status, "(same connection, cert", fingerprint(held), ")")
fresh = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
fresh.request("GET", "/api/health")
fresh.getresponse().read()
after = fingerprint(fresh)
print("new connection cert", after)
print("RESULT:", "PASS" if after != before and fingerprint(held) == before else "FAIL")
