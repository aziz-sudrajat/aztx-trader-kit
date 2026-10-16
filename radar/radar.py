#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""funding-radar -- monitor funding-rate perpetual futures lintas exchange.

Mengambil funding rate publik (TANPA API key) dari Binance, Bybit, Gate.io,
dan OKX, menghitung spread antar-exchange per koin, dan mengirim alert
Telegram saat spread melebar melewati ambang batas.

Kegunaan: memantau peluang arbitrase funding / mendeteksi koin yang
sedang crowded (funding ekstrem di satu exchange).

Bukan saran finansial. Data publik bisa terlambat atau tidak akurat.
"""

from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from common import http as _http
    from common import telegram as _telegram
except ImportError:
    sys.exit("ERROR: modul 'common' tidak ditemukan — "
             "jalankan dari dalam direktori aztx-trader-kit/")

import argparse
import http.client
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("funding-radar")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
DEFAULT_STATE = os.path.join(BASE_DIR, "alert_state.json")

EXCHANGE_ORDER = ("binance", "bybit", "gate", "okx")
EXCHANGE_LABEL = {
    "binance": "Binance",
    "bybit": "Bybit",
    "gate": "Gate.io",
    "okx": "OKX",
}

CONFIG_DEFAULTS = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # 0.05 artinya 0.05% (bukan 5%!). Spread max-min antar-exchange
    # dalam satuan persen yang memicu alert.
    "spread_alert_pct": 0.05,
    "quote_filter": ["USDT"],
    "top_n_okx": 30,
    "scan_interval_minutes": 15,
    # Satu alert per koin per X jam (anti-spam).
    "dedup_hours": 8,
}


# --------------------------------------------------------------------------
# HTTP helpers (dengan retry diam-diam untuk gangguan jaringan sesaat)
# --------------------------------------------------------------------------

# Klasifikasi transient error dipakai dari modul bersama (implementasi identik).
_is_transient = _http.is_retryable


def http_get_json(url: str, timeout: int = 20, retries: int = 2,
                  label: str = "GET") -> object:
    """GET JSON dengan retry diam-diam untuk transient network errors.

    (Retry didelegasikan ke common.http.api_call; exception asli
    diangkat ulang tanpa dibungkus — perilaku identik.)
    """
    try:
        return _http.api_call("GET", url,
                              headers={"User-Agent": "funding-radar/1.0"},
                              timeout=timeout, _retries=retries)
    except (_http.HttpApiError, _http.RequestError) as exc:
        raise exc.original


# --------------------------------------------------------------------------
# Normalisasi simbol & parsing
# --------------------------------------------------------------------------

def normalize_symbol(raw: str) -> str:
    """'BTC-USDT' / 'BTC_USDT' / 'btcusdt' / 'ETH/USDT' -> 'BTCUSDT' dst."""
    return (raw.replace("-", "").replace("_", "")
               .replace("/", "").replace(" ", "").upper())


def okx_base(inst_id: str) -> str:
    """'BTC-USDT-SWAP' -> 'BTCUSDT'."""
    base = inst_id[:-5] if inst_id.endswith("-SWAP") else inst_id
    return normalize_symbol(base)


def parse_rate(value: object) -> float | None:
    """String funding seperti '0.0001' -> float; kosong/invalid -> None."""
    try:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def apply_quote_filter(rates: dict[str, float],
                       quotes: list[str]) -> dict[str, float]:
    """Hanya pertahankan simbol yang berakhiran salah satu quote."""
    return {s: r for s, r in rates.items()
            if any(s.endswith(q) for q in quotes)}


# --------------------------------------------------------------------------
# Fetcher per exchange (semua endpoint publik, tanpa API key)
# --------------------------------------------------------------------------

def fetch_binance() -> dict[str, float]:
    data = http_get_json("https://fapi.binance.com/fapi/v1/premiumIndex",
                         label="binance")
    out: dict[str, float] = {}
    for row in data:  # type: ignore[union-attr]
        sym = normalize_symbol(str(row.get("symbol", "")))
        rate = parse_rate(row.get("fundingRate"))
        if sym and rate is not None:
            out[sym] = rate
    return out


def fetch_bybit() -> dict[str, float]:
    data = http_get_json(
        "https://api.bybit.com/v5/market/tickers?category=linear",
        label="bybit")
    out: dict[str, float] = {}
    rows = (data.get("result", {}) or {}).get("list", [])  # type: ignore[union-attr]
    for row in rows:
        sym = normalize_symbol(str(row.get("symbol", "")))
        rate = parse_rate(row.get("fundingRate"))
        if sym and rate is not None:
            out[sym] = rate
    return out


def fetch_gate() -> dict[str, float]:
    data = http_get_json(
        "https://api.gateio.ws/api/v4/futures/usdt/contracts",
        label="gate")
    out: dict[str, float] = {}
    for row in data:  # type: ignore[union-attr]
        # name seperti 'BTC_USDT' -> 'BTCUSDT'
        sym = normalize_symbol(str(row.get("name", "")))
        rate = parse_rate(row.get("funding_rate"))
        if sym and rate is not None:
            out[sym] = rate
    return out


def fetch_okx(top_n: int = 30) -> dict[str, float]:
    tickers = http_get_json(
        "https://www.okx.com/api/v5/market/tickers?instType=SWAP",
        label="okx-tickers")

    def quote_vol(row: dict) -> float:
        try:
            return float(row.get("volCcy24h") or 0) * float(row.get("last") or 0)
        except (TypeError, ValueError):
            return 0.0

    rows = sorted(tickers.get("data", []) or [],  # type: ignore[union-attr]
                  key=quote_vol, reverse=True)[:max(1, top_n)]
    out: dict[str, float] = {}
    for row in rows:
        inst_id = str(row.get("instId", ""))
        if not inst_id:
            continue
        try:
            fr = http_get_json(
                "https://www.okx.com/api/v5/public/funding-rate?instId="
                + urllib.parse.quote(inst_id),
                label="okx-funding")
            items = (fr.get("data") or [{}])  # type: ignore[union-attr]
            rate = parse_rate(items[0].get("fundingRate"))
            sym = okx_base(inst_id)
            if sym and rate is not None:
                out[sym] = rate
        except Exception as exc:  # satu simbol gagal -> lanjut, jangan crash
            log.warning("okx funding %s gagal (di-skip): %s (%s)",
                        inst_id, exc, type(exc).__name__)
        time.sleep(0.1)  # jeda sopan antar request
    return out


def fetch_all(top_n_okx: int) -> dict[str, dict[str, float]]:
    """Ambil semua exchange; yang down di-skip dengan warning (no crash)."""
    results: dict[str, dict[str, float]] = {}
    jobs = (
        ("binance", fetch_binance, ()),
        ("bybit", fetch_bybit, ()),
        ("gate", fetch_gate, ()),
        ("okx", fetch_okx, (top_n_okx,)),
    )
    for name, fn, args in jobs:
        try:
            rates = fn(*args)
            log.info("%s: %d simbol funding", name, len(rates))
            results[name] = rates
        except Exception as exc:  # noqa: BLE001 - graceful degradation
            log.warning("%s gagal (di-skip): %s (%s)",
                        name, exc, type(exc).__name__)
    return results


# --------------------------------------------------------------------------
# Agregasi & spread
# --------------------------------------------------------------------------

def aggregate(all_rates: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Gabung per koin; hanya koin yang ada di >= 2 exchange."""
    coins: dict[str, dict[str, float]] = {}
    for exchange, rates in all_rates.items():
        for sym, rate in rates.items():
            coins.setdefault(sym, {})[exchange] = rate
    return {s: m for s, m in coins.items() if len(m) >= 2}


