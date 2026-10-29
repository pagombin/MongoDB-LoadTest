# Ballast

Ballast is a load testing tool for MongoDB clusters. It sends a steady, controlled workload, and while it runs you resize nodes, add or remove members, or trigger a failover. Ballast records what the workload experienced, second by second: throughput, latency percentiles from intended start, every failed operation by error class, topology changes seen by the driver, and the longest stretch without a successful write. For each scale operation it reports how long the disruption lasted and how long the workload took to recover.

![Live run view](docs/design/after/03-live.png)

It runs as a normal user with no root access, serves its UI over HTTPS, and makes no network requests other than to your cluster. It also measures Atlas analytics and read-only nodes: reads can be routed by node type, and the report shows what each member served, its replication lag, and a separate read gap for each route.

## Quick start

### 1. Install

Pick the first option your server allows. None needs root or `sudo`.

**A. Release bundle.** Brings its own Python, so nothing is installed on the host. Build the bundles on any machine with internet access (or use ones your team has published), then copy one to the server:

```
git clone https://github.com/pagombin/MongoDB-LoadTest.git ballast-src
cd ballast-src
make build
make release
scp dist/ballast-1.0.0-linux-amd64.tar.gz you@server:
```

On the server:

```
tar -xzf ballast-1.0.0-linux-amd64.tar.gz
cd ballast-1.0.0
```

Bundles are built for `linux-amd64`, `linux-arm64`, `darwin-arm64` and `darwin-amd64`. Check them with `sha256sum -c SHA256SUMS --ignore-missing`.

**B. The server's own Python 3.10+ and pip.** Ballast needs only `aiohttp`, `pydantic` and `pymongo`. It works without `python3-venv`, `cryptography` or `openssl`:

```
git clone https://github.com/pagombin/MongoDB-LoadTest.git ballast-src
sh ballast-src/scripts/install.sh --prefix ~/ballast
cd ~/ballast
```

The installer uses a virtual environment if it can, otherwise `pip --target`. It installs optional packages (PyYAML, Faker) one at a time; if one fails, only the feature that needs it is off. Add `--python /path/to/python3` to choose the interpreter, or `--minimal` to skip the optional packages.

**C. No internet on the server.** Build an offline kit on a connected machine, for the server's Python version and architecture:

```
sh scripts/make-wheelhouse.sh --python-version 3.10 --arch x86_64
scp dist/ballast-1.0.0-offline-py310-x86_64.tar.gz you@server:
```

On the server:

```
tar -xzf ballast-1.0.0-offline-py310-x86_64.tar.gz
sh ballast-1.0.0-offline/scripts/install.sh --wheelhouse ballast-1.0.0-offline/wheelhouse --prefix ~/ballast
cd ~/ballast
```

**If `make build` or a script says "No such file or directory" or `$'\r': command not found`,** the files have Windows line endings. This happens when they are copied from a Windows machine, unpacked from a ZIP made there, or cloned with `core.autocrlf=true`. Check with `file scripts/setup-runtime.sh`; it reports "with CRLF line terminators". Clone again, or convert the copy you have:

```
grep -rlI "$(printf '\r')" --exclude-dir=.git . | xargs sed -i 's/\r$//'
chmod +x bin/ballast bin/ballastctl scripts/*.sh
```

Fresh clones of this repository always get Unix line endings.

### 2. Check the host

```
bin/ballast doctor
bin/ballast doctor --uri 'mongodb+srv://user:password@cluster0.example.mongodb.net/'
```

It checks Python, the required and optional packages, that worker processes can start, the directories and the port, and optionally the cluster. For anything missing it prints the exact command to fix it.

### 3. Start and open the UI

Ballast connects to your cluster from the server it runs on. Your browser only talks to Ballast, so your laptop never needs to reach the cluster. Choose how your browser reaches Ballast:

**Option A: listen on the server's network address.** Use this when your laptop can reach the server directly, but not the cluster. Create an access token, because anyone who can reach the port can otherwise start load against your clusters:

```
mkdir -p config
head -c 24 /dev/urandom | base64 > config/token
chmod 600 config/token
cat > config/ballast.yaml <<'YAML'
server:
  listen: 0.0.0.0:8443
  access_token_file: config/token
YAML
bin/ballastctl start
```

Startup prints a URL for each of the server's addresses and the certificate's SHA-256 fingerprint:

