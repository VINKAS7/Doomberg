from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

_client = httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0), headers={"user-agent": "belem-risk-engine/0.1"})
_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
# Waits before each retry of a request that never reached the server (DNS lookup, connect, timeout).
RETRY_DELAYS = (0.5, 1.5)


class SourceUnreachable(ConnectionError):
    """A data source could not be reached after retries: DNS lookup, connection or timeout."""

    def __init__(self, url: str, err: Exception):
        self.host = urlsplit(url).hostname or url
        dns = "getaddrinfo" in str(err) or "Name or service not known" in str(err) or "nodename nor servname" in str(err)
        what = "its address could not be resolved (DNS lookup failed)" if dns else f"{type(err).__name__}: {err}"
        super().__init__(f"Could not reach {self.host}: {what}. Check the internet connection, VPN, proxy or firewall "
                         f"(and RPC_URL in backend/.env for RPC hosts), then try again")


def send(method: str, url: str, **kw) -> httpx.Response:
    """``_client.request`` with retries on transport errors; an HTTP error status is returned, not retried."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return _client.request(method, url, **kw)
        except httpx.TransportError as e:
            if delay is None:
                raise SourceUnreachable(url, e) from e
            time.sleep(delay)
    raise AssertionError("unreachable")


def cached(key: str, ttl: float, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (now, value)
    return value


def get_json(url: str, params: dict | None = None, headers: dict | None = None, ttl: float = 300) -> Any:
    key = f"GET {url} {json.dumps(params, sort_keys=True)}"
    return cached(key, ttl, lambda: _raise(send("GET", url, params=params, headers=headers)).json())


def post_json(url: str, body: Any, headers: dict | None = None, ttl: float = 0) -> Any:
    if ttl <= 0:
        return _raise(send("POST", url, json=body, headers=headers)).json()
    key = f"POST {url} {json.dumps(body, sort_keys=True)}"
    return cached(key, ttl, lambda: _raise(send("POST", url, json=body, headers=headers)).json())


def _raise(r: httpx.Response) -> httpx.Response:
    if r.status_code == 400 and r.headers.get("content-type", "").startswith("application/json"):
        return r  # GraphQL validation errors come back as 400 with an "errors" body
    r.raise_for_status()
    return r
