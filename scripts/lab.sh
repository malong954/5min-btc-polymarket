#!/usr/bin/env bash
#
# ONE command for the whole lab. No juggling multiple scripts.
#
#   scripts/lab.sh              start EVERYTHING (recorder + paper trader) and
#                               open the live dashboard. Ctrl-C leaves both
#                               running in the background.
#   scripts/lab.sh analyze      the FULL analysis battery in one report:
#                               entry timing, indicator side, confidence bands,
#                               sub-minute velocities, overround (E2),
#                               divergence fade (E3), timeline correlations
#   scripts/lab.sh dash         reopen the trader dashboard
#   scripts/lab.sh recdash      reopen the recorder (price-ladder) dashboard
#   scripts/lab.sh history      full entered/skipped/win/loss timeline
#   scripts/lab.sh status       what is running + how much data so far
#   scripts/lab.sh stop         stop everything
#
# Tunables (env): PROVIDER=binance THRESHOLD=0.60 STAKE=10 BANKROLL=100
#                 RULE=threshold|edge EDGE_MARGIN=0.03
#   RULE=edge enters when confidence >= live ask + EDGE_MARGIN (price = hurdle).
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
TLOG="out/live.jsonl"        # trader stream
RLOG="out/trajectory.jsonl"  # recorder stream
PROVIDER="${PROVIDER:-binance}"
THRESHOLD="${THRESHOLD:-0.60}"
RULE="${RULE:-threshold}"
EDGE_MARGIN="${EDGE_MARGIN:-0.03}"
MAX_PRICE="${MAX_PRICE:-0.97}"   # never buy above this ask (near $1 = pennies upside)
STAKE="${STAKE:-10}"
BANKROLL="${BANKROLL:-100}"

