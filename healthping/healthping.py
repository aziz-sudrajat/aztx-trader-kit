#!/usr/bin/env python3
# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""healthping — generic health monitor with Telegram alerts.

User mendefinisikan check lewat checks.json (array of
{name, type, interval_seconds, params}). Daemon menjalankan tiap check
sesuai intervalnya, menyimpan status di state.json, dan mengirim alert
Telegram HANYA saat status berubah (OK -> FAIL atau FAIL -> OK), plus
pengingat berkala selama masih FAIL.

Stdlib only, Python >= 3.10.
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
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
MIN_INTERVAL = 5  # detik; interval lebih kecil dinaikkan ke nilai ini


class CheckError(Exception):
    """Kesalahan konfigurasi atau definisi check."""


class TelegramError(Exception):
    """Kegagalan pengiriman ke Telegram API."""


# ---------------------------------------------------------------- log

def log(level: str, msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts} {level} {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- json helpers

_MISSING = object()


def load_json(path: str, default=_MISSING):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is _MISSING:
            raise CheckError(f"file tidak ditemukan: {path}")
        return default
    except json.JSONDecodeError as exc:
        raise CheckError(f"JSON tidak valid di {path}: {exc}")


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- check types

def _check_http(p: dict):
    url = p.get("url")
    if not url:
        raise CheckError("http: params 'url' wajib diisi")
    expect = int(p.get("expect_status", 200))
    timeout = float(p.get("timeout", 10))
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": f"healthping/{VERSION}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:  # status non-2xx tetap memberi kode
        status = exc.code
    except Exception as exc:
        return False, f"GET {url} gagal: {type(exc).__name__}"
    ok = status == expect
    return ok, f"GET {url} -> HTTP {status} (expect {expect})"


def _check_tcp(p: dict):
    host = p.get("host")
    port = p.get("port")
    if not host or port is None:
        raise CheckError("tcp: params 'host' dan 'port' wajib diisi")
    timeout = float(p.get("timeout", 5))
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except Exception as exc:
        return False, f"TCP {host}:{port} gagal: {type(exc).__name__}"
    return True, f"TCP {host}:{port} tersambung"


def _check_disk(p: dict):
    path = p.get("path", "/")
    max_pct = float(p.get("max_pct", 90))
    try:
        du = shutil.disk_usage(path)
    except Exception as exc:
        return False, f"disk {path}: {type(exc).__name__}"
    pct = (du.used / du.total * 100.0) if du.total else 0.0
    ok = pct < max_pct
    return ok, f"disk {path}: {pct:.1f}% terpakai (batas {max_pct:g}%)"


def _check_proc(p: dict):
    name = p.get("name")
    if not name:
        raise CheckError("proc: params 'name' wajib diisi")
    found: list[str] = []
    try:
        pids = [e for e in os.listdir("/proc") if e.isdigit()]
    except Exception as exc:
        return False, f"proc: tidak bisa baca /proc: {type(exc).__name__}"
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except Exception:
            continue
        if cmd and name in cmd:
            found.append(pid)
    ok = bool(found)
    detail = f"proc '{name}': jalan (pid {','.join(found[:5])})" if ok else f"proc '{name}': tidak ditemukan"
    return ok, detail


def _check_cmd(p: dict):
    shell = p.get("shell")
    if not shell:
        raise CheckError("cmd: params 'shell' wajib diisi")
    expect = int(p.get("expect_exit", 0))
    timeout = float(p.get("timeout", 30))
    try:
        proc = subprocess.run(
            shell, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"cmd timeout setelah {timeout:g}s: {shell[:60]}"
    except Exception as exc:
        return False, f"cmd error: {type(exc).__name__}"
    ok = proc.returncode == expect
    return ok, f"cmd exit={proc.returncode} (expect {expect}): {shell[:60]}"


_CHECKERS = {
    "http": _check_http,
    "tcp": _check_tcp,
    "disk": _check_disk,
    "proc": _check_proc,
    "cmd": _check_cmd,
}


def run_check(ctype: str, params: dict):
    """Jalankan satu check. Return (ok: bool, detail: str)."""
    fn = _CHECKERS.get(ctype)
    if fn is None:
        raise CheckError(f"tipe check tidak dikenal: {ctype!r}")
    ok, detail = fn(params or {})
    return bool(ok), str(detail)


def validate_checks(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise CheckError("checks.json harus berupa array")
    seen: set[str] = set()
    out: list[dict] = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise CheckError(f"check #{i} harus berupa object")
        name = c.get("name")
        ctype = c.get("type")
        if not name or not isinstance(name, str):
            raise CheckError(f"check #{i}: 'name' wajib string tak-kosong")
        if name in seen:
            raise CheckError(f"check #{i}: nama duplikat: {name!r}")
        seen.add(name)
        if ctype not in _CHECKERS:
            raise CheckError(f"check {name!r}: tipe tidak dikenal: {ctype!r}")
        try:
            interval = int(c.get("interval_seconds", 60))
        except (TypeError, ValueError):
            raise CheckError(f"check {name!r}: 'interval_seconds' harus angka")
        if interval < MIN_INTERVAL:
            log("WARN", f"check {name!r}: interval {interval}s < {MIN_INTERVAL}s, dipakai {MIN_INTERVAL}s")
            interval = MIN_INTERVAL
        params = c.get("params", {})
        if not isinstance(params, dict):
            raise CheckError(f"check {name!r}: 'params' harus object")
        out.append({"name": name, "type": ctype, "interval_seconds": interval, "params": params})
    return out


# ---------------------------------------------------------------- telegram

# Klasifikasi transient error dipakai dari modul bersama (implementasi identik).
_is_retryable = _http.is_retryable


def telegram_api_call(token: str, method: str, fields: dict | None = None, retries: int = 2):
    """Panggil Telegram Bot API; kembalikan dict 'result'.

    (Didelegasikan ke common.telegram.api_call; tipe & pesan TelegramError
    identik dengan implementasi asli, termasuk decode "replace".)
    """
    try:
        return _telegram.api_call(token, method, fields, _retries=retries,
                                  decode_errors="replace")
    except _telegram.TelegramApiError as exc:
        if exc.payload is not None:
            raise TelegramError(f"Telegram API error: {exc.payload}") from None
        raise TelegramError(
            f"Telegram API unavailable ({type(exc.original).__name__})") from None


def telegram_send(token: str, chat_id: str, text: str) -> None:
    telegram_api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})


# Alert sink: hook untuk test. Jika berupa list, alert ditampung di sana
# alih-alih dikirim ke network.
_alert_sink: list | None = None


def notify(cfg: dict, text: str) -> bool:
    """Kirim alert; True jika terkirim (atau ditampung test sink)."""
    if _alert_sink is not None:
        _alert_sink.append(text)
        return True
    token = cfg.get("telegram_bot_token") or ""
    chat_id = cfg.get("telegram_chat_id") or ""
    if not token or not chat_id:
        log("WARN", f"telegram belum dikonfigurasi; alert hanya di-log: {text[:100]}")
        return False
    try:
        telegram_send(str(token), str(chat_id), text)
        return True
    except Exception as exc:
        log("WARN", f"gagal kirim telegram: {exc}")
        return False


# ---------------------------------------------------------------- state & alerting

def evaluate(name: str, ok: bool, detail: str, entry: dict | None, cfg: dict, now: float):
    """Tentukan apakah perlu alert. Return (entry_baru, teks_alert|None).

    Alert hanya saat transisi status (termasuk FAIL pertama kali);
    OK pertama kali diam. Selama FAIL, pengingat tiap remind_minutes.
    """
    status = "ok" if ok else "fail"
    if entry is None:
        entry = {"status": None, "last_change": 0, "last_alert": 0, "last_detail": ""}
    prev = entry.get("status")
    alert = None
    if prev != status:
        entry["status"] = status
        entry["last_change"] = now
        entry["last_alert"] = now
        entry["last_detail"] = detail
        if not ok:
            alert = f"\U0001f6a8 [healthping] FAIL: {name} \u2014 {detail}"
        elif prev == "fail":
            alert = f"\u2705 [healthping] pulih: {name} \u2014 {detail}"
        # prev None + ok -> diam (start bersih)
    else:
        entry["last_detail"] = detail
        if not ok:
            remind = float(cfg.get("remind_minutes", 60)) * 60.0
            if now - float(entry.get("last_alert", 0)) >= remind:
                entry["last_alert"] = now
                alert = f"\u23f0 [healthping] masih FAIL: {name} \u2014 {detail}"
    return entry, alert


# ---------------------------------------------------------------- daemon

def run_daemon(cfg_path: str, checks_path: str | None) -> int:
    cfg = load_json(cfg_path, {})
    if not isinstance(cfg, dict):
        raise CheckError(f"{cfg_path} harus berupa object JSON")
    checks_path = checks_path or cfg.get("checks_file", "checks.json")
    state_path = cfg.get("state_file", "state.json")
    checks = validate_checks(load_json(checks_path))
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}

    stop = {"flag": False}

    def _on_signal(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    schedule = {c["name"]: 0.0 for c in checks}  # jalan sekali saat start
    log("INFO", f"healthping v{VERSION}: memantau {len(checks)} check")
    dirty = False
    while not stop["flag"]:
        now = time.time()
        for c in checks:
            name = c["name"]
            if now < schedule[name]:
                continue
            try:
                ok, detail = run_check(c["type"], c["params"])
            except Exception as exc:  # satu check gagal total -> FAIL, daemon tetap jalan
                ok, detail = False, f"check error: {type(exc).__name__}: {exc}"
            entry, alert = evaluate(name, ok, detail, state.get(name), cfg, now)
            state[name] = entry
            dirty = True
            if alert:
                log("ALERT" if not ok else "INFO", alert)
                notify(cfg, alert)
            else:
                log("DEBUG", f"{name}: {status_word(ok)} — {detail}")
            schedule[name] = now + c["interval_seconds"]
        if dirty:
            save_json(state_path, state)
            dirty = False
        for _ in range(10):  # tidur 1 dtk tapi responsif terhadap sinyal
            if stop["flag"]:
                break
            time.sleep(0.1)
    save_json(state_path, state)
    log("INFO", "healthping berhenti")
    return 0


def status_word(ok: bool) -> str:
    return "OK" if ok else "FAIL"


def cmd_once(checks_path: str | None, cfg: dict) -> int:
    checks_path = checks_path or cfg.get("checks_file", "checks.json")
    checks = validate_checks(load_json(checks_path))
    rc = 0
    for c in checks:
        try:
            ok, detail = run_check(c["type"], c["params"])
        except Exception as exc:
            ok, detail = False, f"check error: {type(exc).__name__}: {exc}"
        print(f"[{status_word(ok)}] {c['name']}: {detail}")
        if not ok:
            rc = 1
    return rc


# ---------------------------------------------------------------- self-check

def self_check() -> bool:
    """Uji semua tipe check + logika alert tanpa network eksternal
    (kecuali http ke 127.0.0.1). Return True jika semua lolos."""
    global _alert_sink
    passed, failed = 0, 0

    def check(label: str, cond: bool):
        nonlocal passed, failed
        print(f"{'PASS' if cond else 'FAIL'}  {label}", flush=True)
        if cond:
            passed += 1
        else:
            failed += 1

    # --- tcp: port tertutup -> FAIL tertangani
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    ok, _ = run_check("tcp", {"host": "127.0.0.1", "port": closed_port, "timeout": 2})
    check("tcp port tertutup -> FAIL tertangani", not ok)

    # --- tcp: port terbuka -> OK
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    open_port = ls.getsockname()[1]

    def _accept():
        try:
            conn, _ = ls.accept()
            conn.close()
        except Exception:
            pass

    threading.Thread(target=_accept, daemon=True).start()
    ok, _ = run_check("tcp", {"host": "127.0.0.1", "port": open_port, "timeout": 2})
    check("tcp port terbuka -> OK", ok)
    ls.close()

    # --- http: server lokal 127.0.0.1
    class _Quiet(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/"
        ok, _ = run_check("http", {"url": url, "expect_status": 200, "timeout": 5})
        check("http lokal 200 sesuai expect -> OK", ok)
        ok, _ = run_check("http", {"url": url, "expect_status": 500, "timeout": 5})
        check("http expect tidak cocok -> FAIL tertangani", not ok)
    finally:
        server.shutdown()
        server.server_close()
    ok, _ = run_check("http", {"url": f"http://127.0.0.1:{closed_port}/", "timeout": 2})
    check("http server mati -> FAIL tertangani", not ok)

    # --- disk
    ok, _ = run_check("disk", {"path": "/", "max_pct": 100})
    check("disk / batas 100% -> OK", ok)
    ok, _ = run_check("disk", {"path": "/", "max_pct": 0})
    check("disk / batas 0% -> FAIL tertangani", not ok)
    ok, _ = run_check("disk", {"path": "/path/yang/tidak/ada-xyz"})
    check("disk path tidak ada -> FAIL tertangani", not ok)

    # --- cmd
    ok, _ = run_check("cmd", {"shell": "exit 0"})
    check("cmd 'exit 0' -> OK", ok)
    ok, _ = run_check("cmd", {"shell": "exit 1"})
    check("cmd 'exit 1' -> FAIL tertangani", not ok)
    ok, _ = run_check("cmd", {"shell": "exit 3", "expect_exit": 3})
    check("cmd expect_exit=3 cocok -> OK", ok)
    ok, _ = run_check("cmd", {"shell": "sleep 5", "timeout": 1})
    check("cmd timeout -> FAIL tertangani", not ok)

    # --- proc
    ok, _ = run_check("proc", {"name": "python3"})
    check("proc 'python3' (diri sendiri) -> OK", ok)
    ok, _ = run_check("proc", {"name": "proses-yang-pasti-tidak-ada-xyz-123"})
    check("proc nama aneh -> FAIL tertangani", not ok)

    # --- tipe tidak dikenal
    try:
        run_check("nope", {})
        check("tipe check tak dikenal -> raise CheckError", False)
    except CheckError:
        check("tipe check tak dikenal -> raise CheckError", True)
    try:
        validate_checks([{"name": "x", "type": "nope", "interval_seconds": 60}])
        check("validate menolak tipe tak dikenal", False)
    except CheckError:
        check("validate menolak tipe tak dikenal", True)

    # --- transisi status + dedup alert (tanpa network; pakai sink)
    _alert_sink = []
    cfg = {"remind_minutes": 60}
    state: dict = {}
    entry, alert = evaluate("web", False, "connection refused", state.get("web"), cfg, 1000.0)
    state["web"] = entry
    check("FAIL pertama -> alert", alert is not None and "FAIL" in alert)
    if alert:
        notify(cfg, alert)
    entry, alert = evaluate("web", False, "connection refused", state.get("web"), cfg, 2000.0)
    state["web"] = entry
    check("tetap FAIL sebelum 60 mnt -> diam", alert is None)
    if alert:
        notify(cfg, alert)
    entry, alert = evaluate("web", False, "connection refused", state.get("web"), cfg, 1000.0 + 3601)
    state["web"] = entry
    check("tetap FAIL lewat 60 mnt -> pengingat", alert is not None and "masih FAIL" in alert)
    if alert:
        notify(cfg, alert)
    entry, alert = evaluate("web", True, "HTTP 200", state.get("web"), cfg, 5000.0)
    state["web"] = entry
    check("FAIL -> OK -> alert pulih", alert is not None and "pulih" in alert)
    if alert:
        notify(cfg, alert)
    entry, alert = evaluate("web", True, "HTTP 200", state.get("web"), cfg, 6000.0)
    check("tetap OK -> diam", alert is None)
    entry2, alert2 = evaluate("baru", True, "HTTP 200", None, cfg, 7000.0)
    check("OK pertama kali -> diam", alert2 is None)
    check("dedup: tepat 3 alert terkirim", len(_alert_sink) == 3)
    _alert_sink = None

    # --- state save/load roundtrip
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "state.json")
        save_json(sp, {"a": {"status": "fail", "last_alert": 1}})
        check("state save/load roundtrip", load_json(sp) == {"a": {"status": "fail", "last_alert": 1}})

    print(f"\nself-check: {passed} lolos, {failed} gagal")
    return failed == 0


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="healthping",
        description="Monitor kesehatan generik dengan alert Telegram.",
    )
    ap.add_argument("--config", default="config.json", help="path config.json")
    ap.add_argument("--checks", default=None, help="override path checks.json")
    ap.add_argument("--once", action="store_true", help="jalankan semua check sekali lalu print")
    ap.add_argument("--self-check", action="store_true", help="uji internal tanpa network eksternal")
    ap.add_argument("--version", action="store_true", help="tampilkan versi")
    args = ap.parse_args(argv)

    if args.version:
        print(f"healthping {VERSION}")
        return 0
    if args.self_check:
        return 0 if self_check() else 1

    cfg = load_json(args.config, {})
    if not isinstance(cfg, dict):
        raise CheckError(f"{args.config} harus berupa object JSON")
    if args.once:
        return cmd_once(args.checks, cfg)
    return run_daemon(args.config, args.checks)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckError as exc:
        log("ERROR", str(exc))
        sys.exit(2)
    except KeyboardInterrupt:
        log("INFO", "interrupted")
        sys.exit(130)
