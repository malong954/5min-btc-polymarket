# PLAYBOOK — what works, what's dead, what's open

Institutional memory for the FLIPPOLYBOT lab. The point of this file is to stop
us re-testing dead ends. Before proposing any "new" idea, check the DEAD list —
if it's there, it was already measured and killed, with the reason.

Rules of evidence used here:
- **Official Polymarket labels only.** Spot-labeled tables disagree ~5–26% of
  the time (worse in fast/flat markets) and have flipped verdicts before.
- **EV = winrate − average entry ask.** Winrate alone is a trap: a 95% winrate
  at a 0.94 ask is +1%; a 76% winrate at 0.70 is +6%. We optimize EV, not winrate.
- **Split-half validation.** A pattern only counts if it is positive in BOTH
  the first and second half of the pooled history independently (the `BOTH+`
  tag in `scripts/btc_deep_study.py`). One-segment rows are noise until they
  replicate forward.

Last updated: 2026-07-24, after the first live-config segment
(BTC 5m +$41.50 / +6.4% per share on 46 entries).

---

## WORKS — validated, keep

| Technique | Evidence | Where |
|---|---|---|
| **Leading side (`side=move`)** — buy whichever side BTC already leads | Positive EV at nearly every entry window, official labels, both halves (truth table: 120–150s +5.8%, 90–120s +2.3%) | `--entry-rule lead` |
| **Volatility regime gate (`REGIME=3`)** — skip rounds whose trailing median move is small | THE load-bearing filter. Live proof: trader's gated entries +2.1%/sh vs the rounds it refused −6.6%/sh (executor-vs-table). Removing it sinks the edge | `--regime-frac 3` |
| **Hard price cap (`MAX_PRICE=0.85`)** — never pay above 0.85 | Kills the pay-0.90+/win-pennies trap that craters accounts (one loss erases ~11 wins at 0.92). Pooled: conf crossing +2.1% uncapped → +4.3% capped | `--max-entry-price 0.85` |
| **BTC lead window 180–240s left, ask ≤0.90** | The BOTH+ cell across all assets. NOT 60–120s (flat/negative), NOT the 0.72 cheap cap (adverse selection) | `LEAD_HI_BTC=240 LEAD_LO_BTC=180 LEAD_CAP_BTC=0.90` |
| **Divergence, asset-signed** — BTC boost, alts veto | Deep study, both halves: BTC crossing +8.9% WITH divergence vs +2.9% without; XRP/SOL the opposite (divergence-free rounds are the good ones). Support is real but small-n (~30–70) — size the boost conservatively, it is unproven live | `DIV_MODE_BTC=boost DIV_MODE_XRP=veto` |

Confidence calibration is monotone (higher conf → higher winrate) on every asset
and segment — it *ranks* well. But the **book prices it**: conf-crossing EV as a
taker is ~0 to slightly negative because the ask has already moved by the time
our confidence arrives. Confidence is a filter, not a standalone taker edge.

---

## DEAD — measured and killed, do NOT re-test

