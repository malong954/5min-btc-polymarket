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
