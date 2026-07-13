#!/usr/bin/env python3
"""
Record the Polymarket UP/DOWN ask trajectory + BTC move + INDICATOR signal
through each 5m round.

Purpose: find the OPTIMAL entry time. Entering earlier is CHEAPER (the contract
hasn't run up to $0.99 yet) but less certain; entering later is surer but you pay
near $1 for almost no profit. To find the sweet spot we need the price at many
points in the round — which the trader never logged (it prices once, ~2m left).

This is a pure OBSERVER: it places no trades. For each round it samples, every
--poll seconds while `--min-left <= seconds_left <= --max-left`:
  - the live BTC move (spot - round open),
  - the real Polymarket UP/DOWN best asks, and
  - what the INDICATOR model (impulse + divergence, the same brain the trader
    uses) says AS OF that instant — direction / confidence / score.
then records the round's outcome. Logging the indicator signal at each moment is
what lets scripts/btc_entry_timing.py test the real thesis: "enter earlier, on a
strong indicator signal, before the order book reprices." Feed it that log.

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
    ap = argparse.ArgumentParser(description="Record Polymarket price + BTC move + indicator trajectory per 5m round")
    ap.add_argument("--provider", default="binance", choices=["binance", "cryptocompare", "coinbase", "kraken"])
    ap.add_argument("--poll", type=float, default=5.0, help="Seconds between samples")
    ap.add_argument("--min-left", type=float, default=30.0, help="Start sampling when this many seconds remain")
    ap.add_argument("--max-left", type=float, default=280.0, help="Stop sampling above this many seconds remaining")
    ap.add_argument("--klines-every", type=float, default=20.0, help="Seconds between the heavier 1m-kline/indicator refreshes")
    ap.add_argument("--history-min", type=int, default=180, help="Minutes of 1m history to fetch for indicators")
    ap.add_argument("--log", default="out/trajectory.jsonl")
    ap.add_argument("--max-steps", type=int, default=None)
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)
    logf = open(args.log, "a")

    from btc_price_feeds import build_feeds
    from btc_polymarket import current_prices, current_slug, resolved_outcome
    from btc_backtest import Bar, DEFAULT_WEIGHTS, MTFModel
    from btc_history import fetch_history

    feeds = build_feeds([args.provider])
    if not feeds:
        print(f"unknown provider {args.provider}", file=sys.stderr)
        return 2
    feed = feeds[0]

    bars: list = []

    def fetch_bars() -> list:
        rows = fetch_history(args.provider, days=args.history_min / 1440.0)
        return [Bar(r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]

    def indicator_signal(round_start: int, now: float, spot: Optional[float] = None):
        """What the indicator model says AS OF now, earlier in the round — with
        the impulse fed the LIVE spot so confidence ticks between bar closes.
        Returns (direction, confidence, score, divergence) or Nones.
        divergence: -1 bearish / 0 none / +1 bullish — the raw leading signal,
        logged so the trailing-side FADE experiment (E3) can be analyzed."""
        if not bars:
            return None, None, None, None
        as_of = (int(now) // 60) * 60   # close time of the last fully-closed 1m bar
        try:
            sig = MTFModel(bars, weights=DEFAULT_WEIGHTS).evaluate(round_start, as_of_ts=as_of, spot=spot)
        except Exception:
            return None, None, None, None
        if sig is None:
            return None, None, None, None
        div = sig.features.get("sub_divergence_1m")
        div = int(div) if isinstance(div, (int, float)) else None
        return sig.direction, round(sig.confidence, 4), round(sig.score, 4), div

    # Rolling spot buffer -> sub-minute velocity (dollars moved over the last
    # N seconds). This is the "faster timeframe" signal: does a 5s/15s/30s BTC
    # move lead the settle beyond what the order-book price already reflects?
    spot_buf: list = []   # (ts, spot), newest last

    def push_spot(ts: float, px: float) -> None:
        spot_buf.append((ts, px))
        cutoff = ts - 120.0
        while len(spot_buf) > 2 and spot_buf[0][0] < cutoff:
            spot_buf.pop(0)

    def velocity(now: float, window: float):
        """Dollars BTC moved over the last `window` seconds (spot_now - spot_then),
        using the buffered sample closest to now-window. None if not enough span."""
        if len(spot_buf) < 2:
            return None
        target = now - window
        older = [s for s in spot_buf if s[0] <= target]
        ref = older[-1] if older else spot_buf[0]
        if now - ref[0] < window * 0.5:   # not enough history to fill the window
            return None
        return round(spot_buf[-1][1] - ref[1], 2)

    def emit(o: dict) -> None:
        logf.write(json.dumps(o, separators=(",", ":")) + "\n")
        logf.flush()

    print(f"# trajectory recorder | provider={args.provider} poll={args.poll}s "
          f"window={args.min_left}-{args.max_left}s left | +indicator signal | "
          f"OBSERVER (no trades)", file=sys.stderr)

    cur_round: Optional[int] = None
    round_open: Optional[float] = None
    last_spot: Optional[float] = None
    last_klines = 0.0
    last_slot_retry = 0.0
    last_diag = 0.0
    pm_missing_logged: set = set()
    # Closed rounds awaiting Polymarket's OFFICIAL resolution (the ground-truth
    # label; the spot-feed result can disagree on near-flat rounds).
    pm_pending: dict = {}
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
                        # Queue the round for its OFFICIAL Polymarket resolution
                        # (takes ~a minute to settle on-chain; poll a few times).
                        pm_pending[cur_round] = {"next": now + 45.0, "tries": 0}
                    cur_round = r
                    round_open = feed.slot_open(r)   # exact 5m-open BTC price
                    last_spot = None
                    if round_open is None:
                        print(f"# slot_open failed for round {r} — will retry / "
                              f"fall back to first spot", file=sys.stderr)
                spot = feed.spot()
                if spot is not None:
                    last_spot = spot
                    push_spot(now, spot)
                # slot_open swallows failures into None (fine for the trading
                # gate, fatal for a recorder): without an open we can emit
                # NOTHING all round, silently. Self-heal two ways -- use the
                # first spot of a YOUNG round as the open (feed-consistent with
                # the last-spot close used to settle), and keep retrying the
                # exact kline open every 30s.
                if round_open is None:
                    if spot is not None and sec_left >= 285:
                        round_open = spot
                        print(f"# round {r}: using first spot {spot:.2f} as open", file=sys.stderr)
                    elif now - last_slot_retry >= 30:
                        last_slot_retry = now
                        round_open = feed.slot_open(r)
                # Fetch official resolutions for recently closed rounds.
                for rs in list(pm_pending):
                    p = pm_pending[rs]
                    if now < p["next"]:
                        continue
                    outc = resolved_outcome(current_slug(rs))
                    if outc in ("UP", "DOWN"):
                        emit({"type": "result_pm", "round": rs, "outcome": outc, "ts": int(now)})
                        del pm_pending[rs]
                    else:
                        p["tries"] += 1
                        p["next"] = now + 30.0
                        if p["tries"] >= 6:   # ~3.5 min of retries -> give up
                            print(f"# round {rs}: no official resolution after "
                                  f"{p['tries']} tries", file=sys.stderr)
                            del pm_pending[rs]
                in_window = args.min_left <= sec_left <= args.max_left
                # Never be silent: if a full minute of in-window polls produced
                # nothing, say exactly which gate is blocking.
                if in_window and now - last_diag >= 60 and (round_open is None or spot is None):
                    last_diag = now
                    print(f"# waiting: round_open={'MISSING' if round_open is None else 'ok'} "
                          f"spot={'MISSING' if spot is None else 'ok'} (round {r}, {sec_left:.0f}s left)",
                          file=sys.stderr)
                # Refresh the heavier 1m klines periodically (and lazily on first use),
                # so the indicator signal stays current without a fetch every poll.
                if in_window and (not bars or now - last_klines >= args.klines_every):
                    try:
                        bars = fetch_bars()
                        last_klines = now
                    except Exception as e:
                        print(f"# kline fetch error: {e}", file=sys.stderr)
                if in_window and round_open is not None and spot is not None:
                    pm = current_prices(now)
                    if not pm and r not in pm_missing_logged:
                        pm_missing_logged.add(r)
                        print(f"# round {r}: no polymarket market found (gamma slug lookup empty)",
                              file=sys.stderr)
                    if pm:
                        idir, iconf, iscore, idiv = indicator_signal(r, now, spot=spot)
                        emit({"type": "sample", "round": r, "sec_left": round(sec_left, 1),
                              "move": round(spot - round_open, 2), "spot": round(spot, 2),
                              "up_ask": pm.get("UP"), "dn_ask": pm.get("DOWN"),
                              "up_sz": pm.get("UP_size"), "dn_sz": pm.get("DOWN_size"),
                              "up_bid": pm.get("UP_bid"), "dn_bid": pm.get("DOWN_bid"),
                              "up_bid_sz": pm.get("UP_bid_size"), "dn_bid_sz": pm.get("DOWN_bid_size"),
                              "ind_dir": idir, "ind_conf": iconf, "ind_score": iscore,
                              "ind_div": idiv,
                              "vel_5s": velocity(now, 5), "vel_15s": velocity(now, 15),
                              "vel_30s": velocity(now, 30), "vel_60s": velocity(now, 60),
                              "ts": int(now)})
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
