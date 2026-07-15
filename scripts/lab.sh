#!/usr/bin/env bash
#
# ONE command for the whole lab. No juggling multiple scripts.
#
#   scripts/lab.sh              start EVERYTHING (recorders + paper traders) and
#                               open the live dashboard. Ctrl-C leaves them
#                               running in the background.
#   scripts/lab.sh analyze      the FULL analysis battery in one report:
#                               entry timing, indicator side, confidence bands,
#                               sub-minute velocities, sessions, overround (E2),
#                               divergence fade (E3), timeline correlations
#   scripts/lab.sh dash         reopen the trader dashboard
#   scripts/lab.sh recdash      reopen the recorder (price-ladder) dashboard
#   scripts/lab.sh history      full entered/skipped/win/loss timeline
#   scripts/lab.sh status       what is running + how much data so far
#   scripts/lab.sh stop         stop everything
#
# Tunables (env): PROVIDER=binance THRESHOLD=0.60 STAKE=10 BANKROLL=100
#                 RULE=threshold|edge|lead EDGE_MARGIN=0.03 LEAD_MIN_CONF=0.40
#                 ASSETS="eth sol xrp"   extra 5m markets to record AND trade
#                                        alongside BTC (each gets its own
#                                        recorder, trader, bankroll and log)
#   RULE=edge enters when confidence >= live ask + EDGE_MARGIN (price = hurdle).
#   Legacy ETH=1 still works (same as adding eth to ASSETS).
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
PY=".venv/bin/python"
TLOG="out/live.jsonl"        # trader stream (BTC)
RLOG="out/trajectory.jsonl"  # recorder stream (BTC)
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
ETH="${ETH:-${SAVED_ETH:-0}}"    # legacy switch; folded into ASSETS below
ASSETS="${ASSETS-${SAVED_ASSETS:-}}"   # extra markets: any of "eth sol xrp"
STAKE="${STAKE:-${SAVED_STAKE:-10}}"
BANKROLL="${BANKROLL:-${SAVED_BANKROLL:-100}}"

# ETH=1 is the old spelling of ASSETS="eth" — honor it, without duplicating.
if [ "$ETH" = "1" ] && ! printf ' %s ' $ASSETS | grep -q ' eth '; then
  ASSETS="eth${ASSETS:+ $ASSETS}"
fi
ASSETS="$(echo "$ASSETS" | tr 'A-Z' 'a-z')"
for A in $ASSETS; do
  case "$A" in eth|sol|xrp) ;; *) echo "unknown asset in ASSETS: $A (allowed: eth sol xrp)"; exit 1 ;; esac
done

[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

trader_up()   { pgrep -f "btc_live_paper.py" >/dev/null 2>&1; }
recorder_up() { pgrep -f "btc_record.py"     >/dev/null 2>&1; }
# Per-market checks. BTC = 'any such process that is NOT an --asset extra'
# (also matches launches from before --asset existed); extras match their flag.
btrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -vE -- "--asset (eth|sol|xrp)" | grep -q .; }
brec_up()     { ps ax -o command 2>/dev/null | grep "[b]tc_record.py"     | grep -vE -- "--asset (eth|sol|xrp)" | grep -q .; }
atrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -q -- "--asset $1"; }
arec_up()     { ps ax -o command 2>/dev/null | grep "[b]tc_record.py"     | grep -q -- "--asset $1"; }

# Extra trader logs -> dashboard merge flags (string, not array: macOS bash 3.2
# chokes on empty-array expansion under set -u; paths contain no spaces).
MERGEOPTS=""
for A in $ASSETS; do MERGEOPTS="$MERGEOPTS --merge out/live-$A.jsonl"; done