[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

trader_up()   { pgrep -f "btc_live_paper.py" >/dev/null 2>&1; }
recorder_up() { pgrep -f "btc_record.py"     >/dev/null 2>&1; }

case "${1:-start}" in
  stop)
    trader_up   && pkill -f "btc_live_paper.py" && echo "stopped the paper trader" || echo "trader not running"
    recorder_up && pkill -f "btc_record.py"     && echo "stopped the recorder"     || echo "recorder not running"
    exit 0 ;;

  newrun)
    # Fresh measurement segment: stop everything, archive BOTH logs with a
    # timestamp, start clean. Use after a model/config change so analyze does
    # not mix data measured with different brains. Old segments stay analyzable:
    #   .venv/bin/python scripts/btc_entry_timing.py --log out/trajectory-<ts>.jsonl --crossing
    pkill -f "btc_live_paper.py" 2>/dev/null || true
    pkill -f "btc_record.py" 2>/dev/null || true
    sleep 1
    TS="$(date +%Y%m%d-%H%M%S)"
    [ -f "$TLOG" ] && mv "$TLOG" "out/live-$TS.jsonl" && echo "archived trader log   -> out/live-$TS.jsonl"
    [ -f "$RLOG" ] && mv "$RLOG" "out/trajectory-$TS.jsonl" && echo "archived recorder log -> out/trajectory-$TS.jsonl"
    echo "starting a fresh run..."
    exec "$0" start ;;

  status)
    trader_up   && echo "trader:   RUNNING (pid $(pgrep -f btc_live_paper.py | tr '\n' ' '))" || echo "trader:   not running"
    recorder_up && echo "recorder: RUNNING (pid $(pgrep -f btc_record.py | tr '\n' ' '))"     || echo "recorder: not running"
    # NOTE: grep -c prints its count but exits 1 when the count is 0, so the
    # naive `grep -c ... || echo 0` printed BOTH a 0 and the fallback 0 (and
    # broke the zero-samples check below). Capture with `|| true` instead.
    TSET=0; RREC=0; RSAMP=0
    [ -f "$TLOG" ] && TSET="$(grep -c '"type":"settle"' "$TLOG" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RREC="$(grep -c '"type":"result"' "$RLOG" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RSAMP="$(grep -c '"type":"sample"' "$RLOG" 2>/dev/null || true)"
    echo "trades settled:  ${TSET:-0}"
    echo "rounds recorded: ${RREC:-0}  (samples: ${RSAMP:-0}; want 100+ rounds before judging)"
    # A recorder that is 'RUNNING' but writing nothing is a hidden failure —
    # surface its recent stderr so the cause is visible right here.
    if [ "${RSAMP:-0}" = "0" ] && [ -f out/record-nohup.log ]; then
      echo
      echo "!! recorder has produced NO samples — its recent output:"
      tail -10 out/record-nohup.log | sed 's/^/   | /'
    fi
    exit 0 ;;

  dash)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" ;;

  recdash)
    exec "$PY" scripts/btc_record_monitor.py --log "$RLOG" ;;

  history)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --bankroll "$BANKROLL" --history ;;

  analyze)
    echo; echo "################ FULL ANALYSIS BATTERY ################"; echo
    if [ -f "$RLOG" ]; then
      "$PY" scripts/btc_entry_timing.py --log "$RLOG"                      || true
      echo
      echo "---- same, robust: executable size only + near-flat rounds dropped ----"
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --min-size 100 --min-move 10 || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --side indicator     || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --by-confidence     || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --crossing          || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --edge-gate         || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --label-risk        || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --label-check       || true
      echo
      for v in vel_15s vel_30s; do
        "$PY" scripts/btc_entry_timing.py --log "$RLOG" --side "$v"       || true
        echo
      done
      "$PY" scripts/btc_overround.py --log "$RLOG"                        || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --fade              || true
      echo
    else
      echo "(no recorder data yet: $RLOG missing)"
    fi
    if [ -f "$TLOG" ]; then
      "$PY" scripts/btc_timeline_analyze.py --log "$TLOG"                 || true
    else
      echo "(no trader data yet: $TLOG missing)"
    fi
    echo; echo "######################## END ##########################"
    exit 0 ;;

  start) ;;
  *) echo "usage: scripts/lab.sh [start|newrun|analyze|dash|recdash|history|status|stop]"; exit 1 ;;
esac

mkdir -p out

# --- recorder (observer: prices + indicators + velocities per round) ---
if recorder_up; then
  echo "recorder already running"
else
  nohup "$PY" scripts/btc_record.py --provider "$PROVIDER" --poll 5 --log "$RLOG" \
    >> out/record-nohup.log 2>&1 &
  echo "started recorder (pid $!) -> $RLOG"
fi

# --- paper trader (flat stake, real pricing, enriched logging) ---
if trader_up; then
  echo "trader already running"
else
  # Fresh trader session = fresh $100 account (prior log archived).
  [ -f "$TLOG" ] && mv "$TLOG" "out/live-prev.jsonl" && echo "archived prior session -> out/live-prev.jsonl"
  nohup "$PY" scripts/btc_live_paper.py \
    --provider "$PROVIDER" --poll 2 --entry-threshold "$THRESHOLD" \
    --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" \
    --entry-price-source polymarket --sizing flat --stake-usd "$STAKE" \
    --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
    --log "$TLOG" --quiet \
    >> out/nohup.log 2>&1 &
  if [ "$RULE" = "edge" ]; then
    echo "started paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=edge (conf >= ask + ${EDGE_MARGIN})"
  elif [ "$RULE" = "lead" ]; then
    echo "started paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=lead (leading side, 180-240s left, ask <= 0.72, |move| >= 10)"
  else
    echo "started paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=threshold (conf >= ${THRESHOLD})"
  fi
fi

echo
echo "opening dashboard — Ctrl-C exits the dashboard; everything keeps running."
echo "later:  scripts/lab.sh analyze   (full report)   scripts/lab.sh stop"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN"
