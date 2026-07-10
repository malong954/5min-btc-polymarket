# Example commands

Dry-run (safe validation):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative
```

Real execution (conservative):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --execute
```

Real execution (aggressive):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile aggressive --execute
```

Conservative with scaling enabled (one $5 add after price confirms +0.08 above entry, total cost capped at $10):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --scale-enabled 1 --scale-stake-usd 5 --max-total-notional-usd 10 --execute
```

Disable the exit hedge (always sell own side at bid, previous behavior):

```bash
.venv/bin/python scripts/test_btc_5m_session_exit_sl.py --profile conservative --hedge-exit 0 --execute
```
