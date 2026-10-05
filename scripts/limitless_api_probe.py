#!/usr/bin/env python3
"""Recon + auth plumbing probe for the Limitless trading API.

Run ON THE MAC (the analysis container cannot reach limitless.exchange):

    # 1) grab the API spec so the executor can be built against the real contract
    .venv/bin/python scripts/limitless_api_probe.py --docs

    # 2) test the HMAC credentials (reads LIMITLESS_API_KEY / LIMITLESS_API_SECRET
    #    from the environment or .env; NEVER pass them as arguments)
    .venv/bin/python scripts/limitless_api_probe.py --auth

--docs saves whatever OpenAPI/Swagger spec it finds to out/limitless_openapi.json
(picked up by `lab.sh sync`, so the analysis side can read it next pull).

--auth tries a small matrix of standard HMAC-signing conventions against
harmless read-only account endpoints and reports the HTTP status per scheme —
the combination that returns 200 tells us Limitless's exact signing recipe.
The secret is never printed, logged, or written anywhere.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE = "https://api.limitless.exchange"

# A bare Python UA is 403'd by many edge/WAF layers regardless of path or
# signature — which looks exactly like "bad auth" and sends you chasing the
# wrong bug. Every request below carries browser-ish headers so a 403 means
# what it says.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
COMMON = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
          "Origin": "https://limitless.exchange",
          "Referer": "https://limitless.exchange/"}

DOC_PATHS = [
    "/api-json", "/api-docs-json", "/docs-json", "/swagger.json", "/openapi.json",
    "/api-docs", "/docs", "/api/docs", "/v1/openapi.json", "/swagger",
    "/swagger-json", "/api/v1/openapi.json", "/redoc",
]

# Read-only endpoints likely to exist on a trading account (safe to probe).
AUTH_PATHS = ["/portfolio", "/portfolio/positions", "/balance", "/balances",
              "/orders", "/auth/verify", "/profile"]


def load_env_creds() -> tuple[str, str]:
    """LIMITLESS_API_KEY / LIMITLESS_API_SECRET from env, falling back to .env."""
    key, sec = os.environ.get("LIMITLESS_API_KEY"), os.environ.get("LIMITLESS_API_SECRET")
    if not (key and sec) and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if line.startswith("LIMITLESS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("LIMITLESS_API_SECRET="):
                sec = line.split("=", 1)[1].strip().strip('"')
    if not (key and sec):
        print("missing LIMITLESS_API_KEY / LIMITLESS_API_SECRET (env or .env)", file=sys.stderr)
        sys.exit(2)
    return key, sec


def fetch(url: str, headers: dict | None = None, timeout: float = 10.0,
          bare: bool = False):
    """bare=True deliberately omits the browser headers (used by the control
    test that proves whether the edge is UA-filtering us)."""
    h = dict(headers or {})
    if not bare:
        for k, v in COMMON.items():
            h.setdefault(k, v)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:500]
    except Exception as e:
        return None, str(e).encode()


def cmd_docs() -> int:
    os.makedirs("out", exist_ok=True)
    for path in DOC_PATHS:
        status, body = fetch(BASE + path)
        note = ""
        if status == 200 and body:
            head = body[:1].decode(errors="ignore")
            if head in "{[":
                with open("out/limitless_openapi.json", "wb") as f:
                    f.write(body)
                note = f"  -> SAVED out/limitless_openapi.json ({len(body):,} bytes)"
            else:
                fn = "out/limitless_docs" + path.replace("/", "_") + ".html"
                with open(fn, "wb") as f:
                    f.write(body)
                note = f"  -> saved {fn} ({len(body):,} bytes)"
        print(f"{path:<18} {status}{note}")
    print("\nIf nothing saved as JSON, open the 'Learn more' link next to the API "
          "token dialog in the Limitless UI and paste that page's URL back.")
    return 0



def sign_variants(key: str, secret: str, method: str, path: str, body: str = ""):
    """Candidate HMAC conventions -> yields (name, headers). One of these is the
    recipe Limitless expects; the --auth matrix reports which returns 200."""
    ts_s = str(int(time.time()))
    ts_ms = str(int(time.time() * 1000))
    sb = secret.encode()

    def h_hex(msg: str) -> str:
        return hmac.new(sb, msg.encode(), hashlib.sha256).hexdigest()

    def h_b64(msg: str) -> str:
        return base64.b64encode(hmac.new(sb, msg.encode(), hashlib.sha256).digest()).decode()

    for ts_name, ts in (("s", ts_s), ("ms", ts_ms)):
        base = ts + method.upper() + path + body            # coinbase-style order
        for enc_name, enc in (("hex", h_hex), ("b64", h_b64)):
            sig = enc(base)
            # header-name set A (x-api-*) and B (x-l-*) — the two seen in the wild
            yield (f"A/{ts_name}/{enc_name}", {
                "x-api-key": key, "x-api-signature": sig, "x-api-timestamp": ts,
                "Content-Type": "application/json"})
            yield (f"B/{ts_name}/{enc_name}", {
                "x-l-api-key": key, "x-l-signature": sig, "x-l-timestamp": ts,
                "Content-Type": "application/json"})
        # OKX-style: base64 over path+method+ts (no body) with OK-ACCESS-* headers
        sig_okx = h_b64(ts + method.upper() + path + body)
        yield (f"OKX/{ts_name}", {
            "OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sig_okx, "OK-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json"})
        # Bearer/token styles (some HMAC APIs still gate on a plain key header)
        yield (f"BEARER/{ts_name}", {
            "Authorization": f"Bearer {key}", "x-api-timestamp": ts,
            "x-api-signature": h_hex(ts + method.upper() + path + body),
            "Content-Type": "application/json"})
        yield (f"KEYONLY/{ts_name}", {
            "x-api-key": key, "Content-Type": "application/json"})
        # signature over ts+path only (no method), both encodings
        for enc_name, enc in (("hex", h_hex), ("b64", h_b64)):
            yield (f"NOMETH/{ts_name}/{enc_name}", {
                "x-api-key": key, "x-api-signature": enc(ts + path),
                "x-api-timestamp": ts, "Content-Type": "application/json"})


def cmd_auth() -> int:
    key, sec = load_env_creds()
    print(f"auth probe: key ...{key[-4:]}  (secret loaded, never printed)\n")

    # --- CONTROL: is the edge blocking us before auth is even considered? ---
    pub = "/markets/active/slugs"
    st_bare, _ = fetch(BASE + pub, headers={"Accept": "application/json"}, bare=True)
    st_ua, _ = fetch(BASE + pub)
    print("control (public endpoint, no credentials):")
    print(f"  {pub:<24} bare python UA -> {st_bare}")
    print(f"  {pub:<24} browser  UA    -> {st_ua}")
    if st_bare == 403 and st_ua == 200:
        print("  => the edge 403s bare Python UAs. Earlier all-403 matrix was UA, not signing.")
    elif st_ua != 200:
        print("  => even the public read fails with browser headers; network/edge issue, not auth.")
    else:
        print("  => UA is not the blocker; 403s below are real auth rejections.")

    # --- CONTROL 2: what does an UNAUTHENTICATED account request return? ---
    st_noauth, body_noauth = fetch(BASE + "/portfolio")
    print(f"\n  /portfolio  with NO auth headers -> {st_noauth} "
          f"({body_noauth[:80].decode(errors='ignore') if body_noauth else ''})")
    print("  (if signed requests return the SAME status, the signature isn't being "
          "accepted at all; if they differ, we are close.)\n")

    hit = False
    for path in AUTH_PATHS:
        for name, headers in sign_variants(key, sec, "GET", path):
            status, body = fetch(BASE + path, headers=headers)
            if status in (200, 401):        # 200 = works; 401 = reached, wrong sig
                tag = "  <== AUTH OK" if status == 200 else ""
                print(f"  {path:<22} {name:<14} -> {status}{tag}")
                if status == 200:
                    hit = True
                    print(f"      body: {body[:160].decode(errors='ignore')}")
    if not hit:
        print("\n(403/404 rows suppressed — only 200/401 shown, since a uniform 403 is "
              "an edge block rather than a signing result.)")
        print("No 200 yet. Next: open the 'Learn more' link beside the API-token dialog "
              "in the Limitless UI and send that URL — it documents the exact recipe.")
    else:
        print("\nPLUMBING CONFIRMED — the 200 row names Limitless's exact HMAC recipe.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Limitless API recon + auth plumbing probe (no orders)")
    ap.add_argument("--docs", action="store_true", help="fetch + save the OpenAPI/Swagger spec")
    ap.add_argument("--auth", action="store_true", help="test HMAC creds against read-only endpoints")
    args = ap.parse_args()
    if not (args.docs or args.auth):
        ap.error("choose --docs and/or --auth")
    rc = 0
    if args.docs:
        rc = cmd_docs() or rc
    if args.auth:
        rc = cmd_auth() or rc
    return rc


if __name__ == "__main__":
    sys.exit(main())
