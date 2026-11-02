#!/bin/bash
# Pull the latest main and restart what changed. Run as the `dash` user on the server
# (GitHub Actions calls this over SSH on every push to main).
set -euo pipefail
cd /opt/personal_dash
git fetch origin main
git reset --hard origin/main
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
sleep 5
if docker compose exec -T backend python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)"; then
  echo "deploy ok: $(git rev-parse --short HEAD)"
else
  echo "backend health check failed"; docker compose logs --tail=50 backend; exit 1
fi
