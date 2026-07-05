#!/usr/bin/env bash
#
# One-command validation on REAL data. Downloads real 1m BTC history and runs
# the full battery: walk-forward (is there out-of-sample edge?), ablation (which
# of the indicators actually earn their weight?), sizing comparison, and — if a
# live paper log exists — the confidence->winrate analysis.
#
# Run on a machine with network (your Mac mini):
#   scripts/validate.sh
#
# Tunables (env):
#   PROVIDER=binance|cryptocompare  DAYS=30  ENTRY_PRICE=0.90
#   WF_TRAIN=500  WF_TEST=250  REFRESH=1 (force re-download)
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

PROVIDER="${PROVIDER:-binance}"
DAYS="${DAYS:-30}"
CSV="${CSV:-data/btc_1m.csv}"
ENTRY_PRICE="${ENTRY_PRICE:-0.90}"   # set to your observed avg REAL entry price
WF_TRAIN="${WF_TRAIN:-500}"
WF_TEST="${WF_TEST:-250}"

mkdir -p data

# 1) Get real 1-minute history (cached; REFRESH=1 to force).
if [ "${REFRESH:-0}" = "1" ] || [ ! -f "$CSV" ]; then
  echo "== downloading $DAYS days of 1m data ($PROVIDER) -> $CSV =="
  if [ "$PROVIDER" = "binance" ]; then
    "$PY" scripts/btc_binance.py history --days "$DAYS" --out "$CSV"
  else
    "$PY" - "$DAYS" "$CSV" <<'PYEOF'
import sys, csv, os
from btc_history import history_cryptocompare
days, out = float(sys.argv[1]), sys.argv[2]
rows = history_cryptocompare(days=days)
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["time", "open", "high", "low", "close", "volume"])
    for r in rows:
        w.writerow([r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
print(f"wrote {len(rows)} bars to {out}")
PYEOF
  fi
else
  echo "== using cached $CSV (REFRESH=1 to re-download) =="
fi

echo
echo "##################################################################"
echo "# 1/4  WALK-FORWARD  — is there out-of-sample edge at all?         "
echo "##################################################################"
"$PY" scripts/btc_backtest.py --csv "$CSV" --walk-forward \
  --wf-train "$WF_TRAIN" --wf-test "$WF_TEST" --entry-price "$ENTRY_PRICE"

echo
echo "##################################################################"
echo "# 2/4  ABLATION  — which indicators earn weight, which are dead?   "
echo "##################################################################"
"$PY" scripts/btc_backtest.py --csv "$CSV" --ablation \
  --wf-train "$WF_TRAIN" --wf-test "$WF_TEST" --entry-price "$ENTRY_PRICE"

echo
echo "##################################################################"
echo "# 3/4  SIZING  — does flat / confidence / kelly clear breakeven?   "
echo "##################################################################"
"$PY" scripts/btc_backtest.py --csv "$CSV" --sizing-report \
  --wf-train "$WF_TRAIN" --wf-test "$WF_TEST" --entry-price "$ENTRY_PRICE"

if [ -f out/live.jsonl ]; then
  echo
  echo "##################################################################"
  echo "# 4/4  LIVE PAPER LOG  — real confidence->winrate on your trades   "
  echo "##################################################################"
  "$PY" scripts/btc_analyze.py --log out/live.jsonl
fi

echo
echo "== HOW TO READ THIS =========================================="
echo "walk-forward : is 'oos_edge_vs_breakeven' POSITIVE? That is THE question."
echo "               Positive = a real, out-of-sample edge. Negative = no edge yet."
echo "ablation     : any feature with NEGATIVE ev_contribution is dead weight —"
echo "               drop it from DEFAULT_WEIGHTS in scripts/btc_backtest.py."
echo "sizing       : if the edge is negative, every mode loses (kelly stakes ~0)."
echo "live log     : does the top confidence bucket clear your real breakeven?"
echo
echo "Honest rule: if the walk-forward OOS edge is negative, no indicator, sizing,"
echo "or threshold change fixes it — the signal is not there on real data (yet)."
echo "Set ENTRY_PRICE to your observed avg real Polymarket ask for a true breakeven."
echo "=============================================================="
