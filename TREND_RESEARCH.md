# BTC Up/Down Parallel Trend Research

## Objective

Find a repeatable BTC 5-minute Polymarket edge across independent trend and
microstructure signals, then measure whether it survives executable prices,
latency, liquidity, fees, and an untouched chronological holdout.

There is no honest way to guarantee `$100/day`. The relevant test is whether a
positive net return survives out of sample and has enough fillable capacity to
make that target plausible without exceeding recorded depth.

## Data Found

- `data/lab/trajectory*.jsonl.gz`: BTC 5m spot, model, velocity, and Polymarket
  top-of-book snapshots sampled about every 6 seconds.
- 3,399 rounds with Polymarket's official `result_pm` outcome.
- Best ask and size are available on most observations; bids are available on
  the newer subset.
- `data/btc_1m.csv`: 43,202 complete Binance 1-minute bars, useful for indicator
  development but largely predating the trajectory capture.
- `Jon-Becker/prediction-market-analysis`: 36 GB compressed Polymarket/Kalshi
  market and trade archive. It is useful for broad calibration and participant
  research, but it has no complete historical second-level order book and its
  default Polymarket trade indexer misses newer exchange contracts.

The official labels are mandatory. Local spot-derived labels disagree with
Polymarket on 282 of 3,399 shared BTC rounds (8.3%), enough to manufacture a
false trend edge.

## Finder Design

`scripts/btc_trend_finder.py` evaluates 15 fixed arms from the same decision
snapshot:

- Spot lead and reversal control.
- 5, 15, 30, and 60-second velocity plus cross-horizon consensus.
- Existing indicator direction.
- Market favorite and underdog control.
- 30-second book momentum and reversal control.
- Bid-demand/ask-scarcity liquidity pressure.
- Spot+book and spot+velocity consensus.

The default simulation uses:

- Decision at the first sample at or below 210 seconds left.
- Fill at the first quote at least 6 seconds later, no more than 15 seconds
  later.
- One cent adverse slippage.
- Current Polymarket crypto taker fee rate `0.07` in
  `fee = shares * rate * price * (1-price)`.
- Entry price from `0.05` through `0.85`.
- At least 5 shares available; fill capped to recorded best-ask size.
- `$10` maximum stake per trade.
- Whole-day 60/20/20 chronological train, validation, and holdout partitions.
- A candidate must be positive in every partition, and holdout significance is
  Bonferroni-adjusted for all tested arms.

## Baseline Result

Command:

```bash
python3 scripts/btc_trend_finder.py \
  --output out/btc_trend_report.json \
  --trade-log out/btc_trend_trades.csv
```

Result on the July 2026 archive:

- No arm was net-positive in both train and validation under default costs.
- Spot lead holdout: 286 trades, `-1.86%` net edge/share, about `-$15/day` at the
  simulated stake.
- Spot+book consensus holdout: 278 trades, `-1.82%` net edge/share.
- Every other arm was also unstable or negative.
- Decisions at 240 and 180 seconds left did not rescue the result.

Cost decomposition for spot+book consensus at the 210-second decision:

| Assumption | Train | Validation | Holdout | Verdict |
|---|---:|---:|---:|---|
| No fee, no slippage | +1.98% | +1.94% | +0.88% | Small gross signal, not statistically proven |
| Current taker fee, no slippage | +0.61% | +0.55% | -0.48% | Failed holdout |
| Current taker fee, 1-cent slippage | -0.86% | -0.72% | -1.82% | Not tradeable as a slow taker |

The data therefore contains weak predictive information, but not enough to pay
for taker execution. Scaling a negative net edge only increases expected loss;
it cannot produce a reliable `$100/day`.

## Remaining Credible Angle

Maker execution could avoid the taker fee, earn spread/rebates, and turn the
small gross signal into a net edge. Historical trajectory data cannot validate
that claim because it does not contain order IDs, queue position, partial fills,
or adverse-selection outcomes for resting orders.

The next gate is a forward-only maker shadow recorder that logs every submitted
quote, queue estimate, exchange acknowledgement, fill, cancellation, and the
post-fill markout. Do not connect real money until that recorder shows positive
net PnL across multiple forward weeks and an untouched holdout after missed
fills and adverse selection.
