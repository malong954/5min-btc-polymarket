#!/usr/bin/env bash
#
# One-command start: run the live paper trader in the background (real Polymarket
# per-trade pricing, FLAT fixed stake) and open the red/green dashboard.
#
#   scripts/start.sh          # start trader (if not already up) + open dashboard
#   scripts/start.sh stop     # stop the background trader
#   scripts/start.sh dash     # just open the dashboard (trader already running)
#
# Sizing is FLAT $10 by default and tiering is OFF. On a bet whose edge is
# negative at market price, percent-of-balance / confidence sizing does not add
# profit — it only controls how fast the account reaches zero (a single loss at
# ~$0.97 costs the whole stake, so a 50%-of-balance stake craters the account on
# one loss). Keep it flat and small while measuring. Override at your own risk.
#
# Tunables (env vars):
#   PROVIDER=binance|cryptocompare  THRESHOLD=0.60  BANKROLL=100
#   STAKE=10                 (flat dollars per trade)
#   SIZING=flat|percent|confidence|kelly     (default flat; percent needs STAKE_PCT)
#   STAKE_PCT=0.15           (only used when SIZING=percent)
#   PRICE_SOURCE=polymarket|fixed
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
LOG="out/live.jsonl"

PROVIDER="${PROVIDER:-binance}"
THRESHOLD="${THRESHOLD:-0.60}"
BANKROLL="${BANKROLL:-100}"
SIZING="${SIZING:-flat}"          # flat by default so a loss can't wipe the account
STAKE="${STAKE:-10}"              # flat dollars per trade
STAKE_PCT="${STAKE_PCT:-0.15}"    # only used when SIZING=percent
PRICE_SOURCE="${PRICE_SOURCE:-polymarket}"
BIG_CONF="${BIG_CONF:-0.80}"
BIG_MULT="${BIG_MULT:-1.0}"       # tiering OFF (1.0 = no size-up on high confidence)
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
    --entry-price-source "$PRICE_SOURCE" --sizing "$SIZING" \
    --stake-usd "$STAKE" --stake-pct "$STAKE_PCT" \
    --big-conf "$BIG_CONF" --big-mult "$BIG_MULT" --confluence "$CONFLUENCE" \
    --bankroll "$BANKROLL" --log "$LOG" --quiet \
    >> out/nohup.log 2>&1 &
  if [ "$SIZING" = "flat" ]; then
    echo "started paper trader (pid $!)  provider=$PROVIDER  sizing=flat \$${STAKE}/trade  price=$PRICE_SOURCE"
  else
    echo "started paper trader (pid $!)  provider=$PROVIDER  sizing=$SIZING  price=$PRICE_SOURCE"
    echo "WARNING: non-flat sizing on a negative-edge bet controls only how fast you reach zero."
  fi
  sleep 1
fi

echo "opening dashboard — Ctrl-C exits the dashboard; the trader keeps running."
echo "(stop the trader later with: scripts/start.sh stop)"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$LOG" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD"
