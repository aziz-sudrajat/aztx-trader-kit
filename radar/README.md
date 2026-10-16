# funding-radar

Monitor **funding rate** perpetual futures lintas exchange — **Binance**,
**Bybit**, **Gate.io**, dan **OKX**. Setiap ada koin yang spread funding
antar-exchange-nya melebar melewati ambang batas, bot mengirim **alert
Telegram**.

Gunanya: memantau peluang **arbitrase funding** (long di exchange yang
funding-nya murah/negatif, short di yang mahal) atau mendeteksi koin yang
sedang *crowded* (funding ekstrem di satu exchange).

- 100% Python standard library — tanpa `pip install` apa pun (Python ≥ 3.10).
- Semua data dari endpoint **publik**, tanpa API key exchange.
- Kalau satu exchange down, otomatis di-skip — scan tetap jalan.

## Instalasi

1. Pastikan Python ≥ 3.10: `python3 --version`
2. Salin folder ini ke mesin yang mau dipakai.
3. `cp config.example.json config.json`, lalu isi:
   - `telegram_bot_token` — token bot dari [@BotFather](https://t.me/BotFather)
   - `telegram_chat_id` — chat ID kamu (atau grup); bisa didapat dari
     [@userinfobot](https://t.me/userinfobot)
4. (Opsional) sesuaikan `spread_alert_pct`, `top_n_okx`, dsb.

## Cara pakai

```bash
python3 radar.py --once        # satu scan: cetak tabel + kirim alert (buat cron)
python3 radar.py --loop        # daemon: scan berulang tiap scan_interval_minutes
python3 radar.py --self-check  # uji internal (tanpa network)
python3 radar.py -v --once     # mode verbose
```

## Contoh cron (tiap 15 menit)

```cron
*/15 * * * * /usr/bin/python3 /path/ke/funding-radar/radar.py --once >> /path/ke/funding-radar/radar.log 2>&1
```

## Contoh output

```
COIN         BINANCE    BYBIT      GATE       OKX        SPREAD
BTCUSDT      +0.0100%   +0.1300%   +0.0450%   +0.0600%   0.1200%  🔔
ETHUSDT      +0.0080%   +0.0110%   +0.0090%   +0.0100%   0.0030%
SOLUSDT      -0.0050%   +0.0200%   -         +0.0120%   0.0250%

3 koin terpantau | ambang alert 0.0500%
```

Contoh pesan Telegram yang dikirim:

```
🔔 Funding spread 0.1200% — BTCUSDT
Binance: +0.0100%
Bybit: +0.1300%
Gate.io: +0.0450%
OKX: +0.0600%
Spread (max-min): 0.1200% (ambang 0.0500%)
```

## Cara kerja

1. Ambil funding rate per simbol dari 4 exchange (endpoint publik).
2. Normalisasi simbol ke format `BASE+QUOTE` (mis. `BTC_USDT` → `BTCUSDT`).
3. Untuk tiap koin yang muncul di **≥ 2 exchange**, hitung
   `spread = max(rate) − min(rate)`.
4. Jika spread ≥ `spread_alert_pct` (default `0.05` = **0.05%**), kirim
   alert Telegram. Satu alert per koin per `dedup_hours` (default 8 jam)
   agar tidak spam.
5. Gangguan jaringan sesaat (`RemoteDisconnected`, timeout, dsb.) di-retry
   diam-diam 2x sebelum menyerah.

## Catatan

- Data publik bisa terlambat atau tidak akurat — selalu verifikasi di
  exchange sebelum bertindak.
- **Bukan saran finansial.** Funding hanyalah satu dari banyak faktor.
- `config.json` berisi token Telegram pribadimu — jangan pernah
  di-commit / dibagikan (sudah masuk `.gitignore`).
- Binance memblokir beberapa region/IP; kalau `fapi.binance.com` tidak
  bisa dijangkau dari mesinmu, exchange itu otomatis di-skip.

---
© 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat
