# 🧰 aztx-trader-kit

Toolkit mandiri untuk trader Binance Futures: pengaman risiko, radar funding-rate,
kokpit trading via terminal, screener signal multi-timeframe, dan monitor
kesehatan generik — semuanya **stdlib-only** (Python ≥ 3.10, tanpa `pip install`).

Repo: https://github.com/aziz-sudrajat/aztx-trader-kit

## Isi toolkit

| Direktori | Tool | Fungsi singkat |
|---|---|---|
| `guardian/` | futures-guardian | Batas rugi harian → auto-flatten + lock; kill switch via Telegram |
| `radar/` | funding-radar | Monitor spread funding-rate Binance/Bybit/Gate.io/OKX, alert Telegram |
| `cockpit/` | termux-futures-cockpit | TUI (curses) posisi & PnL Binance Futures dari Termux/SSH |
| `screener/` | strict-screener | Screener MTF (15m/1h/4h/1d) + quality score 0–100, data publik OKX |
| `healthping/` | healthping | Monitor generik (http/tcp/disk/proc/cmd) + alert Telegram |
| `common/` | — | Modul bersama: `http`, `binance`, `telegram` (dipakai semua tool di atas) |

## Quickstart

Tiap tool mandiri — masuk ke direktorinya, lalu:

```bash
# 1. guardian — jaga akun dari rugi harian berlebihan
cd guardian
cp config.example.json config.json   # isi token Telegram & batas rugi
# API key HANYA di ~/.futures-guardian.env (chmod 600), tidak di repo!
sudo ./install.sh                    # pasang sebagai systemd service
# atau coba satu cycle:
python3 guardian.py --once

# 2. radar — pantau spread funding-rate (tanpa API key)
cd ../radar
cp config.example.json config.json   # isi token Telegram
python3 radar.py --once              # satu scan + tabel
python3 radar.py --loop              # daemon

# 3. cockpit — TUI trading dari HP/SSH (API key via env, bukan file)
cd ../cockpit
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
python3 cockpit.py                   # q=keluar, c=tutup posisi, a=tutup semua

# 4. screener — cari koin yang trennya selaras di semua timeframe
cd ../screener
python3 screener.py --top 10 --min-quality 70
python3 screener.py --json > hasil.json

# 5. healthping — monitor apa saja, alert saat status berubah
cd ../healthping
cp config.example.json config.json       # token Telegram
cp checks.example.json checks.json       # definisi check
sudo ./install.sh
```

Semua tool punya `--self-check` untuk verifikasi tanpa network:

```bash
python3 guardian/guardian.py --self-check
python3 radar/radar.py --self-check
python3 cockpit/cockpit.py --self-check
python3 screener/screener.py --self-check
python3 healthping/healthping.py --self-check
```

## Modul bersama (`common/`)

- `common/http.py` — `api_call()` generik (urllib): retry diam-diam 2x untuk
  transient network errors (`RemoteDisconnected`, `IncompleteRead`, timeout,
  `ConnectionError`, termasuk yang terbungkus `URLError`).
- `common/binance.py` — `sign_params()` HMAC-SHA256 + `signed_request()`
  untuk Binance Futures API.
- `common/telegram.py` — `send_message()` / `api_call()` Telegram Bot API
  (token disamarkan di log).

Tiap tool menambahkan repo root ke `sys.path` lalu `from common import ...`,
jadi harus dijalankan dari dalam struktur direktori ini.

## Keamanan

- **Tidak ada secret di repo ini.** API key/secret hanya dibaca dari
  environment atau file env `chmod 600` di luar repo.
- Contoh config (`*.example.json`) hanya berisi placeholder.
- Tool trading mengirim order market beneran — pahami risikonya
  (slippage, geo-block). Bukan saran finansial.

---

Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat. All rights reserved.
