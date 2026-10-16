#!/usr/bin/env bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
#
# Installer futures-guardian: cek root, cek python3.10+, wizard config.json
# + file env Binance, self-check, pasang systemd service.
set -euo pipefail

INSTALL_DIR="/opt/futures-guardian"
SERVICE_FILE="/etc/systemd/system/futures-guardian.service"

die() { echo "ERROR: $*" >&2; exit 1; }
info() { echo "== $*"; }

[ "$(id -u)" -eq 0 ] || die "jalankan sebagai root: sudo ./install.sh"
command -v python3 >/dev/null 2>&1 || die "python3 tidak ditemukan"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "butuh python3 >= 3.10 (terdeteksi: $(python3 --version 2>&1))"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$SCRIPT_DIR/guardian.py" ] || die "guardian.py tidak ditemukan di $SCRIPT_DIR"

info "futures-guardian installer"
echo "API key Binance hanya disimpan lokal di ~/.futures-guardian.env (chmod 600)."
echo

read -rsp "Telegram bot token: " TG_TOKEN; echo
[ -n "$TG_TOKEN" ] || die "token kosong"
read -rp "Telegram chat id (angka): " TG_CHAT
[[ "$TG_CHAT" =~ ^-?[0-9]+$ ]] || die "chat id harus angka"
read -rp "Max daily loss (USDT) [5.0]: " MAX_LOSS; MAX_LOSS="${MAX_LOSS:-5.0}"
read -rp "Poll interval detik [15]: " POLL; POLL="${POLL:-15}"
read -rsp "BINANCE_API_KEY: " BKEY; echo
[ -n "$BKEY" ] || die "api key kosong"
read -rsp "BINANCE_API_SECRET: " BSECRET; echo
[ -n "$BSECRET" ] || die "api secret kosong"
echo
echo "PERINGATAN: di Binance, matikan permission Withdrawals untuk API key ini."
echo "Disarankan juga membatasi IP (IP whitelist) bila memungkinkan."
read -rp "Lanjut install? [y/N]: " OK
[[ "$OK" =~ ^[yY]$ ]] || die "dibatalkan"

info "menyalin ke $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/state"
cp "$SCRIPT_DIR/guardian.py" "$INSTALL_DIR/guardian.py"
chmod 700 "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/guardian.py"

info "menulis config.json (600)"
printf '{\n  "telegram_bot_token": "%s",\n  "telegram_chat_id": %s,\n  "state_dir": "state",\n  "poll_seconds": %s,\n  "max_daily_loss_usdt": %s,\n  "lock_until_next_day": true,\n  "binance_base_url": "https://fapi.binance.com"\n}\n' \
  "$TG_TOKEN" "$TG_CHAT" "$POLL" "$MAX_LOSS" > "$INSTALL_DIR/config.json"
chmod 600 "$INSTALL_DIR/config.json"
python3 -m json.tool "$INSTALL_DIR/config.json" >/dev/null \
  || die "config.json tidak valid"

info "menulis ~/.futures-guardian.env (600)"
printf 'BINANCE_API_KEY=%s\nBINANCE_API_SECRET=%s\n' "$BKEY" "$BSECRET" \
  > ~/.futures-guardian.env
chmod 600 ~/.futures-guardian.env

info "self-check"
python3 "$INSTALL_DIR/guardian.py" --self-check || die "self-check GAGAL"

info "memasang systemd service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=futures-guardian - Binance Futures risk guard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=FG_CONFIG=$INSTALL_DIR/config.json
Environment=FG_ENV_FILE=/root/.futures-guardian.env
ExecStart=/usr/bin/python3 $INSTALL_DIR/guardian.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now futures-guardian.service

echo
info "SELESAI. Status service:"
systemctl --no-pager status futures-guardian.service | head -12 || true
echo
echo "Perintah Telegram: /status /kill /unlock /help"
echo "Log: journalctl -u futures-guardian.service -f"
