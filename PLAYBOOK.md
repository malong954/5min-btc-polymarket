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

## The current per-asset call (2026-07-24)

- **BTC 5m** — the one validated, mechanism-understood, positive-EV book.
  Rule: lead 180–240s, ask ≤0.90, `MAX_PRICE=0.85`, `REGIME=3`, divergence boost.
  **Trade this. Candidate for going live.**
- **BTC 15m** — losing live (−$47, 64.7% below breakeven across two segments).
  The registry's "15m has higher alpha" hypothesis is not confirming. **Stop trading.**
- **SOL** — quiet rule live-marginal despite backtest. **Stop trading, keep recording.**
- **XRP** — dormant (2 trades/segment). **Stop trading, keep recording.**

Keeping the *recorders* alive on the dropped markets is the cheap insurance
against ever cold-starting again: they cost nothing, keep accruing official
labels, and let us grade any future idea from data already in hand.
