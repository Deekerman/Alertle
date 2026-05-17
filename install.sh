#!/usr/bin/env bash
# EPG Notifier — Debian/Ubuntu installer
# Run as root: sudo bash install.sh

set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}  →${RESET} $*"; }
success() { echo -e "${GREEN}  ✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}  !${RESET} $*"; }
die()     { echo -e "${RED}  ✗ ERROR:${RESET} $*" >&2; exit 1; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

# ── Defaults (override with env vars before running) ───────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/epg-notifier}"
SERVICE_USER="${SERVICE_USER:-epg-notifier}"
WEB_PORT="${WEB_PORT:-8888}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
SCAN_CRON="${SCAN_CRON:-}"          # e.g. "*/30 * * * *" — leave blank to skip cron setup
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Pre-flight ─────────────────────────────────────────────────────────────
header "EPG Notifier installer"

[[ "$EUID" -eq 0 ]] || die "Please run as root: sudo bash install.sh"

if ! grep -qiE 'debian|ubuntu|raspbian' /etc/os-release 2>/dev/null; then
  warn "This script targets Debian/Ubuntu. Proceeding anyway…"
fi

PYTHON=$(command -v python3 || true)
[[ -n "$PYTHON" ]] || die "python3 not found. Run: apt install python3"

PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python $PY_VER detected"

# Require 3.9+
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' \
  || die "Python 3.9+ required (found $PY_VER)"

# ── System dependencies ────────────────────────────────────────────────────
header "Installing system packages…"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip
success "System packages ready"

# ── Service user ───────────────────────────────────────────────────────────
header "Creating service user…"
if id "$SERVICE_USER" &>/dev/null; then
  info "User '$SERVICE_USER' already exists — skipping"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  success "Created user '$SERVICE_USER'"
fi

# ── Install directory ──────────────────────────────────────────────────────
header "Installing to $INSTALL_DIR…"
mkdir -p "$INSTALL_DIR"

# Copy project files
rsync -a --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.db' \
  --exclude='epg_notifier.db' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"

# Preserve existing config.yaml (don't overwrite user's credentials)
if [[ -f "$INSTALL_DIR/config.yaml" && "$SOURCE_DIR/config.yaml" -nt "$INSTALL_DIR/config.yaml" ]]; then
  warn "config.yaml already exists — keeping your existing config."
  cp "$SOURCE_DIR/config.yaml" "$INSTALL_DIR/config.yaml.new"
  info "New default config saved as config.yaml.new for reference."
elif [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
  cp "$SOURCE_DIR/config.yaml" "$INSTALL_DIR/config.yaml"
fi

success "Files copied"

# ── Python virtual environment ─────────────────────────────────────────────
header "Setting up Python virtual environment…"
VENV="$INSTALL_DIR/.venv"

if [[ ! -d "$VENV" ]]; then
  "$PYTHON" -m venv "$VENV"
  success "Virtual environment created"
else
  info "Virtual environment already exists"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
success "Python dependencies installed"

# ── Permissions ────────────────────────────────────────────────────────────
header "Setting permissions…"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 640 "$INSTALL_DIR/config.yaml"
success "Permissions set"

# ── Systemd service (web UI) ───────────────────────────────────────────────
header "Installing systemd service…"
SERVICE_FILE="/etc/systemd/system/epg-notifier.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=EPG Notifier — web UI
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/server.py --host ${WEB_HOST} --port ${WEB_PORT}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
# Harden the service
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable epg-notifier
systemctl restart epg-notifier
success "Service enabled and started"

# ── Cron job (optional scanner) ────────────────────────────────────────────
if [[ -n "$SCAN_CRON" ]]; then
  header "Installing cron job…"
  CRON_CMD="$SCAN_CRON $SERVICE_USER $VENV/bin/python $INSTALL_DIR/main.py >> /var/log/epg-notifier-scan.log 2>&1"
  CRON_FILE="/etc/cron.d/epg-notifier"

  echo "# EPG Notifier scanner — edit schedule as needed" > "$CRON_FILE"
  echo "$CRON_CMD" >> "$CRON_FILE"
  chmod 644 "$CRON_FILE"
  success "Cron job installed at $CRON_FILE (schedule: $SCAN_CRON)"
else
  info "No cron schedule set — the web UI's 'Scan Now' button handles manual scans."
  info "To enable automatic scanning, re-run with:  SCAN_CRON='*/30 * * * *' sudo -E bash install.sh"
fi

# ── Log rotation ───────────────────────────────────────────────────────────
cat > /etc/logrotate.d/epg-notifier <<'EOF'
/var/log/epg-notifier-scan.log {
    weekly
    missingok
    rotate 4
    compress
    notifempty
}
EOF

# ── Status check ───────────────────────────────────────────────────────────
header "Checking service status…"
sleep 1
if systemctl is-active --quiet epg-notifier; then
  success "epg-notifier is running"
else
  warn "Service did not start cleanly. Check logs:"
  echo "       journalctl -u epg-notifier -n 30"
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo
echo -e "  Web UI  →  ${CYAN}http://$(hostname -I | awk '{print $1}'):${WEB_PORT}${RESET}"
echo
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "    1. Open the web UI and fill in your Dispatcharr URL + token under Settings"
echo -e "    2. Add subscriptions on the Subscriptions page or via Browse EPG"
echo -e "    3. Enable at least one notification channel in Settings"
echo
echo -e "  ${BOLD}Useful commands:${RESET}"
echo -e "    View logs   →  journalctl -u epg-notifier -f"
echo -e "    Restart     →  systemctl restart epg-notifier"
echo -e "    Stop        →  systemctl stop epg-notifier"
echo -e "    Config file →  ${INSTALL_DIR}/config.yaml"
echo
