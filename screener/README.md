# strict-screener

Screener signal crypto multi-timeframe dengan quality scoring. Memindai perpetual
futures USDT, menilai keselarasan tren antar timeframe, dan memberi skor kualitas
0–100 per koin — semuanya dari **data publik** (tanpa API key, tanpa login).

## Cara pakai

Butuh Python 3.10+. Tidak ada dependensi eksternal (stdlib only).

```bash
python3 screener.py                        # tabel peringkat default
python3 screener.py --top 10               # tampilkan 10 teratas
python3 screener.py --min-quality 70       # hanya skor >= 70
python3 screener.py --universe-top-n 100   # universe 100 simbol teratas
python3 screener.py --show-neutral         # tampilkan juga yang NEUTRAL
python3 screener.py --json > hasil.json     # output JSON
python3 screener.py --self-check           # uji logika (tanpa network)
```

## Cara kerja

1. **Universe**: ambil semua perpetual `*-USDT-SWAP` dari ticker publik OKX,
   urutkan berdasarkan volume 24 jam (quote), ambil top N
   (`--universe-top-n`, default 50).
2. **Data**: untuk tiap simbol, ambil 100 candle di timeframe 15m, 1h, 4h, 1d.
3. **Indikator** (dihitung sendiri): EMA50 per timeframe, RSI(14) di 15m.
4. **Arah per timeframe**: LONG bila close > EMA50, SHORT bila close < EMA50,
   NEUTRAL bila data kurang.
5. **Skor kualitas 0–100**:
   - +25 — 4h & 1d searah
   - +20 — 1h searah dengan 4h
   - +15 — 15m searah dengan 4h
   - +10 — RSI(14) 15m mendukung arah (LONG: RSI > 55, SHORT: RSI < 45)
   - +10 — harga < 1% dari EMA50 15m (entry tidak ketinggalan harga)
   - +0–20 — proporsional terhadap volume 24h (skala log antar simbol)
   - Penalti: bila arah 1d NEUTRAL (tak bisa ditentukan karena data kurang),
     total skor dikali 0.7.
6. **Arah final**: LONG/SHORT hanya bila 4h, 1h, dan 15m searah; selain itu
   NEUTRAL dan disembunyikan kecuali `--show-neutral`.

Simbol yang gagal diambil datanya di-skip dengan warning (fail-closed) —
screener tidak crash karena satu simbol bermasalah. Gangguan jaringan sesaat
di-retry diam-diam 2x.

## Contoh output

```
RANK | SYMBOL   | DIR     | QUALITY | 4H      | 1H      | 15M     |   RSI
--------------------------------------------------------------------------------
   1 | BTC      | LONG    |    96.4 | LONG    | LONG    | LONG    |    62
   2 | ETH      | LONG    |    91.2 | LONG    | LONG    | LONG    |    58
   3 | SOL      | SHORT   |    84.7 | SHORT   | SHORT   | SHORT   |    38
```

(Contoh ilustrasi — angka aktual tergantung kondisi market saat dijalankan.)

## Catatan

- Ini alat bantu analisa, **bukan saran finansial**. Skor tinggi bukan jaminan
  profit; selalu gunakan manajemen risiko sendiri.
- Data dari endpoint publik OKX (`api/v5/market/tickers`, `api/v5/market/candles`).

---
Copyright (c) 2026 aziz-sudrajat — https://github.com/aziz-sudrajat
