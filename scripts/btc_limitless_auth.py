#!/usr/bin/env python3
"""Authenticated Limitless API client — the exact HMAC scheme, no guessing.

Recipe recovered from the official SDK (limitless-sdk 1.0.12,
limitless_sdk/api/hmac.py + api/http_client.py) and reimplemented here on the
stdlib so the lab keeps no runtime dependency on it:

    secret_key = base64_decode(secret)          # the secret is base64!
    message    = f"{timestamp}\\n{METHOD}\\n{path_with_query}\\n{body}"
    signature  = base64_encode(HMAC_SHA256(secret_key, message))
    headers    = lmts-api-key / lmts-timestamp / lmts-signature
    timestamp  = ISO-8601 UTC, milliseconds, 'Z'  (2026-08-03T00:12:34.567Z)

Two details every hand-rolled attempt missed: the secret is base64-DECODED
before use as the key, and the message is NEWLINE-separated (not concatenated).

Credentials come from the environment or .env — never arguments, never the repo:
    LIMITLESS_API_KEY      (the token id shown as "API Key" in the UI)
    LIMITLESS_API_SECRET   (the base64 "Secret", shown once at creation)

Read-only by default. Placing orders needs an explicit call to place_order();
nothing in this module trades on import or on a plain run.

    python3 scripts/btc_limitless_auth.py            # auth check + balances
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

BASE = "https://api.limitless.exchange"

# A bare Python UA is 403'd at the edge on every path — indistinguishable from
# an auth failure unless you send browser-ish headers (measured 2026-08-02).
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def iso_timestamp() -> str:
    """ISO-8601 UTC with milliseconds and a Z suffix (the SDK's format)."""
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """Limitless HMAC signature. `path` must include the query string."""
    key = base64.b64decode(secret)
    message = f"{timestamp}\n{method.upper()}\n{path}\n{body}"
    digest = hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def load_creds() -> tuple[str, str]:
    key = os.environ.get("LIMITLESS_API_KEY")
    sec = os.environ.get("LIMITLESS_API_SECRET")
    if not (key and sec) and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if line.startswith("LIMITLESS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("LIMITLESS_API_SECRET="):
                sec = line.split("=", 1)[1].strip().strip('"\'')
    if not (key and sec):
        print("missing LIMITLESS_API_KEY / LIMITLESS_API_SECRET (env or .env)",
              file=sys.stderr)
        sys.exit(2)
    return key, sec


def request(method: str, path: str, params: Optional[dict] = None,
            payload: Optional[Any] = None, creds: Optional[tuple] = None,
            timeout: float = 15.0) -> tuple[int, Any]:
    """Signed request. Returns (status, parsed-json-or-text)."""
    key, sec = creds or load_creds()
    request_path = path
    if params:
        request_path = f"{path}?{urllib.parse.urlencode(params, doseq=True)}"
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    ts = iso_timestamp()
    headers = {
        "lmts-api-key": key,
        "lmts-timestamp": ts,
        "lmts-signature": sign(sec, ts, method, request_path, body),
        "User-Agent": UA,
        "Accept": "application/json",
    }
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + request_path, method=method.upper(),
                                 data=body.encode() if body else None,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"
    try:
        return status, json.loads(raw)
    except Exception:  # noqa: BLE001
        return status, raw


# ---- read-only helpers ------------------------------------------------------
def get_positions() -> tuple[int, Any]:
    return request("GET", "/portfolio/positions")


def get_balance() -> tuple[int, Any]:
    return request("GET", "/portfolio/balance")


def get_orders(market_slug: Optional[str] = None) -> tuple[int, Any]:
    return request("GET", "/orders", params={"market": market_slug} if market_slug else None)


def place_order(token_id: str, price: float, size: float, side: str,
                market_slug: str, order_type: str = "GTC") -> tuple[int, Any]:
    """REAL ORDER. Never called by this module's __main__; the executor calls it
    explicitly after its own risk checks."""
    return request("POST", "/orders", payload={
        "tokenId": str(token_id), "price": price, "size": size,
        "side": side.upper(), "orderType": order_type, "marketSlug": market_slug,
    })


def main() -> int:
    key, _ = load_creds()
    print(f"Limitless auth check — key ...{key[-4:]} (secret never printed)\n")
    ok = False
    for label, fn in (("positions", get_positions), ("balance", get_balance),
                      ("open orders", get_orders)):
        st, data = fn()
        body = json.dumps(data)[:220] if not isinstance(data, str) else data[:220]
        print(f"  {label:<12} -> {st}  {body}")
        ok = ok or st == 200
    print("\nAUTH OK — signing recipe confirmed." if ok else
          "\nStill not authenticating. Check the token has the Trading scope and "
          "that the secret is the full base64 string including any '=' padding.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
