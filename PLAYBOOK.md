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
| ~~Session gating~~ | **RETRACTED 2026-07-25 — this entry was WRONG.** It was written off single-segment numbers (n=13). The full live record (972 trades) shows session is a REAL, replicated effect — see the SESSION GATE section below. Lesson: never kill a hypothesis on one segment either |

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
- **VERDICT ON THE 25% TIER (2026-07-31, third round trip):** BTC15's run made
  money per trade (+2.2¢/share; the SAME trades at flat $10 = **+$22.82**) yet
  lost **−$13.56 in cash** with a $421→$86 peak-to-trough. Trade-weighted EV
  positive + dollar-weighted return negative = the tier bets biggest exactly
  when a streak is about to mean-revert. Three cycles now: $100→$202→$72,
  $400→$470→$326, $100→$421→$86. The signal is not the problem; the top tier
  is ~2× the strong book's Kelly and converts winning trade streams into
  losing dollar streams. **Fix: `TIERS=0.10,0.05,0.05`** (10% top tier ≈
  Kelly-neutral for BTC15, still auto-de-escalating).
- **SIZING=tiered (2026-07-27, Felipe's scheme):** 25% of balance at/above the
  starting bankroll, 10% at 50–100%, 5% below 50%. Replayed on the go-forward
  rules: BTC15 ex-US+barbell (edge +9.6¢, Kelly 48%) → **$2,908** at 44% DD —
  under Kelly there, fine. BTC5 ex-US+cooldown (edge +4.1¢, Kelly 15%) → $335
  but with a **96% drawdown** (balance touched ~$4). Acceptable for paper by
  the owner's explicit choice; NOT a real-money scheme on the 5m book.
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
- **Limitless BTC15 edge transfer (2026-08-03, gathering).** First recorder
  sync landed: 3 rounds only (recorder started 00:25Z) — need ~2-3 days
  (~200 resolved rounds) before grading the BTC15 rule on Limitless books.
  First book-quality read (n=308 samples) is GOOD: decision-window (45-180s
  left, x3 for 15m) vig median 2.8c / max 8.4c — Polymarket-comparable — and
  min-side depth >=15 shares in ~87% of samples. The scary spreads (p90 up to
  70c) all live >280s before close, which the rule never touches.
- **Limitless executor built, in dry-run (2026-08-03).** `scripts/
  btc_limitless_exec.py`: paper engine decides (same BTC15 europe cell),
  ExecGuard bounds it ($5/trade cap, $15 UTC-day kill, $10 open-exposure cap,
  one order/round, thin-book + slip refusals), orders go FAK via the official
  SDK (EIP-712 — never hand-rolled). Fee 0.07*p*(1-p)/share charged on every
  logged fill. DRY-RUN unless --live + LIMITLESS_LIVE=1 + wallet/HMAC creds
  (fail-closed, tested). lab.sh auto-starts recorder + dry-run executor
  (LIMITLESS=btc default; lab.sh never arms live). SDK size gotcha, verified
  in its builder: `size` is SHARES, not USDC, despite the docstring.
  Before arming: rotate the burned API token, fund $100 USDC + ETH gas on
  Base, confirm the 0.07 fee rate on the first real ticket.

---

## THE WINNERS/LOSERS TREND — the barbell + loss-clustering (2026-07-27)

Deep dive over every ENTERED trade (BTC5 n=684, BTC15 n=413), all features:

**1. The mid-price dead zone (the trend).** Entered trades win at the price
EXTREMES and lose in the middle, on BOTH books:

| entry price | BTC5 EV/share | BTC15 EV/share |
|---|---|---|
| < 0.60 (we disagree with the book) | **+10.7¢** BOTH+ | **+25.2¢** BOTH+ |
| 0.60–0.80 (the dead zone) | −1 to −3¢ BOTH− | **−5.6¢** BOTH− |
| 0.80–0.85 (book confirms) | +1.6¢ | **+7.0¢** BOTH+ |

Mechanism: a cheap ask = the book disagrees with us — in asia/europe the book
is slow and we are right. 0.80+ = the book confirms. The 0.60–0.80 middle is
the maximum-uncertainty zone where the ask already prices exactly what our
signal knows. Ex-US replays: BTC15 +3.9→+9.6¢/share with the band skipped
(`SKIP_BAND_BTC=0.60:0.80` on the 15m book, skip reason `mid_band`).
Stable BOTH+ on BTC15; on BTC5 h1 was negative — barbell is BTC15-only for now.

**2. After-loss cooldown (BTC5).** The next entered trade after a loser ran
**−4.8¢/share BOTH−** (n=188); dropping it lifts BTC5 ex-US from +1.1¢ to
**+4.1¢/share BOTH+** (4% replay $125→$229). Losses cluster: a loss marks a
regime the rule is mis-reading. `COOLDOWN_BTC=1`, skip reason `cooldown`.
NOTE: BTC15 shows the OPPOSITE (after-loss +8.1¢ BOTH+) — cooldown is
book-specific, do not apply it to 15m.

Also confirmed in the same study: slippage-paying trades lose (−5.9¢ BOTH−),
thick-ask-wall trades lose on BTC5 (−4.6¢ BOTH−), UTC 20-24 is the worst block
on both books (session gate already covers it). Multiple-comparisons caveat:
~30 cells were tested; these survived split-half + mechanism + (for the
barbell) cross-book replication, but they are FORWARD EXPERIMENTS with kill
rules, not settled fact. Kill rule: one full segment of the feature's skip
reason shadow-grading POSITIVE at its refused asks → revert.

---

## THE FEE RECKONING — Felipe's independent research (2026-08-01)

`TREND_RESEARCH.md` + `scripts/btc_trend_finder.py` (Felipe's parallel study,
train/validation/holdout + Bonferroni — STRICTER than our split-half) reached
our conclusions independently AND surfaced a cost our lab never charged:
**Polymarket's crypto taker fee, `0.07 × p × (1−p)` ≈ 1.3–1.5¢/share.**
The paper engine's EV numbers are GROSS. Regraded net (his cost model):

| cell | gross | −fee | −fee −1¢ slip |
|---|---|---|---|
| **BTC15 Europe** | +3.3¢ | **+2.0¢** | **+1.0¢** |
| BTC15 ex-US | +2.1¢ | +0.8¢ | −0.2¢ |
| BTC15 all | +1.5¢ | +0.2¢ | −0.8¢ |
| BTC5 (every cell) | ≤+0.2¢ | negative | negative |

**Only BTC15-Europe survives the full cost stack** (and our measured live
slippage is ~0.1¢, not 1¢, so realistic net ≈ +1.9¢). His 15 single-signal
arms all died under costs — matching our ungated baselines — and his
conclusion is ours, reached independently: **the taker path is nearly
exhausted; the maker side is the destination**, gated on a forward maker
shadow recorder (quotes, queue, fills, markouts) before any real money.
TODO: confirm the 0.07 rate from the live site, then charge the fee in the
paper engine so dashboards report net.

---

## THE ASK-FALL VETO — the loser signature (2026-07-29)

Autopsy of all 154 ex-US BTC5 losers, joined with book context at entry:

| entry-side ask over prior ~60s | n | win% | EV/share |
|---|---|---|---|
| **FELL ≥2¢** | 98 | 55.1% | **−14.3¢** `BOTH−` |
| ~flat | 33 | 78.8% | +6.5¢ `BOTH+` |
| ROSE ≥2¢ (chasing) | 313 | 78.0% | +3.0¢ `BOTH+` |

**Chasing a rising ask is fine. Buying the leading side while its ask is
FALLING is the deepest loss cell ever isolated** — the book repricing against
our side while spot still shows it leading is informed flow calling the
reversal early (the book leads spot; we have measured that repeatedly).
Removing the cell lifts the remaining BTC5 book to ~+3¢/share.
Implemented: `ASK_FALL_BTC=0.02` (trader `--ask-fall-veto`), skip reason
`ask_falling`, shadow-graded. Kill rule: a full segment of ask_falling skips
grading POSITIVE at the refused asks → revert.

Secondary loser signatures (same autopsy, weaker): BTC5 entries at RSI ≤35 ran
−7.0¢ `BOTH−` (late to a falling knife) while BTC15's RSI extremes are
POSITIVE — any RSI filter must be 5m-only and is NOT yet implemented.
Spread, stretch/exhaustion: nothing stable. BTC15 had no `BOTH−` veto cell.

---

## SESSION GATE — validated, implemented (2026-07-25)

Live entered trades, all segments, split by UTC session:

| book | Asia 00-08 | Europe 08-16 | US 16-24 |
|---|---|---|---|
| BTC 5m | +0.6c, **+$56.00** | +0.7c, -$15.22 `BOTH+` | **-3.7c, -$95.08** |
| BTC 15m | +2.6c, +$11.29 | **+4.9c, +$86.10** `BOTH+` | -0.6c, -$24.92 |

BTC 5m is -$64.88 lifetime, but **US alone is -$95.08** — excluding US it is
**+$40.78**. Europe is positive in BOTH halves of history on BOTH books
independently: the strongest evidence standard this lab has.

**Mechanism (why it is not curve-fitting):** split by price, a CHEAP leading
side means opposite things by session —

| | cheap <0.70 | mid 0.70-0.80 | expensive 0.80+ |
|---|---|---|---|
| Asia | **71.1% -> +$118** | 67.6% -> -$41 | 81.6% -> -$21 |
| Europe | 62.4% -> -$1 | 71.6% -> -$46 | 90.3% -> +$32 |
| US | **51.5% -> -$95** | 70.3% -> -$15 | 91.2% -> +$16 |

A cheap leading side means the book disagrees with us. In slow asian hours the
book is wrong and we are right (71%). In US hours the book is **informed**
(macro releases, institutional flow) — when it prices our side cheap it is right
and we are the sucker (51.5%, a coin flip). Same signal, opposite meaning,
depending on who is on the other side.

A tighter price cap does NOT fix US (it makes it worse: -6.1c on BTC5, -9.8c on
BTC15) because in US hours the CHEAP trades are precisely the losers.

Run it with `SESSIONS="asia europe"`. Blocked rounds skip as `off_session` and
are still shadow-graded, so the gate's cost stays measurable.

---

## "WE'RE SKIPPING WINNERS" — settled, stop re-asking (2026-07-25)

Every skip reason, graded at **the ask the round was actually refused at**:

| book | skip reason | n | win% | avg ask | EV/share |
|---|---|---|---|---|---|
| BTC15 | no_lead_setup | 267 | 85.4% | 0.910 | −5.6¢ |
| BTC15 | flat_regime | 73 | 76.7% | 0.916 | **−14.9¢** |
| BTC15 | price_capped | 21 | 85.7% | 0.962 | −10.5¢ |
| **BTC15** | **ENTERED** | **343** | **77.6%** | **0.754** | **+2.1¢** |
| BTC5 | every skip reason | 2,617 | 73–92% | 0.85–0.95 | all negative |

**Not one gate refuses a profitable round.** Skipped rounds win MORE often
(85% vs 78%) and still lose money, because they are only available at 0.91–0.96
where breakeven is 91–96%. The gates' entire job is converting "wins often at a
terrible price" into "wins slightly less often at 0.75". The timeline report's
"skipped rounds won as much or MORE — threshold may be too strict" line compares
WINRATES and is therefore misleading: always re-grade skips against their asks
before believing a gate is costing money.

Corollary: **a long dry spell costs nothing.** BTC 5m sat at exactly $100 for
three consecutive runs (2026-07-24/25) and the shadow book confirms there was no
+EV round to take in any of them.

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
