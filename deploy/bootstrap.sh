#!/bin/bash
# First-time setup of a fresh Ubuntu 22.04/24.04 VPS for Personal Dash.
# Run as root:  curl -fsSL https://raw.githubusercontent.com/kravchenko-dm/personal_dash/main/deploy/bootstrap.sh | bash -s -- dash.example.com
#
# What it does:
#   - creates user `dash` (docker group, no sudo), copies root's SSH keys to it
#   - installs Docker + Compose, ufw (22/80/443), fail2ban, unattended-upgrades
#   - hardens sshd: keys only, no root password login
#   - clones the repo to /opt/personal_dash, creates .env with generated secrets
#   - builds images and generates VAPID keys
# After it finishes: edit /opt/personal_dash/.env (domain, password, tokens) and run
#   sudo -u dash docker compose -f /opt/personal_dash/docker-compose.yml up -d
set -euo pipefail

DOMAIN="${1:-}"
REPO="https://github.com/kravchenko-dm/personal_dash.git"
APP_DIR=/opt/personal_dash
APP_USER=dash

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl git ufw fail2ban unattended-upgrades openssl

echo "== docker"
add_docker_repo() {
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -q
}
if ! command -v docker >/dev/null 2>&1; then
  add_docker_repo
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
# hosting images often ship Docker without the Compose plugin
if ! docker compose version >/dev/null 2>&1; then
  echo "   compose plugin missing, installing"
  apt-get install -y -q docker-compose-v2 || true
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "   apt package unavailable, downloading the plugin binary"
  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi
docker compose version || { echo "docker compose still unavailable"; exit 1; }
systemctl enable --now docker
# keep container logs from eating the disk
mkdir -p /etc/docker
[ -f /etc/docker/daemon.json ] || cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "3" } }
JSON
systemctl restart docker

echo "== user $APP_USER"
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG docker "$APP_USER"
mkdir -p /home/$APP_USER/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
  cat /root/.ssh/authorized_keys >> /home/$APP_USER/.ssh/authorized_keys
  sort -u /home/$APP_USER/.ssh/authorized_keys -o /home/$APP_USER/.ssh/authorized_keys
fi
chown -R $APP_USER:$APP_USER /home/$APP_USER/.ssh
chmod 700 /home/$APP_USER/.ssh
[ -f /home/$APP_USER/.ssh/authorized_keys ] && chmod 600 /home/$APP_USER/.ssh/authorized_keys

echo "== firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== sshd hardening"
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-dash.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 4
SSHD
systemctl reload ssh || systemctl reload sshd || true
systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

echo "== swap (2 GB) if none"
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== repo"
if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO" "$APP_DIR"
  chown -R $APP_USER:$APP_USER "$APP_DIR"
else
  chown -R $APP_USER:$APP_USER "$APP_DIR"
  sudo -u $APP_USER git -C "$APP_DIR" pull -q --ff-only || echo "   (git pull skipped)"
fi
cd "$APP_DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^APP_SECRET=.*|APP_SECRET=$(openssl rand -hex 32)|" .env
  sed -i "s|^TELEGRAM_WEBHOOK_SECRET=.*|TELEGRAM_WEBHOOK_SECRET=$(openssl rand -hex 16)|" .env
  sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 16)|" .env
  PGPASS=$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2)
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://dash:${PGPASS}@db:5432/dash|" .env
  [ -n "$DOMAIN" ] && sed -i "s|^APP_DOMAIN=.*|APP_DOMAIN=$DOMAIN|" .env
  chmod 600 .env
  chown $APP_USER:$APP_USER .env
fi

echo "== build backend and generate VAPID keys"
sudo -u $APP_USER docker compose build backend
if ! grep -q '^VAPID_PUBLIC_KEY=.\+' .env; then
  KEYS=$(sudo -u $APP_USER docker compose run --rm --no-deps backend python -m app.tools.vapid)
  PUB=$(echo "$KEYS" | grep VAPID_PUBLIC_KEY | cut -d= -f2)
  PRIV=$(echo "$KEYS" | grep VAPID_PRIVATE_KEY | cut -d= -f2)
  sed -i "s|^VAPID_PUBLIC_KEY=.*|VAPID_PUBLIC_KEY=$PUB|" .env
  sed -i "s|^VAPID_PRIVATE_KEY=.*|VAPID_PRIVATE_KEY=$PRIV|" .env
fi

echo
echo "================================================================"
echo "Done. Next steps:"
echo "  1. nano $APP_DIR/.env   -> APP_DOMAIN, DASH_PASSWORD, TELEGRAM_*, ANTHROPIC_API_KEY/LLM_*, GOOGLE_*, YOUGILE_API_KEY"
echo "  2. sudo -u $APP_USER docker compose -f $APP_DIR/docker-compose.yml up -d --build"
echo "  3. open https://${DOMAIN:-<your-domain>}"
echo "For auto-deploy from GitHub: create a deploy key for user '$APP_USER' (see README -> Деплой)."
echo "================================================================"