```
started: https://127.0.0.1:8443 (server pid 453, supervisor pid 452)
also at: https://db-tools-1:8443
also at: https://10.0.0.5:8443
certificate SHA-256: 18:D2:E1:57:...
```

Open `https://<server address>:8443` from your laptop, check that the fingerprint matches before you accept the certificate, then enter the token from `config/token`. The browser remembers it for the session.

- To listen on one address only, use it instead of `0.0.0.0`, for example `listen: 10.0.0.5:8443`.
- The self-signed certificate covers the server's host name and the addresses it can see. A public IP that is mapped onto the server (an AWS Elastic IP, for example) or a DNS name is not visible from the server. Add those to `tls.self_signed_hosts`, for example `self_signed_hosts: ["localhost", "127.0.0.1", "203.0.113.10"]`.
- Open port 8443 in the server's firewall and in any cloud security group. On Ubuntu with ufw, `sudo ufw allow 8443/tcp` needs an administrator.
- For a one-off test, `bin/ballastctl start --listen 0.0.0.0:8443` does the same without editing the file. It warns that no token is set.

**Option B: an SSH tunnel.** Use this when only SSH is open to the server. Start Ballast as installed (`bin/ballastctl start`; it listens on `127.0.0.1:8443`), then run this on your laptop:

```
ssh -L 8443:127.0.0.1:8443 you@server
```

Then open `https://localhost:8443`. Only browser traffic goes through the tunnel; Ballast still connects to the cluster from the server.

### 4. Run a test

1. **Targets**: add your connection string and press **Test connection**. It lists every member, with its Atlas node type if it has one.
2. **New run**: pick the target, a database for benchmark data, and a workload. Under **Where reads go**, choose the primary, any secondary, **Atlas analytics nodes** or **Atlas read-only nodes**. Optionally set guardrails and pass/fail criteria, then start.
3. Mark the scale operation. Press **M** in the live view, or run this on the server next to your scale command:

   ```
   bin/ballastctl mark "resize started"
   bin/ballastctl mark "resize finished"
   ```

4. Stop the run (**S**). The report shows each marked window: failures by class, write and read gaps (one per read route), primary and member changes, p99 before, during and after, recovery time, and what each member served.

## Without the UI

Run one workload from a script or CI job. The exit code is 0 for pass, 1 when a pass/fail criterion failed, 4 when a guardrail stopped the run:

```
bin/ballast run --uri "$MONGODB_URI" --profile ci.yaml --duration 300 --json result.json --markdown result.md
```

Other commands: `bin/ballastctl status | stop | restart | logs | health | backup | restore | autostart enable`. `GET /metrics` serves Prometheus metrics, and `notifications.webhook_url` posts run events to Slack or any JSON endpoint.

## Documentation

| Document | Contents |
|---|---|
| `docs/INSTALL.md` | All install routes in detail, upgrade, uninstall |
| `docs/CONFIGURATION.md` | Every configuration key, its default, and its environment variable |
| `docs/OPERATIONS.md` | Start, stop, logs, autostart, certificates, doctor, backup, metrics, webhooks, CI runs, troubleshooting |
| `docs/SCALE-TESTING.md` | Running a workload around scale operations, including Atlas analytics and read-only nodes, and reading the report |
| `docs/METHODOLOGY.md` | How latency is measured, percentiles, coordinated omission, members and routes, client-bound runs |
| `docs/ENHANCEMENTS.md` | Changes since the first rebuild, each with how it was checked |
| `docs/VERIFICATION.md` | Test results, soak test, fresh-host test |
| `docs/DECISIONS.md` | Design decisions and their trade-offs |
| `docs/AUDIT.md` | Audit of the previous version of this tool |
| `docs/LEGACY.md` | The original dashboard, still available behind a flag |

## Development

```
make build
make test
make lint
```

`make build` puts a private Python runtime in `runtime/`; nothing is installed system-wide. `make release` builds the bundles. Integration tests need a local replica set: `scripts/dev-replset.sh` starts a plain one, and `scripts/dev-replset-atlas.sh` starts one tagged like an Atlas cluster with analytics and read-only nodes. Both need Docker.

## License

Copyright Paul Agombin. All rights reserved. See `LICENSE`. Third-party components are listed in `THIRD_PARTY_NOTICES` inside each bundle.
