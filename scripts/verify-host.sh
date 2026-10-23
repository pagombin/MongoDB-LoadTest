#!/bin/sh
# Checks a Ballast install on a real host, as a normal user, without sudo.
# Run from the extracted bundle:  bin/verify-host.sh [--uri 'mongodb://user:pass@host/...'] [--autostart]
# With --uri it also runs a short workload, adds a marker, stops the run and exports the report.
set -u
here=$(cd "$(dirname "$0")" && pwd -P)
home=$(dirname "$here")
case $(basename "$here") in bin) ;; *) home=$(cd "$here/.." && pwd -P) ;; esac
bin="$home/bin"
uri=""
autostart=0
while [ $# -gt 0 ]; do
  case $1 in
    --uri) uri=$2; shift 2 ;;
    --autostart) autostart=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'PASS  %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL  %s\n' "$1"; [ $# -gt 1 ] && printf '      %s\n' "$2"; }
py="$home/runtime/python/bin/python3"
json() { "$py" -I -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Ballast host check in $home"
if [ "$(id -u)" = "0" ]; then bad "running as a normal user" "you are root; run this as the user that will own the install"; else ok "running as a normal user ($(id -un))"; fi
if "$bin/ballast" version >/dev/null 2>&1; then ok "bin/ballast runs: $("$bin/ballast" version)"; else bad "bin/ballast runs" "$("$bin/ballast" version 2>&1 | tail -1)"; fi
if "$bin/ballast" config validate >/dev/null 2>&1; then ok "configuration is valid"; else bad "configuration is valid" "$("$bin/ballast" config validate 2>&1 | tail -3)"; fi
listen=$("$bin/ballast" config print --json | json "d['server']['listen']")
mode=$("$bin/ballast" config print --json | json "d['tls']['mode']")
port=${listen##*:}
scheme=https
[ "$mode" = "off" ] && scheme=http
url="$scheme://127.0.0.1:$port"
for d in data logs run certs; do
  if [ -w "$home/$d" ] || mkdir -p "$home/$d" 2>/dev/null; then ok "$d/ is writable"; else bad "$d/ is writable"; fi
done
"$bin/ballastctl" stop >/dev/null 2>&1
if "$bin/ballastctl" start; then ok "ballastctl start"; else bad "ballastctl start" "see logs/console.log"; fi
"$bin/ballastctl" start >/dev/null 2>&1
count=$(ls "$home/run"/supervisor.pid 2>/dev/null | wc -l)
if "$bin/ballastctl" status >/dev/null 2>&1; then ok "ballastctl status exits 0 while running"; else bad "ballastctl status exits 0 while running"; fi
[ "$count" -eq 1 ] && ok "a second start does not start a second copy" || bad "a second start does not start a second copy"
if "$bin/ballastctl" health >/dev/null 2>&1; then ok "ballastctl health exits 0"; else bad "ballastctl health exits 0"; fi
certfile=$("$bin/ballast" config print --json | json "d['tls']['cert_file']")
case $certfile in /*) ;; *) certfile="$home/$certfile" ;; esac
curl_tls=""
[ "$scheme" = "https" ] && curl_tls="--cacert $certfile"
if command -v curl >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  headers=$(curl -sS $curl_tls -D - -o /dev/null "$url/" 2>&1)
  case $headers in *"Content-Security-Policy"*) ok "HTTPS page served with CSP via curl ($url)" ;; *) bad "HTTPS page served with CSP via curl" "$headers" ;; esac
else
  bad "curl available for the HTTPS check"
fi
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -q ":$port "; then ok "listening on port $port"; else bad "listening on port $port"; fi
fi
tokfile=$("$bin/ballast" config print --json | json "d['server']['access_token_file']")
auth=""
if [ -n "$tokfile" ]; then
  case $tokfile in /*) ;; *) tokfile="$home/$tokfile" ;; esac
  auth="Authorization: Bearer $(head -n 1 "$tokfile")"
fi
if [ -n "$auth" ]; then
  code=$(curl -sS $curl_tls -o /dev/null -w '%{http_code}' "$url/api/runs")
  [ "$code" = "401" ] && ok "access gate rejects requests without the token" || bad "access gate rejects requests without the token" "HTTP $code"
fi
if [ -n "$uri" ]; then
  api() {
    # shellcheck disable=SC2086
    curl -sS $curl_tls -H 'Content-Type: application/json' -H 'X-Ballast-Request: 1' ${auth:+-H "$auth"} "$@"
  }
  tid=$(api -X POST "$url/api/targets" -d "{\"name\":\"verify-$$\",\"uri\":\"$uri\"}" | json "d.get('id','')")
  if [ -n "$tid" ]; then ok "target saved"; else bad "target saved"; fi
  test=$(api -X POST "$url/api/targets/test" -d "{\"target_id\":\"$tid\"}" | json "('ok ' + str(d.get('server_version'))) if d.get('ok') else d.get('title','')")
  case $test in ok*) ok "connection test: $test" ;; *) bad "connection test" "$test" ;; esac
  rid=$(api -X POST "$url/api/runs" -d "{\"name\":\"verify host\",\"target_id\":\"$tid\",\"acknowledge_foreign\":true,\"profile\":{\"name\":\"verify\",\"load\":{\"rate\":200,\"concurrency\":32},\"schedule\":{\"warmup_seconds\":2,\"steady_seconds\":20},\"dataset\":{\"database\":\"ballast_verify\",\"load\":{\"documents\":500}}}}" | json "d.get('id','')")
  if [ -n "$rid" ]; then ok "run started ($rid)"; else bad "run started"; fi
  sleep 10
  if "$bin/ballastctl" mark "verify marker" >/dev/null; then ok "ballastctl mark"; else bad "ballastctl mark"; fi
  i=0
  while [ $i -lt 60 ]; do
    st=$(api "$url/api/runs/$rid" | json "d.get('status')")
    case $st in completed|stopped|failed) break ;; esac
    sleep 2; i=$((i + 1))
  done
  [ "$st" = "completed" ] && ok "run completed" || bad "run completed" "status $st"
  size=$(api "$url/api/runs/$rid/export.html" | wc -c)
  [ "$size" -gt 50000 ] && ok "offline HTML report exported ($size bytes)" || bad "offline HTML report exported"
  pw=$(printf '%s' "$uri" | sed -n 's#^mongodb[^:]*://[^:]*:\([^@]*\)@.*#\1#p')
  if [ -n "$pw" ]; then
    if grep -rqF "$pw" "$home/logs" 2>/dev/null; then bad "password absent from logs"; else ok "password absent from logs"; fi
  fi
fi
spid=$(cat "$home/run/ballast.pid" 2>/dev/null)
if [ -n "$spid" ]; then
  kill -9 "$spid" 2>/dev/null
  i=0
  while [ $i -lt 30 ]; do
    sleep 1; i=$((i + 1))
    npid=$(cat "$home/run/ballast.pid" 2>/dev/null)
    if [ -n "$npid" ] && [ "$npid" != "$spid" ] && "$bin/ballastctl" health >/dev/null 2>&1; then break; fi
  done
  if [ -n "$npid" ] && [ "$npid" != "$spid" ]; then ok "supervisor restarted the server after kill -9 (pid $spid -> $npid)"; else bad "supervisor restarted the server after kill -9"; fi
fi
if [ $autostart -eq 1 ]; then
  if "$bin/ballastctl" autostart enable; then ok "autostart enable"; else bad "autostart enable"; fi
fi
if "$bin/ballastctl" stop; then ok "ballastctl stop"; else bad "ballastctl stop"; fi
"$bin/ballastctl" status >/dev/null 2>&1
code=$?
[ $code -eq 3 ] && ok "status exits 3 when stopped" || bad "status exits 3 when stopped" "exit $code"
echo
echo "$pass passed, $fail failed"
[ $fail -eq 0 ]
