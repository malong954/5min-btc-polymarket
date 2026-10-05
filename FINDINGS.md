# FINDINGS — BTC 5-minute Up/Down momentum bot

**Conclusion: the momentum/indicator strategy, executed as a taker, does not
have a positive expected value on this market. We stopped it deliberately after
the evidence converged.** This is a negative result, and a solid one — it was
proven three independent ways, not guessed.

This file records what we tested, what we found, and why we stopped, so the work
is preserved as knowledge rather than lost. The code that produced these findings
is kept (it is the evidence and the measurement toolkit), not deleted.

---

## What the bot actually does

It reads a multi-timeframe indicator ensemble (1m impulse, RSI, MACD, Bollinger,
ROC, RSI divergence, 5m/15m EMA trend), scores a direction and a confidence
~2 minutes before each 5-minute round closes, and — when confidence clears a
threshold — **buys the leading side at the resting ask (a taker order)** and
holds to settlement.

## The core economics

A Polymarket Up/Down contract bought at price `c` pays `$1` on a win. So:

- **Breakeven win-rate = c.** Buy at $0.90 → you must win >90% just to break even.
- **EV per $1 staked = (winrate − c) / c.** Positive only when winrate > price.
- **Payoff is brutally asymmetric near the top:** at c≈0.97 a win nets ~$0.03
  per share, a loss costs the full ~$0.97. One loss erases ~30 wins.

## The three independent lines of evidence

1. **Real-priced paper account.** Running with the REAL per-trade Polymarket
   ask, flat/tiered stakes, the account went $100 → peak $128 → **$37** (−63%).
   Win-rate 86.7% but average entry price ~0.92–0.97, i.e. **below breakeven**.
   The wins were pennies; two full-stake losses craterd it. (Percent/confidence
   sizing made the ride violent but was not the cause — see below.)

2. **Efficient-market analysis.** A price-sensitivity sweep showed the edge
   evaporates as the entry price rises toward $0.98. The reason is
   **price/accuracy coupling**: a contract is cheap precisely when the round is
   uncertain (~coin flip) and expensive precisely when it is near-decided. The
   market prices the momentum we detect. A flat-$0.90 backtest *broke* that
   coupling and manufactured a fake +6.6% edge; with real per-round prices the
   edge is negative.

3. **Feature ablation.** A 30-day walk-forward, leave-one-out ablation showed
   only **impulse** and **RSI divergence** carried any out-of-sample information;
   the other six indicators each *dragged accuracy down* (removing 5m-trend alone
   lifted OOS accuracy 88.2% → 91.4%). More indicators and bigger timeframes made
   it worse, not better.

## Things we tested that did NOT rescue it

- **Enter earlier / cheaper** (trajectory recorder, `btc_record.py`): buying the
  leading side at each offset before close. Cheaper entries had bigger edge *when
  right* but were right less often — the same coupling. No offset showed a
  durable positive edge.
- **Confidence-based sizing:** sizing up on high confidence only pays if *edge*
  (not just win-rate) rises with confidence. It doesn't, because higher
  confidence is already reflected in a higher price. On a negative-EV bet, sizing
  controls only *how fast* the account reaches zero — it cannot add profit.
- **Sub-minute / faster timeframes** (`vel_5s..vel_60s`): the only place a real
  (latency) edge could hide. But this is an execution-speed race — arb windows
  now close in <30ms and >70% of profits go to sub-100ms bots. A Python REST
  loop on a home connection is structurally on the losing side.
- **Taker fees** (added late, `--fee-rate`): Polymarket now charges takers. Per
  $1 staked the fee is `rate·(1−price)` — tiny at our 0.97 entry (~0.3%), huge at
  0.50 (~5%). For our bot it *widens* the loss but is not the cause; the cause is
  the negative gross edge. (It is, however, why pure arb bots taking 2–4¢ gaps
  at 50/50 go net-negative.)
- **An LLM interpretation layer:** an LLM reading the same candles cannot create
  an edge the market has already priced; it adds latency and non-determinism.
  Confirmed independently by practitioner write-ups.

## Why the "$313 → $414k, 98% win-rate" bots are a different game

