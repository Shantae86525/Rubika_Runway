#!/usr/bin/env bash
# =========================================================================== #
#  V_2rubby — one-command installer for the MASTER server.
#  Usage:
#     chmod +x setup.sh
#     ./setup.sh
#
#  It does everything: system packages, virtualenv, dependencies, generates the
#  WORKER_SECRET, asks for your Telegram settings, writes .env, and (optionally)
#  installs a systemd service named "rubika-master" so the bot stays up.
#
#  No secrets are hard-coded here — you paste them when prompted, and they only
#  ever land in the local .env file (which is git-ignored).
# =========================================================================== #
set -euo pipefail

APP_NAME="rubika-master"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_BIN="${PYTHON:-python3}"

c_green() { printf "\033[32m%s\033[0m\n" "$1"; }
c_yellow() { printf "\033[33m%s\033[0m\n" "$1"; }
c_red() { printf "\033[31m%s\033[0m\n" "$1"; }
line() { printf "%s\n" "------------------------------------------------------------"; }

line
c_green "🚀 V_2rubby installer — project: ${APP_NAME}"
c_green "📁 directory: ${PROJECT_DIR}"
line

# --------------------------------------------------------------------------- #
# 1) System packages (best-effort; skipped if not Debian/Ubuntu or no sudo).
# --------------------------------------------------------------------------- #
if command -v apt-get >/dev/null 2>&1; then
    c_yellow "📦 installing system packages (python venv, pip, git) ..."
    SUDO=""
    if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
    $SUDO apt-get update -y || true
    $SUDO apt-get install -y python3-venv python3-pip git || true
else
    c_yellow "⏭  apt-get not found — make sure python3-venv, pip and git are installed."
fi

# --------------------------------------------------------------------------- #
# 2) Virtualenv + Python dependencies.
# --------------------------------------------------------------------------- #
c_yellow "🐍 creating virtualenv (venv) ..."
"$PY_BIN" -m venv "${PROJECT_DIR}/venv"
# shellcheck disable=SC1091
source "${PROJECT_DIR}/venv/bin/activate"

c_yellow "⬇️  installing Python dependencies (this can take a few minutes) ..."
pip install --upgrade pip >/dev/null
pip install -r "${PROJECT_DIR}/requirements.txt"

# --------------------------------------------------------------------------- #
# 3) .env — keep existing one if present, otherwise build it interactively.
# --------------------------------------------------------------------------- #
ENV_FILE="${PROJECT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    c_yellow "ℹ️  .env already exists — keeping it (delete it to reconfigure)."
else
    c_yellow "📝 let's fill in your settings (paste each value, press Enter):"
    read -r -p "  API_ID:        " API_ID
    read -r -p "  API_HASH:      " API_HASH
    read -r -p "  BOT_TOKEN:     " BOT_TOKEN
    read -r -p "  OWNER_ID:      " OWNER_ID
    read -r -p "  LOG_GROUP_ID:  " LOG_GROUP_ID

    # Generate the Fernet key used to encrypt worker SSH credentials.
    c_yellow "🔐 generating WORKER_SECRET ..."
    WORKER_SECRET="$("$PY_BIN" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

    cat > "$ENV_FILE" <<EOF
# ---- Telegram (control panel) ----
API_ID=${API_ID}
API_HASH=${API_HASH}
BOT_TOKEN=${BOT_TOKEN}
OWNER_ID=${OWNER_ID}
LOG_GROUP_ID=${LOG_GROUP_ID}

# ---- Sending behaviour ----
SEND_DELAY=1.0
FORWARD_MARKER=کد135
MAX_ERRORS=3
VERSION=V2

# ---- Worker subsystem ----
MODE=master
WORKER_SECRET=${WORKER_SECRET}
MASTER_AS_WORKER=true
GIT_REPO_URL=https://github.com/Shantae86525/Rubika_Runway
GIT_BRANCH=main
WORKER_API_PORT=8765
WORKER_API_TOKEN=
HEALTH_URL=https://upmessenger490.iranlms.ir/UploadFile.ashx
HEALTH_INTERVAL=1800
PING_GREEN_MS=800
PING_YELLOW_MS=2000
TIMEZONE=Asia/Tehran
EOF
    c_green "✅ .env written."
fi

# --------------------------------------------------------------------------- #
# 4) Optional systemd service so the bot survives reboots / disconnects.
# --------------------------------------------------------------------------- #
line
if command -v systemctl >/dev/null 2>&1 && [ "$(id -u)" -eq 0 ]; then
    read -r -p "🛠  install systemd service '${APP_NAME}' so it runs in the background? [y/N] " WANT_SVC
    if [[ "${WANT_SVC:-N}" =~ ^[Yy]$ ]]; then
        SERVICE_PATH="/etc/systemd/system/${APP_NAME}.service"
        cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=V_2rubby Master (${APP_NAME})
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/venv/bin/python ${PROJECT_DIR}/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable --now "${APP_NAME}"
        c_green "✅ service '${APP_NAME}' installed and started."
        echo "   • live logs : journalctl -u ${APP_NAME} -f"
        echo "   • restart   : systemctl restart ${APP_NAME}"
        echo "   • stop      : systemctl stop ${APP_NAME}"
        SVC_DONE=1
    fi
fi

# --------------------------------------------------------------------------- #
# 5) Done — how to run.
# --------------------------------------------------------------------------- #
line
c_green "🎉 setup complete for '${APP_NAME}'."
if [ "${SVC_DONE:-0}" -ne 1 ]; then
    echo "To run it now (foreground):"
    echo "    source venv/bin/activate && python main.py"
    echo
    echo "Tip: re-run this script as root to install the '${APP_NAME}' systemd service."
fi
line
