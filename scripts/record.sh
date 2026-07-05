#!/usr/bin/env bash
#
# Record the Polymarket price + BTC move trajectory through each 5m round (an
# OBSERVER — it places no trades), so we can find the optimal entry TIME:
# earlier = cheaper but less certain, later = surer but pricier.
#
#   scripts/record.sh            # start recording (background)
#   scripts/record.sh status     # rounds recorded so far
#   scripts/record.sh analyze    # the entry-time table (winrate/price/edge per offset)
#   scripts/record.sh stop
#
# Tunables (env): PROVIDER=binance|cryptocompare
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=".venv/bin/python"
LOG="out/trajectory.jsonl"
PROVIDER="${PROVIDER:-binance}"
[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

case "${1:-start}" in
  stop)
    pkill -f "btc_record.py" 2>/dev/null && echo "stopped the recorder" || echo "nothing running"
    exit 0 ;;
  status)
    if pgrep -f "btc_record.py" >/dev/null 2>&1; then
      echo "RUNNING (pid $(pgrep -f btc_record.py | tr '\n' ' '))"
    else
      echo "NOT running"
    fi
    echo "rounds recorded: $([ -f "$LOG" ] && grep -c '"type":"result"' "$LOG" 2>/dev/null || echo 0)   (need ~100+ to judge)"
    exit 0 ;;
  analyze)
    exec "$PY" scripts/btc_entry_timing.py --log "$LOG" ;;
  start) ;;
  *) echo "usage: scripts/record.sh [start|status|analyze|stop]"; exit 1 ;;
esac

mkdir -p out
if pgrep -f "btc_record.py" >/dev/null 2>&1; then
  echo "already running. stop first: scripts/record.sh stop"; exit 0
fi
nohup "$PY" scripts/btc_record.py --provider "$PROVIDER" --poll 5 --log "$LOG" \
  >> out/record-nohup.log 2>&1 &
echo "started trajectory recorder (pid $!)  provider=$PROVIDER  -> $LOG"
echo "let it run several hours, then: scripts/record.sh analyze"
echo "NOTE: nohup does not survive a reboot."
