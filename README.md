# 5min BTC Polymarket Skill

Open-source OpenClaw skill for **BTC 5-minute Up/Down** markets on Polymarket.

Repository: https://github.com/Novals83/5min-btc-polymarket

## Strategy (Momentum into Close)
This skill is aligned with a short-horizon momentum strategy:

1. Trade BTC 5m event markets near expiry.
2. Main entry window: around **2 minutes left**.
3. Confirm that BTC has already moved by about **$70-$100** in the active interval.
4. Check market skew (crowd positioning). If flow supports the move direction, enter **with** momentum.
5. Typical sizing: around **50% of trading allocation** (user-defined risk tolerance).
6. Optional micro-hedge when skew is extreme (for example, 95/5): place a small opposite position ($1-$2 equivalent) to reduce tail risk.

This is a momentum-following approach, not a reversal strategy.

## Repository Structure
- `SKILL.md` — skill definition and operating rules
- `config/` — profiles and risk parameters
- `scripts/` — runners/wrappers/hot commands
- `examples/` — practical command examples

## Deploy / Run
### Prerequisites
- OpenClaw environment
- Polymarket execution stack available at:
  - `<your-workspace>/pm-hl-conservative-plus-repo`
- Python virtual env for runner scripts
- Valid API credentials configured outside this repository

### Quick Start
```bash
git clone https://github.com/Novals83/5min-btc-polymarket.git
cd 5min-btc-polymarket
```

Read:
- `SKILL.md`
- `config/btc_5m_profiles.yaml`

Run a conservative real test (example):
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Run aggressive profile:
```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Unified skill control (recommended):
```bash
scripts/btc5m_ctl.sh start --profile conservative
scripts/btc5m_ctl.sh status
scripts/btc5m_ctl.sh report --limit 20
scripts/btc5m_ctl.sh stop
```

Runtime isolation:
- skill runtime dir: `./runtime`
- auth/env source (default): `<your-workspace>/pm-hl-conservative-plus-repo/.env`
- overrides: `BTC5M_REPO`, `BTC5M_ENV_FILE`, `BTC5M_RUNNER`
- completion auto-report cron (topic 184): `btc5m-completion-autoreport-topic184`

Optional Docker isolation:
```bash
scripts/btc5m_docker.sh up
scripts/btc5m_docker.sh status
scripts/btc5m_docker.sh down
```

## Real-Time BTC Impulse Gate
The runner no longer trusts the Polymarket contract price alone. Before entry it
confirms the **actual BTC move** in the current 5m round using two independent
price feeds (`scripts/btc_price_feeds.py`, default Binance + Coinbase):

- Measures `spot - round_open` on each feed and enforces the documented
  ~$70-$100 impulse (`--btc-move-min-usd`, `--btc-move-max-usd`).
- Cross-checks the feeds and skips when they disagree beyond
  `--btc-feed-divergence-usd` (one feed lagging / bad tick).
- Vetoes entries where the contract book points opposite to real BTC direction.

Flags: `--btc-impulse` / `--no-btc-impulse`, `--btc-feeds binance,coinbase`,
`--btc-move-min-usd`, `--btc-move-max-usd`, `--btc-feed-divergence-usd`,
`--btc-min-feeds`. Feeds run on your machine; no API keys required.

## Backtesting (Multi-Timeframe Indicators)
`scripts/btc_backtest.py` backtests a 1m/5m/15m indicator ensemble
(current-round impulse, RSI, MACD, EMA trend, relative volume) against the
historical 5m settle. It freezes the clock ~2 min before close (no lookahead),
predicts up/down, and reports directional accuracy, coverage, per-feature
correlation, and simulated PnL at a configurable contract entry price.

Each 5m market is a **Yes/No pair** (an "Up" token and a "Down" token). A
prediction of `UP` means buy the Up/Yes contract; `DOWN` means buy the Down
contract (equivalently "No" on Up). The backtest picks that side from the trend
and prices the fill at `--entry-price`, settling $1 on a correct call.

```bash
# Offline demo (synthetic data, no network):
python3 scripts/btc_backtest.py --synth 6000

# Real data on your PC:
python3 scripts/btc_backtest.py --fetch-binance --days 7 --entry-price 0.85
python3 scripts/btc_backtest.py --csv data/btc_1m.csv --entry-threshold 0.80 --json

