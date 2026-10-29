# Operating Ballast

All commands run from the install directory as the user who owns it.

## Start, stop, status

```
bin/ballastctl start
bin/ballastctl status
bin/ballastctl stop
bin/ballastctl restart
```

`start` runs Ballast in the background under a supervisor, detached from your terminal, so it keeps running after you log out. It waits until the server is ready and prints the URL and certificate fingerprint. Running `start` again while it is running prints `already running` and does nothing.

`stop` sends a graceful stop. An active run is stopped, its in-flight operations get the drain timeout, and its data and report are saved. If the server has not exited after `supervisor.stop_timeout_seconds` plus 5 seconds, `stop` kills it and says so.

`status` prints the state and exits with:

| Exit code | Meaning |
|---|---|
| 0 | running |
| 1 | not running, but a stale PID file was found |
| 3 | stopped |
| 4 | the supervisor is running but the server is restarting |

`health` asks the running server for `/api/health` over the loopback address and exits 0 if it answered, 1 if not:

```
bin/ballastctl health && echo up
```

The supervisor restarts the server if it crashes, waiting 1, 2, 4 seconds and so on up to 60. If the server crashes more than 5 times within 2 minutes the supervisor gives up and logs why. A server that exits because of a configuration error or a failed startup check is not restarted.

PID files live in `run/`: `supervisor.pid` and `ballast.pid`. Stale ones (the process is gone) are removed automatically.

## Logs

```
bin/ballastctl logs
bin/ballastctl logs -f
bin/ballastctl logs --supervisor
bin/ballastctl logs --console
```

| File | Contents |
|---|---|
| `logs/ballast.log` | The server: requests that changed something, runs, markers, warnings, errors. JSON lines by default. |
| `logs/supervisor.log` | Starts, crashes, restarts. |
| `logs/console.log` | Anything the server printed to its console, including startup failures. |

Files rotate at `logging.max_size_mb` and `logging.max_files` are kept. Ballast does the rotation itself; `logrotate` is not needed. Connection string passwords and the access token are replaced with `****` before anything is written.

For support, **System > Download log bundle** produces a zip with the logs, the configuration with secrets redacted, version information and the run list.

## Markers from scripts

`bin/ballastctl mark` adds a marker to the active run, timestamped when the command runs:

```
bin/ballastctl mark "resize started"
bin/ballastctl mark "resize finished" --note "M40 to M50"
bin/ballastctl mark "maintenance" --phase begin
```

It talks to the local server over the loopback address, checks that the certificate is the one in `certs/`, and sends the access token if one is configured. It exits non-zero if there is no active run or the server is down. The same thing over HTTP:

```
curl --cacert certs/server.crt -H 'X-Ballast-Request: 1' -H 'Content-Type: application/json' \
  -d '{"label":"resize started"}' https://127.0.0.1:8443/api/runs/active/markers
```

## Autostart after reboot

```
bin/ballastctl autostart enable
bin/ballastctl autostart disable
```

`enable` tries, in order:

1. A systemd user unit, if `systemctl --user` works for you. It writes `~/.config/systemd/user/ballast-<id>.service` and enables it. User services only run while you are logged in unless lingering is on; the command checks this and tells you. Turning it on is `loginctl enable-linger <you>`, which on many hosts needs an administrator.
2. A crontab `@reboot` entry, if `crontab` works for you.
3. Otherwise it prints instructions instead of failing silently.

`status` shows which method is active. These are the only files Ballast writes outside its install directory.

## Listening on the network

By default Ballast listens on `127.0.0.1` only. Two ways to reach it from another machine:

An SSH tunnel, with no configuration change:

```
ssh -L 8443:127.0.0.1:8443 you@server
```

Then open `https://localhost:8443`.

Through a jump server, or where SSH port forwarding is disabled, see `docs/REMOTE-ACCESS.md`.

Or listen on all interfaces. Use this when your laptop can reach the server but not the cluster: Ballast connects to the cluster from the server, and your browser only talks to Ballast. Put in `config/ballast.yaml`:

```yaml
server:
  listen: 0.0.0.0:8443
```

When Ballast listens beyond loopback and no token is configured, it creates one at startup in `config/token` (mode 600) and keeps it across restarts. `bin/ballastctl start` prints it, `bin/ballastctl token` shows it again, and `bin/ballastctl token --new` replaces it; restart to apply the new one. To use your own token, put it in `config/token` before the first start, or set `server.access_token`, `server.access_token_env` or `server.access_token_file`. Set `server.auto_access_token: false` only when something in front of Ballast already controls access.

When listening on all interfaces, the self-signed certificate automatically covers the server's host name and every address the server can see, and startup prints a URL for each one. A public IP that is mapped onto the server (an AWS Elastic IP or GCP external IP, for example) or a DNS name is not visible from the server, so add it yourself:

```yaml
tls:
  self_signed_hosts: ["localhost", "127.0.0.1", "203.0.113.10", "ballast.example.com"]
```

Startup warns when Ballast listens beyond loopback with no access token (only possible with `server.auto_access_token: false`), or with `tls.mode: off`. Open the port (8443 by default) in the host firewall and in any cloud security group.

Browsers ask for the token once and then keep a session cookie (HttpOnly, SameSite=Strict, Secure over HTTPS).

## Certificates

In `self-signed` mode Ballast writes `certs/server.crt` and `certs/server.key` at first start, valid for 397 days, with every host in `tls.self_signed_hosts` as a subject alternative name. It renews the certificate automatically at startup when fewer than 30 days remain or the host list changed. Startup prints the SHA-256 fingerprint; compare it with what your browser shows before accepting the certificate.

