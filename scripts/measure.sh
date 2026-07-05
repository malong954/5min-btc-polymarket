#!/usr/bin/env bash
#
# Gather a CLEAN real-priced sample to settle one question: is this market
# efficiently priced, or is there exploitable mispricing (a lagging/thin book)?
#
# Uses REAL per-trade Polymarket pricing, a FLAT stake (so every trade weighs
# equally in the EV), NO confidence tiering, NO percent compounding, and a
# moderate threshold. Logs to a dedicated file so nothing contaminates it.
#
#   scripts/measure.sh            # start the measurement run (background)
#   scripts/measure.sh status     # is it running? how many trades so far?
#   scripts/measure.sh analyze    # read the real confidence/price -> winrate
#   scripts/measure.sh dash       # live color dashboard for this run
#   scripts/measure.sh stop       # stop it
#
# Tunables (env): THRESHOLD=0.60  STAKE=10  PROVIDER=binance|cryptocompare
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
LOG="out/measure.jsonl"
THRESHOLD="${THRESHOLD:-0.60}"
STAKE="${STAKE:-10}"
PROVIDER="${PROVIDER:-binance}"

[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

settle_count() { [ -f "$LOG" ] && grep -c '"type":"settle"' "$LOG" 2>/dev/null || echo 0; }

case "${1:-start}" in
  stop)
    pkill -f "btc_live_paper.py" 2>/dev/null && echo "stopped the measurement run" || echo "nothing running"
    exit 0 ;;
  status)
    if pgrep -f "btc_live_paper.py" >/dev/null 2>&1; then
      echo "RUNNING (pid $(pgrep -f btc_live_paper.py | tr '\n' ' '))"
    else
      echo "NOT running"
    fi
    echo "settled trades so far: $(settle_count)   (need ~200-300 to judge)"
    exit 0 ;;
  analyze)
    exec "$PY" scripts/btc_analyze.py --log "$LOG" ;;
  dash)
    exec "$PY" scripts/btc_live_monitor.py --log "$LOG" --bankroll 100 --entry-threshold "$THRESHOLD" ;;
  start) ;;
  *) echo "usage: scripts/measure.sh [start|status|analyze|dash|stop]"; exit 1 ;;
esac

mkdir -p out
if pgrep -f "btc_live_paper.py" >/dev/null 2>&1; then
  echo "already running (pid $(pgrep -f btc_live_paper.py | tr '\n' ' ')). stop it first: scripts/measure.sh stop"
  exit 0
fi
# Archive any prior measurement log so this sample stays clean.
[ -f "$LOG" ] && mv "$LOG" "out/measure-prev.jsonl" && echo "archived prior log -> out/measure-prev.jsonl"

nohup "$PY" scripts/btc_live_paper.py \
  --provider "$PROVIDER" --poll 2 --entry-threshold "$THRESHOLD" \
  --entry-price-source polymarket --sizing flat --stake-usd "$STAKE" \
  --big-mult 1.0 --confluence 0.0 --bankroll 100 \
  --log "$LOG" --quiet \
  >> out/measure-nohup.log 2>&1 &

echo "started CLEAN measurement run (pid $!)"
echo "  real pricing, flat \$${STAKE} stake, threshold ${THRESHOLD}, no tiering"
echo "  log: $LOG"
echo
echo "let it run 1-2 days, then:"
echo "  scripts/measure.sh status     # trade count"
echo "  scripts/measure.sh analyze    # the verdict (real-priced EV by price/confidence)"
echo "  scripts/measure.sh dash       # watch it live"
echo
echo "NOTE: nohup does not survive a reboot; re-run this if the Mac restarts."