def spread_pct_of(rates: dict[str, float]) -> float:
    """Spread max-min dalam satuan persen (0.0007 -> 0.07)."""
    vals = list(rates.values())
    return (max(vals) - min(vals)) * 100.0


# --------------------------------------------------------------------------
# Dedup alert (satu alert per koin per X jam)
# --------------------------------------------------------------------------

def should_alert(state: dict[str, float], coin: str,
                 now: float, dedup_hours: float) -> bool:
    last = state.get(coin, 0.0)
    return (now - last) >= dedup_hours * 3600.0


def mark_alerted(state: dict[str, float], coin: str, now: float) -> None:
    state[coin] = now


def load_state(path: str) -> dict[str, float]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): float(v) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        log.warning("state %s korup, mulai fresh: %s", path, exc)
        return {}


def save_state(path: str, state: dict[str, float]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, text: str,
                  retries: int = 2) -> bool:
    """Kirim pesan Telegram; True jika terkirim.

    (Pengiriman + retry didelegasikan ke common.telegram.send_message;
    nilai kembali dan pesan log identik dengan implementasi asli.)
    """
    if not token or not chat_id:
        return False
    try:
        _telegram.send_message(token, chat_id, text, _retries=retries)
        return True
    except _telegram.TelegramApiError as exc:
        if exc.payload is not None:
            log.warning("telegram menolak pesan: %s", exc.payload)
        else:
            log.error("telegram gagal: %s (%s)",
                      exc.original, type(exc.original).__name__)
        return False


