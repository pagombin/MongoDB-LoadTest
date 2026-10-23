#!/bin/sh
# Starts a local three-member replica set in Docker for development and CI only.
set -e
V=${1:-8.0}
P=${2:-rs}
BASE=${3:-2701}
docker network create ballast-$P >/dev/null 2>&1 || true
for i in 1 2 3; do
  docker rm -f $P$i >/dev/null 2>&1 || true
  docker run -d --name $P$i --hostname $P$i --network ballast-$P -p $BASE$i:$BASE$i mongo:$V mongod --replSet rs0 --bind_ip_all --port $BASE$i >/dev/null
done
sleep 4
docker exec ${P}1 mongosh --port ${BASE}1 --quiet --eval "rs.initiate({_id:'rs0',members:[{_id:0,host:'${P}1:${BASE}1',priority:2},{_id:1,host:'${P}2:${BASE}2'},{_id:2,host:'${P}3:${BASE}3'}]})"
for n in $(seq 1 30); do
  s=$(docker exec ${P}1 mongosh --port ${BASE}1 --quiet --eval 'db.hello().isWritablePrimary' 2>/dev/null || true)
  [ "$s" = "true" ] && echo primary-ready && exit 0
  sleep 1
done
echo not-ready; exit 1