case "${1:-start}" in
  stop)
    trader_up   && pkill -f "btc_live_paper.py" && echo "stopped the paper trader(s)" || echo "trader not running"
    recorder_up && pkill -f "btc_record.py"     && echo "stopped the recorder(s)"     || echo "recorder not running"
    exit 0 ;;

  newrun)
    # Fresh measurement segment: stop everything, archive ALL logs with a
    # timestamp, start clean. Use after a model/config change so analyze does
    # not mix data measured with different brains. Old segments stay analyzable:
    #   .venv/bin/python scripts/btc_entry_timing.py --log out/trajectory-<ts>.jsonl --crossing
    pkill -f "btc_live_paper.py" 2>/dev/null || true
    pkill -f "btc_record.py" 2>/dev/null || true
    sleep 1
    TS="$(date +%Y%m%d-%H%M%S)"
    [ -f "$TLOG" ] && mv "$TLOG" "out/live-$TS.jsonl" && echo "archived trader log   -> out/live-$TS.jsonl"
    [ -f "$RLOG" ] && mv "$RLOG" "out/trajectory-$TS.jsonl" && echo "archived recorder log -> out/trajectory-$TS.jsonl"
    for A in eth sol xrp; do
      [ -f "out/live-$A.jsonl" ] && mv "out/live-$A.jsonl" "out/live-$A-$TS.jsonl" && echo "archived $A trader log -> out/live-$A-$TS.jsonl"
      [ -f "out/trajectory-$A.jsonl" ] && mv "out/trajectory-$A.jsonl" "out/trajectory-$A-$TS.jsonl" && echo "archived $A recorder log -> out/trajectory-$A-$TS.jsonl"
    done
    echo "starting a fresh run..."
    exec "$0" start ;;

  status)
    btrader_up  && echo "trader (BTC):   RUNNING" || echo "trader (BTC):   not running"
    brec_up     && echo "recorder (BTC): RUNNING" || echo "recorder (BTC): not running"
    for A in $ASSETS; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      atrader_up "$A" && echo "trader ($AU):   RUNNING" || echo "trader ($AU):   not running"
      arec_up "$A"    && echo "recorder ($AU): RUNNING" || echo "recorder ($AU): not running"
    done
    # NOTE: grep -c prints its count but exits 1 when the count is 0, so the
    # naive `grep -c ... || echo 0` printed BOTH a 0 and the fallback 0 (and
    # broke the zero-samples check below). Capture with `|| true` instead.
    TSET=0; RREC=0; RSAMP=0
    [ -f "$TLOG" ] && TSET="$(grep -c '"type":"settle"' "$TLOG" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RREC="$(grep -c '"type":"result"' "$RLOG" 2>/dev/null || true)"
    [ -f "$RLOG" ] && RSAMP="$(grep -c '"type":"sample"' "$RLOG" 2>/dev/null || true)"
    echo "BTC trades settled: ${TSET:-0}"
    echo "BTC rounds recorded: ${RREC:-0}  (samples: ${RSAMP:-0}; want 100+ rounds before judging)"
    for A in $ASSETS; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      ASET=0; AREC=0; ASAMP=0
      [ -f "out/live-$A.jsonl" ] && ASET="$(grep -c '"type":"settle"' "out/live-$A.jsonl" 2>/dev/null || true)"
      [ -f "out/trajectory-$A.jsonl" ] && AREC="$(grep -c '"type":"result"' "out/trajectory-$A.jsonl" 2>/dev/null || true)"
      [ -f "out/trajectory-$A.jsonl" ] && ASAMP="$(grep -c '"type":"sample"' "out/trajectory-$A.jsonl" 2>/dev/null || true)"
      echo "$AU trades settled: ${ASET:-0}   rounds recorded: ${AREC:-0}  (samples: ${ASAMP:-0})"
    done
    # A recorder that is 'RUNNING' but writing nothing is a hidden failure —
    # surface its recent stderr so the cause is visible right here.
    if [ "${RSAMP:-0}" = "0" ] && [ -f out/record-nohup.log ]; then
      echo
      echo "!! BTC recorder has produced NO samples — its recent output:"
      tail -10 out/record-nohup.log | sed 's/^/   | /'
    fi
    for A in $ASSETS; do
      ASAMP="$(grep -c '"type":"sample"' "out/trajectory-$A.jsonl" 2>/dev/null || true)"
      if [ "${ASAMP:-0}" = "0" ] && [ -f "out/record-$A-nohup.log" ]; then
        echo
        echo "!! $A recorder has produced NO samples — its recent output:"
        tail -10 "out/record-$A-nohup.log" | sed 's/^/   | /'
      fi
    done
    exit 0 ;;

  dash)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" $MERGEOPTS --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" ;;

  recdash)
    exec "$PY" scripts/btc_record_monitor.py --log "$RLOG" ;;

  history)
    exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" $MERGEOPTS --bankroll "$BANKROLL" --history ;;

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
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --by-session --min-size 100 || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --calibration || true
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
      for A in eth sol xrp; do
        ARLOG="out/trajectory-$A.jsonl"
        [ -f "$ARLOG" ] || continue
        AU="$(echo "$A" | tr 'a-z' 'A-Z')"
        echo "---- $AU 5m market: overround / dislocations ----"
        "$PY" scripts/btc_overround.py --log "$ARLOG"                     || true
        echo
        echo "---- $AU 5m market: lead + confidence combo (move auto-scaled) ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --combo --min-size 100 || true
        echo
        echo "---- $AU 5m market: sessions ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --by-session --min-size 100 || true
        echo
        echo "---- $AU 5m market: confidence calibration ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --calibration || true
        echo
      done
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
  echo "SAVED_ASSETS=\"$ASSETS\""
  echo "SAVED_STAKE=$STAKE"
  echo "SAVED_BANKROLL=$BANKROLL"
} > "$CONF"

