---
name: btc-5m-live
description: Run and monitor BTC 5-minute Up/Down trading on Polymarket using momentum-near-close logic (time-left, BTC move, market skew), fixed/controlled sizing, optional micro-hedge, and one-shot or loop execution.
---

# BTC 5m Live

## Paths
- Main trading repo: `<your-workspace>/pm-hl-conservative-plus-repo` (or set `BTC5M_REPO`)
- Core runner: `src/live/pm_live_trade_runner.py`
- Canonical skill runner: `scripts/test_btc_5m_session_exit_sl.py`
- Skill control entrypoint: `scripts/btc5m_ctl.sh`
- Compatibility wrapper (deprecated): `scripts/run_btc_5m_threshold_test.py`

## Strategy Alignment
Use this skill when the operator wants to execute a BTC 5m momentum strategy:
- Entry focus near event close (around 2 minutes left).
- Confirm meaningful BTC move in the interval (about $70-$100).
- Prefer direction supported by market skew.
- Enter with momentum, not against it.
- Optional small opposite hedge when skew becomes extreme.

## Operational Rules
- Default is dry-run unless `--execute` is set.
- Use controlled stake sizing (`--stake-usd`, profile caps).
- If both UP and DOWN satisfy threshold logic, choose the stronger side.
- Keep stop-loss and timing guards enabled in profile config.

## Position Scaling (add to a winner)
Opt-in per profile (`--scale-enabled 1`; default on for `aggressive`, off for `conservative`). After entry, one extra lot may be added only when ALL guards pass:
- side price has confirmed at least `--scale-trigger-delta` above entry (default 0.06-0.08);
- price is not above `--scale-max-price` (default 0.94-0.95) — adding near 1.00 risks ~$0.90 to win ~$0.10/share;
- at least `--scale-min-seconds-left` remain in the slot;
- total position cost stays within `--max-total-notional-usd`;
- at most `--scale-max-adds` attempts (default 1).
After a matched add, the stop-loss re-anchors to the blended average entry so the larger position keeps the same risk profile.

## Exit Hedge (loss minimize via opposite side)
Enabled by default (`--hedge-exit 1`). At exit time (stop-loss OR time exit) the runner compares two economically equivalent exits:
- sell own side at its best bid, vs
- buy an equal number of opposite-side shares at their best ask, locking $1/share at resolution (UP + DOWN pair).
If `(1 - opposite_ask) >= own_bid + --hedge-min-edge`, it buys the opposite side instead of selling into a thin bid. This is an equal-size hedge that locks the outcome — deliberately NOT an over-hedge "flip", which would be a fresh directional bet rather than loss minimization. Hedged runs report `result: done_hedged` with `pnl_basis: locked_min_at_resolution`; the pair pays out at market resolution, so winnings redemption must be handled as usual.

## One-shot real test
From trading repo root:

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Aggressive profile:

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Override profile params manually (example):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --stake-usd 5 --entry-timeout-min 90 --execute
```

## Strategy Profiles
- File: `config/btc_5m_profiles.yaml`
- Presets: `conservative`, `aggressive`
- Includes entry/exit timing, quote staleness checks, spread/liquidity guards, hedge triggers, and risk caps.

## Hot Commands (chat-friendly)
Examples:
- `btc5m conservative start`
- `btc5m aggressive start`

Handlers:
- `scripts/btc5m_hot.sh [conservative|aggressive]`
- `scripts/btc5m_ctl.sh start --profile [conservative|aggressive]`
- `scripts/btc5m_ctl.sh status|stop|report|logs`
- completion summary utility: `scripts/btc5m_latest_report.py --mark`

Output:
- isolated skill runtime logs: `skills/btc-5m-live/runtime/btc5m_<profile>_<UTCSTAMP>.log`

## Notes
- Canonical runner resolves current BTC 5m market slug (`btc-updown-5m-<bucket>`).
- Real order placement is delegated to `pm_live_trade_runner.py` with `--force-side` and `--max-notional-usd`.
- Keep BTC5m automation scoped to this skill contour (`btc5m_ctl.sh` + `skills/btc-5m-live/runtime`) to avoid cross-skill interference.
- Keep all GitHub-facing docs and metadata in English.
