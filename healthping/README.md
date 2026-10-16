# healthping — monitor kesehatan generik dengan alert Telegram

`healthping` memantau apa pun yang kamu definisikan di `checks.json` (web, port TCP,
disk, proses, perintah shell), lalu mengirim alert Telegram **hanya saat status
berubah** (OK → FAIL atau FAIL → OK). Selama masih FAIL, ada pengingat berkala
(default tiap 60 menit) supaya nggak spam tiap menit.

Stdlib Python only (>= 3.10), tanpa dependensi eksternal.

## Instalasi

Sebagai root, di mesin yang mau dipantau:

```bash
sudo ./install.sh
```

Installer akan:

1. Cek root + python3.10+.
2. Wizard `config.json` (bot token — diketik tanpa echo — + chat ID). File disimpan
   mode 600.
3. Buat `checks.json` dari contoh bila belum ada (silakan edit).
4. Jalankan `--self-check`.
5. Pasang + enable systemd service `healthping`.

Buat bot Telegram via [@BotFather](https://t.me/BotFather), chat bot-mu sekali,
lalu ambil chat ID via [@userinfobot](https://t.me/userinfobot).

Perintah service:

```bash
systemctl status healthping
journalctl -u healthping -f
systemctl restart healthping
```

## Format `checks.json`

Array berisi object, masing-masing:

| Field              | Wajib | Keterangan                                              |
|--------------------|-------|----------------------------------------------------------|
| `name`             | Ya    | Nama unik check (dipakai di alert + `state.json`)        |
| `type`             | Ya    | `http` / `tcp` / `disk` / `proc` / `cmd`                 |
| `interval_seconds` | Tidak | Tiap berapa detik dijalankan (default 60, minimal 5)     |
| `params`           | Tidak | Parameter sesuai tipe (lihat bawah)                      |

### `http` — cek web/API

```json
{"name": "situs-utama", "type": "http", "interval_seconds": 60,
 "params": {"url": "https://example.com", "expect_status": 200, "timeout": 10}}
```

`expect_status` default 200, `timeout` default 10 detik. OK jika status HTTP sesuai.

### `tcp` — cek port terbuka

```json
{"name": "ssh-lokal", "type": "tcp", "interval_seconds": 60,
 "params": {"host": "127.0.0.1", "port": 22, "timeout": 5}}
```

OK jika koneksi TCP berhasil. `timeout` default 5 detik.

### `disk` — cek pemakaian disk

```json
{"name": "disk-root", "type": "disk", "interval_seconds": 300,
 "params": {"path": "/", "max_pct": 90}}
```

OK jika pemakaian di bawah `max_pct` (default 90).

### `proc` — cek proses jalan

```json
{"name": "bot-jalan", "type": "proc", "interval_seconds": 60,
 "params": {"name": "bot.py"}}
```

OK jika ada proses yang command line-nya mengandung string `name` (dibaca dari `/proc`).

### `cmd` — cek perintah shell

```json
{"name": "backup-harian", "type": "cmd", "interval_seconds": 3600,
 "params": {"shell": "test -f /var/backups/daily.tgz", "expect_exit": 0, "timeout": 30}}
```

OK jika exit code sesuai `expect_exit` (default 0). `timeout` default 30 detik.

## Format `config.json`

```json
{
  "telegram_bot_token": "123456:ABC...",
  "telegram_chat_id": "123456789",
  "remind_minutes": 60,
  "checks_file": "checks.json",
  "state_file": "state.json"
}
```

`remind_minutes` = jeda pengingat selama status masih FAIL (default 60).

## Cara pakai

```bash
# jalankan semua check sekali, print hasil (tanpa kirim alert)
python3 healthping.py --once

# uji internal: semua tipe check + logika transisi/dedup alert (tanpa network eksternal)
python3 healthping.py --self-check

# jalan sebagai daemon (dibaca config.json di working directory)
python3 healthping.py
```

## Cara kerja alert

- Status tiap check disimpan di `state.json` (`last_status`, `last_change`, `last_alert`).
- Alert Telegram dikirim **hanya saat transisi**: pertama kali FAIL, dan saat pulih (FAIL → OK).
- Selama tetap FAIL: pengingat maksimal tiap `remind_minutes`.
- OK pertama kali (saat daemon start): diam, tidak ada alert.
- Koneksi ke Telegram yang putus sesaat di-retry otomatis 2x secara diam-diam.

Contoh `checks.json` 5 tipe ada di `checks.example.json`,
contoh konfigurasi di `config.example.json`.

---

Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat
