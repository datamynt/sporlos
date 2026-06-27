#!/usr/bin/env bash
# Pull-basert deploy på b550 (erstatter GitHub-Actions push-deploy, som ikke når
# b550 fordi SSH ikke er åpent utad). Kjør manuelt ved ship, ELLER legg i cron:
#   */2 * * * * /home/<bruker>/sporlos/scripts/deploy-b550.sh >> /var/log/sporlos-deploy.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

before=$(git rev-parse HEAD)
git fetch --quiet origin master
git merge --ff-only origin/master
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
  exit 0   # ingenting nytt — ikke bygg/restart i unødvendig
fi

echo "[$(date -u +%FT%TZ)] $before -> $after, bygger…"
docker compose -f docker-compose.b550.yml up -d --build
docker image prune -f >/dev/null 2>&1 || true
echo "[$(date -u +%FT%TZ)] ferdig"
