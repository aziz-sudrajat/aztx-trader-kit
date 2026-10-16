#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""strict-screener — screener signal crypto multi-timeframe dengan quality scoring.

Memindai perpetual futures USDT dari data PUBLIK OKX, menilai keselarasan tren
antar timeframe (15m / 1h / 4h / 1d) memakai EMA50 + RSI(14), lalu memberi skor
kualitas 0-100 per koin.

Stdlib only (Python >= 3.10). Tanpa API key — seluruh data bersifat publik.
Bukan saran finansial.

Contoh:
    python3 screener.py
    python3 screener.py --top 10 --min-quality 70
    python3 screener.py --json > hasil.json
    python3 screener.py --self-check
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from common import http as _http
except ImportError:
    sys.exit("ERROR: modul 'common' tidak ditemukan — "
             "jalankan dari dalam direktori aztx-trader-kit/")

import argparse
import http.client
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Konfigurasi
# ---------------------------------------------------------------------------
OKX_BASE = "https://www.okx.com"
USER_AGENT = "strict-screener/1.0"
EMA_PERIOD = 50
RSI_PERIOD = 14
CANDLE_LIMIT = 100
TIMEFRAMES = ("15m", "1H", "4H", "1D")

LONG = "LONG"
SHORT = "SHORT"
NEUTRAL = "NEUTRAL"


# ---------------------------------------------------------------------------
# Lapisan network (dengan retry diam-diam untuk gangguan sesaat)
# ---------------------------------------------------------------------------
class FetchError(Exception):
    """Gagal mengambil data dari OKX setelah semua percobaan."""


# Klasifikasi transient error dipakai dari modul bersama (implementasi identik).
_is_retryable = _http.is_retryable


def api_get_json(url: str, timeout: int = 30, _retries: int = 2) -> dict:
    """GET JSON dengan retry diam-diam 2x untuk transient network errors.

    (Retry didelegasikan ke common.http.api_call; pesan FetchError identik
    dengan implementasi asli.)
    """
    try:
        payload = _http.api_call("GET", url,
                                 headers={"User-Agent": USER_AGENT},
                                 timeout=timeout, _retries=_retries)
    except (_http.HttpApiError, _http.RequestError) as exc:
        orig = exc.original
        raise FetchError(
            f"network gagal: {url} ({type(orig).__name__})") from None
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise FetchError(
            f"respon OKX tidak valid: {url} "
            f"(code={payload.get('code') if isinstance(payload, dict) else '?'})")
    return payload


