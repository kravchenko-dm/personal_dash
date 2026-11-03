#!/bin/bash
# Pull the latest main and restart what changed. Run as the `dash` user on the server
# (GitHub Actions calls this over SSH on every push to main).
set -euo pipefail
cd /opt/personal_dash
git fetch origin main
git reset --hard origin/main
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
# migrations run on startup, give the backend up to 90 s to come up
for i in $(seq 1 18); do
  if docker compose exec -T backend python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)" 2>/dev/null; then
    echo "deploy ok: $(git rev-parse --short HEAD) (healthy after ~$((i*5)) s)"
    exit 0
  fi
  sleep 5
done
echo "backend health check failed"; docker compose logs --tail=60 backend; exit 1