To make a new self-signed certificate now and load it without dropping connections:

```
bin/ballastctl gen-cert
```

In `files` mode, renew your certificate as usual (for example with a client that writes to the paths in `tls.cert_file` and `tls.key_file`), then reload it:

```
kill -HUP "$(cat run/supervisor.pid)"
```

The supervisor passes the signal on. New connections use the new certificate; open connections are not dropped. A failed reload keeps the previous certificate and logs why.

## Checking the host

```
bin/ballast doctor
bin/ballast doctor --uri 'mongodb://user:password@host:27017/?replicaSet=rs0'
```

`doctor` checks the Python version, `sqlite3` and `ssl`, the required and optional packages and their compiled speedups, that generator processes can start, that `data/`, `logs/` and `run/` are writable, and that the listen port is free. With `--uri` it also connects and checks whether server sampling is allowed. Each WARN or FAIL line comes with the command that fixes it, and the exit code is 1 if anything failed. `--json` prints the same as JSON. The System page shows the same list under **Installed components**.

## Backup and restore

Everything Ballast stores is in `data/`. A consistent copy, safe while it is running:

```
bin/ballastctl backup
bin/ballastctl backup /path/to/ballast-backup.db
```

Without a file name it writes `data/backups/ballast-manual-<time>.db`. To restore one, stop Ballast first. The current store is kept as `data/backups/ballast-pre-restore-<time>.db`:

```
bin/ballastctl stop
bin/ballastctl restore /path/to/ballast-backup.db
bin/ballastctl start
```

Or stop Ballast and copy the whole directory:

```
bin/ballastctl stop
tar -czf ballast-data-$(date +%Y%m%d).tar.gz data
bin/ballastctl start
```

To restore, stop Ballast, replace `data/` with the copy, keep `data/` at mode 0700 and `data/ballast.db` at 0600, and start it. `data/session.key` signs access-gate cookies; if it is lost, users enter the token again.

## Metrics for Prometheus

`GET /metrics` returns the active run's latest second in Prometheus text format: successful and failed operations per second, target rate, backlog, generator CPU, p50/p99/max response time, failures by class since the run started, and the number of stored runs by status. With an access token configured, scrape with a bearer token:

```yaml
scrape_configs:
  - job_name: ballast
    scheme: https
    bearer_token_file: /etc/prometheus/ballast.token
    tls_config:
      ca_file: /etc/prometheus/ballast.crt
    static_configs:
      - targets: ["ballast.example.com:8443"]
```

Turn it off with `features.prometheus_metrics: false`.

## Notifications

Set `notifications.webhook_url` to a Slack incoming webhook, or any URL that accepts a JSON POST. Ballast posts `{"text": ..., "event": ..., "run": {...}}` for the events listed in `notifications.events`: `run_started`, `run_finished` (with throughput, p99, failures, the SLO verdict and the number of impact windows), `guardrail`, and `disruption` (operations started failing after at least 30 clean seconds, at most once a minute). With `server.public_url` set, each message links to the report. A failed delivery is logged and never affects the run.

## Running from scripts and CI

`bin/ballast run` runs one workload without the web server and exits when it ends:

```
bin/ballast run --uri "$MONGODB_URI" --profile ci.yaml --duration 300 --json result.json --markdown result.md
```

`--profile` takes a JSON or YAML profile file or the name of a saved profile. `--rate`, `--concurrency`, `--processes`, `--duration` and `--warmup` override the profile. It prints a progress line every 5 seconds to stderr and a Markdown summary to stdout. Pass `--yes` to confirm writing into a database that already has other collections. The run is saved in `data/`, so it appears in the UI later.

| Exit code | Meaning |
|---|---|
| 0 | Completed, and the profile's pass/fail criteria passed or none were set |
| 1 | Completed, but a pass/fail criterion failed |
| 2 | Could not start (connection, confirmation needed, invalid profile) |
| 3 | The run failed |
| 4 | Stopped by a guardrail |
| 130 | Interrupted with Ctrl-C (the first Ctrl-C drains and keeps the results) |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `start` says the server did not become ready | Read the lines it prints from `logs/console.log`. Configuration errors name the key. |
| `port 443 is below 1024` | Ports below 1024 need root. Use 8443, or put a reverse proxy in front. |
| `The data directory ... is not writable` | Fix ownership of the install directory, or set `paths.data_dir`. |
| `Only N MB free on the disk` | Free space or lower `paths.min_free_disk_mb`. |
| `unknown environment variable BALLAST_...` | A variable with the Ballast prefix that is not a setting. Unset it. |
| Browser shows a certificate warning | Expected with `self-signed`. Check the fingerprint against startup output or **System**, or use `tls.mode: files`. |
| Test connection: DNS SRV lookup failed | The server cannot resolve `_mongodb._tcp.<host>`. Check DNS from the server, or use a `mongodb://` string listing the members. |
| Test connection: TLS handshake failed | Set the CA file for a private CA, or set TLS to on. |
| Test connection: Server selection timed out | Firewall or the cluster's IP access list is blocking this server's address. |
| Report says "Client-bound" | The generator, not the database, limited throughput. See `docs/METHODOLOGY.md`. |
| Server sampling unavailable | The database user lacks `clusterMonitor`. Runs work without it; only member state and lag samples are missing. |
| A run shows "Interrupted" | Ballast stopped while the run was active. Its data up to that point is kept. |
