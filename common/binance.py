# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""Helper Binance Futures API (stdlib only): HMAC-SHA256 signing dan
signed_request generik. Diekstrak dari pola identik di guardian & cockpit.
"""

import hashlib
import hmac
import json
import time
import urllib.parse

from . import http as _http

DEFAULT_BASE_URL = "https://fapi.binance.com"


class BinanceApiError(Exception):
    """Error dari Binance API.

    - code  : kode error API (mis. -2015); None jika body JSON tidak punya
              key "code"; MISSING jika body bukan JSON dict.
    - msg   : pesan error (msg / description / potongan body).
    - status: HTTP status; None untuk error API di dalam respons 200.
    """

    def __init__(self, code, msg, status, original=None):
        self.code = code
        self.msg = msg
        self.status = status
        self.original = original
        super().__init__(f"Binance [{code}] {msg} (http={status})")


#: Sentinel: body error bukan JSON dict (membedakan dari dict tanpa "code").
MISSING = object()


def sign_params(params, secret):
    """Bangun query string + signature HMAC-SHA256 (fungsi murni)."""
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


def _api_error_from_http(http_err):
    """Terjemahkan HttpApiError -> BinanceApiError (format pesan identik
    dengan implementasi asli di guardian/cockpit)."""
    body = http_err.body or ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            code = payload.get("code")
            msg = payload.get("msg") or payload.get("description") or body
        else:
            code, msg = MISSING, body[:200]
    except Exception:
        code, msg = MISSING, body[:200]
    return BinanceApiError(code, msg, http_err.status, http_err.original)


def signed_request(method, path, params, api_key, api_secret,
                   base_url=DEFAULT_BASE_URL, timeout=30, recv_window=5000,
                   _retries=2):
    """Signed request ke Binance Futures API.

    GET  -> parameter di query string. POST -> form-urlencoded body.
    Header X-MBX-APIKEY selalu dikirim. timestamp + recvWindow dibangun
    fresh di setiap percobaan retry (seperti implementasi asli).

    Raise BinanceApiError untuk error API (termasuk respons 200 yang
    membawa {"code": ...}); biarkan RequestError lolos untuk kegagalan
    network agar pemanggil bisa menerjemahkannya sendiri.
    """
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    attempt = 0
    while True:
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p["recvWindow"] = recv_window
        headers = {"X-MBX-APIKEY": api_key}
        if method == "GET":
            url = base_url + path + "?" + sign_params(p, api_secret)
            data = None
        else:  # POST
            url = base_url + path
            data = sign_params(p, api_secret).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            result = _http.api_call(method, url, data=data, headers=headers,
                                    timeout=timeout, _retries=0)
        except _http.HttpApiError as exc:
            raise _api_error_from_http(exc) from None
        except _http.RequestError as exc:
            if _http.is_retryable(exc.original) and attempt < _retries:
                attempt += 1
                time.sleep(min(2 ** attempt, 4))
                continue
            raise
        if isinstance(result, dict) and result.get("code"):
            raise BinanceApiError(result.get("code"), result.get("msg"),
                                  None, None)
        return result