# Per-round CSV of every decision (taken/skipped, win/loss, all features):
python3 scripts/btc_backtest.py --csv data/btc_1m.csv --trade-log out/trades.csv

# Walk-forward: tune the threshold on rolling train blocks, score the NEXT
# (unseen) block. The reported edge is out-of-sample, not curve-fit:
python3 scripts/btc_backtest.py --csv data/btc_1m.csv --walk-forward \
    --wf-train 500 --wf-test 250 --trade-log out/oos_trades.csv

# Ablation: re-run walk-forward dropping one indicator at a time to see which
# of impulse / RSI / MACD / 5m-trend / 15m-trend actually earns its weight:
python3 scripts/btc_backtest.py --csv data/btc_1m.csv --ablation \
    --wf-train 500 --wf-test 250

# Weight optimization: head-to-head OOS comparison of fixed default weights vs
# per-fold tuned weights (coordinate ascent on each train block, scored on the
# next unseen block). Reports a verdict on whether tuning actually helps:
python3 scripts/btc_backtest.py --csv data/btc_1m.csv --optimize-weights \
    --wf-train 500 --wf-test 250
```

The ablation ranks features by **out-of-sample EV contribution** (baseline EV
minus EV-with-feature-removed). A positive number means dropping the feature
lowers EV, so it's pulling its weight; a negative number flags dead weight you
can prune from `DEFAULT_WEIGHTS` in `scripts/btc_backtest.py`. Re-run on your own
data — the ranking is data-dependent, not universal.

Weight optimization tunes only on each in-sample block and is scored purely on
the following unseen block, so its verdict is honest: if tuning does not beat the
fixed baseline out-of-sample, the message says so (fixed weights are good enough;
tuning would overfit). The whole pipeline carries a **leakage guard** in the test
suite — when outcome labels are shuffled to break the feature→outcome link, the
optimizer's out-of-sample edge collapses to negative, proving it cannot
manufacture edge from noise. Trust the OOS numbers, not in-sample ones.

**Key lesson the backtest makes explicit:** buying contracts at $0.80-$0.99 means
your breakeven win-rate is 80-99%. High directional accuracy alone loses money;
positive EV only appears when you raise `--entry-threshold` to trade only the
highest-confidence rounds. **Always confirm with `--walk-forward`** — an
in-sample threshold that looks great is easy to overfit; the walk-forward number
is the honest one. Validate that the out-of-sample edge clears breakeven on real
data before going live.

Tests (stdlib only, no network):
```bash
python3 scripts/test_btc_impulse_feeds.py
python3 scripts/test_btc_backtest.py
```

## Execution Checklist (Before Live Trade)
Use this quick pre-flight checklist before any real order:

1. **Market validity**
   - Confirm the BTC 5m market is active and not about to close unexpectedly.
2. **Time-to-close window**
   - Prefer entries around ~120 seconds left (with reasonable tolerance).
3. **Impulse confirmation**
   - Confirm the observed BTC move is meaningful (strategy reference: ~$70-$100).
4. **Skew confirmation**
   - Verify market skew supports the intended direction (do not fade strong momentum by default).
5. **Liquidity/spread checks**
   - Ensure spread and top-of-book notional pass your minimum thresholds.
6. **Sizing guardrails**
   - Validate stake, max notional, and daily loss limits before execution.
7. **Stop / exit controls**
   - Confirm stop-loss and `exit_before_sec` are configured.
8. **Execution mode**
   - Start in dry-run when changing parameters; switch to `--execute` only after validation.

## Risk Controls Template
Suggested baseline controls (adapt to your risk profile):

- **Per-trade risk cap**: 1%-15% of account equity (profile dependent)
- **Daily max loss**: hard stop at 10%-15%
- **Max trades/day**: fixed ceiling to avoid overtrading
- **Max notional/trade**: strict upper bound
- **Quote staleness guard**: skip if market data is stale
- **Spread guard**: skip when spread exceeds threshold
- **Liquidity guard**: skip when top ask/bid notional is too thin
- **Extreme skew hedge**: optional small opposite hedge in 95/5-type scenarios
- **Operational kill switch**: immediate stop on repeated API/DNS/execution failures

## Risk Notice
This repository is educational/operational infrastructure, not financial advice.
Use your own risk limits, daily loss caps, and capital controls.

## Contributing
- Fork the repository
- Create a feature branch
- Commit changes
- Open a PR to `main`

PRs are welcome.
