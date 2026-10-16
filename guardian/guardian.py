#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""futures-guardian: penjaga risiko mandiri untuk Binance USD-M Futures.

Berjalan di samping bot trading apa pun. Tiap cycle:
  1. Ambil ekuitas akun (wallet + unrealized PnL) via /fapi/v2/account.
  2. Bandingkan dengan ekuitas awal hari (UTC): day_pnl = sekarang - awal.
  3. Jika day_pnl <= -max_daily_loss_usdt: tutup SEMUA posisi (market,
     reduce-only), batalkan semua open order, kunci sampai hari UTC
     berikutnya, kirim alert Telegram.

Perintah Telegram (hanya dari chat id resmi): /status /kill /unlock /help.

API key/secret Binance HANYA dibaca dari file env terpisah
(~/.futures-guardian.env, chmod 600). Tidak pernah di-hardcode.
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
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("guardian")

# ---------------------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------------------

DEFAULTS = {
    "state_dir": "state",
    "poll_seconds": 15,
    "max_daily_loss_usdt": 5.0,
    "lock_until_next_day": True,
    "binance_base_url": "https://fapi.binance.com",
    "recv_window": 5000,
}

ENV_FILE_DEFAULT = os.path.expanduser("~/.futures-guardian.env")
STATE_FILENAME = "guardian_state.json"


class GuardianError(Exception):
    """Error operasional guardian (network, API, config)."""


# ---------------------------------------------------------------------------
# HTTP dengan retry diam-diam untuk transient errors
# (pola yang terbukti di proyek shellgram)
# ---------------------------------------------------------------------------

# Klasifikasi transient error dipakai dari modul bersama (implementasi identik).
_is_retryable = _http.is_retryable


def http_json(method, url, *, data=None, headers=None, timeout=30, _retries=2):
    """HTTP request -> dict JSON, dengan retry diam-diam utk transient errors.

    HTTPError (4xx/5xx dari API) TIDAK di-retry: dibaca bodinya lalu
    diangkat sebagai GuardianError berisi code/msg dari API.
    (Retry didelegasikan ke common.http.api_call; format pesan error identik.)
    """
    try:
        return _http.api_call(method, url, data=data, headers=headers,
                              timeout=timeout, _retries=_retries)
    except _http.HttpApiError as exc:
        body = exc.body or ""
        try:
            payload = json.loads(body)
            detail = payload.get("msg") or payload.get("description") or body
            code = payload.get("code", "")
        except Exception:
            detail, code = body[:200], ""
        raise GuardianError(
            f"HTTP {exc.status} [{code}] {detail}".strip()) from None
    except _http.RequestError as exc:
        orig = exc.original
        raise GuardianError(
            f"request gagal ({type(orig).__name__}): {orig}") from None


# ---------------------------------------------------------------------------
# Config & env
# ---------------------------------------------------------------------------

def load_env_file(path):
    """Parse file KEY=VALUE sederhana (abaikan komentar/# dan baris kosong)."""
    data = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def resolve_config_path(cli_path=None):
    """Urutan: argumen CLI -> env FG_CONFIG -> ./config.json -> ~/.futures-guardian/config.json."""
    candidates = []
    if cli_path:
        candidates.append(cli_path)
    env_path = os.environ.get("FG_CONFIG")
    if env_path:
        candidates.append(env_path)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    candidates.append(os.path.expanduser("~/.futures-guardian/config.json"))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise GuardianError(
        "config.json tidak ditemukan. Buat dari config.example.json "
        "atau set env FG_CONFIG ke path config.json.")


