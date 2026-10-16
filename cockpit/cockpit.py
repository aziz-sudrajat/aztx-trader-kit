#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""Termux Futures Cockpit — TUI (curses) untuk memantau & mengelola posisi
Binance Futures dari Termux/SSH. Dibuat untuk trader yang trading dari HP.

API key/secret HANYA dibaca dari environment (jangan taruh di file!):
    export BINANCE_API_KEY="..."
    export BINANCE_API_SECRET="..."
    python3 cockpit.py

Stdlib only (>=3.10). Self-check tanpa network & tanpa curses:
    python3 cockpit.py --self-check
"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from common import http as _http
    from common import binance as _binance
except ImportError:
    sys.exit("ERROR: modul 'common' tidak ditemukan — "
             "jalankan dari dalam direktori aztx-trader-kit/")

import argparse
import hashlib
import hmac
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import curses
except ImportError:  # mis. Windows tanpa curses
    curses = None


BASE_URL = "https://fapi.binance.com"
RECV_WINDOW = 5000
REFRESH_SEC = 5
MIN_H = 24
MIN_W = 80


class ApiError(Exception):
    """Error dari Binance API atau kegagalan network."""


# ---------------------------------------------------------------------------
# Network helpers (dengan retry diam-diam untuk transient errors)
# ---------------------------------------------------------------------------

# Klasifikasi transient error & HMAC signing dipakai dari modul bersama
# (implementasi identik dengan yang sebelumnya ada di file ini).
_is_retryable = _http.is_retryable
sign_params = _binance.sign_params


def _request(method: str, path: str, api_key: str, api_secret: str,
             params: dict | None = None, _retries: int = 2):
    """Signed request ke Binance Futures API.

    (Didelegasikan ke common.binance.signed_request; wire format GET/POST,
    timeout, recvWindow, timestamp fresh per retry, serta tipe & pesan
    ApiError identik dengan implementasi asli.)
    """
    try:
        return _binance.signed_request(
            method, path, params or {}, api_key, api_secret,
            base_url=BASE_URL, timeout=20, recv_window=RECV_WINDOW,
            _retries=_retries)
    except _binance.BinanceApiError as exc:
        if exc.code is _binance.MISSING:
            raise ApiError(f"HTTP {exc.status}") from None
        raise ApiError(f"Binance {exc.code}: {exc.msg}") from None
    except _http.RequestError as exc:
        raise ApiError(f"network: {type(exc.original).__name__}") from None


def api_get(path: str, api_key: str, api_secret: str, params: dict | None = None):
    return _request("GET", path, api_key, api_secret, params)


def api_post(path: str, api_key: str, api_secret: str, params: dict | None = None):
    return _request("POST", path, api_key, api_secret, params)


# ---------------------------------------------------------------------------
# Binance data access
# ---------------------------------------------------------------------------

def get_dual_side(api_key: str, api_secret: str) -> bool:
    """True jika akun memakai Hedge Mode (butuh positionSide di order)."""
    data = api_get("/fapi/v1/positionSide/dual", api_key, api_secret)
    return bool(data.get("dualSidePosition"))


def parse_account(data: dict) -> dict:
    """Ringkasan akun dari GET /fapi/v2/account."""
    return {
        "equity": float(data.get("totalMarginBalance", 0) or 0),
        "available": float(data.get("availableBalance", 0) or 0),
        "upnl": float(data.get("totalUnrealizedProfit", 0) or 0),
    }


def parse_positions(data: list) -> list:
    """Parse GET /fapi/v2/positionRisk -> list posisi terbuka saja."""
    out = []
    for p in data:
        amt = float(p.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        mark = float(p.get("markPrice", 0) or 0)
        upnl = float(p.get("unRealizedProfit", 0) or 0)
        lev = int(float(p.get("leverage", 1) or 1))
        margin_type = p.get("marginType", "cross")
        # qty_str mentah dari API (sudah sesuai step-size) untuk order close
        raw = str(p.get("positionAmt", "0"))
        qty_str = raw[1:] if raw.startswith("-") else raw
        notional = abs(amt) * mark
        iso = float(p.get("isolatedMargin", 0) or 0)
        if margin_type == "isolated" and iso > 0:
            margin_used = iso
        else:
            margin_used = notional / lev if lev else 0.0
        roe = (upnl / margin_used * 100.0) if margin_used > 0 else 0.0
        out.append({
            "symbol": p.get("symbol", "?"),
            "side": "LONG" if amt > 0 else "SHORT",
            "qty": abs(amt),
            "qty_str": qty_str,
            "entry": entry,
            "mark": mark,
            "upnl": upnl,
            "roe": roe,
            "leverage": lev,
            "margin_type": margin_type,
            "position_side": p.get("positionSide", "BOTH"),
        })
    return out


def close_position(pos: dict, api_key: str, api_secret: str, dual_side: bool) -> dict:
    """Market-close SATU posisi (reduce-only). Return response order."""
    side = "SELL" if pos["side"] == "LONG" else "BUY"
    params = {
        "symbol": pos["symbol"],
        "side": side,
        "type": "MARKET",
        "quantity": pos["qty_str"],
        "reduceOnly": "true",
    }
    if dual_side:
        params["positionSide"] = pos["position_side"]
    data = api_post("/fapi/v1/order", api_key, api_secret, params)
    if not data.get("orderId"):
        raise ApiError(f"order ditolak: {data}")
    return data


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usdt(x: float) -> str:
    """$1,234.56 (tanpa tanda +)."""
    return f"${x:,.2f}"


def fmt_signed_usdt(x: float) -> str:
    """+$1,234.56 / -$12.30 untuk kolom uPNL."""
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):,.2f}"


