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
#   scripts/lab.sh analyze-all  the decision tables over EVERY archived segment
#                               pooled with the current one (full-history truth
#                               table, combo, split-halves, crossing, calibration)
#   scripts/lab.sh sync         push the lab's logs (gzipped) into the repo so
#                               the analysis side can run reports on the data
#                               directly instead of via pasted terminal output
#   scripts/lab.sh dash         reopen the trader dashboard
#   scripts/lab.sh recdash      reopen the recorder (price-ladder) dashboard
#   scripts/lab.sh history      full entered/skipped/win/loss timeline
#   scripts/lab.sh status       what is running + how much data so far
#   scripts/lab.sh stop         stop everything
#
# Tunables (env): PROVIDER=binance THRESHOLD=0.60 STAKE=10 BANKROLL=100
#                 RULE=threshold|edge|lead EDGE_MARGIN=0.03 LEAD_MIN_CONF=0.40
#                 LEAD_HI=240 LEAD_LO=180   lead-rule window (seconds left when
#                                        it opens/closes; 15m traders get x3)
#                 LEAD_MAX_PRICE=0.72    lead rule: only enter while the ask is
#                                        at/below this (raise to ~0.90 for the
#                                        late-confirmed variant)
#                 REGIME=0.5             volatility regime gate: median trailing
#                                        round |move| must reach this fraction of
#                                        the asset's decisive-move threshold or
#                                        the round skips as flat_regime (0 = off)
#                 ASSETS="eth sol xrp"   extra 5m markets to record AND trade
#                                        alongside BTC (each gets its own
#                                        recorder, trader, bankroll and log)
#                 M15="btc"              assets to ALSO trade on the 15m window
#                                        (any of "btc eth sol xrp"; each gets its
#                                        own trader + log, tagged e.g. BTC15 on
#                                        the dashboard; lead windows scale x3)
#                 CONF_BTC/CONF_ETH/CONF_SOL/CONF_XRP   per-market conf floor
#                                        (overrides LEAD_MIN_CONF for that market)
#                 RULE_<A>/THRESH_<A>       per-market entry RULE + threshold
#                                        (A = BTC/ETH/SOL/XRP), falling back to
#                                        the global RULE/THRESHOLD. The per-asset
#                                        playbook: BTC on the lead rule, SOL/XRP
#                                        on the conf-threshold rule their
#                                        calibration supports:
#                                          RULE_SOL=threshold THRESH_SOL=0.55
#                 LEAD_HI_<A>/LEAD_LO_<A>/LEAD_CAP_<A>  per-market lead window +
#                                        ask cap (A = BTC/ETH/SOL/XRP), each
#                                        falling back to the global LEAD_*. Use
#                                        when one rule doesn't fit all assets —
#                                        e.g. BTC late+expensive, SOL/XRP
#                                        early+cheap:  LEAD_HI_SOL=240 LEAD_LO_SOL=150
#                                        LEAD_CAP_SOL=0.72  LEAD_HI_BTC=120 ...
#   RULE=edge enters when confidence >= live ask + EDGE_MARGIN (price = hurdle).
#   Legacy ETH=1 still works (same as adding eth to ASSETS).
#
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
# Gitignored .env carries Chainlink credentials (CHAINLINK_CANDLE_*) etc. —
# export everything so recorders/traders inherit it. No inline comments on
# env lines (a stray character there has broken sourcing before).
if [ -f .env ]; then set -a; . ./.env; set +a; fi
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
LEAD_HI="${LEAD_HI:-${SAVED_LEAD_HI:-240}}"   # lead window opens at this many seconds left
LEAD_LO="${LEAD_LO:-${SAVED_LEAD_LO:-180}}"   # ...and closes at this many seconds left
LEAD_MAX_PRICE="${LEAD_MAX_PRICE:-${SAVED_LEAD_MAX_PRICE:-0.72}}"  # lead rule ask cap
REGIME="${REGIME:-${SAVED_REGIME:-0}}"        # regime gate fraction (0 = off)
# Per-market confidence floors override the global LEAD_MIN_CONF for that market
# (each market calibrates + gets adversely selected differently). Any unset one
# falls back to LEAD_MIN_CONF. NOTE: choosing these FROM the combo tables makes
# only FORWARD trades honest evidence — the pooled table includes the in-sample
# rounds you picked from.
CONF_BTC="${CONF_BTC:-${SAVED_CONF_BTC:-$LEAD_MIN_CONF}}"
CONF_ETH="${CONF_ETH:-${SAVED_CONF_ETH:-$LEAD_MIN_CONF}}"
CONF_SOL="${CONF_SOL:-${SAVED_CONF_SOL:-$LEAD_MIN_CONF}}"
CONF_XRP="${CONF_XRP:-${SAVED_CONF_XRP:-$LEAD_MIN_CONF}}"
# Per-market entry RULE + threshold: each asset runs the signal that validated
# for it (BTC: lead+conf; SOL/XRP: the move-based lead never fires at their tick
# size — conf-threshold is the signal their calibration supports every segment).
RULE_BTC="${RULE_BTC:-${SAVED_RULE_BTC:-$RULE}}"
RULE_ETH="${RULE_ETH:-${SAVED_RULE_ETH:-$RULE}}"
RULE_SOL="${RULE_SOL:-${SAVED_RULE_SOL:-$RULE}}"
RULE_XRP="${RULE_XRP:-${SAVED_RULE_XRP:-$RULE}}"
THRESH_BTC="${THRESH_BTC:-${SAVED_THRESH_BTC:-$THRESHOLD}}"
THRESH_ETH="${THRESH_ETH:-${SAVED_THRESH_ETH:-$THRESHOLD}}"
THRESH_SOL="${THRESH_SOL:-${SAVED_THRESH_SOL:-$THRESHOLD}}"
THRESH_XRP="${THRESH_XRP:-${SAVED_THRESH_XRP:-$THRESHOLD}}"
for RV in "$RULE_BTC" "$RULE_ETH" "$RULE_SOL" "$RULE_XRP"; do
  case "$RV" in threshold|edge|lead) ;; *) echo "invalid per-asset rule: $RV (allowed: threshold edge lead)"; exit 1 ;; esac
