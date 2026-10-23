#!/bin/sh
# Development only. Simulates a fresh droplet in Docker: Ubuntu 24.04, a normal user
# without sudo, and a network that reaches only the test replica set (no internet).
# Usage: scripts/simulate-fresh-host.sh dist/ballast-1.0.0-linux-amd64.tar.gz
set -u
bundle=$1
root=$(cd "$(dirname "$0")/.." && pwd -P)
log() { printf '\n### %s\n' "$*"; }
run() { printf '$ %s\n' "$*"; sh -c "$*" 2>&1; printf '(exit %s)\n' "$?"; }
in_box() { docker exec -u bench -w /home/bench droplet sh -c "$1"; }
box() { printf '$ [bench@droplet] %s\n' "$1" | sed -E 's/Bearer [^ '"'"']+/Bearer ****/g'; in_box "$1" 2>&1; printf '(exit %s)\n' "$?"; }

log "Network with no route to the internet, joined by the replica set"
docker rm -f droplet >/dev/null 2>&1
for m in rs1 rs2 rs3; do docker network disconnect ballast-offline $m >/dev/null 2>&1; done
docker network rm ballast-offline >/dev/null 2>&1
run "docker network create --internal ballast-offline"
for m in rs1 rs2 rs3; do docker network connect --alias $m ballast-offline $m; done
run "docker run -d --name droplet --hostname droplet --network ballast-offline ballast-droplet-sim"
ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' droplet)
echo "droplet address: $ip"

log "The host has no internet access and no sudo"
box "curl -sS -m 5 -o /dev/null https://pypi.org/ || echo 'no internet: as expected'"
box "command -v sudo || echo 'no sudo: as expected'"
box "id"

log "Copy the bundle in with scp-like copy, extract as the normal user"
run "docker cp $bundle droplet:/home/bench/ && docker exec droplet chown bench:bench /home/bench/$(basename "$bundle")"
box "tar -xzf $(basename "$bundle") && ls ballast-1.0.0"

log "Configure: all interfaces, certificate paths outside the default, access token"
box "mkdir -p mycerts && head -c 24 /dev/urandom | base64 > ballast-1.0.0/config/token && chmod 600 ballast-1.0.0/config/token"
docker exec -i -u bench -w /home/bench droplet sh -c "cat > ballast-1.0.0/config/ballast.yaml" <<YAML
server:
  listen: 0.0.0.0:8443
  access_token_file: config/token
tls:
  mode: self-signed
  cert_file: /home/bench/mycerts/ballast.crt
  key_file: /home/bench/mycerts/ballast.key
  self_signed_hosts: ["localhost", "127.0.0.1", "$ip"]
  redirect_http_listen: 0.0.0.0:8080
YAML
box "cd ballast-1.0.0 && bin/ballast config validate && bin/ballast config print | grep -E 'listen|cert_file|key_file|access_token_file'"

log "Host check script, including a workload against the replica set and a kill -9"
box "cd ballast-1.0.0 && bin/verify-host.sh --uri 'mongodb://rs1:27011,rs2:27012,rs3:27013/?replicaSet=rs0'"

log "Start again and reach it over HTTPS from outside the box"
box "cd ballast-1.0.0 && bin/ballastctl start"
run "curl -sS --cacert /dev/null -k -o /dev/null -w 'https %{http_code} from host\n' https://$ip:8443/api/health"
run "curl -sS -o /dev/null -w 'http redirect %{http_code} -> %{redirect_url}\n' http://$ip:8080/"
docker cp droplet:/home/bench/mycerts/ballast.crt /tmp/droplet-ballast.crt
run "curl -sS --cacert /tmp/droplet-ballast.crt -o /dev/null -w 'verified TLS %{http_code}\n' https://$ip:8443/api/health"
run "openssl s_client -connect $ip:8443 -tls1_2 -CAfile /tmp/droplet-ballast.crt </dev/null 2>&1 | grep -E '^ *Protocol|Verify return code' | head -2"
echo "droplet_ip=$ip" > /tmp/droplet.env
echo "droplet_token=$(in_box 'cat ballast-1.0.0/config/token')" >> /tmp/droplet.env

log "Headless browser from outside the box, through the access gate"
tok=$(in_box 'cat ballast-1.0.0/config/token')
printf '$ BASE=https://%s:8443/ TOKEN=**** node scripts/gate-check.mjs\n' "$ip"
BASE="https://$ip:8443/" TOKEN="$tok" node "$root/scripts/gate-check.mjs" 2>&1
printf '(exit %s)\n' "$?"

log "A run interrupted by a crash keeps its data"
api() { in_box "curl -sS --cacert mycerts/ballast.crt -H 'Authorization: Bearer $tok' -H 'X-Ballast-Request: 1' -H 'Content-Type: application/json' $1"; }
tid=$(api "https://127.0.0.1:8443/api/targets" | sed -n 's/.*"id": "\([a-f0-9]*\)".*/\1/p' | head -1)
rid=$(api "-d '{\"name\":\"crash test\",\"target_id\":\"$tid\",\"acknowledge_foreign\":true,\"profile\":{\"name\":\"c\",\"load\":{\"rate\":200,\"concurrency\":16},\"schedule\":{\"warmup_seconds\":1,\"steady_seconds\":600},\"dataset\":{\"database\":\"ballast_verify\"}}}' https://127.0.0.1:8443/api/runs" | sed -n 's/.*"id": "\([a-f0-9]*\)".*/\1/p' | head -1)
echo "run $rid started"
sleep 15
box "kill -9 \$(cat ballast-1.0.0/run/ballast.pid); sleep 6; cd ballast-1.0.0 && bin/ballastctl health"
box "curl -sS --cacert mycerts/ballast.crt -H 'Authorization: Bearer $tok' https://127.0.0.1:8443/api/runs/$rid | ballast-1.0.0/runtime/python/bin/python3 -c 'import json,sys; d=json.load(sys.stdin); print(\"status:\", d[\"status\"], \"| measured seconds kept:\", d[\"analysis\"][\"summary\"][\"measured_seconds\"], \"| ops kept:\", d[\"analysis\"][\"summary\"][\"ok\"], \"|\", d[\"error\"][\"detail\"])'"

log "Autostart without root, then restart the whole box"
box "cd ballast-1.0.0 && bin/ballastctl autostart enable && crontab -l"
run "docker restart droplet"
sleep 25
box "cd ballast-1.0.0 && bin/ballastctl status; bin/ballastctl health"
box "cd ballast-1.0.0 && tail -3 logs/supervisor.log"

log "Clean up"
box "cd ballast-1.0.0 && bin/ballastctl autostart disable && bin/ballastctl stop"