# --- BTC recorder (observer: prices + indicators + velocities per round) ---
if brec_up; then
  echo "BTC recorder already running"
else
  nohup "$PY" scripts/btc_record.py --provider "$PROVIDER" --poll 5 --log "$RLOG" \
    >> out/record-nohup.log 2>&1 &
  echo "started BTC recorder (pid $!) -> $RLOG"
fi

# --- BTC paper trader (flat stake, real pricing, enriched logging) ---
if btrader_up; then
  echo "BTC trader already running"
else
  # Fresh trader session = fresh account (prior log archived).
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
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=lead (leading side, 180-240s left, ask <= 0.72, decisive move, conf >= ${LEAD_MIN_CONF})"
  else
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=threshold (conf >= ${THRESHOLD})"
  fi
fi

# --- extra markets: one recorder + one trader per asset in ASSETS ---
for A in $ASSETS; do
  AU="$(echo "$A" | tr 'a-z' 'A-Z')"
  if arec_up "$A"; then
    echo "$AU recorder already running"
  else
    nohup "$PY" scripts/btc_record.py --asset "$A" --provider binance --poll 5 --log "out/trajectory-$A.jsonl" \
      >> "out/record-$A-nohup.log" 2>&1 &
    echo "started $AU recorder (pid $!) -> out/trajectory-$A.jsonl"
  fi
  if atrader_up "$A"; then
    echo "$AU trader already running"
  else
    [ -f "out/live-$A.jsonl" ] && mv "out/live-$A.jsonl" "out/live-$A-prev.jsonl" && echo "archived prior $AU session -> out/live-$A-prev.jsonl"
    nohup "$PY" scripts/btc_live_paper.py --asset "$A" \
      --provider binance --poll 3 --entry-threshold "$THRESHOLD" \
      --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" \
      --lead-min-conf "$LEAD_MIN_CONF" \
      --entry-price-source polymarket --sizing flat --stake-usd "$STAKE" \
      --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
      --log "out/live-$A.jsonl" --quiet \
      >> "out/nohup-$A.log" 2>&1 &
    echo "started $AU paper trader (pid $!)  same rule, own \$${BANKROLL} account (move threshold auto-scales to $AU)"
  fi
done

echo
echo "opening dashboard — Ctrl-C exits the dashboard; everything keeps running."
echo "later:  scripts/lab.sh analyze   (full report)   scripts/lab.sh stop"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" $MERGEOPTS --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN"