| Dead end | Why it dies | 
|---|---|
| **Naive conf-threshold taker, no price cap** | Fills arrive at 0.90+; breakeven ~90%; one loss wipes many wins. Account $100→$37 (−63%). This is the whole of the old `FINDINGS.md` |
| **Cheap-ask cap (≤0.72)** | Adversely selects losers — the fills you can get that cheap are the unconvinced rounds. Positive-looking, negative-real |
| **Confidence floor ON TOP of the lead rule** | Subtracts EV at every level: lead baseline (conf≥0) beats conf≥0.30 beats 0.50… every segment. The lead edge is *being on the right side cheap*, and conf floors just raise the price |
| **Round momentum / streaks** | Repeat rate 48–51% (coin flip). Buying last round's winner is −EV. After 2-in-a-row, third repeats 50% |
| **Book-depth imbalance (ask/bid size)** | Faint (+1–2% at ~0.50 prices), not stably both-halves positive. Not tradeable alone |
| **Trailing-side divergence fade** | Negative every measurement (−53.9% latest). Divergence does not beat the cheap ask |
| **Velocity signals (`vel_15s`, `vel_30s`)** | Noisy; best windows shuffle segment to segment; no stable edge |
| **Session gating** | Mostly noise at pooled n. Only BTC-US leading-side is marginally +; the dramatic per-segment session splits (e.g. "US −22%") were n≈16 mirages |
| **ETH** | No edge under any rule. Excluded from `ASSETS` |
| **Quiet rule with continuous re-arming** | Busted SOL to $0: entered 66 rounds vs the 22 the validated one-shot rule selects, paying ~5c more for rounds that turned quiet *after* the book converged. FIXED to one-shot (commit 3fa6ba6) — the decision is frozen at the first evaluable poll |
| **SOL quiet rule (even one-shot)** | **486 live trades: 69.3% @ 0.715 = −2.2¢/share, −$110.26 cumulative.** Backtest said +4.0% BOTH+ at ~0.645 entries; live entries land at 0.715 — the 7¢ backtest-to-live price gap eats the whole edge. Decisively dead; stop trading SOL |
| **Divergence boost ×2 on BTC live** | Busted BTC 5m $100→$0 (2026-07-24). div_signal correlation was **+0.000** that segment — the 2× fired on rounds where divergence had zero predictive value, pure variance amplification, turning a normal drawdown into ruin. The boost's pooled support (+8.9% vs +2.9%) is real but too small-n to justify 2× sizing live. Run divergence as a filter at most, never as a stake multiplier |
| **Session gating (Asia/Euro-only, US-ban)** | Tempting after a bad US print, but it's a one-segment mirage. 2026-07-24: US −24.6% but n=13; Europe −10.4% at n=71 did the real damage; Asia −1.9% still negative. And the sign FLIPS between segments — pooled data had US marginally *positive*. Cutting US would not have saved the account. Do not gate on session |

---

## SIZING — settled empirically (2026-07-25), do not re-argue

Replayed over **1,332 real trades of the EXACT live rule** — REGIME=3 gate +
180–240s window + **decisive move ≥0.016% of spot** + ask ≤0.85, official
labels, 14.6 days. Starting bankroll $100:

| bet size | final | max drawdown |
|---|---|---|
| 2% of balance | $225 | 28% |
| **4% (half-Kelly)** | **$390** | **50%** |
| 6% | $519 | 67% |
| 8% (≈full Kelly) | $530 | 79% |
| 10% | $411 | 88% |
| 15% | $63 | 98% |
| 20% | ruin | 100% |

- **Live-rule edge: win 70.7% @ 0.681 = +2.6¢/share → full Kelly = 8.2%.**
- **Use `SIZING=percent STAKE_PCT=0.04`** (half-Kelly): ~73% of peak growth at
  ~2/3 the drawdown, and robust to the edge being overestimated. Percent-of-
  balance auto-scales up as the account grows and down in a drawdown, and cannot
  bust the way flat staking did on 2026-07-24.
- Growth still peaks at full Kelly and collapses past ~2× Kelly: 15% → $63,
  20% → ruin. More size stops meaning more money above ~8%.

**METHODOLOGY WARNING (cost us a wrong answer once):** an earlier version of this
table omitted the **decisive-move** condition, admitting every leading-side round
instead of only decisive ones. That diluted the trade set and understated the
edge as +0.8¢/share with Kelly 2.2% — off by 3×, and it produced an
over-conservative 2% recommendation. **Any rule replay must reproduce ALL of the
live gates** (regime, window, decisive move, ask cap), or it measures a different
strategy than the one being run.
- **Never flat-stake real money.** Flat $10 on $100 ruins on this edge — it did,
  live, on 2026-07-24.
- **The way to bet bigger is a bigger edge, not a bigger fraction.** Maker entry
  (buy at the bid) takes edge +1.2¢ → +3.5¢ and full Kelly 3.5% → **9.8%**.
  Caveat: that replay assumes bid fills; ~20% of rounds had no usable bid and
  real maker fills are adversely selected, so the true maker edge is between the
  two. Measuring the real fill rate is the Limitless prototype's job.
- **Confidence-weighted sizing stays BLOCKED** until confidence is calibrated:
  the conf→winrate gap runs +30 to +45 points and flips sign at the top band.
  Kelly needs a true probability; feeding it a miscalibrated one mis-sizes
  exactly where it hurts (see: the div boost ×2 blowup).

---

## OPEN — unresolved, still gathering

