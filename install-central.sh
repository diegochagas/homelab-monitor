#!/usr/bin/env bash
# Installs/updates the central Homelab Monitor as a Docker container.
# Run on the always-on server from the repo checkout:
#
#   cd homelab-monitor && ./install-central.sh
#
# Re-run after `git pull` to rebuild and restart with the new code.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEFAULT_DATA_DIR="/DATA/AppData/homelab-monitor"

echo "=========================================="
echo "   Homelab Monitor — central install"
echo "=========================================="
echo

# 1. Dependencies ---------------------------------------------------------
echo "Checking dependencies..."
for command in docker curl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "❌ Missing dependency: $command"
        exit 1
    fi
done

SUDO=""
if ! docker info >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null || sudo -v; then
        SUDO="sudo"
    else
        echo "❌ Cannot talk to Docker (need docker group membership or sudo)"
        exit 1
    fi
fi

if $SUDO docker compose version >/dev/null 2>&1; then
    COMPOSE="$SUDO docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="$SUDO docker-compose"
else
    COMPOSE=""
fi
echo "✅ Dependencies OK"
echo

# 2. Configuration --------------------------------------------------------
if [[ ! -f monitor.env ]]; then
    cp monitor.env.example monitor.env
    chmod 600 monitor.env
    echo "Created monitor.env from the example."
    echo
    read -r -p "Telegram bot token (from @BotFather): " token
    read -r -p "Telegram chat id (where alerts go): " chat_id
    read -r -p "Allowed Telegram user id(s), comma separated: " users
    read -r -p "Name of this server [server]: " name
    name="${name:-server}"
    key="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"

    sed -i \
        -e "s|^NODE_NAME=.*|NODE_NAME=$name|" \
        -e "s|^TELEGRAM_BOT_TOKEN=.*|TELEGRAM_BOT_TOKEN=$token|" \
        -e "s|^TELEGRAM_CHAT_ID=.*|TELEGRAM_CHAT_ID=$chat_id|" \
        -e "s|^TELEGRAM_ALLOWED_USER_IDS=.*|TELEGRAM_ALLOWED_USER_IDS=$users|" \
        -e "s|^REGISTRATION_KEY=.*|REGISTRATION_KEY=$key|" \
        monitor.env
    echo
    echo "Registration key for agents: $key"
    echo "(also stored in monitor.env)"
    echo
else
    echo "Using existing monitor.env"
    echo
fi

# shellcheck disable=SC1091
# shellcheck source=/dev/null  # filtered KEY=VALUE lines from monitor.env
source <(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|REGISTRATION_KEY|LISTEN_PORT)=' monitor.env)
for var in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID REGISTRATION_KEY; do
    if [[ -z "${!var:-}" ]]; then
        echo "❌ $var is empty in monitor.env"
        exit 1
    fi
done

# 3. Data directory (compose reads MONITOR_DATA_DIR from .env) ------------
if [[ ! -f .env ]]; then
    read -r -p "Directory for the bot's database [$DEFAULT_DATA_DIR]: " data_dir
    echo "MONITOR_DATA_DIR=${data_dir:-$DEFAULT_DATA_DIR}" > .env
fi
# shellcheck disable=SC1091
source .env
DATA_DIR="${MONITOR_DATA_DIR:-$DEFAULT_DATA_DIR}"
$SUDO mkdir -p "$DATA_DIR"
echo "Data directory: $DATA_DIR"
echo

# 4. Build & start --------------------------------------------------------
echo "Building image and starting the container..."
# A container of the same name from another checkout/compose project would
# block `up`; replace it.
if [[ -n "$COMPOSE" ]]; then
    if ! $COMPOSE ps --format '{{.Name}}' 2>/dev/null | grep -qx homelab-monitor; then
        $SUDO docker rm -f homelab-monitor >/dev/null 2>&1 || true
    fi
    $COMPOSE up -d --build
else
    $SUDO docker build -t homelab-monitor:latest .
    $SUDO docker rm -f homelab-monitor >/dev/null 2>&1 || true
    $SUDO docker run -d \
        --name homelab-monitor \
        --restart unless-stopped \
        --network host \
        --pid host \
        --cap-add SYS_PTRACE \
        --security-opt apparmor=unconfined \
        --env-file monitor.env \
        -e HOST_ROOT=/host -e DATA_DIR=/data \
        --mount type=bind,source=/,target=/host,readonly,bind-propagation=rslave \
        -v /var/run/docker.sock:/var/run/docker.sock:ro \
        -v "$DATA_DIR":/data \
        --log-opt max-size=5m --log-opt max-file=3 \
        homelab-monitor:latest
fi
echo

# 5. Verify ---------------------------------------------------------------
echo "Verifying (the bot talks to Telegram before opening the port)..."
ready=false
for _ in $(seq 1 12); do
    sleep 5
    if curl -fsS -m 5 "http://127.0.0.1:${LISTEN_PORT:-8787}/api/v1/ping" >/dev/null 2>&1; then
        ready=true
        break
    fi
done
if [[ "$ready" == true ]]; then
    echo "✅ Agent endpoint is answering on port ${LISTEN_PORT:-8787}"
else
    echo "⚠️  Agent endpoint not answering after 60s — check: $SUDO docker logs homelab-monitor"
fi
$SUDO docker logs --tail 5 homelab-monitor 2>&1 | sed 's/^/   /'
echo
echo "🎉 Done. Send /help to your bot on Telegram."
echo
echo "Next: on other machines run monitor/install-agent.sh with"
# hostname -I isn't supported by ZimaOS's busybox hostname, and under
# pipefail a failed `|| true`-less pipeline here would abort the whole
# script via set -e — this step is informational only, never fatal.
central_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)" || true
[[ -n "$central_ip" ]] || central_ip="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
echo "  Central URL:      http://${central_ip:-CENTRAL-IP}:${LISTEN_PORT:-8787}"
echo "  Registration key: $REGISTRATION_KEY"
