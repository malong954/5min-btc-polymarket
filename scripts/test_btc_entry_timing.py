#!/usr/bin/env python3
"""Offline tests for the entry-timing analyzer. Run:
    python3 scripts/test_btc_entry_timing.py"""

from btc_entry_timing import analyze


def check(desc, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {desc}")
    if not cond:
        raise SystemExit(1)


def build(rounds):
    """rounds: list of (samples, outcome) where samples is [(sec_left, move, up, dn), ...]."""
    evs = []
    for i, (samps, outcome) in enumerate(rounds):
        for sl, mv, up, dn in samps:
            evs.append({"type": "sample", "round": i, "sec_left": sl, "move": mv,
                        "up_ask": up, "dn_ask": dn, "spot": 60000})
        evs.append({"type": "result", "round": i, "outcome": outcome})
    return evs


def test_price_rises_toward_close():
    # Every round: BTC up (move>0 -> buy UP), UP ask climbs from 0.80 (early) to 0.98
    # (late), and it always resolves UP -> 100% win at every offset, cheaper early.
    rounds = []
    for _ in range(20):
        samps = [(250, 40, 0.80, 0.20), (190, 55, 0.88, 0.12),
                 (130, 70, 0.94, 0.06), (70, 80, 0.98, 0.02)]
        rounds.append((samps, "UP"))
    rows = analyze(build(rounds))
    by = {tuple(r["offset"]): r for r in rows}
    check("all offset bins populated", len(rows) >= 4)
    check("early entry cheaper than late",
          by[(240, 270)]["avg_price"] < by[(60, 90)]["avg_price"])
    check("early entry has higher edge when winrate equal",
          by[(240, 270)]["edge"] > by[(60, 90)]["edge"])
    check("winrate 100% (always resolves with the move)", all(r["winrate"] == 1.0 for r in rows))


def test_early_less_certain():
    # Half the rounds reverse: early the move points the wrong way sometimes, but by
    # late the move aligns with outcome. So late winrate should exceed early winrate.
    rounds = []
    for i in range(20):
        outcome = "UP" if i % 2 == 0 else "DOWN"
        # early move is noisy (random-ish), late move matches outcome
        early_mv = 30 if i % 3 == 0 else -30
        late_mv = 60 if outcome == "UP" else -60
        samps = [(250, early_mv, 0.82, 0.18), (70, late_mv, 0.97, 0.03)]
        rounds.append((samps, outcome))
    rows = analyze(build(rounds))
    by = {tuple(r["offset"]): r for r in rows}
    check("both bins present", (240, 270) in by and (60, 90) in by)
    check("late entry more accurate than early", by[(60, 90)]["winrate"] >= by[(240, 270)]["winrate"])


def test_efficient_market_negative_edge():
    # Price always equals the true win probability -> edge ~0 or negative everywhere.
    rounds = []
    for i in range(40):
        outcome = "UP" if i % 10 < 8 else "DOWN"   # 80% up
        samps = [(120, 50, 0.80, 0.20)]            # priced exactly at 0.80
        rounds.append((samps, outcome))
    rows = analyze(build(rounds))
    check("priced-fair market shows ~zero edge", all(r["edge"] <= 0.05 for r in rows))


def test_empty_and_missing():
    check("empty log -> no rows", analyze([]) == [])
    # samples without a result round are ignored.
    evs = [{"type": "sample", "round": 5, "sec_left": 100, "move": 10, "up_ask": 0.9, "dn_ask": 0.1}]
    check("samples without result ignored", analyze(evs) == [])


def main():
    test_price_rises_toward_close()
    test_early_less_certain()
    test_efficient_market_negative_edge()
    test_empty_and_missing()
    print("\nAll entry-timing tests passed.")


if __name__ == "__main__":
    main()
