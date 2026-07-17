#!/usr/bin/env python3
"""
Verify Chainlink Data Streams credentials across MAINNET and TESTNET bases,
discover feed IDs, and (with --discover) map the candlestick API.

Reads from the environment (multibot_ctl.sh sources multi/.env first):
  CHAINLINK_STREAMS_API_KEY / CHAINLINK_STREAMS_API_SECRET   required
  CHAINLINK_FEED_ID_BTC                                      stream id (0x...)
  CHAINLINK_STREAMS_BASE                                     optional override
  CHAINLINK_CANDLE_API_KEY                                   optional, --discover

  multi/scripts/multibot_ctl.sh streams
  multi/scripts/multibot_ctl.sh streams --discover

Stream IDs are network-agnostic; testnet serves real market data free of the
mainnet subscription, so if mainnet says "feeds not authorized" the testnet
base may still work — this probe tries both and prints the .env lines to use.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402

MAINNET = "https://api.dataengine.chain.link"
TESTNET = "https://api.testnet-dataengine.chain.link"


def http_err(e) -> str:
    try:
        code = e.response.status_code if e.response is not None else "?"
        body = (e.response.text or "")[:200] if e.response is not None else ""
        return f"HTTP {code}: {body}"
    except Exception:
        return str(e)[:200]


def check_base(base: str, feed: str | None) -> dict:
    import requests
    md.STREAMS_BASE = base
    out: dict = {"base": base, "ok": False}
    try:
        feeds = md.streams_get("/api/v1/feeds")
        rows = feeds.get("feeds")
        if rows is None:
            rows = feeds.get("data")
        out["feeds"] = rows if isinstance(rows, list) else feeds
    except requests.HTTPError as e:
        out["feeds_error"] = http_err(e)
    except Exception as e:
        out["feeds_error"] = str(e)[:200]
        return out  # unreachable — skip report attempt

    if isinstance(out.get("feeds"), list):
        for f in out["feeds"]:
            fid = f.get("feedID") or f.get("feed_id") or f.get("id")
            name = f.get("name") or f.get("description") or ""
            if fid and ("btc" in str(name).lower() and not feed):
                feed = fid
    if not feed:
        out["report_error"] = "no feed id to test (set CHAINLINK_FEED_ID_BTC)"
        return out
    out["feed_tested"] = feed
    try:
        j = md.streams_get("/api/v1/reports/latest", {"feedID": feed})
        rep = md._streams_extract(j)
        if rep:
            out["ok"] = True
            out["price"] = rep["price"]
            out["bid"], out["ask"] = rep["bid"], rep["ask"]
            out["obs_age_s"] = round(time.time() - rep["observations_ts"], 1)
        else:
            out["report_error"] = "undecodable report payload"
    except requests.HTTPError as e:
        out["report_error"] = http_err(e)
    except Exception as e:
        out["report_error"] = str(e)[:200]
    return out


def discover_candles(_bases, _feed) -> None:
    """Probe the documented Candlestick API (separate priceapi.* host,
    Bearer auth): GET /api/v1/history/rows?symbol=BTCUSD&resolution=1m."""
    import requests
    print("\n=== CANDLESTICK API (priceapi hosts, Bearer auth) ===")
    keys = []
    if os.getenv("CHAINLINK_CANDLE_API_KEY"):
        keys.append(("candle-key", os.environ["CHAINLINK_CANDLE_API_KEY"]))
    if md.streams_creds():
        keys.append(("streams-key", md.streams_creds()[0]))
    if not keys:
        print("no keys configured")
        return
    now = int(time.time())
    working = None
    for base in (md.CANDLE_BASE_MAINNET, md.CANDLE_BASE_TESTNET):
        for kname, key in keys:
            try:
                j = md.candles_history("BTCUSD", "1m", now - 300, now,
                                       base=base, api_key=key)
                o = md._candle_first_open(j, now - 300)
                print(f"✔ {base} [{kname}] -> 200, last-5min BTC candle open: "
                      f"{('$%s' % f'{o:,.2f}') if o else 'shape? '}"
                      f"{'' if o else json.dumps(j)[:200]}")
                working = working or (base, kname)
            except requests.HTTPError as e:
                print(f"✘ {base} [{kname}] -> {http_err(e)}")
            except Exception as e:
                print(f"✘ {base} [{kname}] -> {str(e)[:140]}")
        # quick reconnaissance for the 1s live endpoint
        for path in ("/api/v1/live/rows", "/api/v1/latest", "/api/v1/price"):
            try:
                r = requests.get(base + path, params={"symbol": "BTCUSD"},
                                 headers={"Authorization": f"Bearer {keys[0][1]}"},
                                 timeout=6)
                print(f"  live-guess {path} -> {r.status_code} {r.text[:120]}")
            except Exception as e:
                print(f"  live-guess {path} -> {str(e)[:80]}")
    if working:
        base, kname = working
        print("\nCandles WORK — add to multi/.env:")
        print(f"  CHAINLINK_CANDLE_BASE={base}")
        if kname == "streams-key" and not os.getenv("CHAINLINK_CANDLE_API_KEY"):
            print("  (streams key doubles as the candle key — nothing more needed)")
        print("Slot-boundary opens for settlement then use Chainlink-source "
              "candles automatically after a bot restart.")
    else:
        print("\nNo candle host accepted the keys — paste this back.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true",
                    help="also map candlestick API endpoints")
    args = ap.parse_args()

    creds = md.streams_creds()
    if not creds:
        print("✘ CHAINLINK_STREAMS_API_KEY / CHAINLINK_STREAMS_API_SECRET not set —")
        print("  put them in multi/.env (see multi/.env.example).")
        return 1
    print(f"✔ credentials present (key …{creds[0][-6:]})")

    feed = md.streams_feed_id("btc")
    override = os.getenv("CHAINLINK_STREAMS_BASE")
    bases = [override] if override else [MAINNET, TESTNET]

    results = []
    for base in bases:
        tag = "TESTNET" if "testnet" in base else ("MAINNET" if base == MAINNET else "CUSTOM")
        print(f"\n=== {tag}  {base} ===")
        r = check_base(base, feed)
        results.append(r)
        if isinstance(r.get("feeds"), list):
            n = len(r["feeds"])
            print(f"  feeds: {n} enabled" + ("" if n else " (none granted on this base)"))
            for f in r["feeds"][:15]:
                print(f"    {f.get('feedID') or f.get('id')}  "
                      f"{f.get('name') or f.get('description') or ''}")
        elif "feeds_error" in r:
            print(f"  feeds: {r['feeds_error']}")
        if r.get("ok"):
            print(f"  ✔ LIVE BTC: ${r['price']:,.2f} (bid {r['bid']:,.2f} / "
                  f"ask {r['ask']:,.2f}), observation {r['obs_age_s']}s old")
        elif "report_error" in r:
            print(f"  reports: {r['report_error']}")

    working = next((r for r in results if r.get("ok")), None)
    print()
    if working:
        base = working["base"]
        print("=== RESULT: WORKING ===")
        print("Ensure multi/.env contains exactly these lines:")
        if base != MAINNET:
            print(f"  CHAINLINK_STREAMS_BASE={base}")
        print(f"  CHAINLINK_FEED_ID_BTC={working['feed_tested']}")
        print("then restart the bots + dashboard — Streams becomes the primary "
              "spot provider automatically.")
    else:
        print("=== RESULT: NO BASE SERVED A REPORT ===")
        print("If both bases said 'feeds not authorized', the account needs a")
        print("stream granted (testnet grants are usually free in the dashboard).")
        print("Paste this whole output back for the next adjustment.")

    if args.discover:
        discover_candles(bases, feed or (working or {}).get("feed_tested"))
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(main())
