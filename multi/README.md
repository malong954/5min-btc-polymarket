# Multi-Timeframe Parallel Testing Bot

A **separate** bot that tests the same momentum-into-close strategy across all
Polymarket Up/Down timeframes **in parallel** — 5m, 15m, 1h, 4h, 1d — with the
option to add assets beyond BTC (ETH, SOL, XRP).

It does **not** replace the og 5m bot. It lives in its own contour
(`multi/runtime`, `multibot_ctl.sh`, `btcmulti-*` naming) and can run alongside
it. Strategy parameters are seeded from the data gathered by the og 5m bot
(threshold 0.70 on CLOB best ask, $5 stake, 25% stop-loss, pre-close time
exit); **only the timing scales per timeframe**.

## Why

- Longer markets are the same strategy with different timing — so test them
  all at once and let the data pick the winners (15m is the closest, most
  likely next step).
- Multiple positions can be open **simultaneously** (e.g. a 15m and a 1h
  position at the same time). A shared portfolio guard caps total risk.

## Architecture

```
multi/
  config/multi_profiles.yaml   # assets, per-timeframe params, portfolio caps
  scripts/
    multibot_ctl.sh            # start|stop|status|report|logs|probe
    orchestrator.py            # spawns + supervises one worker per (asset, tf)
    multi_worker.py            # one (asset, timeframe) session loop
    market_discovery.py        # slug templates -> Gamma API resolution, CLOB books
    multi_report.py            # aggregate PnL/win-rate comparison per timeframe
    probe_markets.py           # verify which timeframes resolve in your env
  runtime/                     # isolated: pids, worker logs, per-slot reports
```

- **Worker** = generalized port of the proven og 5m runner
  (`scripts/test_btc_5m_session_exit_sl.py`): wait for the entry window,
  threshold-trigger on CLOB best ask, pick the stronger side, stop-loss +
  forced exit before close, FAK close with GTC/force fallback ladder. Unlike
  the og one-shot runner, a worker loops over consecutive slots continuously.
- **Portfolio guard**: all workers share one fcntl-locked state file enforcing
  `max_concurrent_positions`, `max_total_exposure_usd`, and a shared
  `daily_max_loss_usd` across every open position. A worker that gets denied
  keeps polling — capacity frees up when another worker closes.
- **Two modes**:
  - `paper` (default): simulated fills at live CLOB best ask/bid. No auth, no
    external repo needed. This is how you test all timeframes cheaply.
  - `live`: real orders through the same external
    `pm-hl-conservative-plus-repo/src/live/pm_live_trade_runner.py` engine the
    og bot uses.

## Market discovery

Polymarket slug formats differ per timeframe and have changed over time:

| timeframe | primary pattern | fallbacks |
|---|---|---|
| 5m | `btc-updown-5m-<unix slot start>` | — |
| 15m | `btc-updown-15m-<unix slot start>` | — |
| 1h | `btc-updown-1h-<unix>` | `bitcoin-up-or-down-<month>-<day>-<h><am/pm>-et` (± year) |
| 4h | `btc-updown-4h-<unix>` | — |
| 1d | `btc-updown-1d-<ET midnight epoch>` | `bitcoin-up-or-down-on-<month>-<day>` |

Discovery tries each candidate template in order and validates against the
Gamma API. **Before enabling a timeframe, run the probe** in your trading
environment:

```bash
multi/scripts/multibot_ctl.sh probe
```

Any pair reported unresolvable: open that market on polymarket.com, copy the
slug pattern from the URL, and add it under `timeframes.<tf>.slug_templates`
in the config. No code change needed.

## Quick start

```bash
# 0. One-time setup: dedicated venv, no external repo needed for paper mode
python3 -m venv multi/.venv
multi/.venv/bin/pip install -r multi/requirements.txt
# (auto-detected by multibot_ctl.sh; override any time with BTCMULTI_PYTHON)

# 1. Verify markets resolve in your environment
multi/scripts/multibot_ctl.sh probe

# 2. Start ALL enabled timeframes in paper mode (default, safe)
multi/scripts/multibot_ctl.sh start

# 3. Watch it
multi/scripts/multibot_ctl.sh status
multi/scripts/multibot_ctl.sh logs btc_15m

# 4. Compare timeframes head-to-head
multi/scripts/multibot_ctl.sh report

# 5. Stop everything (workers close open positions gracefully first)
multi/scripts/multibot_ctl.sh stop
```

Going live later (after paper results look good):

```bash
multi/scripts/multibot_ctl.sh start --mode live
```

Run a single timeframe by hand:

```bash
python multi/scripts/multi_worker.py --asset btc --timeframe 15m --mode paper --max-slots 10
```

## Per-timeframe defaults (seeded from og 5m data)

| tf | entry window (sec left) | min entry | exit before | poll |
|---|---|---|---|---|
| 5m | 120 | 60 | 20s | 3s |
| 15m | 300 | 120 | 30s | 5s |
| 1h | 900 | 300 | 60s | 15s |
| 4h | 2700 | 900 | 120s | 30s |
| 1d | 10800 | 3600 | 300s | 60s |

Threshold 0.70 / stake $5 / SL 25% everywhere as the starting point — tune per
timeframe in `config/multi_profiles.yaml` as data comes in.

## Running alongside the og 5m bot

- Runtime, pidfiles, logs, and reports are fully isolated from
  `skills/btc-5m-live/runtime` — the two bots never touch each other's state.
- **When the og bot trades 5m live, keep the multi 5m worker in paper mode**
  (or set `timeframes.5m.enabled: false`) so you never hold duplicate
  exposure on the same market. The paper 5m worker doubles as a baseline
  check: its simulated results should track the og bot's real ones.
- The portfolio guard only covers multi-bot workers; og bot exposure is
  budgeted separately.

## Adding assets

Uncomment in `config/multi_profiles.yaml`:

```yaml
assets:
  - btc
  - eth
```

Then re-run `probe` — asset name mapping (btc→bitcoin, eth→ethereum, …) is
handled by the slug templates.

## Requirements

- Paper mode: Python 3.10+ with `multi/requirements.txt` installed
  (`requests`, `pyyaml`) — see step 0 above. Fully standalone, no external
  repo needed.
- Live mode: same as og bot — `pm-hl-conservative-plus-repo` with its `.venv`
  and `.env` auth (override paths: `BTCMULTI_REPO`, `BTCMULTI_ENV_FILE`,
  `BTCMULTI_PYTHON`).
- Interpreter resolution order: `BTCMULTI_PYTHON` (if set) → `multi/.venv` →
  trading repo `.venv` → bare `python3`. `multibot_ctl.sh` checks for
  `requests`/`pyyaml` up front and tells you exactly what to run if missing.

## Risk notice

Educational/operational infrastructure, not financial advice. Start in paper
mode, go live on one timeframe (15m) with minimal stake, and only then widen.
The shared caps (`portfolio:`) are the ceiling for **simultaneous** positions —
size them to what you can lose across all open markets at once.
