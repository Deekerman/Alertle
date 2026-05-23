#!/usr/bin/env bash
# Alertle — Debian/Ubuntu installer
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
INSTALL_DIR="${INSTALL_DIR:-/opt/alertle}"
SERVICE_USER="${SERVICE_USER:-alertle}"
WEB_PORT="${WEB_PORT:-8888}"
WEB_HOST="${WEB_HOST:-0.0.0.0}"
SCAN_CRON="${SCAN_CRON:-}"          # e.g. "*/30 * * * *" — leave blank; the web UI handles polling
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Pre-flight ─────────────────────────────────────────────────────────────
header "Alertle installer"

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
apt-get install -y -qq python3-venv python3-pip rsync
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

# Copy project files (preserve the live DB and user config)
rsync -a --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.db' \
  --exclude='alertle.db' \
  "$SOURCE_DIR/" "$INSTALL_DIR/"

# Preserve existing config.yaml — never overwrite user's settings on upgrade
if [[ -f "$INSTALL_DIR/config.yaml" ]]; then
  warn "config.yaml already exists — keeping your existing config."
  cp "$SOURCE_DIR/config.yaml" "$INSTALL_DIR/config.yaml.new"
  info "New default config saved as config.yaml.new for reference."
else
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

# ── Systemd service ────────────────────────────────────────────────────────
header "Installing systemd service…"
SERVICE_FILE="/etc/systemd/system/alertle.service"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Alertle — EPG game notification service
After=network.target
Wants=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${VENV}/bin/python ${INSTALL_DIR}/server.py --host ${WEB_HOST} --port ${WEB_PORT}
Restart=always
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
systemctl enable alertle

# Restart if already running, otherwise start fresh
if systemctl is-active --quiet alertle; then
  systemctl restart alertle
else
  systemctl start alertle
fi
success "Service enabled and started"

# ── Cron job (optional — only needed for headless / no-web-UI operation) ───
if [[ -n "$SCAN_CRON" ]]; then
  header "Installing cron job…"
  if ! [[ "$SCAN_CRON" =~ ^[0-9*/,\-]+[[:space:]]+[0-9*/,\-]+[[:space:]]+[0-9*/,\-]+[[:space:]]+[0-9*/,\-]+[[:space:]]+[0-9*/,\-]+$ ]]; then
    echo "ERROR: SCAN_CRON contains invalid characters: $SCAN_CRON" >&2
    exit 1
  fi
  CRON_CMD="$SCAN_CRON $SERVICE_USER $VENV/bin/python $INSTALL_DIR/main.py >> /var/log/alertle-scan.log 2>&1"
  CRON_FILE="/etc/cron.d/alertle"

  echo "# Alertle scanner — edit schedule as needed" > "$CRON_FILE"
  echo "$CRON_CMD" >> "$CRON_FILE"
  chmod 644 "$CRON_FILE"
  success "Cron job installed at $CRON_FILE (schedule: $SCAN_CRON)"
else
  info "No cron schedule set — the web UI handles polling automatically (configurable in Settings)."
  info "To add a standalone cron scanner, re-run with:  SCAN_CRON='*/30 * * * *' sudo -E bash install.sh"
fi

# ── Log rotation ───────────────────────────────────────────────────────────
cat > /etc/logrotate.d/alertle <<'EOF'
/var/log/alertle-scan.log {
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
if systemctl is-active --quiet alertle; then
  success "alertle is running"
else
  warn "Service did not start cleanly. Check logs:"
  echo "       journalctl -u alertle -n 30"
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo
echo -e "  Web UI  →  ${CYAN}http://$(hostname -I | awk '{print $1}'):${WEB_PORT}${RESET}"
echo
echo -e "  ${BOLD}Next steps:${RESET}"
echo -e "    1. Open the web UI and paste your XMLTV URL under Settings → EPG Source"
echo -e "    2. Add subscriptions on the Subscriptions page or via Browse EPG"
echo -e "    3. Add at least one notification endpoint in Settings"
echo
echo -e "  ${BOLD}Useful commands:${RESET}"
echo -e "    View logs   →  journalctl -u alertle -f"
echo -e "    Restart     →  systemctl restart alertle"
echo -e "    Stop        →  systemctl stop alertle"
echo -e "    Config file →  ${INSTALL_DIR}/config.yaml"
echo