def fetch_tickers() -> list[tuple[str, float]]:
    """Ambil semua perpetual SWAP USDT dari OKX.

    Mengembalikan list (inst_id, quote_volume_24h). Untuk SWAP, volCcy24h
    dinyatakan dalam base currency, jadi dikali harga terakhir.
    """
    url = f"{OKX_BASE}/api/v5/market/tickers?instType=SWAP"
    payload = api_get_json(url)
    out: list[tuple[str, float]] = []
    for t in payload.get("data", []):
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        try:
            last = float(t["last"])
            vol_base = float(t["volCcy24h"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append((inst_id, vol_base * last))
    return out


def fetch_closes(inst_id: str, bar: str) -> list[float]:
    """Ambil candle OKX -> list harga close, urut dari terlama ke terbaru."""
    url = (
        f"{OKX_BASE}/api/v5/market/candles"
        f"?instId={inst_id}&bar={bar}&limit={CANDLE_LIMIT}"
    )
    payload = api_get_json(url)
    closes: list[float] = []
    for row in payload.get("data", []):
        try:
            closes.append(float(row[4]))
        except (IndexError, TypeError, ValueError):
            continue
    closes.reverse()  # OKX mengembalikan terbaru dulu
    if len(closes) < 2:
        raise FetchError(f"candle {inst_id} {bar}: data tidak cukup")
    return closes


# ---------------------------------------------------------------------------
# Indikator (implementasi sendiri, tanpa library)
# ---------------------------------------------------------------------------
def ema(values: list[float], period: int) -> float | None:
    """Exponential Moving Average. None bila data kurang dari period."""
    if len(values) < period or period < 1:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period  # seed = SMA
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def rsi(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Relative Strength Index ala Wilder. None bila data kurang."""
    if len(closes) < period + 1 or period < 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def timeframe_direction(closes: list[float]) -> str:
    """Arah timeframe: LONG bila close > EMA50, SHORT bila < EMA50."""
    e = ema(closes, EMA_PERIOD)
    if e is None:
        return NEUTRAL  # data kurang -> tidak bisa ditentukan
    last = closes[-1]
    if last > e:
        return LONG
    if last < e:
        return SHORT
    return NEUTRAL


# ---------------------------------------------------------------------------
# Scoring kualitas 0-100
# ---------------------------------------------------------------------------
def compute_score(
    dir_4h: str,
    dir_1h: str,
    dir_15m: str,
    dir_1d: str,
    rsi_15m: float | None,
    dist_pct_15m: float | None,
    vol_score: float,
) -> tuple[float, dict]:
    """Hitung skor kualitas 0-100 + rincian komponen.

    Komponen tren (maks 80):
      +25  4h & 1d searah
      +20  1h searah dengan 4h
      +15  15m searah dengan 4h
      +10  RSI(14) 15m mendukung arah (LONG: >55, SHORT: <45)
      +10  harga < 1% dari EMA50 15m (entry tidak ketinggalan)
    Komponen likuiditas (maks 20): proporsional terhadap log volume 24h
    antar simbol yang berhasil di-screening (dihitung pemanggil via vol_score).

    Penalti: bila arah 1d NEUTRAL (tak bisa ditentukan karena data kurang),
    total skor dikali 0.7.
    """
    anchor = dir_4h
    score = 0.0
    parts: dict[str, float] = {}
    if anchor != NEUTRAL:
        if dir_1d != NEUTRAL and dir_1d == anchor:
            score += 25.0
            parts["trend_4h_1d"] = 25.0
        if dir_1h == anchor:
            score += 20.0
            parts["trend_1h"] = 20.0
        if dir_15m == anchor:
            score += 15.0
            parts["trend_15m"] = 15.0
        if rsi_15m is not None and (
            (anchor == LONG and rsi_15m > 55.0)
            or (anchor == SHORT and rsi_15m < 45.0)
        ):
            score += 10.0
            parts["rsi"] = 10.0
        if dist_pct_15m is not None and dist_pct_15m < 0.01:
            score += 10.0
            parts["ema_distance"] = 10.0
    score += vol_score
    parts["volume"] = round(vol_score, 1)
    if dir_1d == NEUTRAL:
        score *= 0.7
        parts["penalty_1d_neutral"] = 0.7
    score = max(0.0, min(100.0, score))
    return round(score, 1), parts


def final_direction(dir_4h: str, dir_1h: str, dir_15m: str) -> str:
    """Arah final: LONG/SHORT hanya bila 4h, 1h, 15m searah; else NEUTRAL."""
    if dir_4h != NEUTRAL and dir_4h == dir_1h == dir_15m:
        return dir_4h
    return NEUTRAL


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------
def _warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr)


def screen_universe(universe_top_n: int) -> list[dict]:
    """Jalankan screening penuh. Simbol gagal -> skip dengan warning (fail-closed)."""
    tickers = fetch_tickers()
    if not tickers:
        raise FetchError("daftar ticker OKX kosong")
    tickers.sort(key=lambda x: x[1], reverse=True)
    universe = tickers[: max(1, universe_top_n)]
    print(f"universe: {len(universe)} simbol (top {universe_top_n} by 24h quote volume)", file=sys.stderr)

    collected: list[dict] = []
    for i, (inst_id, qvol) in enumerate(universe, 1):
        print(f"[{i}/{len(universe)}] {inst_id} ...", file=sys.stderr)
        try:
            closes = {tf: fetch_closes(inst_id, tf) for tf in TIMEFRAMES}
        except FetchError as exc:
            _warn(f"skip {inst_id}: {exc}")
            continue
        dirs = {tf: timeframe_direction(closes[tf]) for tf in TIMEFRAMES}
        rsi_15m = rsi(closes["15m"])
        e50_15m = ema(closes["15m"], EMA_PERIOD)
        dist_pct = (
            abs(closes["15m"][-1] - e50_15m) / e50_15m
            if e50_15m
            else None
        )
        collected.append(
            {
                "inst_id": inst_id,
                "symbol": inst_id.split("-")[0],
                "quote_volume_24h": qvol,
                "dirs": dirs,
                "rsi_15m": rsi_15m,
                "dist_pct_15m": dist_pct,
            }
        )

    if not collected:
        raise FetchError("tidak ada simbol yang berhasil diambil datanya")

    # Komponen volume proporsional 0-20 (skala log antar simbol yang lolos).
    log_vols = [math.log10(max(c["quote_volume_24h"], 1.0)) for c in collected]
    lo, hi = min(log_vols), max(log_vols)
    rows: list[dict] = []
    for c, lv in zip(collected, log_vols):
        vol_score = 20.0 if hi == lo else 20.0 * (lv - lo) / (hi - lo)
        score, parts = compute_score(
            c["dirs"]["4H"],
            c["dirs"]["1H"],
            c["dirs"]["15m"],
            c["dirs"]["1D"],
            c["rsi_15m"],
            c["dist_pct_15m"],
            vol_score,
        )
        rows.append(
            {
                "symbol": c["symbol"],
                "inst_id": c["inst_id"],
                "direction": final_direction(c["dirs"]["4H"], c["dirs"]["1H"], c["dirs"]["15m"]),
                "quality": score,
                "tf_4h": c["dirs"]["4H"],
                "tf_1h": c["dirs"]["1H"],
                "tf_15m": c["dirs"]["15m"],
                "tf_1d": c["dirs"]["1D"],
                "rsi_15m": c["rsi_15m"],
                "quote_volume_24h": c["quote_volume_24h"],
                "score_parts": parts,
            }
        )
    rows.sort(key=lambda r: (-r["quality"], r["symbol"]))
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def print_table(rows: list[dict]) -> None:
    header = f"{'RANK':>4} | {'SYMBOL':<8} | {'DIR':<7} | {'QUALITY':>7} | {'4H':<7} | {'1H':<7} | {'15M':<7} | {'RSI':>5}"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        rsi_txt = f"{r['rsi_15m']:.0f}" if r["rsi_15m"] is not None else "n/a"
        print(
            f"{i:>4} | {r['symbol']:<8} | {r['direction']:<7} | {r['quality']:>7.1f} | "
            f"{r['tf_4h']:<7} | {r['tf_1h']:<7} | {r['tf_15m']:<7} | {rsi_txt:>5}"
        )


def print_json(rows: list[dict]) -> None:
    out = []
    for i, r in enumerate(rows, 1):
        out.append(
            {
                "rank": i,
                "symbol": r["symbol"],
                "inst_id": r["inst_id"],
                "direction": r["direction"],
                "quality": r["quality"],
                "timeframes": {
                    "1d": r["tf_1d"],
                    "4h": r["tf_4h"],
                    "1h": r["tf_1h"],
                    "15m": r["tf_15m"],
                },
                "rsi_15m": r["rsi_15m"],
                "quote_volume_24h": r["quote_volume_24h"],
                "score_parts": r["score_parts"],
            }
        )
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------------------
# Self-check: uji logika dengan candle sintetis (tanpa network)
# ---------------------------------------------------------------------------
def _check(name: str, cond: bool, detail: str = "") -> bool:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    return cond


def self_check() -> int:
    ok = True

    # EMA: deret konstan -> EMA == konstanta
    ok &= _check("ema konstan", ema([100.0] * 100, 50) == 100.0)
    # EMA: deret naik -> EMA di bawah harga terakhir
    rising = [float(i) for i in range(1, 101)]
    e_rising = ema(rising, 50)
    ok &= _check("ema deret naik < close terakhir", e_rising is not None and e_rising < 100.0, f"ema={e_rising}")
    # EMA: data kurang -> None
    ok &= _check("ema data kurang -> None", ema([1.0] * 10, 50) is None)

    # RSI: semua naik -> 100
    ok &= _check("rsi full gain = 100", rsi([100.0 + i for i in range(20)]) == 100.0)
    # RSI: semua turun -> 0
    ok &= _check("rsi full loss = 0", rsi([100.0 - i for i in range(20)]) == 0.0)
    # RSI: data kurang -> None
    ok &= _check("rsi data kurang -> None", rsi([1.0, 2.0, 3.0]) is None)
    # RSI: campuran di rentang wajar
    mixed = [100.0, 102.0, 101.0, 103.0, 102.5, 104.0, 103.0, 105.0, 104.0,
             106.0, 105.0, 107.0, 106.0, 108.0, 107.5, 109.0]
    r_mixed = rsi(mixed)
    ok &= _check("rsi campuran 0..100", r_mixed is not None and 0.0 < r_mixed < 100.0, f"rsi={r_mixed}")

    # Direction
    ok &= _check("direction naik -> LONG", timeframe_direction(rising) == LONG)
    falling = [float(100 - i) for i in range(100)]
    ok &= _check("direction turun -> SHORT", timeframe_direction(falling) == SHORT)
    ok &= _check("direction data kurang -> NEUTRAL", timeframe_direction([1.0] * 10) == NEUTRAL)

    # Skor: sempurna -> 100
    s, _ = compute_score(LONG, LONG, LONG, LONG, 60.0, 0.005, 20.0)
    ok &= _check("skor sempurna = 100", s == 100.0, f"s={s}")
    # Skor: 1d NEUTRAL -> +25 hangus (tak searah) lalu x0.7: (20+15+10+10+20)*0.7
    s, _ = compute_score(LONG, LONG, LONG, NEUTRAL, 60.0, 0.005, 20.0)
    ok &= _check("skor 1d neutral = 52.5 (x0.7)", s == 52.5, f"s={s}")
    # Skor: 1h tidak searah -> kehilangan 20 poin
    s, _ = compute_score(LONG, SHORT, LONG, LONG, 60.0, 0.005, 20.0)
    ok &= _check("skor 1h misaligned = 80", s == 80.0, f"s={s}")
    # Skor: RSI tidak mendukung -> kehilangan 10 poin
    s, _ = compute_score(LONG, LONG, LONG, LONG, 50.0, 0.005, 20.0)
    ok &= _check("skor rsi netral = 90", s == 90.0, f"s={s}")
    # Skor: harga jauh dari EMA -> kehilangan 10 poin
    s, _ = compute_score(LONG, LONG, LONG, LONG, 60.0, 0.05, 20.0)
    ok &= _check("skor jarak EMA jauh = 90", s == 90.0, f"s={s}")
    # Skor: anchor NEUTRAL -> hanya komponen volume
    s, _ = compute_score(NEUTRAL, LONG, LONG, LONG, 60.0, 0.005, 12.5)
    ok &= _check("skor anchor neutral = vol saja", s == 12.5, f"s={s}")

    # Final direction gate
    ok &= _check("final dir searah -> LONG", final_direction(LONG, LONG, LONG) == LONG)
    ok &= _check("final dir searah -> SHORT", final_direction(SHORT, SHORT, SHORT) == SHORT)
    ok &= _check("final dir beda -> NEUTRAL", final_direction(LONG, SHORT, LONG) == NEUTRAL)
    ok &= _check("final dir ada neutral -> NEUTRAL", final_direction(NEUTRAL, LONG, LONG) == NEUTRAL)

    # Retry classifier (tanpa network)
    ok &= _check("retry RemoteDisconnected", _is_retryable(http.client.RemoteDisconnected()))
    ok &= _check("retry IncompleteRead", _is_retryable(http.client.IncompleteRead(b"partial", 10)))
    ok &= _check("retry timeout", _is_retryable(TimeoutError()))
    ok &= _check("retry ConnectionError", _is_retryable(ConnectionError()))
    ok &= _check(
        "retry URLError terbungkus",
        _is_retryable(urllib.error.URLError(http.client.RemoteDisconnected())),
    )
    ok &= _check("no-retry ValueError", not _is_retryable(ValueError("x")))

    # Integrasi: candle sintetis uptrend di semua TF -> LONG, skor tinggi
    # (naik lalu flat cukup lama agar EMA50 15m mengejar harga < 1%)
    synth = [100.0 + 0.5 * i for i in range(40)] + [120.0] * 60
    d = timeframe_direction(synth)
    r = rsi(synth)
    e = ema(synth, EMA_PERIOD)
    dist = abs(synth[-1] - e) / e if e else None
    s, _ = compute_score(d, d, d, d, r, dist, 20.0)
    ok &= _check(
        "integrasi uptrend -> LONG & skor>=95",
        d == LONG and s >= 95.0,
        f"d={d} rsi={r} dist={dist} s={s}",
    )
    # Integrasi: downtrend -> SHORT
    synth_s = [200.0 - 0.5 * i for i in range(60)] + [170.0] * 40
    d_s = timeframe_direction(synth_s)
    ok &= _check("integrasi downtrend -> SHORT", d_s == SHORT, f"d={d_s}")

    print("SELF-CHECK " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="strict-screener: screener signal crypto multi-timeframe (data publik OKX)."
    )
    ap.add_argument("--top", type=int, default=20, help="maks baris ditampilkan (default: 20)")
    ap.add_argument("--min-quality", type=float, default=60.0, help="skor minimum (default: 60)")
    ap.add_argument("--universe-top-n", type=int, default=50, help="jumlah simbol universe (default: 50)")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--show-neutral", action="store_true", help="tampilkan juga arah NEUTRAL")
    ap.add_argument("--self-check", action="store_true", help="uji logika dengan data sintetis (tanpa network)")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    try:
        rows = screen_universe(args.universe_top_n)
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ndibatalkan.", file=sys.stderr)
        return 130

    rows = [r for r in rows if r["quality"] >= args.min_quality]
    if not args.show_neutral:
        rows = [r for r in rows if r["direction"] != NEUTRAL]
    rows = rows[: max(0, args.top)]

    if not rows:
        print("Tidak ada simbol yang lolos filter.", file=sys.stderr)
        return 0
    if args.json:
        print_json(rows)
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