def fmt_pct(frac: float) -> str:
    return "%+.4f%%" % (frac * 100.0)


def build_alert(coin: str, rates: dict[str, float],
                spread_pct: float, threshold: float) -> str:
    lines = ["🔔 Funding spread %.4f%% — %s" % (spread_pct, coin)]
    for ex in EXCHANGE_ORDER:
        if ex in rates:
            lines.append("%s: %s" % (EXCHANGE_LABEL[ex], fmt_pct(rates[ex])))
    lines.append("Spread (max-min): %.4f%% (ambang %.4f%%)"
                 % (spread_pct, threshold))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Scan & tampilan
# --------------------------------------------------------------------------

def run_scan(cfg: dict, state: dict[str, float], state_path: str,
             do_alert: bool = True) -> list[tuple]:
    all_rates = fetch_all(int(cfg.get("top_n_okx", 30)))
    quotes = cfg.get("quote_filter") or ["USDT"]
    filtered = {ex: apply_quote_filter(r, quotes)
                for ex, r in all_rates.items()}
    coins = aggregate(filtered)

    threshold = float(cfg.get("spread_alert_pct", 0.05))
    dedup_h = float(cfg.get("dedup_hours", 8))
    now = time.time()

    rows: list[tuple] = []
    for sym in sorted(coins):
        m = coins[sym]
        spread = spread_pct_of(m)
        alerted = False
        if spread >= threshold and do_alert and should_alert(state, sym, now, dedup_h):
            msg = build_alert(sym, m, spread, threshold)
            if send_telegram(str(cfg.get("telegram_bot_token", "")),
                             str(cfg.get("telegram_chat_id", "")), msg):
                mark_alerted(state, sym, now)
                alerted = True
                log.info("alert terkirim: %s spread %.4f%%", sym, spread)
            else:
                log.warning("alert %s GAGAL terkirim (telegram tidak "
                            "terkonfigurasi / error); coba lagi scan berikut",
                            sym)
        rows.append((sym, m, spread, alerted))
    rows.sort(key=lambda r: r[2], reverse=True)
    if do_alert:
        save_state(state_path, state)
    return rows


def print_table(rows: list[tuple], threshold: float) -> None:
    header = "%-12s %-10s %-10s %-10s %-10s %-9s" % (
        "COIN", "BINANCE", "BYBIT", "GATE", "OKX", "SPREAD")
    print(header)
    print("-" * len(header))
    for sym, m, spread, _alerted in rows:
        cells = [sym]
        for ex in EXCHANGE_ORDER:
            cells.append(fmt_pct(m[ex]) if ex in m else "-")
        cells.append("%.4f%%" % spread)
        line = "%-12s %-10s %-10s %-10s %-10s %-9s" % tuple(cells)
        if spread >= threshold:
            line += "  🔔"
        print(line)
    print("\n%d koin terpantau | ambang alert %.4f%%" % (len(rows), threshold))


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    else:
        log.warning("config %s tidak ada; pakai default "
                    "(salin config.example.json -> config.json)", path)
    return cfg


# --------------------------------------------------------------------------
# Self-check (tanpa network, tanpa file state asli)
# --------------------------------------------------------------------------