done
# Per-market lead WINDOW + ASK CAP overrides. Measured motivation: BTC's edge
# is late-and-expensive (150-180s @ ~0.70, hold to resolution) while SOL/XRP had
# their edge earlier-and-cheaper under the prior rule. One rule can't fit all
# assets, so each falls back to the global LEAD_* unless set. ETH is negative
# under both configs (no edge) — drop it from ASSETS rather than tune it.
LEAD_HI_BTC="${LEAD_HI_BTC:-${SAVED_LEAD_HI_BTC:-$LEAD_HI}}"
LEAD_HI_ETH="${LEAD_HI_ETH:-${SAVED_LEAD_HI_ETH:-$LEAD_HI}}"
LEAD_HI_SOL="${LEAD_HI_SOL:-${SAVED_LEAD_HI_SOL:-$LEAD_HI}}"
LEAD_HI_XRP="${LEAD_HI_XRP:-${SAVED_LEAD_HI_XRP:-$LEAD_HI}}"
LEAD_LO_BTC="${LEAD_LO_BTC:-${SAVED_LEAD_LO_BTC:-$LEAD_LO}}"
LEAD_LO_ETH="${LEAD_LO_ETH:-${SAVED_LEAD_LO_ETH:-$LEAD_LO}}"
LEAD_LO_SOL="${LEAD_LO_SOL:-${SAVED_LEAD_LO_SOL:-$LEAD_LO}}"
LEAD_LO_XRP="${LEAD_LO_XRP:-${SAVED_LEAD_LO_XRP:-$LEAD_LO}}"
LEAD_CAP_BTC="${LEAD_CAP_BTC:-${SAVED_LEAD_CAP_BTC:-$LEAD_MAX_PRICE}}"
LEAD_CAP_ETH="${LEAD_CAP_ETH:-${SAVED_LEAD_CAP_ETH:-$LEAD_MAX_PRICE}}"
LEAD_CAP_SOL="${LEAD_CAP_SOL:-${SAVED_LEAD_CAP_SOL:-$LEAD_MAX_PRICE}}"
LEAD_CAP_XRP="${LEAD_CAP_XRP:-${SAVED_LEAD_CAP_XRP:-$LEAD_MAX_PRICE}}"
ETH="${ETH:-${SAVED_ETH:-0}}"    # legacy switch; folded into ASSETS below
ASSETS="${ASSETS-${SAVED_ASSETS:-}}"   # extra markets: any of "eth sol xrp"
M15="${M15-${SAVED_M15:-}}"      # assets to ALSO trade on the 15m window
STAKE="${STAKE:-${SAVED_STAKE:-10}}"
# Sizing model: flat = same $STAKE every trade (measurement default). kelly /
# confidence scale the stake with the edge — the fix for "high winrate, thin
# percentage, full cap risk": the flat model ignores that a conf-0.9 setup
# justifies more size than a conf-0.5 graze (Kelly f* = (q-p)/(1-p)).
SIZING="${SIZING:-${SAVED_SIZING:-flat}}"
STAKE_PCT="${STAKE_PCT:-${SAVED_STAKE_PCT:-0.10}}"
case "$SIZING" in flat|percent|confidence|kelly) ;; *) echo "invalid SIZING: $SIZING (allowed: flat percent confidence kelly)"; exit 1 ;; esac
BANKROLL="${BANKROLL:-${SAVED_BANKROLL:-100}}"

