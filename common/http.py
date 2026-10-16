# Copyright (c) 2026 aziz-sudrajat <azizsudrajat.stb4@gmail.com>
# https://github.com/aziz-sudrajat
#
# All rights reserved. This file is part of the aztx-trader-kit project.
# Unauthorized copying, modification, distribution, or false claim of
# authorship is prohibited.
"""HTTP JSON generik (urllib, stdlib only) dengan retry diam-diam 2x
untuk transient network errors.

Pola ini diekstrak dari implementasi yang terbukti identik di kelima tool
(guardian, radar, cockpit, screener, healthping): RemoteDisconnected,
IncompleteRead, timeout, ConnectionError — termasuk yang terbungkus URLError.
"""

import http.client
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class HttpApiError(Exception):
    """Respons HTTP error (4xx/5xx). Tidak di-retry."""

    def __init__(self, original, status, body, url=""):
        self.original = original
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status}: {body[:200]}")


class RequestError(Exception):
    """Kegagalan network / non-HTTP (setelah retry habis)."""

    def __init__(self, original, url=""):
        self.original = original
        self.url = url
        super().__init__(
            f"request gagal ({type(original).__name__}): {original}")


def is_retryable(exc):
    """True jika exc adalah kegagalan jaringan sesaat yang layak di-retry."""
    if isinstance(exc, (http.client.RemoteDisconnected,
                        http.client.IncompleteRead,
                        TimeoutError, socket.timeout,
                        ConnectionError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return is_retryable(reason) if isinstance(reason, BaseException) else False
    return False


def redact_url(url):
    """Samarkan token bot Telegram (/bot<token>/) sebelum masuk log."""
    return re.sub(r"/bot[^/]+/", "/bot***/", url or "")


def api_call(method, url, *, data=None, headers=None, timeout=30, _retries=2,
             decode_errors="strict"):
    """HTTP request -> JSON (dict/list), dengan retry diam-diam.

    - Respons 4xx/5xx (urllib.error.HTTPError) TIDAK di-retry: langsung
      diangkat sebagai HttpApiError (status + body tersedia).
    - Transient network errors di-retry hingga _retries kali dengan
      backoff time.sleep(min(2**attempt, 4)), lalu RequestError.
    - Body yang bukan JSON valid juga menjadi RequestError.
    """
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(
                url, data=data, headers=headers or {}, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", decode_errors))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise HttpApiError(exc, exc.code, body, url) from None
        except Exception as exc:
            if is_retryable(exc) and attempt < _retries:
                attempt += 1
                log.warning("transient %s %s (%s); retry %d/%d",
                            method, redact_url(url), type(exc).__name__,
                            attempt, _retries)
                time.sleep(min(2 ** attempt, 4))
                continue
            raise RequestError(exc, url) from None