def self_check() -> bool:
    fails: list[str] = []
    total = [0]

    def check(name: str, cond: bool) -> None:
        total[0] += 1
        print(("PASS" if cond else "FAIL"), "-", name)
        if not cond:
            fails.append(name)

    # 1. normalisasi simbol
    check("normalize 'BTC-USDT'", normalize_symbol("BTC-USDT") == "BTCUSDT")
    check("normalize 'BTC_USDT'", normalize_symbol("BTC_USDT") == "BTCUSDT")
    check("normalize 'btcusdt'", normalize_symbol("btcusdt") == "BTCUSDT")
    check("normalize 'ETH/USDT'", normalize_symbol("ETH/USDT") == "ETHUSDT")
    check("okx_base 'BTC-USDT-SWAP'",
          okx_base("BTC-USDT-SWAP") == "BTCUSDT")

    # 2. parse_rate
    check("parse_rate '0.0001'", parse_rate("0.0001") == 0.0001)
    check("parse_rate '' -> None", parse_rate("") is None)
    check("parse_rate None -> None", parse_rate(None) is None)
    check("parse_rate 'abc' -> None", parse_rate("abc") is None)

    # 3. quote filter
    qf = apply_quote_filter({"BTCUSDT": 0.1, "BTCUSD": 0.2}, ["USDT"])
    check("quote_filter hanya USDT", qf == {"BTCUSDT": 0.1})

    # 4. agregasi: hanya koin di >= 2 exchange
    agg = aggregate({
        "binance": {"BTCUSDT": 0.0001, "SOLUSDT": 0.0002},
        "bybit": {"BTCUSDT": 0.0008},
        "gate": {"DOGEUSDT": 0.0003},
    })
    check("aggregate hanya BTCUSDT", set(agg) == {"BTCUSDT"})
    check("aggregate isi 2 exchange", set(agg["BTCUSDT"]) == {"binance", "bybit"})

    # 5. matematika spread
    sp = spread_pct_of({"binance": 0.0001, "bybit": 0.0008})
    check("spread 0.0007 -> 0.07%", abs(sp - 0.07) < 1e-9)
    check("spread >= ambang 0.05 memicu", sp >= 0.05)
    sp2 = spread_pct_of({"binance": 0.0001, "bybit": 0.00012})
    check("spread kecil tidak memicu", sp2 < 0.05)

    # 6. dedup alert (8 jam)
    now = 1_000_000.0
    st: dict[str, float] = {}
    check("dedup: state kosong -> boleh", should_alert(st, "BTCUSDT", now, 8))
    mark_alerted(st, "BTCUSDT", now)
    check("dedup: 1 jam lalu -> ditahan",
          not should_alert(st, "BTCUSDT", now + 3600, 8))
    check("dedup: 9 jam lalu -> boleh",
          should_alert(st, "BTCUSDT", now + 9 * 3600, 8))
    check("dedup: koin lain tidak terpengaruh",
          should_alert(st, "ETHUSDT", now + 3600, 8))

    # 7. transient error classification
    check("transient: RemoteDisconnected",
          _is_transient(http.client.RemoteDisconnected()))
    check("transient: IncompleteRead",
          _is_transient(http.client.IncompleteRead(b"partial", 10)))
    check("transient: TimeoutError", _is_transient(TimeoutError()))
    check("transient: ConnectionError", _is_transient(ConnectionError()))
    check("transient: URLError(RemoteDisconnected)",
          _is_transient(urllib.error.URLError(http.client.RemoteDisconnected())))
    check("non-transient: ValueError", not _is_transient(ValueError("x")))

    # 8. format alert
    msg = build_alert("BTCUSDT",
                      {"binance": 0.0001, "bybit": 0.0008}, 0.07, 0.05)
    check("build_alert memuat koin", "BTCUSDT" in msg)
    check("build_alert memuat spread", "0.0700%" in msg)
    check("build_alert memuat label exchange", "Binance" in msg and "Bybit" in msg)
    check("fmt_pct '+0.0100%'", fmt_pct(0.0001) == "+0.0100%")
    check("fmt_pct '-0.0250%'", fmt_pct(-0.00025) == "-0.0250%")

    print("\n%d/%d checks passed" % (total[0] - len(fails), total[0]))
    if fails:
        print("GAGAL:", ", ".join(fails))
        return False
    return True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="funding-radar: monitor spread funding-rate lintas exchange")
    ap.add_argument("--once", action="store_true",
                    help="satu scan: cetak tabel + kirim alert (default)")
    ap.add_argument("--loop", action="store_true",
                    help="daemon: scan berulang tiap scan_interval_minutes")
    ap.add_argument("--self-check", action="store_true",
                    help="uji internal (tanpa network)")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr)

    if args.self_check:
        return 0 if self_check() else 1

    cfg = load_config(args.config)
    state = load_state(args.state)

    if args.loop:
        interval_min = float(cfg.get("scan_interval_minutes", 15))
        log.info("loop mode: scan tiap %.0f menit (Ctrl-C untuk berhenti)",
                 interval_min)
        while True:
            try:
                rows = run_scan(cfg, state, args.state)
                print_table(rows, float(cfg.get("spread_alert_pct", 0.05)))
                print("", flush=True)
            except KeyboardInterrupt:
                log.info("berhenti (Ctrl-C)")
                return 0
            except Exception as exc:  # noqa: BLE001 - loop jangan mati
                log.error("scan gagal: %s (%s)", exc, type(exc).__name__)
            time.sleep(max(60.0, interval_min * 60.0))
    else:
        # default: --once (dipakai cron)
        try:
            rows = run_scan(cfg, state, args.state)
        except Exception as exc:  # noqa: BLE001 - graceful, bukan traceback
            log.error("scan gagal total: %s (%s)", exc, type(exc).__name__)
            return 1
        print_table(rows, float(cfg.get("spread_alert_pct", 0.05)))
        if not rows:
            log.warning("tidak ada data: semua exchange gagal / "
                        "tidak ada koin yang muncul di >= 2 exchange")
    return 0


if __name__ == "__main__":
    sys.exit(main())