# ETH=1 is the old spelling of ASSETS="eth" — honor it, without duplicating.
if [ "$ETH" = "1" ] && ! printf ' %s ' $ASSETS | grep -q ' eth '; then
  ASSETS="eth${ASSETS:+ $ASSETS}"
fi
ASSETS="$(echo "$ASSETS" | tr 'A-Z' 'a-z')"
for A in $ASSETS; do
  case "$A" in eth|sol|xrp) ;; *) echo "unknown asset in ASSETS: $A (allowed: eth sol xrp)"; exit 1 ;; esac
done
M15="$(echo "$M15" | tr 'A-Z' 'a-z')"
for A in $M15; do
  case "$A" in btc|eth|sol|xrp) ;; *) echo "unknown asset in M15: $A (allowed: btc eth sol xrp)"; exit 1 ;; esac
done

[ -x "$PY" ] || { echo "create the venv first: python3 -m venv .venv && .venv/bin/pip install requests"; exit 1; }

# Per-market rule params for an asset (btc/eth/sol/xrp), default global.
conf_for()    { case "$1" in btc) echo "$CONF_BTC";; eth) echo "$CONF_ETH";; sol) echo "$CONF_SOL";; xrp) echo "$CONF_XRP";; *) echo "$LEAD_MIN_CONF";; esac; }
rule_for()    { case "$1" in btc) echo "$RULE_BTC";; eth) echo "$RULE_ETH";; sol) echo "$RULE_SOL";; xrp) echo "$RULE_XRP";; *) echo "$RULE";; esac; }
thresh_for()  { case "$1" in btc) echo "$THRESH_BTC";; eth) echo "$THRESH_ETH";; sol) echo "$THRESH_SOL";; xrp) echo "$THRESH_XRP";; *) echo "$THRESHOLD";; esac; }
lead_hi_for() { case "$1" in btc) echo "$LEAD_HI_BTC";; eth) echo "$LEAD_HI_ETH";; sol) echo "$LEAD_HI_SOL";; xrp) echo "$LEAD_HI_XRP";; *) echo "$LEAD_HI";; esac; }
lead_lo_for() { case "$1" in btc) echo "$LEAD_LO_BTC";; eth) echo "$LEAD_LO_ETH";; sol) echo "$LEAD_LO_SOL";; xrp) echo "$LEAD_LO_XRP";; *) echo "$LEAD_LO";; esac; }
lead_cap_for(){ case "$1" in btc) echo "$LEAD_CAP_BTC";; eth) echo "$LEAD_CAP_ETH";; sol) echo "$LEAD_CAP_SOL";; xrp) echo "$LEAD_CAP_XRP";; *) echo "$LEAD_MAX_PRICE";; esac; }