- **BTC 5m going LIVE (real money).** Paper edge is validated; live execution
  (real fills, fees, latency, partial fills) is not. The paper trader assumes
  honest fills at recorded asks — live will be worse. See `GOLIVE.md`.
  The +41.5% segment is small-n; the honest expectation is **~+6%/share**,
  ~76% winrate at ~0.70. Do not size for +40%.
- **Divergence boost x2.** Directionally supported (BTC +8.9% vs +2.9%) but the
  2x *multiplier* is unvalidated and amplifies variance. For real money, drop
  it or halve it until several forward segments confirm.
- **SOL quiet rule.** Backtest +4.0% BOTH+, but live marginal-to-negative across
  two segments (−1.0%/sh this one, one-shot-fixed). Verdict pending more
  one-shot segments; keep recording, do not trust yet.
- **XRP threshold+cap.** Crossing table still pooled-positive (+3.1% at 0.70)
  but the live rule barely fires (2 entries/segment) because the 0.85 cap blocks
  the 0.90+ fills where its winrate lives. Dormant, not disproven.
- **Limitless maker venue.** The structural escape from the taker overround +
  label noise (venue-published Chainlink settle, maker rebates flip the fee
  sign). Feed built + tested (`scripts/btc_limitless.py`), not wired to trade.

---

## THE VERDICT THAT MATTERS — full live record (2026-07-25, 1,991 paper trades)

Judge books on the **cumulative** record and **segment consistency**, never on
one segment. Cumulative live P&L, all segments, real fills at real asks:

| book | trades | win% | avg px | EV/share | segments +EV | cum P&L |
|---|---|---|---|---|---|---|
| **BTC 15m** | 338 | 77.8% | 0.755 | **+2.3¢** | **13/15** | **+$76.48** |
| BTC 5m | 633 | 72.2% | 0.730 | −0.8¢ | 11/18 | −$64.88 |
| SOL (quiet) | 486 | 69.3% | 0.715 | −2.2¢ | — | −$110.26 |
| XRP | 534 | 77.7% | 0.780 | −0.3¢ | — | +$7.94 |

**BTC 15m is the franchise.** 13 of 15 segments positive is the only result in
this lab that has ever replicated at that rate. It confirms the original
registry hypothesis (the 15m cohort has higher alpha per trade and is less
latency-crowded) that we had written off. Kelly on its edge = 0.023/(1−0.755)
= **9.4%**, so `STAKE_PCT=0.04` (half-Kelly) fits it too.

**BTC 5m is not the franchise** — despite a +$41.50 segment and a +6.4%/share
segment, it is −0.8¢/share and −$64.88 across 633 trades and coin-flip
consistent. Its good segments are variance, not edge.

**COSTLY MISTAKE TO NEVER REPEAT (2026-07-24):** we recommended dropping BTC 15m
and going live on BTC 5m, based on ONE segment each (BTC5 +$41.50 vs BTC15 −$47).
The cumulative record says the exact opposite. **Never promote or kill a book on
a single segment — always pull the cumulative + per-segment table first.**

---

## The current per-asset call (2026-07-24, SUPERSEDED — see verdict above)

- **BTC 5m** — the pooled edge (+2.4% BOTH+ over 2 weeks) is REAL but TINY, and
  it is high-variance: +$41.50 one segment (2026-07-24 AM), then **$100→$0** the
  next (2026-07-24 PM). Both are within variance for a 2–3¢/share edge. The
  blowup was risk-management, not a session: the ×2 divergence boost amplified a
  losing draw into ruin, and $10–20 stakes on $100 (10–20% of bankroll) make
  ruin reachable in one bad segment. Rule stays lead 180–240s, ask ≤0.90,
  `MAX_PRICE=0.85`, `REGIME=3` — but **boost OFF and stakes ≤2% of bankroll**
  before it trades again. The thin taker edge argues for the maker/Limitless
  path, not bigger taker bets.
- **BTC 15m** — losing live (−$47, 64.7% below breakeven across two segments).
  The registry's "15m has higher alpha" hypothesis is not confirming. **Stop trading.**
- **SOL** — quiet rule live-marginal despite backtest. **Stop trading, keep recording.**
- **XRP** — dormant (2 trades/segment). **Stop trading, keep recording.**

Keeping the *recorders* alive on the dropped markets is the cheap insurance
against ever cold-starting again: they cost nothing, keep accruing official
labels, and let us grade any future idea from data already in hand.
