#!/usr/bin/env bash
# Deploys the committed code (git HEAD) to the EcoPulse LXC and (re)starts the stack.
#
#   bash deploy/deploy.sh                    # code + secrets + map data, then docker compose up
#   WEB_DIST=../EcoPulse_Frontend/dist bash deploy/deploy.sh   # also publish a frontend build
#   REBUILD_GRAPH=1 bash deploy/deploy.sh    # after a new iasi.osm.pbf: rebuild the routing graph
#
# Needs: deploy/.env.production (uv run python deploy/make_env.py)
#        deploy/graphhopper/data/iasi.osm.pbf (uv run python deploy/graphhopper/build_extract.py)
set -euo pipefail

HOST="${ECOPULSE_HOST:-root@192.168.0.27}"
KEY="${ECOPULSE_KEY:-$HOME/.ssh/ecopulse_deploy}"
DIR="/opt/ecopulse-v1"  # the v0 stack lives in /opt/ecopulse (project "ecopulse"): never touch it
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "$HOST")
SCP=(scp -q -i "$KEY")

cd "$(dirname "$0")/.."
test -f deploy/.env.production || { echo "Missing deploy/.env.production (run deploy/make_env.py)"; exit 1; }
test -f deploy/graphhopper/data/iasi.osm.pbf || { echo "Missing the OSM extract (run build_extract.py)"; exit 1; }
if ! git diff --quiet HEAD -- app migrations deploy pyproject.toml uv.lock Dockerfile; then
  echo "Warning: uncommitted changes are NOT deployed (only git HEAD is)." >&2
fi

echo "==> Code $(git rev-parse --short HEAD) -> $HOST:$DIR"
git archive --format=tar HEAD | "${SSH[@]}" "mkdir -p $DIR && tar -x -C $DIR && echo $(git rev-parse --short HEAD) > $DIR/REVISION"

echo "==> Secrets and map data"
"${SSH[@]}" "mkdir -p $DIR/deploy/graphhopper/data $DIR/deploy/backups && chmod 700 $DIR/deploy"
"${SCP[@]}" deploy/.env.production "$HOST:$DIR/deploy/.env.production"
"${SSH[@]}" "chmod 600 $DIR/deploy/.env.production"
"${SCP[@]}" deploy/graphhopper/data/iasi.osm.pbf "$HOST:$DIR/deploy/graphhopper/data/iasi.osm.pbf"
if [ "${REBUILD_GRAPH:-0}" = "1" ]; then
  "${SSH[@]}" "rm -rf $DIR/deploy/graphhopper/data/graph-cache"
fi

if [ -n "${WEB_DIST:-}" ]; then
  echo "==> Frontend from $WEB_DIST"
  # Unpack next to the live folder, then swap the contents in one step. Never `rm -rf` the folder itself:
  # it is bind-mounted into nginx, which would keep serving the deleted (empty) directory.
  tar -C "$WEB_DIST" -cf - . | "${SSH[@]}" "set -e; rm -rf $DIR/deploy/web.new; mkdir -p $DIR/deploy/web.new $DIR/deploy/web; tar --no-same-owner -x -C $DIR/deploy/web.new; find $DIR/deploy/web -mindepth 1 -delete; cp -a $DIR/deploy/web.new/. $DIR/deploy/web/; rm -rf $DIR/deploy/web.new"
fi

echo "==> docker compose up"
COMPOSE="docker compose -f deploy/compose.yaml --env-file deploy/.env.production"
# nginx.conf is a bind mount: reload it explicitly, since an unchanged container is not recreated.
"${SSH[@]}" "cd $DIR && $COMPOSE up -d --build --remove-orphans && $COMPOSE exec -T web nginx -s reload && $COMPOSE ps"