trader_up()   { pgrep -f "btc_live_paper.py" >/dev/null 2>&1; }
recorder_up() { pgrep -f "btc_record.py"     >/dev/null 2>&1; }
# Per-market checks. BTC = 'any such process that is NOT an --asset extra'
# (also matches launches from before --asset existed); extras match their flag.
# 5m matchers exclude --window 15m so a 15m trader never masks its 5m sibling.
btrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -vE -- "--asset (eth|sol|xrp)" | grep -v -- "--window 15m" | grep -q .; }
brec_up()     { ps ax -o command 2>/dev/null | grep "[b]tc_record.py"     | grep -vE -- "--asset (eth|sol|xrp)" | grep -q .; }
atrader_up()  { ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -- "--asset $1" | grep -v -- "--window 15m" | grep -q .; }
arec_up()     { ps ax -o command 2>/dev/null | grep "[b]tc_record.py"     | grep -q -- "--asset $1"; }
m15trader_up(){ ps ax -o command 2>/dev/null | grep "[b]tc_live_paper.py" | grep -- "--window 15m" | grep -q -- "--asset $1"; }

# Extra trader logs -> dashboard merge flags (string, not array: macOS bash 3.2
# chokes on empty-array expansion under set -u; paths contain no spaces).
MERGEOPTS=""
for A in $ASSETS; do MERGEOPTS="$MERGEOPTS --merge out/live-$A.jsonl"; done
for A in $M15;    do MERGEOPTS="$MERGEOPTS --merge out/live-$A-15m.jsonl"; done

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
    for A in btc eth sol xrp; do
      [ -f "out/live-$A-15m.jsonl" ] && mv "out/live-$A-15m.jsonl" "out/live-$A-15m-$TS.jsonl" && echo "archived $A 15m trader log -> out/live-$A-15m-$TS.jsonl"
    done
    echo "starting a fresh run..."
    exec "$0" start ;;

  status)
    echo "conf floors:  BTC ${CONF_BTC}  ETH ${CONF_ETH}  SOL ${CONF_SOL}  XRP ${CONF_XRP}  (rule=$RULE)"
    echo "lead window:  ${LEAD_HI}-${LEAD_LO}s left  (15m traders: x3)   ask cap: ${LEAD_MAX_PRICE}  (global defaults)"
    for A in btc eth sol xrp; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      AHI="$(lead_hi_for "$A")"; ALO="$(lead_lo_for "$A")"; ACAP="$(lead_cap_for "$A")"
      ARULE="$(rule_for "$A")"; ATHR="$(thresh_for "$A")"
      if [ "$ARULE" != "$RULE" ] || [ "$ATHR" != "$THRESHOLD" ]; then
        echo "  $AU override: rule=${ARULE} thr=${ATHR}"
      fi
      if [ "$AHI" != "$LEAD_HI" ] || [ "$ALO" != "$LEAD_LO" ] || [ "$ACAP" != "$LEAD_MAX_PRICE" ]; then
        echo "  $AU override: ${ALO}-${AHI}s left, ask <= ${ACAP}"
      fi
    done
    echo "regime gate:  ${REGIME}  (fraction of decisive move; 0 = off)"
    btrader_up  && echo "trader (BTC):   RUNNING" || echo "trader (BTC):   not running"
    brec_up     && echo "recorder (BTC): RUNNING" || echo "recorder (BTC): not running"
    for A in $ASSETS; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      atrader_up "$A" && echo "trader ($AU):   RUNNING" || echo "trader ($AU):   not running"
      arec_up "$A"    && echo "recorder ($AU): RUNNING" || echo "recorder ($AU): not running"
    done
    for A in $M15; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      m15trader_up "$A" && echo "trader (${AU} 15m): RUNNING" || echo "trader (${AU} 15m): not running"
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
    for A in $M15; do
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      MSET=0
      [ -f "out/live-$A-15m.jsonl" ] && MSET="$(grep -c '"type":"settle"' "out/live-$A-15m.jsonl" 2>/dev/null || true)"
      echo "${AU} 15m trades settled: ${MSET:-0}"
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
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --combo --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --by-session --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
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
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --disagreements --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$RLOG" --exit --min-size 100 --min-conf "$CONF_BTC" --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
      echo
      "$PY" scripts/btc_executor_check.py --log "$RLOG" --trader-log "$TLOG" --floor "$CONF_BTC" --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
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
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --combo --min-size 100 --lead-hi "$(lead_hi_for "$A")" --lead-lo "$(lead_lo_for "$A")" --lead-max-price "$(lead_cap_for "$A")" || true
        echo
        echo "---- $AU 5m market: sessions ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --by-session --min-size 100 --lead-hi "$(lead_hi_for "$A")" --lead-lo "$(lead_lo_for "$A")" --lead-max-price "$(lead_cap_for "$A")" || true
        echo
        echo "---- $AU 5m market: confidence calibration ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --calibration || true
        echo
        echo "---- $AU 5m market: disagreement anatomy ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --disagreements --min-size 100 --lead-hi "$(lead_hi_for "$A")" --lead-lo "$(lead_lo_for "$A")" --lead-max-price "$(lead_cap_for "$A")" || true
        echo
        echo "---- $AU 5m market: exit policies ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --exit --min-size 100 --min-conf "$(conf_for "$A")" --lead-hi "$(lead_hi_for "$A")" --lead-lo "$(lead_lo_for "$A")" --lead-max-price "$(lead_cap_for "$A")" || true
        echo
        # The question a conf-threshold trader lives or dies on: by the time
        # confidence crosses, has the ask already priced the move? EV = winrate
        # at the crossing MINUS the price then.
        echo "---- $AU 5m market: confidence crossing (does conf beat the ask?) ----"
        "$PY" scripts/btc_entry_timing.py --log "$ARLOG" --crossing || true
        echo
        # The trader's ACTUAL entries under its per-asset rule, graded.
        ATLOG="out/live-$A.jsonl"
        if [ -f "$ATLOG" ]; then
          echo "---- $AU 5m market: trader timeline (rule=$(rule_for "$A")) ----"
          "$PY" scripts/btc_timeline_analyze.py --log "$ATLOG"            || true
          echo
        fi
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
    # 15m trader streams (no recorder yet — the paper trader's own timeline:
    # entries, settles, shadow-graded skips, confidence bands).
    for A in btc eth sol xrp; do
      MLOG="out/live-$A-15m.jsonl"
      [ -f "$MLOG" ] || continue
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      echo
      echo "---- $AU 15m market: trader timeline (paper stream) ----"
      MSET="$(grep -c '"type":"settle"' "$MLOG" 2>/dev/null || true)"
      MFLAT="$(grep -c '"reason":"flat_regime"' "$MLOG" 2>/dev/null || true)"
      echo "  settled: ${MSET:-0}   flat_regime skips: ${MFLAT:-0}"
      "$PY" scripts/btc_timeline_analyze.py --log "$MLOG"                 || true
    done
    echo; echo "######################## END ##########################"
    exit 0 ;;

  analyze-all)
    # Pool EVERY archived segment (each newrun timestamps its logs) with the
    # live one and run the decision tables on the FULL history — so "what did
    # all our testing say" is answered by one dataset, not by memory of
    # segment-by-segment reports. Entry-time/crossing/calibration measure the
    # market+model independent of trader config, so pooling across differently
    # configured segments is legitimate for them; the combo table is graded at
    # the CURRENT per-asset lead definition (printed in its header).
    echo; echo "########## ALL-SEGMENT TABLES (every archived newrun + current) ##########"
    ALLB="out/_pooled-trajectory.jsonl"
    cat out/trajectory-[0-9]*.jsonl out/trajectory.jsonl > "$ALLB" 2>/dev/null || true
    if [ -s "$ALLB" ]; then
      SEGS="$(ls out/trajectory-[0-9]*.jsonl 2>/dev/null | wc -l | tr -d ' ')"
      echo; echo "======== BTC: pooled recorder history ($((SEGS + 1)) segments) ========"
      echo "---- truth table (official labels, full history) ----"
      "$PY" scripts/btc_entry_timing.py --log "$ALLB" --official-only --min-size 100 || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLB" --combo --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLB" --combo --split-halves --min-size 100 --lead-hi "$(lead_hi_for btc)" --lead-lo "$(lead_lo_for btc)" --lead-max-price "$(lead_cap_for btc)" || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLB" --crossing || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLB" --calibration || true
    else
      echo "(no BTC recorder history found)"
    fi
    for A in eth sol xrp; do
      ALLA="out/_pooled-trajectory-$A.jsonl"
      cat out/trajectory-"$A"-[0-9]*.jsonl "out/trajectory-$A.jsonl" > "$ALLA" 2>/dev/null || true
      [ -s "$ALLA" ] || continue
      AU="$(echo "$A" | tr 'a-z' 'A-Z')"
      echo; echo "======== $AU: pooled recorder history ========"
      "$PY" scripts/btc_entry_timing.py --log "$ALLA" --official-only --min-size 100 || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLA" --combo --min-size 100 --lead-hi "$(lead_hi_for "$A")" --lead-lo "$(lead_lo_for "$A")" --lead-max-price "$(lead_cap_for "$A")" || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLA" --crossing || true
      echo
      "$PY" scripts/btc_entry_timing.py --log "$ALLA" --calibration || true
    done
    echo; echo "######################## END ##########################"
    exit 0 ;;

  sync)
    # Ship the lab's logs into the repo (gzipped, data/lab/) and push, so the
    # analysis side (a Claude session or any other machine) can run the reports
    # directly on the data instead of working from pasted terminal output.
    # Logs are paper-trading JSONL — market prices + simulated trades, no
    # credentials (.env is gitignored and never touched here).
    TS="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p data/lab
    n=0
    for f in out/*.jsonl; do
      [ -f "$f" ] || continue
      case "$(basename "$f")" in _*) continue ;; esac   # skip pooled temp files
      gzip -c "$f" > "data/lab/$(basename "$f").gz" && n=$((n + 1))
    done
    [ -f out/lab.conf ] && cp out/lab.conf data/lab/lab.conf
    if [ "$n" = "0" ]; then echo "no logs to sync"; exit 0; fi
    git add data/lab
    if git commit -m "lab data sync $TS ($n logs)"; then
      git push && echo "synced $n logs -> data/lab on branch $(git rev-parse --abbrev-ref HEAD)" \
        || { echo "commit made but push failed — retry with: git push"; exit 1; }
    else
      echo "nothing new to sync (logs unchanged since last sync)"
    fi
    exit 0 ;;

  start) ;;
  *) echo "usage: scripts/lab.sh [start|newrun|analyze|analyze-all|sync|dash|recdash|history|status|stop]"; exit 1 ;;
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
  echo "SAVED_LEAD_HI=$LEAD_HI"
  echo "SAVED_LEAD_LO=$LEAD_LO"
  echo "SAVED_LEAD_MAX_PRICE=$LEAD_MAX_PRICE"
  echo "SAVED_REGIME=$REGIME"
  echo "SAVED_CONF_BTC=$CONF_BTC"
  echo "SAVED_CONF_ETH=$CONF_ETH"
  echo "SAVED_CONF_SOL=$CONF_SOL"
  echo "SAVED_CONF_XRP=$CONF_XRP"
  for A in BTC ETH SOL XRP; do
    for K in LEAD_HI LEAD_LO LEAD_CAP RULE THRESH; do
      eval "echo \"SAVED_${K}_${A}=\$${K}_${A}\""
    done
  done
  echo "SAVED_ASSETS=\"$ASSETS\""
  echo "SAVED_M15=\"$M15\""
  echo "SAVED_STAKE=$STAKE"
  echo "SAVED_SIZING=$SIZING"
  echo "SAVED_STAKE_PCT=$STAKE_PCT"
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
  BHI="$(lead_hi_for btc)"; BLO="$(lead_lo_for btc)"; BCAP="$(lead_cap_for btc)"
  BRULE="$(rule_for btc)"; BTHR="$(thresh_for btc)"
  nohup "$PY" scripts/btc_live_paper.py --asset btc \
    --provider "$PROVIDER" --poll 2 --entry-threshold "$BTHR" \
    --entry-rule "$BRULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" --lead-max-price "$BCAP" \
    --lead-min-conf "$CONF_BTC" --lead-hi "$BHI" --lead-lo "$BLO" --regime-frac "$REGIME" \
    --entry-price-source polymarket --sizing "$SIZING" --stake-pct "$STAKE_PCT" --stake-usd "$STAKE" \
    --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
    --log "$TLOG" --quiet \
    >> out/nohup.log 2>&1 &
  if [ "$BRULE" = "edge" ]; then
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=edge (conf >= ask + ${EDGE_MARGIN})"
  elif [ "$BRULE" = "lead" ]; then
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=lead (leading side, ${BLO}-${BHI}s left, ask <= ${BCAP}, decisive move, conf >= ${CONF_BTC})"
  else
    echo "started BTC paper trader (pid $!)  flat \$${STAKE}/trade, real pricing, RULE=threshold (conf >= ${BTHR})"
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
    AF="$(conf_for "$A")"; AHI="$(lead_hi_for "$A")"; ALO="$(lead_lo_for "$A")"; ACAP="$(lead_cap_for "$A")"
    ARULE="$(rule_for "$A")"; ATHR="$(thresh_for "$A")"
    nohup "$PY" scripts/btc_live_paper.py --asset "$A" \
      --provider binance --poll 3 --entry-threshold "$ATHR" \
      --entry-rule "$ARULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" --lead-max-price "$ACAP" \
      --lead-min-conf "$AF" --lead-hi "$AHI" --lead-lo "$ALO" --regime-frac "$REGIME" \
      --entry-price-source polymarket --sizing "$SIZING" --stake-pct "$STAKE_PCT" --stake-usd "$STAKE" \
      --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
      --log "out/live-$A.jsonl" --quiet \
      >> "out/nohup-$A.log" 2>&1 &
    if [ "$ARULE" = "threshold" ]; then
      echo "started $AU paper trader (pid $!)  own \$${BANKROLL} account, RULE=threshold (conf >= ${ATHR})"
    else
      echo "started $AU paper trader (pid $!)  own \$${BANKROLL} account, RULE=${ARULE}, conf >= ${AF}, lead ${ALO}-${AHI}s ask <= ${ACAP} (move auto-scales to $AU)"
    fi
  fi
done

# --- 15m markets: one trader per asset in M15 (registry analysis: the 15m
# --- cohort shows ~50% higher alpha per trade and is less latency-crowded) ---
# Passing --lead-hi/--lead-lo explicitly overrides the trader's own x3 window
# scaling, so scale the PER-ASSET window here (awk: values may be non-integer).
for A in $M15; do
  AU="$(echo "$A" | tr 'a-z' 'A-Z')"
  if m15trader_up "$A"; then
    echo "$AU 15m trader already running"
  else
    [ -f "out/live-$A-15m.jsonl" ] && mv "out/live-$A-15m.jsonl" "out/live-$A-15m-prev.jsonl" && echo "archived prior $AU 15m session -> out/live-$A-15m-prev.jsonl"
    AF="$(conf_for "$A")"; ACAP="$(lead_cap_for "$A")"
    ARULE="$(rule_for "$A")"; ATHR="$(thresh_for "$A")"
    AHI15="$(awk "BEGIN{print $(lead_hi_for "$A")*3}")"
    ALO15="$(awk "BEGIN{print $(lead_lo_for "$A")*3}")"
    nohup "$PY" scripts/btc_live_paper.py --asset "$A" --window 15m \
      --provider binance --poll 3 --entry-threshold "$ATHR" \
      --entry-rule "$ARULE" --edge-margin "$EDGE_MARGIN" --max-entry-price "$MAX_PRICE" --lead-max-price "$ACAP" \
      --lead-min-conf "$AF" --lead-hi "$AHI15" --lead-lo "$ALO15" --regime-frac "$REGIME" \
      --entry-price-source polymarket --sizing "$SIZING" --stake-pct "$STAKE_PCT" --stake-usd "$STAKE" \
      --big-mult 1.0 --confluence 0.0 --bankroll "$BANKROLL" \
      --log "out/live-$A-15m.jsonl" --quiet \
      >> "out/nohup-$A-15m.log" 2>&1 &
    echo "started $AU 15m paper trader (pid $!)  own \$${BANKROLL} account, conf >= ${AF}, lead ${ALO15}-${AHI15}s ask <= ${ACAP} (x3 for 900s rounds)"
  fi
done

echo
echo "opening dashboard — Ctrl-C exits the dashboard; everything keeps running."
echo "later:  scripts/lab.sh analyze   (full report)   scripts/lab.sh stop"
sleep 1
exec "$PY" scripts/btc_live_monitor.py --log "$TLOG" $MERGEOPTS --bankroll "$BANKROLL" --entry-threshold "$THRESHOLD" --entry-rule "$RULE" --edge-margin "$EDGE_MARGIN"
