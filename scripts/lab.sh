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
TLOG="out/live.jsonl"        # trader stream (BTC)
ELOGT="out/live-eth.jsonl"   # trader stream (ETH, opt-in via ETH=1)
RLOG="out/trajectory.jsonl"  # recorder stream (BTC)
ELOG="out/trajectory-eth.jsonl" # recorder stream (ETH, opt-in via ETH=1)
CONF="out/lab.conf"          # last run's settings — a plain restart reuses them

# Precedence: explicit env var > saved config from the last start > default.
# (Prevents the classic footgun: RULE=lead scripts/lab.sh ... then later a
# plain scripts/lab.sh silently reverting to the threshold rule.)
[ -f "$CONF" ] && . "$CONF"
PROVIDER="${PROVIDER:-${SAVED_PROVIDER:-binance}}"
THRESHOLD="${THRESHOLD:-${SAVED_THRESHOLD:-0.60}}"
RULE="${RULE:-${SAVED_RULE:-threshold}}"
EDGE_MARGIN="${EDGE_MARGIN:-${SAVED_EDGE_MARGIN:-0.03}}"
MAX_PRICE="${MAX_PRICE:-${SAVED_MAX_PRICE:-0.97}}"   # never buy above this ask
LEAD_MIN_CONF="${LEAD_MIN_CONF:-${SAVED_LEAD_MIN_CONF:-0.0}}"  # lead+confidence combo floor (0 = off)
ETH="${ETH:-${SAVED_ETH:-0}}"    # ETH=1 also records AND paper-trades the ETH 5m market
STAKE="${STAKE:-${SAVED_STAKE:-10}}"
BANKROLL="${BANKROLL:-${SAVED_BANKROLL:-100}}"

[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

trader_up()   { pgrep -f "btc_live_paper.py" >/dev/null 2>&1; }
recorder_up() { pgrep -f "btc_record.py"     >/dev/null 2>&1; }
# Per-market checks (ps, not pgrep -f regex: the BTC trader is 'any trader
# process that is NOT --asset eth', which also matches pre---asset launches).
btrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -v -- "--asset eth" | grep -q .; }
etrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -q -- "--asset eth"; }

case "${1:-start}" in
  stop)
    trader_up   && pkill -f "btc_live_paper.py" && echo "stopped the paper trader(s)" || echo "trader not running"
    recorder_up && pkill -f "btc_record.py"     && echo "stopped the recorder(s)"     || echo "recorder not running"
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
    [ -f "$ELOGT" ] && mv "$ELOGT" "out/live-eth-$TS.jsonl" && echo "archived ETH trader log -> out/live-eth-$TS.jsonl"
    [ -f "$RLOG" ] && mv "$RLOG" "out/trajectory-$TS.jsonl" && echo "archived recorder log -> out/trajectory-$TS.jsonl"
    [ -f "$ELOG" ] && mv "$ELOG" "out/trajectory-eth-$TS.jsonl" && echo "archived ETH recorder log -> out/trajectory-eth-$TS.jsonl"
    echo "starting a fresh run..."
    exec "$0" start ;;

  status)
    btrader_up  && echo "trader (BTC): RUNNING" || echo "trader (BTC): not running"
    if [ "$ETH" = "1" ] || [ -f "$ELOGT" ]; then
      etrader_up && echo "trader (ETH): RUNNING" || echo "trader (ETH): not running"
    fi
    recorder_up && echo "recorder: RUNNING (pid $(pgrep -f btc_record.py | tr '\n' ' '))"     || echo "recorder: not running"
    # NOTE: grep -c prints its count but exits 1 when the count is 0, so the
    # naive `grep -c ... || echo 0` printed BOTH a 0 and the fallback 0 (and
    # broke the zero-samples check below). Capture with `|| true` instead.
    TSET=0; ESET=0; RREC=0; RSAMP=0
    [ -f "$TLOG" ] && TSET="$(grep -c '"type":"settle"' "$TLOG" 2>/dev/null || true)"
    [ -f "$ELOGT" ] && ESET="$(grep -c '"type":"settle"' "$ELOGT" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RREC="$(grep -c '"type":"result"' "$RLOG" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RSAMP="$(grep -c '"type":"sample"' "$RLOG" 2>/dev/null || true)"
    if [ -f "$ELOGT" ]; then
      echo "trades settled:  ${TSET:-0} BTC + ${ESET:-0} ETH"
    else
      echo "trades settled:  ${TSET:-0}"
    fi
    echo "rounds recorded: ${RREC:-0}  (samples: ${RSAMP:-0}; want 100+ rounds before judging)"
    if [ -f "$ELOG" ]; then
      EREC=0; ESAMP=0
      EREC="$(grep -c '"type":"result"' "$ELOG" 2>/dev/null || true)"
      ESAMP="$(grep -c '"type":"sample"' "$ELOG" 2>/dev/null || true)"
      echo "ETH rounds:      ${EREC:-0}  (samples: ${ESAMP:-0})"
    fi
    # A recorder that is 'RUNNING' but writing nothing is a hidden failure —
    # surface its recent stderr so the cause is visible right here.
    if [ "${RSAMP:-0}" = "0" ] && [ -f out/record-nohup.log ]; then
      echo
      echo "!! recorder has produced NO samples — its recent output:"
      tail -10 out/record-nohup.log | sed 's/^/   | /'
    fi
    exit 0 ;;

  dash)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --eth-log "$ELOGT" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" ;;

  recdash)
    exec "$PY" scripts/btc_record_monitor.py --log "$RLOG" ;;

  history)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --eth-log "$ELOGT" --bankroll "$BANKROLL" --history ;;

  analyze)
    echo; echo "################ FULL ANALYSIS BATTERY ################"; echo
    if [ -f "$RLOG" ]; then
      echo "NOTE: spot-labeled tables below are UNRELIABLE (measured ~26% label"
      echo "disagreement vs official settles). Read the TRUTH TABLE and COMBO"
      echo "sections — official Polymarket resolutions only — for real answers."
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG"                      || true
      echo
      echo "---- same, robust: executable size only + near-flat rounds dropped ----"
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --min-size 100 --min-move 10 || true
      echo
      echo "---- THE TRUTH TABLE: official Polymarket resolutions only ----"
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --official-only --min-size 100 || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --combo --min-size 100 || true
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
      if [ -f "$ELOG" ]; then
        echo "---- ETH 5m market: overround / dislocations ----"
        "$PY" scripts/btc_overround.py --log "$ELOG"                      || true
        echo
        echo "---- ETH 5m market: lead + confidence combo (move threshold scaled) ----"
        "$PY" scripts/btc_entry_timing.py --log "$ELOG" --combo --min-size 100 --combo-min-move 0.35 || true
        echo
      fi
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

