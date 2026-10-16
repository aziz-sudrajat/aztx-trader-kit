# Termux Futures Cockpit

TUI (curses) untuk memantau & mengelola posisi **Binance Futures** langsung dari Termux/SSH — dibuat untuk trader yang trading dari HP.

Fitur:

- Auto-refresh tiap 5 detik: equity, available balance, unrealized PnL total
- Tabel posisi: SYMBOL | SIDE | QTY | ENTRY | MARK | uPNL | ROE% (hijau = profit, merah = loss)
- Close posisi terpilih (market, reduce-only) atau close SEMUA posisi — selalu dengan konfirmasi `y/n`
- API key/secret **hanya** dari environment, tidak pernah dari file
- Stdlib only (Python ≥ 3.10), tanpa dependensi

## Cara pakai di Termux

```bash
pkg install python -y
# ambil folder ini ke HP (git clone / copy), lalu:
cd termux-futures-cockpit
export BINANCE_API_KEY="isi_key_kamu"
export BINANCE_API_SECRET="isi_secret_kamu"
python3 cockpit.py
```

Key diketik manual via `export` — jangan simpan di file apapun. Di dashboard API Binance: aktifkan **Enable Futures**, matikan **Enable Withdrawals**.

## Tombol

| Tombol | Aksi |
|--------|------|
| `↑` / `↓` (atau `k` / `j`) | Pilih posisi |
| `c` | Close posisi terpilih (market, reduce-only) + konfirmasi `y/n` |
| `a` | Close SEMUA posisi + konfirmasi `y/n` |
| `r` | Refresh manual |
| `q` | Keluar |

Terminal minimal 80x24 — kalau kekecilan akan ada pesan jelas.

## Uji tanpa network

```bash
python3 cockpit.py --self-check
```

## ⚠️ Peringatan risiko

- Tombol `c` dan `a` mengirim **order MARKET beneran** ke Binance. Sekali `y`, posisi langsung tertutup di harga pasar — tidak bisa dibatalkan.
- Tool ini tidak memberi saran trading. Semua keputusan dan risikonya milikmu.
- Jaga API key/secret seperti password: jangan screenshot, jangan forward, jangan commit.

---

Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com> — https://github.com/aziz-sudrajat
