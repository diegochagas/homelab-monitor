#!/usr/bin/env bash
# Installs the Homelab Monitor agent on a Linux machine (systemd --user).
# Run from the repo checkout on the machine to monitor:
#
#   cd homelab-monitor && ./install-agent.sh
#
# The agent is a stdlib-only Python 3 script; nothing else is installed.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

UNIT_DIR="$HOME/.config/systemd/user"
UNIT="homelab-monitor-agent.service"

echo "=========================================="
echo "   Homelab Monitor — agent install"
echo "=========================================="
echo

# 1. Dependencies ---------------------------------------------------------
echo "Checking dependencies..."
for command in python3 systemctl curl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "❌ Missing dependency: $command"
        exit 1
    fi
done
python3 - <<'PY' || { echo "❌ Python 3.8+ required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY
echo "✅ Dependencies OK"
echo

# 2. Configuration --------------------------------------------------------
if [[ ! -f monitor.env ]]; then
    cp monitor.env.example monitor.env
    chmod 600 monitor.env
    echo "Created monitor.env from the example."
fi

# shellcheck disable=SC1091
source <(grep -E '^(NODE_NAME|CENTRAL_URL|AGENT_ID|AGENT_TOKEN|REGISTRATION_KEY|BACKUP_LOG_DIR)=' monitor.env || true)

set_var() {
    local key="$1" value="$2"
    if grep -qE "^#?\s*$key=" monitor.env; then
        sed -i -E "s|^#?\s*$key=.*|$key=$value|" monitor.env
    else
        echo "$key=$value" >> monitor.env
    fi
}

default_name="$(hostname -s 2>/dev/null || hostname)"
read -r -p "Name of this machine [${NODE_NAME:-$default_name}]: " name
name="${name:-${NODE_NAME:-$default_name}}"

while :; do
    read -r -p "Central URL${CENTRAL_URL:+ [$CENTRAL_URL]} (e.g. http://192.168.1.10:8787): " central
    central="${central:-${CENTRAL_URL:-}}"
    [[ -n "$central" ]] && break
done

read -r -p "Directory with backup.sh logs to report, blank for none${BACKUP_LOG_DIR:+ [$BACKUP_LOG_DIR]}: " log_dir
log_dir="${log_dir:-${BACKUP_LOG_DIR:-}}"

if [[ -z "${REGISTRATION_KEY:-}" ]]; then
    read -r -p "Registration key (from the central monitor.env): " REGISTRATION_KEY
fi

if [[ -z "${AGENT_ID:-}" ]]; then
    AGENT_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
fi
if [[ -z "${AGENT_TOKEN:-}" ]]; then
    AGENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
fi

set_var NODE_NAME "$name"
set_var CENTRAL_URL "$central"
set_var REGISTRATION_KEY "$REGISTRATION_KEY"
set_var AGENT_ID "$AGENT_ID"
set_var AGENT_TOKEN "$AGENT_TOKEN"
set_var BACKUP_LOG_DIR "$log_dir"
# Central-only settings must stay empty on an agent.
sed -i -e 's|^DISK_IGNORE=.*|DISK_IGNORE=|' -e 's|^SERVER_BACKUP_LOG=.*|SERVER_BACKUP_LOG=|' monitor.env
echo "✅ monitor.env updated (id $AGENT_ID)"
echo

# 3. Connectivity ---------------------------------------------------------
echo "Testing central server..."
if curl -fsS -m 5 "$central/api/v1/ping" >/dev/null; then
    echo "✅ Central reachable at $central"
else
    echo "⚠️  Central not reachable at $central — the agent will keep retrying"
fi
echo

# 4. Service --------------------------------------------------------------
mkdir -p "$UNIT_DIR"
sed \
    -e "s|@MONITOR_DIR@|$SCRIPT_DIR|g" \
    "$SCRIPT_DIR/$UNIT" > "$UNIT_DIR/$UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
# Keep the user session (and so the agent) alive without an active login.
if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true
fi
echo "✅ Service installed: systemctl --user status $UNIT"
echo

# 5. Verify ---------------------------------------------------------------
echo "Sending a first heartbeat..."
if python3 -m homelab_monitor.agent monitor.env --once; then
    echo
    echo "🎉 Done. Run /servers on Telegram — '$name' should be listed."
else
    echo
    echo "⚠️  First heartbeat failed. Check the registration key and central URL, then:"
    echo "    journalctl --user -u $UNIT -f"
fi