def load_config(path):
    """Baca config.json, gabung dengan DEFAULTS + override dari env."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = dict(DEFAULTS)
    cfg.update(raw or {})

    # Token & chat id boleh dari env (prioritas: config file, lalu env).
    token = cfg.get("telegram_bot_token") or os.environ.get("FG_TELEGRAM_BOT_TOKEN") \
        or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = cfg.get("telegram_chat_id") or os.environ.get("FG_TELEGRAM_CHAT_ID") \
        or os.environ.get("TELEGRAM_CHAT_ID") or ""
    cfg["telegram_bot_token"] = str(token).strip()
    try:
        cfg["telegram_chat_id"] = int(str(chat_id).strip())
    except (TypeError, ValueError):
        cfg["telegram_chat_id"] = 0

    cfg["poll_seconds"] = int(cfg.get("poll_seconds") or 15)
    cfg["max_daily_loss_usdt"] = abs(float(cfg.get("max_daily_loss_usdt") or 5.0))
    cfg["lock_until_next_day"] = bool(cfg.get("lock_until_next_day", True))
    cfg["config_path"] = path

    # state_dir relatif -> relatif terhadap folder config.json (predictable).
    state_dir = cfg.get("state_dir") or "state"
    if not os.path.isabs(state_dir):
        state_dir = os.path.join(os.path.dirname(os.path.abspath(path)), state_dir)
    cfg["state_dir"] = state_dir
    return cfg


def load_binance_keys():
    """Ambil API key/secret HANYA dari file env (default ~/.futures-guardian.env)."""
    env_path = os.environ.get("FG_ENV_FILE") or ENV_FILE_DEFAULT
    if not os.path.isfile(env_path):
        raise GuardianError(
            f"file env Binance tidak ditemukan: {env_path}. "
            "Buat dari .env.example lalu chmod 600.")
    data = load_env_file(env_path)
    key = data.get("BINANCE_API_KEY", "").strip()
    secret = data.get("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        raise GuardianError(f"BINANCE_API_KEY/SECRET kosong di {env_path}.")
    return key, secret, env_path


# ---------------------------------------------------------------------------
# State harian (UTC)
# ---------------------------------------------------------------------------

def today_utc():
    return datetime.now(timezone.utc).date().isoformat()


def _state_path(state_dir):
    return os.path.join(state_dir, STATE_FILENAME)


def load_state(state_dir):
    try:
        with open(_state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state_dir, state):
    os.makedirs(state_dir, exist_ok=True)
    path = _state_path(state_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Logika keputusan MURNI (tanpa network — bisa di-unit-test)
# ---------------------------------------------------------------------------

def decide_action(*, equity, today, state, max_daily_loss_usdt, lock_until_next_day):
    """Tentukan aksi dari state + ekuitas saat ini.

    Returns (new_state, action):
      - "day_reset": hari UTC baru -> baseline di-reset, kunci dibuka.
      - "breach": day_pnl <= -max_daily_loss_usdt -> flatten + kunci.
      - "none": tidak ada aksi.
    """
    state = dict(state or {})
    if state.get("day") != today:
        new_state = {
            "day": today,
            "start_equity": equity,
            "last_equity": equity,
            "last_day_pnl": 0.0,
            "locked": False,
            "lock_reason": "",
        }
        return new_state, "day_reset"

    if state.get("locked"):
        return state, "none"

    start = float(state.get("start_equity", equity))
    day_pnl = equity - start
    if day_pnl <= -abs(float(max_daily_loss_usdt)):
        state["locked"] = bool(lock_until_next_day)
        state["lock_reason"] = (
            f"day_pnl {day_pnl:+.2f} USDT <= -{abs(float(max_daily_loss_usdt)):.2f} USDT")
        state["last_equity"] = equity
        state["last_day_pnl"] = day_pnl
        return state, "breach"

    state["last_equity"] = equity
    state["last_day_pnl"] = day_pnl
    return state, "none"


# ---------------------------------------------------------------------------
# Binance USD-M Futures client (signed, urllib + HMAC-SHA256, stdlib only)
# ---------------------------------------------------------------------------

# HMAC-SHA256 signing dipakai dari modul bersama (implementasi identik).
sign_params = _binance.sign_params


class BinanceClient:
    def __init__(self, api_key, api_secret, base_url, recv_window=5000):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = recv_window

    def signed(self, method, path, params=None):
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        url = self.base_url + path + "?" + sign_params(params, self.api_secret)
        headers = {"X-MBX-APIKEY": self.api_key}
        return http_json(method, url, headers=headers, timeout=30)

    def account(self):
        return self.signed("GET", "/fapi/v2/account")

    def positions(self):
        return self.signed("GET", "/fapi/v2/positionRisk")

    def open_orders(self):
        return self.signed("GET", "/fapi/v1/openOrders")

    def equity(self):
        acct = self.account()
        wallet = float(acct.get("totalWalletBalance", 0) or 0)
        upnl = float(acct.get("totalUnrealizedProfit", 0) or 0)
        return wallet + upnl, acct

    def close_position(self, pos):
        """Market-close SATU posisi, reduce-only. Kembalikan symbol."""
        symbol = pos["symbol"]
        amt = float(pos.get("positionAmt", 0) or 0)
        if amt == 0:
            return None
        side = "SELL" if amt > 0 else "BUY"
        # Pakai string mentah dari API agar presisi quantity tetap valid.
        qty = str(pos.get("positionAmt", "")).lstrip("-").lstrip("+")
        order = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        }
        if pos.get("positionSide") not in (None, "", "BOTH"):
            order["positionSide"] = pos["positionSide"]
        self.signed("POST", "/fapi/v1/order", order)
        return symbol

    def flatten_all(self):
        """Tutup semua posisi (market, reduce-only) + batalkan semua order.

        Kembalikan (daftar_symbol_ditutup, jumlah_order_dibatalkan).
        """
        closed = []
        for pos in self.positions():
            try:
                symbol = self.close_position(pos)
            except GuardianError as exc:
                log.warning("gagal menutup %s: %s", pos.get("symbol"), exc)
                continue
            if symbol:
                closed.append(symbol)
        cancelled = 0
        try:
            orders = self.open_orders()
            if orders:
                self.signed("DELETE", "/fapi/v1/allOpenOrders")
                cancelled = len(orders)
        except GuardianError as exc:
            log.warning("gagal membatalkan open orders: %s", exc)
        return closed, cancelled


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = int(chat_id)
        self.offset = 0

    def api(self, method, fields=None, timeout=30):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = urllib.parse.urlencode(fields or {}).encode("utf-8")
        return http_json("POST", url, data=body,
                         headers={"Content-Type": "application/x-www-form-urlencoded"},
                         timeout=timeout)

    def send(self, text):
        try:
            self.api("sendMessage", {"chat_id": self.chat_id, "text": text},
                     timeout=30)
        except GuardianError as exc:
            log.warning("telegram send gagal: %s", exc)

    def poll(self, timeout=30):
        """Long-poll getUpdates. Kembalikan list update (kosong jika gagal)."""
        try:
            data = self.api("getUpdates",
                            {"timeout": timeout, "offset": self.offset},
                            timeout=timeout + 15)
        except GuardianError as exc:
            log.warning("telegram poll gagal: %s", exc)
            return []
        updates = data.get("result", []) if isinstance(data, dict) else []
        for upd in updates:
            self.offset = max(self.offset, int(upd.get("update_id", 0)) + 1)
        return updates


# ---------------------------------------------------------------------------
# Perintah Telegram
# ---------------------------------------------------------------------------

def _fmt_positions(client):
    try:
        positions = client.positions()
    except GuardianError as exc:
        return f"(gagal ambil posisi: {exc})"
    lines = []
    for pos in positions:
        amt = float(pos.get("positionAmt", 0) or 0)
        if amt == 0:
            continue
        arah = "LONG" if amt > 0 else "SHORT"
        upnl = float(pos.get("unRealizedProfit", 0) or 0)
        lines.append(f"  {pos['symbol']} {arah} {abs(amt):g} "
                     f"@ {pos.get('entryPrice', '?')} (uPnL {upnl:+.2f})")
    return "\n".join(lines) if lines else "(tidak ada posisi terbuka)"


def build_status(cfg, client, state):
    try:
        equity, _ = client.equity()
        err = None
    except GuardianError as exc:
        equity, err = None, str(exc)
    start = float(state.get("start_equity", 0) or 0)
    if equity is None:
        pnl_line = f"(gagal ambil ekuitas: {err})"
        eq_line = "-"
    else:
        pnl = equity - start if state.get("day") == today_utc() else 0.0
        eq_line = f"{equity:.2f} USDT"
        pnl_line = f"{pnl:+.2f} USDT (baseline {start:.2f})"
    locked = state.get("locked", False)
    status = "🔒 TERKUNCI" if locked else "✅ AKTIF"
    reason = f"\nAlasan kunci: {state.get('lock_reason', '')}" if locked and state.get("lock_reason") else ""
    return (
        "🛡️ futures-guardian — /status\n"
        f"Ekuitas: {eq_line}\n"
        f"Day PnL (UTC): {pnl_line}\n"
        f"Batas rugi harian: {cfg['max_daily_loss_usdt']:.2f} USDT\n"
        f"Status: {status}{reason}\n"
        f"Posisi terbuka:\n{_fmt_positions(client)}"
    )


def handle_command(cmd, cfg, client, tg):
    """Eksekusi perintah Telegram resmi. Kembalikan True jika dikenal."""
    state = load_state(cfg["state_dir"])
    if cmd == "/status":
        tg.send(build_status(cfg, client, state))
    elif cmd == "/kill":
        closed, cancelled = client.flatten_all()
        state["locked"] = True
        state["lock_reason"] = "manual /kill oleh pemilik"
        save_state(cfg["state_dir"], state)
        tg.send("🛡️ /kill dieksekusi:\n"
                f"Posisi ditutup: {', '.join(closed) if closed else '(tidak ada)'}\n"
                f"Order dibatalkan: {cancelled}\n"
                "Guardian DIKUNCI. /unlock untuk membuka.")
        log.warning("/kill manual: closed=%s cancelled=%d", closed, cancelled)
    elif cmd == "/unlock":
        state["locked"] = False
        state["lock_reason"] = ""
        save_state(cfg["state_dir"], state)
        tg.send("🔓 Guardian dibuka. Pengawasan rugi harian aktif kembali.")
        log.warning("kunci dibuka manual via /unlock")
    elif cmd == "/help":
        tg.send("🛡️ futures-guardian\n"
                "/status — ekuitas, day PnL, posisi terbuka\n"
                "/kill — tutup SEMUA posisi + batalkan order + kunci\n"
                "/unlock — buka kunci guardian\n"
                "/help — daftar perintah ini")
    else:
        return False
    return True


def handle_update(update, cfg, client, tg):
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    if int(chat.get("id", 0)) != int(cfg["telegram_chat_id"]):
        return  # abaikan chat yang tidak resmi, tanpa balasan
    text = (msg.get("text") or "").strip().split()[0] if (msg.get("text") or "").strip() else ""
    if not text.startswith("/"):
        return
    cmd = text.split("@")[0].lower()
    try:
        if not handle_command(cmd, cfg, client, tg):
            tg.send(f"Perintah tidak dikenal: {cmd}. Ketik /help.")
    except GuardianError as exc:
        tg.send(f"Gagal menjalankan {cmd}: {exc}")
    except Exception as exc:  # noqa: BLE001 - jangan matikan loop karena 1 perintah
        log.exception("error tak terduga pada %s", cmd)
        tg.send(f"Error tak terduga pada {cmd}: {type(exc).__name__}")


# ---------------------------------------------------------------------------
# Cycle utama
# ---------------------------------------------------------------------------

def run_cycle(cfg, client, tg):
    """Satu cycle pengawasan. Kembalikan dict laporan."""
    equity, _ = client.equity()
    today = today_utc()
    state = load_state(cfg["state_dir"])
    was_locked = bool(state.get("locked"))
    new_state, action = decide_action(
        equity=equity, today=today, state=state,
        max_daily_loss_usdt=cfg["max_daily_loss_usdt"],
        lock_until_next_day=cfg["lock_until_next_day"])
    report = {"equity": round(equity, 4),
              "day_pnl": round(new_state.get("last_day_pnl", 0.0), 4),
              "action": action, "locked": bool(new_state.get("locked"))}

    if action == "breach":
        closed, cancelled = client.flatten_all()
        save_state(cfg["state_dir"], new_state)
        report.update({"closed": closed, "cancelled_orders": cancelled})
        log.warning("BATAS RUGI HARIAN TERSENTUH: day_pnl=%+.2f -> flatten %s",
                    report["day_pnl"], closed)
        tg.send("🚨🛡️ GUARDIAN: batas rugi harian tersentuh!\n"
                f"Day PnL: {report['day_pnl']:+.2f} USDT "
                f"(batas -{cfg['max_daily_loss_usdt']:.2f})\n"
                f"Posisi ditutup: {', '.join(closed) if closed else '(tidak ada)'}\n"
                f"Order dibatalkan: {cancelled}\n"
                "Guardian dikunci sampai hari UTC berikutnya.")
    elif action == "day_reset":
        save_state(cfg["state_dir"], new_state)
        if was_locked:
            tg.send("🔓 Hari UTC baru: kunci guardian dibuka otomatis. "
                    f"Baseline ekuitas: {equity:.2f} USDT.")
            log.info("hari baru: kunci dibuka otomatis, baseline=%.2f", equity)
    else:
        save_state(cfg["state_dir"], new_state)
    return report


def main_loop(cfg, client, tg):
    log.info("futures-guardian aktif: max_daily_loss=%.2f USDT, poll=%ds",
             cfg["max_daily_loss_usdt"], cfg["poll_seconds"])
    tg.send("🛡️ futures-guardian aktif.\n"
            f"Batas rugi harian: {cfg['max_daily_loss_usdt']:.2f} USDT. "
            "Ketik /help untuk perintah.")
    while True:
        t0 = time.time()
        try:
            report = run_cycle(cfg, client, tg)
            log.info("cycle: equity=%.2f day_pnl=%+.2f action=%s",
                     report["equity"], report["day_pnl"], report["action"])
        except GuardianError as exc:
            log.warning("cycle gagal: %s", exc)
        except Exception:  # noqa: BLE001 - loop tidak boleh mati
            log.exception("cycle error tak terduga")
        for upd in tg.poll(timeout=min(cfg["poll_seconds"], 50)):
            handle_update(upd, cfg, client, tg)
        sisa = cfg["poll_seconds"] - (time.time() - t0)
        if sisa > 0:
            time.sleep(sisa)


# ---------------------------------------------------------------------------
# Self-check (tanpa network)
# ---------------------------------------------------------------------------

# Vektor regresi internal: memastikan sign_params() deterministik dan tidak
# berubah diam-diam. Dihitung dari implementasi ini (bukan vektor eksternal).
_HMAC_VECTOR_PARAMS = {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET",
                       "quantity": "0.001", "recvWindow": 5000,
                       "timestamp": 1700000000000}
_HMAC_VECTOR_SECRET = "guardian_test_secret_001"
_HMAC_VECTOR_EXPECTED = None  # diisi di bawah setelah definisi


def _compute_hmac_vector():
    return sign_params(_HMAC_VECTOR_PARAMS, _HMAC_VECTOR_SECRET)


_HMAC_VECTOR_EXPECTED = (
    "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=0.001&recvWindow=5000"
    "&timestamp=1700000000000&signature="
    "aed9197a9bf4b3e687eef110a8b1bd2f535d0cbba94a4dba2ba8552b0dd4ad17"
)


def self_check():
    """Validasi tanpa network. Return 0 jika semua lolos."""
    import tempfile
    gagal = []

    def check(nama, fn):
        try:
            fn()
            print(f"  [OK] {nama}")
        except Exception as exc:
            print(f"  [FAIL] {nama}: {exc}")
            gagal.append(nama)

    def check_version():
        assert sys.version_info >= (3, 10), "butuh python >= 3.10"
    check("python >= 3.10", check_version)

    def check_config_defaults():
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "config.json")
            with open(p, "w") as fh:
                json.dump({"telegram_bot_token": "x", "telegram_chat_id": 1}, fh)
            cfg = load_config(p)
            assert cfg["poll_seconds"] == 15
            assert cfg["max_daily_loss_usdt"] == 5.0
            assert cfg["lock_until_next_day"] is True
            assert cfg["state_dir"] == os.path.join(td, "state")
    check("config parsing + defaults", check_config_defaults)

    def check_env_override():
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "config.json")
            with open(p, "w") as fh:
                json.dump({}, fh)
            os.environ["FG_TELEGRAM_BOT_TOKEN"] = "tokendarienv"
            os.environ["FG_TELEGRAM_CHAT_ID"] = "987654"
            try:
                cfg = load_config(p)
                assert cfg["telegram_bot_token"] == "tokendarienv"
                assert cfg["telegram_chat_id"] == 987654
            finally:
                del os.environ["FG_TELEGRAM_BOT_TOKEN"]
                del os.environ["FG_TELEGRAM_CHAT_ID"]
    check("override token/chat-id via env", check_env_override)

    def check_hmac_deterministik():
        a = sign_params(_HMAC_VECTOR_PARAMS, _HMAC_VECTOR_SECRET)
        b = sign_params(_HMAC_VECTOR_PARAMS, _HMAC_VECTOR_SECRET)
        assert a == b, "tidak deterministik"
        sig = a.rsplit("signature=", 1)[1]
        assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig), \
            "signature bukan hex 64 char"
        c = sign_params(_HMAC_VECTOR_PARAMS, "secret_lain")
        assert c != a, "secret berbeda harus hasilkan signature berbeda"
    check("HMAC-SHA256 deterministik & format", check_hmac_deterministik)

    def check_hmac_vector():
        if "PLACEHOLDER" in _HMAC_VECTOR_EXPECTED:
            raise AssertionError("vektor regresi belum diisi (tahap dev)")
        assert _compute_hmac_vector() == _HMAC_VECTOR_EXPECTED, "vektor regresi berubah!"
    check("HMAC vektor regresi internal", check_hmac_vector)

    def check_tidak_breach():
        st, aksi = decide_action(equity=102.0, today="2026-10-16",
                                 state={"day": "2026-10-16", "start_equity": 100.0,
                                        "locked": False},
                                 max_daily_loss_usdt=5.0, lock_until_next_day=True)
        assert aksi == "none" and not st["locked"]
        assert abs(st["last_day_pnl"] - 2.0) < 1e-9
    check("logika: tidak breach saat PnL positif", check_tidak_breach)

    def check_breach_tepat():
        st, aksi = decide_action(equity=95.0, today="2026-10-16",
                                 state={"day": "2026-10-16", "start_equity": 100.0,
                                        "locked": False},
                                 max_daily_loss_usdt=5.0, lock_until_next_day=True)
        assert aksi == "breach" and st["locked"] is True, f"aksi={aksi}"
    check("logika: breach tepat di -max_daily_loss", check_breach_tepat)

    def check_terkunci_tetap():
        st, aksi = decide_action(equity=80.0, today="2026-10-16",
                                 state={"day": "2026-10-16", "start_equity": 100.0,
                                        "locked": True, "lock_reason": "x"},
                                 max_daily_loss_usdt=5.0, lock_until_next_day=True)
        assert aksi == "none" and st["locked"] is True
    check("logika: terkunci -> tidak flatten ganda", check_terkunci_tetap)

    def check_hari_baru():
        st, aksi = decide_action(equity=90.0, today="2026-10-17",
                                 state={"day": "2026-10-16", "start_equity": 100.0,
                                        "locked": True, "lock_reason": "x"},
                                 max_daily_loss_usdt=5.0, lock_until_next_day=True)
        assert aksi == "day_reset"
        assert st["locked"] is False and st["start_equity"] == 90.0
        assert st["day"] == "2026-10-17"
    check("logika: hari UTC baru reset baseline + buka kunci", check_hari_baru)

    def check_state_roundtrip():
        with tempfile.TemporaryDirectory() as td:
            s = {"day": "2026-10-16", "start_equity": 100.0, "locked": True}
            save_state(td, s)
            assert load_state(td) == s
    check("state save/load roundtrip", check_state_roundtrip)

    def check_contoh_tanpa_secret():
        d = os.path.dirname(os.path.abspath(__file__))
        for nama in ("config.example.json", ".env.example"):
            p = os.path.join(d, nama)
            isi = open(p, encoding="utf-8").read()
            assert "YOUR_" in isi, f"{nama} tidak berisi placeholder"
    check("file contoh hanya berisi placeholder", check_contoh_tanpa_secret)

    def check_retryable():
        assert _is_retryable(http.client.RemoteDisconnected())
        assert _is_retryable(http.client.IncompleteRead(b"partial"))
        assert _is_retryable(socket.timeout())
        assert _is_retryable(ConnectionError("x"))
        assert _is_retryable(urllib.error.URLError(ConnectionError("x")))
        assert not _is_retryable(ValueError("x"))
    check("deteksi transient errors untuk retry", check_retryable)

    print()
    if gagal:
        print(f"SELF-CHECK FAILED ({len(gagal)} gagal): {', '.join(gagal)}")
        return 1
    print("SELF-CHECK PASSED: semua pemeriksaan lolos.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_argparser():
    ap = argparse.ArgumentParser(description="futures-guardian: risk guard Binance Futures")
    ap.add_argument("--config", default=None, help="path config.json")
    ap.add_argument("--once", action="store_true", help="jalankan satu cycle lalu keluar")
    ap.add_argument("--self-check", action="store_true",
                    help="validasi tanpa network lalu keluar")
    return ap


def main(argv=None):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    args = build_argparser().parse_args(argv)
    if args.self_check:
        return self_check()

    cfg_path = resolve_config_path(args.config)
    cfg = load_config(cfg_path)
    if not cfg["telegram_bot_token"] or cfg["telegram_chat_id"] == 0:
        print("ERROR: telegram_bot_token / telegram_chat_id belum diisi "
              "(config.json atau env FG_TELEGRAM_BOT_TOKEN / FG_TELEGRAM_CHAT_ID).",
              file=sys.stderr)
        return 2
    api_key, api_secret, env_path = load_binance_keys()
    log.info("config: %s | env: %s", cfg_path, env_path)
    client = BinanceClient(api_key, api_secret, cfg["binance_base_url"],
                           recv_window=5000)
    tg = Telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"])

    if args.once:
        try:
            report = run_cycle(cfg, client, tg)
        except GuardianError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(report, indent=2))
        return 0

    main_loop(cfg, client, tg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