def fmt_price(x: float) -> str:
    """Harga adaptif: makin kecil makin banyak desimal."""
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:,.6f}"


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def fmt_qty(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


# ---------------------------------------------------------------------------
# TUI (curses)
# ---------------------------------------------------------------------------

HELP_LINE = "UP/DOWN pilih | c close posisi | a close SEMUA | r refresh | q keluar"

C_GREEN = 1
C_RED = 2
C_HEAD = 3


def _addstr(stdscr, y: int, x: int, text: str, attr: int = 0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        stdscr.addstr(y, x, text[: max(0, w - x - 1)], attr)
    except curses.error:
        pass


def _pnl_attr(upnl: float) -> int:
    if upnl > 0:
        return curses.color_pair(C_GREEN) | curses.A_BOLD
    if upnl < 0:
        return curses.color_pair(C_RED) | curses.A_BOLD
    return curses.A_NORMAL


def draw(stdscr, account: dict, positions: list, selected: int,
         status: str, confirm: str | None):
    stdscr.clear()
    h, w = stdscr.getmaxyx()

    # Header
    upnl = account.get("upnl", 0.0)
    header = (f" TERMUX FUTURES COCKPIT   Equity {fmt_usdt(account.get('equity', 0.0))}"
              f"   Avail {fmt_usdt(account.get('available', 0.0))}   uPNL ")
    _addstr(stdscr, 0, 0, header, curses.color_pair(C_HEAD) | curses.A_BOLD)
    _addstr(stdscr, 0, len(header), fmt_signed_usdt(upnl), _pnl_attr(upnl))

    # Kolom tabel
    _addstr(stdscr, 2, 0,
            f"{'SYMBOL':<10} {'SIDE':<6} {'QTY':>12} {'ENTRY':>11} "
            f"{'MARK':>11} {'uPNL':>11} {'ROE%':>8} {'LEV':>5}",
            curses.A_UNDERLINE)

    # Baris posisi
    max_rows = h - 6
    for i, pos in enumerate(positions[:max_rows]):
        y = 3 + i
        attr = _pnl_attr(pos["upnl"])
        if i == selected:
            attr |= curses.A_REVERSE
        line = (f"{pos['symbol']:<10} {pos['side']:<6} {fmt_qty(pos['qty']):>12} "
                f"{fmt_price(pos['entry']):>11} {fmt_price(pos['mark']):>11} "
                f"{fmt_signed_usdt(pos['upnl']):>11} {fmt_pct(pos['roe']):>8} "
                f"{pos['leverage']:>4}x")
        _addstr(stdscr, y, 0, line, attr)

    if not positions:
        _addstr(stdscr, 3, 0, "  (tidak ada posisi terbuka)", curses.A_DIM)

    # Footer: status + bantuan
    if confirm:
        _addstr(stdscr, h - 2, 0, confirm[: w - 1],
                curses.color_pair(C_RED) | curses.A_BOLD)
    else:
        _addstr(stdscr, h - 2, 0, status[: w - 1], curses.A_DIM)
    _addstr(stdscr, h - 1, 0, HELP_LINE[: w - 1], curses.A_DIM)
    stdscr.refresh()


def run_tui(stdscr, api_key: str, api_secret: str):
    curses.curs_set(0)
    stdscr.timeout(200)  # getch non-blocking tiap 200ms -> refresh timer jalan
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_GREEN, curses.COLOR_GREEN, bg)
    curses.init_pair(C_RED, curses.COLOR_RED, bg)
    curses.init_pair(C_HEAD, curses.COLOR_CYAN, bg)

    account = {"equity": 0.0, "available": 0.0, "upnl": 0.0}
    positions: list = []
    selected = 0
    status = "menghubungi Binance..."
    confirm: str | None = None
    pending = None  # ("one", pos) | ("all", None)
    dual_side: bool | None = None
    last_fetch = 0.0
    force_refresh = True

    while True:
        h, w = stdscr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            stdscr.clear()
            _addstr(stdscr, 0, 0,
                    f"Terminal terlalu kecil: {w}x{h}. Butuh minimal 80x24.")
            _addstr(stdscr, 1, 0,
                    "Perbesar/zoom-out terminal, atau tekan q untuk keluar.")
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            continue

        now = time.time()
        if force_refresh or (now - last_fetch >= REFRESH_SEC):
            try:
                if dual_side is None:
                    dual_side = get_dual_side(api_key, api_secret)
                account = parse_account(
                    api_get("/fapi/v2/account", api_key, api_secret))
                positions = parse_positions(
                    api_get("/fapi/v2/positionRisk", api_key, api_secret))
                selected = max(0, min(selected, len(positions) - 1))
                status = f"update {time.strftime('%H:%M:%S')} | {len(positions)} posisi"
            except ApiError as exc:
                status = f"ERR: {exc}"
            last_fetch = now
            force_refresh = False

        draw(stdscr, account, positions, selected, status, confirm)
        key = stdscr.getch()

        # Mode konfirmasi y/n
        if pending is not None:
            if key in (ord("y"), ord("Y")):
                kind, pos = pending
                try:
                    if kind == "one":
                        res = close_position(pos, api_key, api_secret,
                                             bool(dual_side))
                        status = (f"OK close {pos['symbol']} "
                                  f"orderId={res.get('orderId')}")
                    else:
                        n, fails = 0, []
                        for p in positions:
                            try:
                                close_position(p, api_key, api_secret,
                                               bool(dual_side))
                                n += 1
                            except ApiError as exc:
                                fails.append(f"{p['symbol']}:{exc}")
                        status = f"OK close {n} posisi" + (
                            f" | GAGAL: {'; '.join(fails)}" if fails else "")
                    force_refresh = True
                except ApiError as exc:
                    status = f"ERR close: {exc}"
                pending, confirm = None, None
            elif key in (ord("n"), ord("N"), 27):  # n / ESC = batal
                status = "dibatalkan"
                pending, confirm = None, None
            continue

        if key == -1:
            continue
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = max(0, min(len(positions) - 1, selected + 1))
        elif key in (ord("r"), ord("R")):
            force_refresh = True
            status = "refresh manual..."
        elif key in (ord("c"), ord("C")):
            if not positions:
                status = "tidak ada posisi untuk di-close"
            else:
                pos = positions[selected]
                pending = ("one", pos)
                confirm = (f"Close {pos['side']} {fmt_qty(pos['qty'])} "
                           f"{pos['symbol']} @MARKET? [y/n]")
        elif key in (ord("a"), ord("A")):
            if not positions:
                status = "tidak ada posisi untuk di-close"
            else:
                pending = ("all", None)
                confirm = (f"Close SEMUA {len(positions)} posisi @MARKET? "
                           f"[y/n]")
        elif key in (ord("q"), ord("Q")):
            return


# ---------------------------------------------------------------------------
# Self-check (tanpa network, tanpa curses)
# ---------------------------------------------------------------------------

_SELFCHECK_FIXTURE = [
    {"symbol": "BTCUSDT", "positionAmt": "0.500", "entryPrice": "67000.0",
     "markPrice": "69000.5", "unRealizedProfit": "1000.25",
     "liquidationPrice": "60000", "leverage": "10", "marginType": "isolated",
     "isolatedMargin": "3350.0", "positionSide": "BOTH"},
    {"symbol": "ETHUSDT", "positionAmt": "-2.0", "entryPrice": "3500",
     "markPrice": "3400", "unRealizedProfit": "200",
     "liquidationPrice": "4200", "leverage": "10", "marginType": "cross",
     "isolatedMargin": "0", "positionSide": "BOTH"},
    {"symbol": "BNBUSDT", "positionAmt": "0.0", "entryPrice": "0",
     "markPrice": "600", "unRealizedProfit": "0",
     "liquidationPrice": "0", "leverage": "10", "marginType": "cross",
     "isolatedMargin": "0", "positionSide": "BOTH"},
]

_SELFCHECK_ACCOUNT = {
    "totalMarginBalance": "12345.678",
    "availableBalance": "1200.50",
    "totalUnrealizedProfit": "1200.25",
}


def self_check() -> int:
    """Jalankan semua uji tanpa network/curses. Return 0 jika lolos."""
    fails = []

    def check(name: str, cond: bool, detail: str = ""):
        print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
        if not cond:
            fails.append(name)

    # 1. HMAC-SHA256 sesuai vektor uji standar (RFC 4231-style)
    vec = hmac.new(b"key", b"The quick brown fox jumps over the lazy dog",
                   hashlib.sha256).hexdigest()
    check("hmac_sha256_test_vector",
          vec == "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8")

    # 2. sign_params deterministik + format signature benar
    s1 = sign_params({"symbol": "BTCUSDT", "timestamp": 123}, "secret")
    s2 = sign_params({"symbol": "BTCUSDT", "timestamp": 123}, "secret")
    check("sign_deterministic", s1 == s2)
    sig = s1.rsplit("signature=", 1)[1]
    check("sign_format_64hex", len(sig) == 64 and all(c in "0123456789abcdef" for c in sig))
    s3 = sign_params({"symbol": "BTCUSDT", "timestamp": 123}, "other")
    check("sign_secret_matters", s3 != s1)

    # 3. Parsing positionRisk dari fixture
    poss = parse_positions(_SELFCHECK_FIXTURE)
    check("parse_count", len(poss) == 2, f"got {len(poss)}")
    btc = poss[0]
    check("parse_btc_side", btc["side"] == "LONG")
    check("parse_btc_qty", btc["qty"] == 0.5)
    check("parse_btc_qty_str", btc["qty_str"] == "0.500", btc["qty_str"])
    check("parse_btc_upnl", abs(btc["upnl"] - 1000.25) < 1e-9)
    # isolated: ROE = 1000.25 / 3350 * 100 = 29.858...
    check("parse_btc_roe", abs(btc["roe"] - 29.8582089552) < 1e-6, str(btc["roe"]))
    eth = poss[1]
    check("parse_eth_side", eth["side"] == "SHORT")
    check("parse_eth_qty_str", eth["qty_str"] == "2.0", eth["qty_str"])
    # cross: margin = notional/lev = 2*3400/10 = 680 -> ROE = 200/680*100
    check("parse_eth_roe", abs(eth["roe"] - 29.4117647059) < 1e-6, str(eth["roe"]))

    # 4. Parsing account
    acc = parse_account(_SELFCHECK_ACCOUNT)
    check("parse_account",
          abs(acc["equity"] - 12345.678) < 1e-9
          and abs(acc["available"] - 1200.50) < 1e-9
          and abs(acc["upnl"] - 1200.25) < 1e-9)

    # 5. Format angka
    check("fmt_usdt", fmt_usdt(12345.678) == "$12,345.68", fmt_usdt(12345.678))
    check("fmt_signed_usdt_pos", fmt_signed_usdt(1000.25) == "+$1,000.25")
    check("fmt_signed_usdt_neg", fmt_signed_usdt(-12.3) == "-$12.30")
    check("fmt_price_big", fmt_price(69000.5) == "69,000.50", fmt_price(69000.5))
    check("fmt_price_mid", fmt_price(3.14159) == "3.1416", fmt_price(3.14159))
    check("fmt_price_small", fmt_price(0.000123456) == "0.000123", fmt_price(0.000123456))
    check("fmt_pct", fmt_pct(29.858) == "+29.86%", fmt_pct(29.858))
    check("fmt_pct_neg", fmt_pct(-5.2) == "-5.20%")
    check("fmt_qty", fmt_qty(8.7) == "8.7" and fmt_qty(0.5) == "0.5")

    # 6. Guard: margin nol -> ROE 0 (tidak ZeroDivisionError)
    zero = parse_positions([{
        "symbol": "XRPUSDT", "positionAmt": "100", "entryPrice": "0",
        "markPrice": "0", "unRealizedProfit": "0", "leverage": "0",
        "marginType": "cross", "isolatedMargin": "0", "positionSide": "BOTH"}])
    check("roe_zero_margin_guard", zero[0]["roe"] == 0.0)

    print(f"\n{len(fails)} gagal dari self-check.")
    return 1 if fails else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Termux Futures Cockpit — TUI Binance Futures")
    ap.add_argument("--self-check", action="store_true",
                    help="uji internal tanpa network/curses")
    args = ap.parse_args()

    if args.self_check:
        return self_check()

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")
    if not api_key or not api_secret:
        print("ERROR: BINANCE_API_KEY / BINANCE_API_SECRET belum di-export.\n"
              "Contoh:\n"
              '  export BINANCE_API_KEY="..." \n'
              '  export BINANCE_API_SECRET="..."\n'
              "Lihat .env.example (jangan commit key asli!).",
              file=sys.stderr)
        return 2

    if curses is None:
        print("ERROR: modul curses tidak tersedia di Python ini.", file=sys.stderr)
        return 2

    try:
        curses.wrapper(run_tui, api_key, api_secret)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
