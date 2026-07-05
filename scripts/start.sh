#!/usr/bin/env bash
#
# One-command start: run the live paper trader in the background (real Polymarket
# per-trade pricing, stake that scales with the balance) and open the red/green
# dashboard in the foreground.
#
#   scripts/start.sh          # start trader (if not already up) + open dashboard
#   scripts/start.sh stop     # stop the background trader
#   scripts/start.sh dash     # just open the dashboard (trader already running)
#
# Tunables (env vars):
#   PROVIDER=binance|cryptocompare  THRESHOLD=0.60  BANKROLL=100
#   STAKE_PCT=0.15   (fraction of CURRENT balance per trade -> grows as it grows)
#   PRICE_SOURCE=polymarket|fixed
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
LOG="out/live.jsonl"

PROVIDER="${PROVIDER:-binance}"
THRESHOLD="${THRESHOLD:-0.60}"
BANKROLL="${BANKROLL:-100}"
STAKE_PCT="${STAKE_PCT:-0.15}"
PRICE_SOURCE="${PRICE_SOURCE:-polymarket}"
BIG_CONF="${BIG_CONF:-0.80}"
BIG_MULT="${BIG_MULT:-1.5}"   # size up 1.5x on confidence >= BIG_CONF (1.0 = off)
CONFLUENCE="${CONFLUENCE:-0.0}"  # 0..1: require indicator agreement for confidence

if [ ! -x "$PY" ]; then
  echo "No venv found. Create it once:"
  echo "  python3 -m venv .venv && .venv/bin/pip install requests"
  exit 1
fi

case "${1:-start}" in
  stop)
    pkill -f "btc_live_paper.py" 2>/dev/null && echo "stopped the paper trader" || echo "no paper trader running"
    exit 0
    ;;
  dash)
    exec "$PY" scripts/btc_live_monitor.py --log "$LOG" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD"
    ;;
  start) ;;
  *) echo "usage: scripts/start.sh [start|stop|dash]"; exit 1 ;;
esac

mkdir -p out
if pgrep -f "btc_live_paper.py" >/dev/null 2>&1; then
  echo "paper trader already running (pid $(pgrep -f btc_live_paper.py | tr '\n' ' '))"
else
  nohup "$PY" scripts/btc_live_paper.py \
    --provider "$PROVIDER" --poll 2 --entry-threshold "$THRESHOLD" \
    --entry-price-source "$PRICE_SOURCE" --sizing percent --stake-pct "$STAKE_PCT" \
    --big-conf "$BIG_CONF" --big-mult "$BIG_MULT" --confluence "$CONFLUENCE" \
    --bankroll "$BANKROLL" --log "$LOG" --quiet \
    >> out/nohup.log 2>&1 &
  echo "started paper trader (pid $!)  provider=$PROVIDER  sizing=percent ${STAKE_PCT} of balance  price=$PRICE_SOURCE"
  sleep 1
fi

echo "opening dashboard — Ctrl-C exits the dashboard; the trader keeps running."
echo "(stop the trader later with: scripts/start.sh stop)"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$LOG" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD"
