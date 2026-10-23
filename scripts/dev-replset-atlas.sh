#!/bin/sh
# Development and CI only. Starts a five-member replica set in Docker shaped like an Atlas cluster with
# analytics and read-only nodes: three electable members, one analytics node and one read-only node,
# with the replica set tags Atlas sets (nodeType, workloadType, provider, region). Analytics and
# read-only members have priority 0 and no vote, as in Atlas.
# Usage: scripts/dev-replset-atlas.sh [mongo version] [name prefix] [port base]
# Members are <prefix>1..<prefix>5 on ports <base>1..<base>5, for example ra1:27041.
set -e
V=${1:-8.0}
P=${2:-ra}
BASE=${3:-2704}
docker network create ballast-$P >/dev/null 2>&1 || true
for i in 1 2 3 4 5; do
  docker rm -f $P$i >/dev/null 2>&1 || true
  docker run -d --name $P$i --hostname $P$i --network ballast-$P -p $BASE$i:$BASE$i mongo:$V mongod --replSet rs0 --bind_ip_all --port $BASE$i >/dev/null
done
sleep 4
electable="{nodeType: 'ELECTABLE', workloadType: 'OPERATIONAL', provider: 'AWS', region: 'US_EAST_1'}"
docker exec ${P}1 mongosh --port ${BASE}1 --quiet --eval "rs.initiate({_id: 'rs0', members: [
  {_id: 0, host: '${P}1:${BASE}1', tags: $electable},
  {_id: 1, host: '${P}2:${BASE}2', tags: $electable},
  {_id: 2, host: '${P}3:${BASE}3', tags: $electable},
  {_id: 3, host: '${P}4:${BASE}4', priority: 0, votes: 0, tags: {nodeType: 'ANALYTICS', workloadType: 'ANALYTICS', provider: 'AWS', region: 'US_EAST_1'}},
  {_id: 4, host: '${P}5:${BASE}5', priority: 0, votes: 0, tags: {nodeType: 'READ_ONLY', workloadType: 'OPERATIONAL', provider: 'AWS', region: 'US_WEST_2'}}
]})" >/dev/null
for n in $(seq 1 40); do
  s=$(docker exec ${P}1 mongosh --port ${BASE}1 --quiet --eval 'db.hello().isWritablePrimary' 2>/dev/null || true)
  ok=$(docker exec ${P}1 mongosh --port ${BASE}1 --quiet --eval 'rs.status().members.filter(m => m.stateStr == "SECONDARY").length' 2>/dev/null || true)
  [ "$s" = "true" ] && [ "$ok" = "4" ] && echo primary-ready && exit 0
  sleep 1
done
echo not-ready; exit 1
