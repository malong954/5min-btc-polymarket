#!/usr/bin/env python3
"""Offline tests for the Limitless HMAC signing. No network.

Pins the recipe recovered from the official SDK so a future edit can't silently
break it: base64-DECODED secret as the key, newline-separated message, base64
signature, ISO-8601-ms-Z timestamps, lmts-* headers.
Run: python3 scripts/test_btc_limitless_auth.py
"""
import base64
import hashlib
import hmac
import re

from btc_limitless_auth import sign, iso_timestamp


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


SECRET = base64.b64encode(b"secret-material-for-tests-0123456789").decode()


def reference(secret, ts, method, path, body):
    key = base64.b64decode(secret)
    msg = f"{ts}\n{method.upper()}\n{path}\n{body}"
    return base64.b64encode(
        hmac.new(key, msg.encode(), hashlib.sha256).digest()).decode()


def test_matches_the_documented_recipe():
    ts = "2026-08-03T00:12:34.567Z"
    for method, path, body in (("GET", "/portfolio/positions", ""),
                               ("POST", "/orders", '{"size":5}'),
                               ("GET", "/orders?market=btc-up-or-down-15-min-1", "")):
        check(f"{method} {path[:34]} matches reference",
              sign(SECRET, ts, method, path, body) == reference(SECRET, ts, method, path, body))


def test_secret_is_base64_decoded_not_raw():
    """The single most common mistake: using the secret string as the key."""
    ts = "2026-08-03T00:12:34.567Z"
    raw_key = base64.b64encode(
        hmac.new(SECRET.encode(), f"{ts}\nGET\n/x\n".encode(), hashlib.sha256).digest()).decode()
    check("signature differs from the raw-secret variant",
          sign(SECRET, ts, "GET", "/x") != raw_key)


def test_message_is_newline_separated():
    ts = "2026-08-03T00:12:34.567Z"
    concat = base64.b64encode(hmac.new(base64.b64decode(SECRET),
                                       f"{ts}GET/x".encode(), hashlib.sha256).digest()).decode()
    check("signature differs from the concatenated variant",
          sign(SECRET, ts, "GET", "/x") != concat)


def test_method_case_and_inputs_matter():
    ts = "2026-08-03T00:12:34.567Z"
    check("method is upper-cased before signing",
          sign(SECRET, ts, "get", "/x") == sign(SECRET, ts, "GET", "/x"))
    check("a different path changes the signature",
          sign(SECRET, ts, "GET", "/x") != sign(SECRET, ts, "GET", "/y"))
    check("a different body changes the signature",
          sign(SECRET, ts, "POST", "/o", "{}") != sign(SECRET, ts, "POST", "/o", '{"a":1}'))
    check("a different timestamp changes the signature",
          sign(SECRET, ts, "GET", "/x") != sign(SECRET, "2026-08-03T00:12:34.568Z", "GET", "/x"))


def test_timestamp_format():
    t = iso_timestamp()
    check("ISO-8601 UTC with milliseconds and Z",
          bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", t)))


def main():
    test_matches_the_documented_recipe()
    test_secret_is_base64_decoded_not_raw()
    test_message_is_newline_separated()
    test_method_case_and_inputs_matter()
    test_timestamp_format()
    print("\nAll Limitless auth tests passed.")


if __name__ == "__main__":
    main()
