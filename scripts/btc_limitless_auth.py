#!/usr/bin/env python3
"""Limitless API plumbing probe — run on a machine that can reach the exchange.

This does NOT place orders. It verifies, in order:
  1. the OpenAPI spec is reachable and prints the AUTH scheme + the order/
     portfolio/balance endpoints (so the executor is built from fact, not guesses),
  2. a public read works (no auth),
  3. an AUTHENTICATED read works with your HMAC credentials — the actual
     plumbing test.

Credentials are read from the environment ONLY (never args, never a file in the
repo):
    export LIMITLESS_API_KEY=...
    export LIMITLESS_API_SECRET=...
    python3 scripts/btc_limitless_auth.py

The HMAC signing scheme below is the common convention
(sig = HMAC_SHA256(secret, timestamp + METHOD + path + body), hex). Limitless's
exact scheme is confirmed from step (1)'s securitySchemes dump — if the header
names or base string differ, only `sign_request()` changes; nothing else does.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error

REST = "https://api.limitless.exchange"


def _http(method: str, path: str, headers: dict | None = None,
          body: bytes | None = None, timeout: float = 12.0):
    url = REST + path
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


# ---- 1. OpenAPI spec: what is the REAL auth scheme + trade endpoints? --------
def dump_spec() -> dict:
    status, raw = _http("GET", "/api-json")
    print(f"[spec] GET /api-json -> {status}")
    if status != 200:
        print(f"[spec] could not read spec: {raw[:200]}")
        return {}
    try:
        spec = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        print(f"[spec] not JSON: {e}")
        return {}
    comps = (spec.get("components") or {}).get("securitySchemes") or {}
    print(f"[spec] securitySchemes: {json.dumps(comps, indent=2)[:800] or '(none)'}")
    paths = spec.get("paths") or {}
    hits = [p for p in paths if any(k in p.lower() for k in
            ("order", "portfolio", "balance", "position", "trade", "auth", "token"))]
    print(f"[spec] {len(paths)} paths; trade/auth-relevant ({len(hits)}):")
    for p in sorted(hits):
        methods = ",".join(m.upper() for m in paths[p] if m in
                           ("get", "post", "put", "delete", "patch"))
        # note whether the path declares a security requirement
        secured = any((paths[p][m] or {}).get("security") for m in paths[p]
                      if isinstance(paths[p].get(m), dict))
        print(f"        {methods:<12} {p}{'  [secured]' if secured else ''}")
    return spec


# ---- 3. HMAC signing (adjust ONLY this if the spec says otherwise) -----------
def sign_request(secret: str, api_key: str, method: str, path: str,
                 body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    base = ts + method.upper() + path + body
    sig = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    # Candidate header set (the dump in step 1 confirms the real names).
    return {
        "x-api-key": api_key,
        "x-api-signature": sig,
        "x-api-timestamp": ts,
        "Content-Type": "application/json",
    }


def main() -> int:
    key = os.environ.get("LIMITLESS_API_KEY")
    sec = os.environ.get("LIMITLESS_API_SECRET")
    print("=" * 64)
    print("LIMITLESS PLUMBING PROBE — no orders are placed")
    print("=" * 64)

    dump_spec()

    print("\n[public] read a market list (no auth):")
    st, raw = _http("GET", "/markets/active/slugs")
    print(f"[public] GET /markets/active/slugs -> {st}  ({len(raw)} bytes)")

    if not (key and sec):
        print("\n[auth] SKIPPED — set LIMITLESS_API_KEY and LIMITLESS_API_SECRET to test auth.")
        print("       (export them inline for a one-off; never commit them.)")
        return 0

    print(f"\n[auth] key ...{key[-4:]}  secret set: {bool(sec)}")
    # Try a couple of likely read-only, account-scoped endpoints; the spec dump
    # above shows the real one if these miss.
    for path in ("/portfolio", "/portfolio/positions", "/balance", "/account"):
        hdr = sign_request(sec, key, "GET", path)
        st, raw = _http("GET", path, headers=hdr)
        verdict = ("AUTH OK" if st == 200 else
                   "reached, auth rejected" if st in (401, 403) else
                   "not found (try another path from the spec)" if st == 404 else
                   f"status {st}")
        print(f"[auth] GET {path:<22} -> {st}  {verdict}")
        if st == 200:
            print(f"       body: {raw[:200]}")
            print("\n[auth] PLUMBING CONFIRMED — HMAC scheme + credentials work.")
            return 0
    print("\n[auth] no 200 yet. Paste this output back: the spec dump names the")
    print("       real account endpoint + header scheme, and sign_request() adjusts to it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
