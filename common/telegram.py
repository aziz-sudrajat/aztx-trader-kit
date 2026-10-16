# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""Pengiriman pesan via Telegram Bot API (stdlib only).

Diekstrak dari pola identik di radar (send_telegram -> bool),
healthping (telegram_api_call -> raise) dan guardian (Telegram.api).
Token selalu disamarkan sebelum masuk log.
"""

import json
import logging
import urllib.parse

from . import http as _http

log = logging.getLogger(__name__)


class TelegramApiError(Exception):
    """Kegagalan Telegram Bot API.

    - original: exception jaringan asli (None jika API menolak / !ok).
    - payload : dict respons API saat !ok (None untuk kegagalan network).
    """

    def __init__(self, original, payload=None):
        self.original = original
        self.payload = payload
        if payload is not None:
            super().__init__(f"Telegram API error: {payload}")
        else:
            super().__init__(
                f"Telegram API unavailable ({type(original).__name__})")


def api_call(bot_token, method, fields=None, timeout=30, _retries=2,
             decode_errors="strict"):
    """Panggil method Telegram Bot API. Kembalikan dict 'result'.

    Retry diam-diam untuk transient network errors. Raise TelegramApiError
    jika API menolak (!ok) atau network gagal setelah retry habis.
    """
    body = urllib.parse.urlencode(fields or {}).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        data = _http.api_call("POST", url, data=body, headers=headers,
                              timeout=timeout, _retries=_retries,
                              decode_errors=decode_errors)
    except (_http.HttpApiError, _http.RequestError) as exc:
        raise TelegramApiError(exc.original) from None
    if not isinstance(data, dict) or not data.get("ok"):
        raise TelegramApiError(None, payload=data)
    return data["result"]


def send_message(bot_token, chat_id, text, timeout=30, _retries=2,
                 decode_errors="strict"):
    """Kirim pesan teks. Kembalikan dict 'result' dari API."""
    return api_call(bot_token, "sendMessage",
                    {"chat_id": chat_id, "text": text},
                    timeout=timeout, _retries=_retries,
                    decode_errors=decode_errors)
