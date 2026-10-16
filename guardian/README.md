# 🛡️ futures-guardian

Penjaga risiko mandiri untuk **Binance USD-M Futures**. Berjalan di samping bot
trading apa pun — tugasnya satu: **mencegah akun rugi harian melebihi batas
yang kamu tentukan**.

## Cara kerja

Setiap cycle (default 15 detik):

1. Ambil ekuitas akun = `totalWalletBalance + totalUnrealizedProfit`.
2. Bandingkan dengan **ekuitas awal hari (UTC)** yang disimpan di `state/`.
3. `day_pnl = ekuitas_sekarang − ekuitas_awal_hari`.
4. Jika `day_pnl ≤ −max_daily_loss_usdt`:
   - tutup **SEMUA** posisi terbuka (market, reduce-only),
   - batalkan **semua** open order,
   - **kunci** guardian sampai hari UTC berikutnya,
   - kirim alert ke Telegram.

Kunci dibuka otomatis saat hari UTC berganti (atau manual via `/unlock`).

## Fitur

- 100% Python standard library — tanpa `pip install` apa pun.
- Signed request HMAC-SHA256 implementasi sendiri (urllib).
- Retry diam-diam 2x untuk gangguan jaringan sesaat (pola teruji dari proyek shellgram).
- Perintah Telegram (hanya dari chat id resmi): `/status`, `/kill`, `/unlock`, `/help`.
- `/kill` darurat: flatten semua posisi + kunci, kapan pun, dari HP.
- Kunci API tidak pernah di-hardcode — hanya dari `~/.futures-guardian.env` (chmod 600).

## Instalasi

```bash
sudo ./install.sh
```

Installer akan: cek root & python3.10+, menanyakan token Telegram / chat id /
batas rugi / API key Binance (diketik tanpa echo), menulis `config.json` dan
`~/.futures-guardian.env` (keduanya chmod 600), menjalankan `--self-check`,
lalu memasang + menyalakan systemd service `futures-guardian`.

## Konfigurasi manual

Salin `config.example.json` menjadi `config.json` dan isi nilainya.
`telegram_bot_token` dan `telegram_chat_id` boleh juga diisi via env
`FG_TELEGRAM_BOT_TOKEN` / `FG_TELEGRAM_CHAT_ID`.

| Key | Default | Arti |
|---|---|---|
| `max_daily_loss_usdt` | `5.0` | Batas rugi harian (USDT). Tersentuh → flatten + kunci. |
| `lock_until_next_day` | `true` | Kunci sampai hari UTC berikutnya setelah breach. |
| `poll_seconds` | `15` | Interval cycle (detik). |
| `state_dir` | `"state"` | Folder state (relatif terhadap folder config). |
| `binance_base_url` | `https://fapi.binance.com` | Endpoint Binance Futures. |

API key/secret: buat file `~/.futures-guardian.env` dari `.env.example`,
lalu `chmod 600 ~/.futures-guardian.env`.

## Menjalankan manual

```bash
python3 guardian.py --self-check   # validasi tanpa network (wajib PASS)
python3 guardian.py --once          # satu cycle, cetak laporan JSON
python3 guardian.py                # daemon loop (butuh config.json + env)
```

## Perintah Telegram

- `/status` — ekuitas, day PnL (UTC), status kunci, posisi terbuka.
- `/kill` — tutup SEMUA posisi + batalkan order + kunci guardian.
- `/unlock` — buka kunci guardian.
- `/help` — daftar perintah.

## ⚠️ Peringatan risiko

- futures-guardian adalah **jaring pengaman, bukan jaminan**. Slippage,
  gap harga, atau API down tetap bisa membuat rugi melebihi batas.
- Ini **bukan nasihat finansial**. Trading futures berisiko tinggi.
- Uji dulu dengan `--once` dan batas kecil sebelum mempercayakan akun besar.
- Di Binance: **matikan permission Withdrawals** pada API key, dan gunakan
  IP whitelist bila memungkinkan.
- Dari sebagian region, `fapi.binance.com` tidak bisa diakses langsung
  (geo-block) — jalankan guardian di mesin yang bisa menjangkaunya.

## Struktur file

```
futures-guardian/
├── guardian.py          # daemon utama (stdlib only)
├── install.sh           # installer interaktif + systemd
├── config.example.json  # contoh konfigurasi (placeholder)
├── .env.example         # contoh file env Binance (placeholder)
├── .gitignore
└── README.md
```

---
Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat. All rights reserved.