Those are **latency-arbitrage** or **market-maker** bots, not directional
predictors. The arb version exploits a sub-second lag between the Binance spot
tick and the Polymarket reprice — an infrastructure race we cannot win from a
Mac. The maker version posts resting limit orders (zero fee + rebate) and earns
the spread, trading our problem for a different hard one (queue priority +
adverse selection + real capital). Neither is the strategy in this repo.

## The toolkit we built (kept for reuse / future research)

- `scripts/btc_backtest.py` — MTF model, walk-forward, ablation, weight
  optimization with a label-shuffle leakage guard.
- `scripts/validate.sh` — one-command real-data validation battery + price sweep.
- `scripts/btc_record.py` + `btc_entry_timing.py` — entry-time / confidence /
  sub-minute-velocity / fee-aware analysis from a real order-book trajectory log.
- `scripts/btc_record_monitor.py`, `btc_live_monitor.py` — live dashboards.
- `scripts/measure.sh`, `start.sh`, `record.sh` — clean measurement runners.

## If anyone revisits this

The only paths with a *structural* edge are **maker/market-making** (build a
paper order-book simulator first — model queue position, fills, adverse
selection, and the fee rebate — before risking any capital) and **true
low-latency arb** (co-located infra, direct WebSocket, sub-100ms). Directional
prediction as a taker — the approach here — is efficiently priced against.

_Status: stopped. Retained as a documented negative result._

---

# CHAPTER 2 — The lead-rule investigation (July 2026)

After the original stop, the project was revived to test one remaining
hypothesis: that the Polymarket order book reprices seconds behind the live
spot, leaving cheap asks buyable by a slow taker ("the lead rule": buy the
leading side mid-round while the ask is still $0.60-0.72). This chapter records
how that edge appeared, survived two artifact filters, and died on the third.

## The apparent edge

Recorder data (thousands of samples across five multi-day segments) showed the
lead rule winning ~73-77% at ~$0.62-0.69 asks — +12 to +33% EV per trade,
positive in every time bin, every segment. A live paper run delivered
$100 -> $211 in ~36 hours at 73.6% winrate, matching the tables out-of-sample.

It survived two validity filters:
1. **Dust quotes** — requiring >=100 shares at the best ask (edge held).
2. **Near-flat rounds** — dropping rounds settling within $10 of open (held).

## The artifact that killed it: self-graded labels

Win/loss labels came from the same spot feed as the signal. When settles were
re-graded against **Polymarket's OFFICIAL resolutions** (fetched per round from
the settled market), the truth emerged:

- **21.2% of rounds disagreed** between our spot settle and the official one —
  including rounds our feed measured as $26-53 moves. A 5s polling interval and
  mirror latency made our close price stale; the same stale feed generated both
  the signal and the scoreboard. Shared measurement noise inflated
  leader-follow-through winrates in every spot-graded table.
- **The truth table** (official labels only, executable size only): the
  180-270s "sweet spot" collapsed from +13..+33% to **+0.7..+3.2%**, and the
  best surviving bin (+5.8%, n=77) is within one standard error of zero —
  before taker fees and before fill slippage.
- The officially-graded live paper run agreed with money: 56-60% winrate
  against a ~65% real breakeven, -$88 on $100.

**Verdict: no-go.** The market is efficiently priced against a slow taker even
at the seconds timescale; the apparent latency edge was mostly our own
measurement latency reflected back at us.

## The method is the takeaway

Four layers of fake edge, four instruments that caught them, zero real dollars
spent:

| apparent edge | instrument that killed it |
|---|---|
| +6.6% at fixed $0.90 pricing | real per-round CLOB pricing |
| confidence/indicator rules | regime testing + OOS ablation |
| cheap-ask entry timing | best-ask SIZE capture (survived), then... |
| ...the whole lead rule | **official-resolution grading** |

The phased go-live process (GOLIVE.md) worked exactly as designed: Phase 0's
exit bar — >=70% winrate at <=$0.68 average cost, **officially graded** — was
the tripwire. It fired one segment before real money would have been staked.

If anyone extends this work: grade every strategy against the venue's own
resolutions from day one, never against the feed that generates your signal.

_Status: concluded. Efficient market, twice-proven — once per grading layer._
