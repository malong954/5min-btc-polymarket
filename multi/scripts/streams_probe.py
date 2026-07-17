#!/usr/bin/env python3
"""
Verify Chainlink Data Streams credentials and discover feed IDs.

Reads CHAINLINK_STREAMS_API_KEY / CHAINLINK_STREAMS_API_SECRET (and optional
CHAINLINK_STREAMS_BASE, CHAINLINK_FEED_ID_BTC) from the environment —
multibot_ctl.sh sources multi/.env first, so put them there.

  multi/scripts/multibot_ctl.sh streams

Prints: credential status, the feeds available to your account, and a live
decoded BTC report (price / bid / ask / observation age). On auth failure it
prints the server's response body so the HMAC scheme can be corrected fast.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402


def main() -> int:
    creds = md.streams_creds()
    if not creds:
        print("✘ CHAINLINK_STREAMS_API_KEY / CHAINLINK_STREAMS_API_SECRET not set.")
        print("  Put them in multi/.env (see multi/.env.example) — they come from")
        print("  your Chainlink account's Data Streams credentials page.")
        return 1
    print(f"✔ credentials present (key …{creds[0][-6:]})  base={md.STREAMS_BASE}")

    import requests
    try:
        feeds = md.streams_get("/api/v1/feeds")
    except requests.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        print(f"✘ /api/v1/feeds -> HTTP {e.response.status_code if e.response is not None else '?'}")
        print(f"  server said: {body}")
        print("  (401/403 usually means the key lacks Data Streams access, or the")
        print("  auth scheme needs adjusting — paste this output back for a fix.)")
        return 1
    except Exception as e:
        print(f"✘ /api/v1/feeds unreachable: {e}")
        return 1

    rows = feeds.get("feeds")
    if rows is None:
        rows = feeds.get("data")
    if rows is None and isinstance(feeds, list):
        rows = feeds
    btc_candidates = []
    if isinstance(rows, list) and not rows:
        print("✔ authenticated OK, but the account has NO streams enabled yet.")
        print("  In your Chainlink dashboard, open Data Streams and enable the")
        print("  BTC/USD stream for this key (docs.chain.link/data-streams/crypto-streams")
        print("  lists all stream IDs). Then set CHAINLINK_FEED_ID_BTC in multi/.env")
        print("  and re-run this probe — /reports may work once the feed is granted.")
    if isinstance(rows, list) and rows:
        print(f"✔ account has {len(rows)} feed(s):")
        for f in rows[:25]:
            fid = f.get("feedID") or f.get("feed_id") or f.get("id") or "?"
            name = f.get("name") or f.get("description") or f.get("pair") or ""
            print(f"    {fid}  {name}")
            if "btc" in str(name).lower() or "btc" in str(fid).lower():
                btc_candidates.append(fid)
    if rows is None:
        print(f"✔ feeds response: {json.dumps(feeds)[:400]}")

    feed = md.streams_feed_id("btc") or (btc_candidates[0] if btc_candidates else None)
    if not feed:
        print("✘ no CHAINLINK_FEED_ID_BTC configured and no BTC feed auto-detected —")
        print("  copy the BTC/USD feedID from the list above (or your dashboard)")
        print("  into multi/.env as CHAINLINK_FEED_ID_BTC=0x...")
        return 1
    if not md.streams_feed_id("btc"):
        print(f"→ using auto-detected BTC feed {feed} for this probe; persist it in "
              "multi/.env as CHAINLINK_FEED_ID_BTC")
        import os
        os.environ["CHAINLINK_FEED_ID_BTC"] = str(feed)

    try:
        rep = md.streams_latest("btc")
    except requests.HTTPError as e:
        body = e.response.text[:500] if e.response is not None else ""
        print(f"✘ /api/v1/reports/latest -> HTTP "
              f"{e.response.status_code if e.response is not None else '?'}: {body}")
        return 1
    if not rep:
        print("✘ report fetch returned no decodable payload")
        return 1
    age = time.time() - rep["observations_ts"]
    print(f"✔ LIVE BTC via Data Streams: ${rep['price']:,.2f} "
          f"(bid {rep['bid']:,.2f} / ask {rep['ask']:,.2f}), observation {age:.1f}s old")
    print("\nAll good — workers and dashboard will now prefer Streams automatically")
    print("(spot signals, impulse velocity, and settlement all use the resolution feed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
