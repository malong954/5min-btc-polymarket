#!/usr/bin/env python3
"""Offline tests for the log analyzer. Run: python3 scripts/test_btc_analyze.py"""

import json
import os
import tempfile

from btc_analyze import load_events, join_by_round, analyze


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def make_log(path):
    evs = []
    base = 1_700_000_000
    # 6 rounds: prediction + entry + settle each, joined by round.
    data = [
        # round, conf, move, side, entry, result
        (1, 0.85, 90, "UP", 0.88, "win"),
        (2, 0.90, 120, "UP", 0.90, "win"),
        (3, 0.55, 30, "DOWN", 0.84, "loss"),
        (4, 0.92, 150, "DOWN", 0.91, "win"),
        (5, 0.62, 40, "UP", 0.86, "loss"),
        (6, 0.95, 200, "DOWN", 0.93, "win"),
    ]
    bal = 100.0
    for r, conf, move, side, ep, res in data:
        ts = base + r * 300
        evs.append({"ts": ts, "type": "prediction", "round": r, "direction": side,
                    "confidence": conf, "btc_move_usd": move, "rsi_1m": 60})
        evs.append({"ts": ts, "type": "entry", "round": r, "side": side, "entry_price": ep,
                    "price_source": "polymarket", "confidence": conf, "stake_usd": 10})
        pnl = round(10 * (1 - ep) / ep, 2) if res == "win" else -10.0
        bal = round(bal + pnl, 2)
        actual = side if res == "win" else ("DOWN" if side == "UP" else "UP")
        evs.append({"ts": ts, "type": "settle", "round": r, "side": side, "actual": actual,
                    "result": res, "entry_price": ep, "pnl_usd": pnl, "balance": bal, "stake_usd": 10})
        # a heartbeat too, to prove non-round-joined events are ignored gracefully
        evs.append({"ts": ts, "type": "heartbeat", "round": r, "price": 60000})
    with open(path, "w") as f:
        for e in evs:
            f.write(json.dumps(e) + "\n")


def test_join_and_analyze():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "live.jsonl")
        make_log(path)
        recs = join_by_round(load_events(path))
        check("joined exactly the 6 settled rounds", len(recs) == 6)
        check("join pulled confidence from entry/prediction",
              all(r["confidence"] is not None for r in recs))
        check("join pulled btc_move from prediction",
              all(r["btc_move_usd"] is not None for r in recs))
        a = analyze(recs)
        check("winrate computed (4 wins / 6)", abs(a["winrate"] - round(4 / 6, 4)) < 1e-9)
        check("all trades flagged real-priced", a["real_priced"] == 6)
        check("avg real entry price computed", a["avg_real_entry_price"] is not None)
        check("breakeven uses avg real entry", abs(a["breakeven"] - a["avg_real_entry_price"]) < 1e-9)
        check("by-confidence buckets present", len(a["by_confidence"]) >= 2)
        check("by-side present for UP and DOWN", set(a["by_side"]) == {"UP", "DOWN"})
        check("small-sample warning present", any("SAMPLE" in o for o in a["observations"]))
        check("edge observation present", any("EDGE" in o for o in a["observations"]))


def test_handles_missing_and_empty():
    a = analyze([])
    check("empty log -> zero trades, no crash", a["settled_trades"] == 0 and a["winrate"] == 0.0)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.jsonl")
        with open(path, "w") as f:
            f.write("not json\n")
            f.write(json.dumps({"type": "heartbeat", "price": 1}) + "\n")  # no round
        recs = join_by_round(load_events(path))
        check("garbage + round-less events -> no settled records", recs == [])


def main():
    test_join_and_analyze()
    test_handles_missing_and_empty()
    print("\nAll analyzer tests passed.")


if __name__ == "__main__":
    main()
