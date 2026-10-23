#!/bin/sh
# Starts a local three-member replica set in Docker for development and CI only.
set -e
V=${1:-8.0}
docker network create ballast-rs >/dev/null 2>&1 || true
for i in 1 2 3; do
  docker rm -f rs$i >/dev/null 2>&1 || true
  docker run -d --name rs$i --hostname rs$i --network ballast-rs -p 2701$i:2701$i mongo:$V mongod --replSet rs0 --bind_ip_all --port 2701$i >/dev/null
done
sleep 4
docker exec rs1 mongosh --port 27011 --quiet --eval 'rs.initiate({_id:"rs0",members:[{_id:0,host:"rs1:27011",priority:2},{_id:1,host:"rs2:27012"},{_id:2,host:"rs3:27013"}]})'
for n in $(seq 1 30); do
  s=$(docker exec rs1 mongosh --port 27011 --quiet --eval 'db.hello().isWritablePrimary' 2>/dev/null || true)
  [ "$s" = "true" ] && echo primary-ready && exit 0
  sleep 1
done
echo not-ready; exit 1
