#!/usr/bin/env bash
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
#
# Instalasi healthping: cek root, cek python3.10+, wizard config.json,
# self-check, lalu pasang + enable systemd service.
set -euo pipefail

INSTALL_DIR="${1:-/opt/healthping}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- harus root
if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: jalankan sebagai root: sudo ./install.sh" >&2
  exit 1
fi

# --- cek python3.10+
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 tidak ditemukan. Install dulu: apt install python3" >&2
  exit 1
fi
PYV="$(python3 -c 'import sys; print(sys.version_info[0]*100 + sys.version_info[1])')"
if [ "$PYV" -lt 310 ]; then
  echo "ERROR: butuh python3.10+, yang ada: $(python3 --version)" >&2
  exit 1
fi
echo "OK: $(python3 --version)"

# --- salin program
mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/healthping.py" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/healthping.py"
echo "OK: terpasang di $INSTALL_DIR"

# --- wizard config.json (token tidak di-echo)
if [ -f "$INSTALL_DIR/config.json" ]; then
  echo "config.json sudah ada — tidak diubah."
else
  echo ""
  echo "=== Wizard konfigurasi Telegram ==="
  echo "Buat bot via @BotFather, lalu chat bot-mu sekali dan ambil chat ID via @userinfobot."
  read -rsp "Bot token   : " TOKEN; echo ""
  read -rp  "Chat ID     : " CHATID
  if [ -z "${TOKEN:-}" ]; then echo "ERROR: token kosong." >&2; exit 1; fi
  if [ -z "${CHATID:-}" ]; then echo "ERROR: chat ID kosong." >&2; exit 1; fi
  cat > "$INSTALL_DIR/config.json" <<EOF
{
  "_copyright": "Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat",
  "telegram_bot_token": "$TOKEN",
  "telegram_chat_id": "$CHATID",
  "remind_minutes": 60,
  "checks_file": "checks.json",
  "state_file": "state.json"
}
EOF
  chmod 600 "$INSTALL_DIR/config.json"
  echo "OK: config.json tersimpan (mode 600)."
fi

# --- checks.json dari contoh bila belum ada
if [ ! -f "$INSTALL_DIR/checks.json" ]; then
  cp "$SRC_DIR/checks.example.json" "$INSTALL_DIR/checks.json"
  echo "OK: checks.json dibuat dari contoh — silakan edit sesuai kebutuhan."
else
  echo "checks.json sudah ada — tidak diubah."
fi

# --- self-check sebelum service dipasang
echo ""
echo "=== Self-check ==="
python3 "$INSTALL_DIR/healthping.py" --self-check

# --- systemd service
cat > /etc/systemd/system/healthping.service <<EOF
[Unit]
Description=healthping — generic health monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/healthping.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now healthping.service
echo ""
echo "OK: service healthping aktif. Cek status: systemctl status healthping"
echo "Lihat log: journalctl -u healthping -f"