# Persist the effective settings — a later plain `scripts/lab.sh` reuses them.
{
  echo "SAVED_PROVIDER=$PROVIDER"
  echo "SAVED_THRESHOLD=$THRESHOLD"
  echo "SAVED_RULE=$RULE"
  echo "SAVED_EDGE_MARGIN=$EDGE_MARGIN"
  echo "SAVED_MAX_PRICE=$MAX_PRICE"
  echo "SAVED_LEAD_MIN_CONF=$LEAD_MIN_CONF"
  echo "SAVED_ETH=$ETH"
  echo "SAVED_STAKE=$STAKE"
  echo "SAVED_BANKROLL=$BANKROLL"
} > "$CONF"

# --- recorder (observer: prices + indicators + velocities per round) ---
if recorder_up; then
  echo "recorder already running"
else
  nohup "$PY" scripts/btc_record.py --provider "$PROVIDER" --poll 5 --log "$RLOG" \
    >> out/record-nohup.log 2>&1 &
  echo "started recorder (pid $!) -> $RLOG"
fi

# --- optional ETH recorder (structural angles: dislocations, spreads) ---
if [ "$ETH" = "1" ]; then
  if pgrep -f "btc_record.py.*--asset eth" >/dev/null 2>&1; then
    echo "ETH recorder already running"
  else
    nohup "$PY" scripts/btc_record.py --asset eth --provider binance --poll 5 --log "$ELOG" \
      >> out/record-eth-nohup.log 2>&1 &
    echo "started ETH recorder (pid $!) -> $ELOG"
  fi
fi

# --- paper trader BTC (flat stake, real pricing, enriched logging) ---
if btrader_up; then
  echo "BTC trader already running"
else
  # Fresh trader session = fresh $100 account (prior log archived).
  [ -f "$TLOG" ] && mv "$TLOG" "out/live-prev.jsonl" && echo "archived prior session -> out/live-prev.jsonl"
  nohup "$PY" scripts/btc_live_paper.py --asset btc \
    --provider "$PROVIDER" --poll 2 --entry-threshold "$THRESHOLD" \
    --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" \
    --lead-min-conf "$LEAD_MIN_CONF" \
    --entry-price-source polymarket --sizing flat --stake-usd "$STAKE" \
    --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
    --log "$TLOG" --quiet \
    >> out/nohup.log 2>&1 &
  if [ "$RULE" = "edge" ]; then
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=edge (conf >= ask + ${EDGE_MARGIN})"
  elif [ "$RULE" = "lead" ]; then
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=lead (leading side, 180-240s left, ask <= 0.72, |move| >= 10, conf >= ${LEAD_MIN_CONF})"
  else
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=threshold (conf >= ${THRESHOLD})"
  fi
fi

# --- optional paper trader ETH (same rule/stake, own $BANKROLL, own log) ---
if [ "$ETH" = "1" ]; then
  if etrader_up; then
    echo "ETH trader already running"
  else
    [ -f "$ELOGT" ] && mv "$ELOGT" "out/live-eth-prev.jsonl" && echo "archived prior ETH session -> out/live-eth-prev.jsonl"
    nohup "$PY" scripts/btc_live_paper.py --asset eth \
      --provider binance --poll 3 --entry-threshold "$THRESHOLD" \
      --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" \
      --lead-min-conf "$LEAD_MIN_CONF" \
      --entry-price-source polymarket --sizing flat --stake-usd "$STAKE" \
      --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
      --log "$ELOGT" --quiet \
      >> out/nohup-eth.log 2>&1 &
    echo "started ETH paper trader (pid $!)  same rule, own \$${BANKROLL} account (lead move threshold auto-scales to ETH)"
  fi
fi

echo
echo "opening dashboard — Ctrl-C exits the dashboard; everything keeps running."
echo "later:  scripts/lab.sh analyze   (full report)   scripts/lab.sh stop"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" --eth-log "$ELOGT" --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN"
