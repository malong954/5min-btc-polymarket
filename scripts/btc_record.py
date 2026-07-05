#!/usr/bin/env python3
"""
Record the Polymarket UP/DOWN ask trajectory + BTC move through each 5m round.

Purpose: find the OPTIMAL entry time. Entering earlier is CHEAPER (the contract
hasn't run up to $0.99 yet) but less certain; entering later is surer but you pay
near $1 for almost no profit. To find the sweet spot we need the price at many
points in the round — which the trader never logged (it prices once, ~2m left).

This is a pure OBSERVER: it places no trades. For each round it samples, every
--poll seconds while `--min-left <= seconds_left <= --max-left`, the live BTC
move and the real Polymarket UP/DOWN best asks, then records the round's outcome.
Feed the log to scripts/btc_entry_timing.py.

    python3 scripts/btc_record.py --provider binance --log out/trajectory.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Record Polymarket price + BTC move trajectory per 5m round")
    ap.add_argument("--provider", default="binance", choices=["binance", "cryptocompare", "coinbase", "kraken"])
    ap.add_argument("--poll", type=float, default=5.0, help="Seconds between samples")
    ap.add_argument("--min-left", type=float, default=30.0, help="Start sampling when this many seconds remain")
    ap.add_argument("--max-left", type=float, default=280.0, help="Stop sampling above this many seconds remaining")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--max-steps", type=int, default=None)
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    logf = open(args.log, "a")

    from btc_price_feeds import build_feeds
    from btc_polymarket import current_prices

    feeds = build_feeds([args.provider])
    if not feeds:
        print(f"unknown provider {args.provider}", file=sys.stderr)
        return 2
    feed = feeds[0]

    def emit(o: dict) -> None:
        logf.write(json.dumps(o, separators=(",", ":")) + "\n")
        logf.flush()

    print(f"# trajectory recorder | provider={args.provider} poll={args.poll}s "
          f"window={args.min_left}-{args.max_left}s left | OBSERVER (no trades)", file=sys.stderr)

    cur_round: Optional[int] = None
    round_open: Optional[float] = None
    last_spot: Optional[float] = None
    n = 0
    try:
        while args.max_steps is None or n < args.max_steps:
            try:
                now = time.time()
                r = bucket_5m(int(now))
                sec_left = (r + 300) - now
                if r != cur_round:
                    # Round rolled over -> record the previous round's outcome.
                    if cur_round is not None and round_open is not None and last_spot is not None:
                        outcome = "UP" if last_spot > round_open else "DOWN"
                        emit({"type": "result", "round": cur_round, "open": round(round_open, 2),
                              "close": round(last_spot, 2), "outcome": outcome, "ts": int(now)})
                    cur_round = r
                    round_open = feed.slot_open(r)   # exact 5m-open BTC price
                    last_spot = None
                spot = feed.spot()
                if spot is not None:
                    last_spot = spot
                if args.min_left <= sec_left <= args.max_left and round_open is not None and spot is not None:
                    pm = current_prices(now)
                    if pm:
                        emit({"type": "sample", "round": r, "sec_left": round(sec_left, 1),
                              "move": round(spot - round_open, 2), "spot": round(spot, 2),
                              "up_ask": pm.get("UP"), "dn_ask": pm.get("DOWN"), "ts": int(now)})
            except Exception as e:
                print(f"# record error: {e}", file=sys.stderr)
            n += 1
            if args.max_steps is not None and n >= args.max_steps:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    finally:
        logf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
