#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MULTI_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_ROOT="$(cd "$MULTI_ROOT/.." && pwd)"
RUNTIME_DIR="$MULTI_ROOT/runtime"
WORKSPACE_ROOT="$(cd "$SKILL_ROOT/../.." && pwd 2>/dev/null || cd "$SKILL_ROOT/.." && pwd)"
REPO_DEFAULT="$WORKSPACE_ROOT/pm-hl-conservative-plus-repo"
REPO="${BTCMULTI_REPO:-${BTC5M_REPO:-$REPO_DEFAULT}}"
ENV_FILE="${BTCMULTI_ENV_FILE:-${BTC5M_ENV_FILE:-$REPO/.env}}"
CONFIG_DEFAULT="$MULTI_ROOT/config/multi_profiles.yaml"

# Resolution order: explicit override > dedicated multi/.venv (paper mode,
# no external repo needed) > trading repo venv (has py_clob_client, for live
# mode) > bare python3.
if [[ -n "${BTCMULTI_PYTHON:-}" ]]; then
  PY="$BTCMULTI_PYTHON"
elif [[ -x "$MULTI_ROOT/.venv/bin/python" ]]; then
  PY="$MULTI_ROOT/.venv/bin/python"
elif [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
else
  PY="python3"
fi

check_deps() {
  if ! "$PY" -c "import requests, yaml" >/dev/null 2>&1; then
    cat >&2 <<EOF
Missing Python deps (requests, pyyaml) for interpreter: $PY

Fix — set up a dedicated venv for this bot (recommended, no external repo needed):
  python3 -m venv "$MULTI_ROOT/.venv"
  "$MULTI_ROOT/.venv/bin/pip" install -r "$MULTI_ROOT/requirements.txt"
  # then re-run this command (the .venv above is auto-detected)

Or point at an interpreter that already has them:
  export BTCMULTI_PYTHON=/path/to/python
EOF
    exit 1
  fi
}

PIDFILE="$RUNTIME_DIR/orchestrator.pid"
METAFILE="$RUNTIME_DIR/orchestrator.meta.json"
ORCH_LOG_LINK="$RUNTIME_DIR/orchestrator.latest.log"

mkdir -p "$RUNTIME_DIR"

# optional multi-local env (Chainlink Data Streams creds etc.) — gitignored.
# Parse-check first: a stray character in .env must degrade to a warning,
# never abort every ctl command.
if [[ -f "$MULTI_ROOT/.env" ]]; then
  if bash -n "$MULTI_ROOT/.env" 2>/dev/null; then
    set -a
    # shellcheck disable=SC1091
    source "$MULTI_ROOT/.env"
    set +a
  else
    echo "warning: multi/.env has a shell syntax error — ignoring it" >&2
  fi
fi

usage() {
  cat <<'EOF'
Usage:
  multibot_ctl.sh start [--mode paper|live] [--config PATH]
  multibot_ctl.sh stop
  multibot_ctl.sh status
  multibot_ctl.sh report [--limit N] [--mode paper|live]
  multibot_ctl.sh logs [asset_tf]        e.g. logs btc_15m (default: orchestrator)
  multibot_ctl.sh probe [--assets a,b] [--timeframes 5m,15m,...]
  multibot_ctl.sh dashboard [--port N]   local web dashboard (default :8787)
  multibot_ctl.sh dashboard stop
  multibot_ctl.sh analyze --wallet 0x... [--dump raw.json]   profile another
                  account's Up/Down strategy + match against our logs
  multibot_ctl.sh settle [--dry-run]     backfill reports stuck at
                  settle_unresolved with the real outcome/PnL
  multibot_ctl.sh cohort --csv FILE      batch-scan a wallet leaderboard csv,
                  classify species, compare vs our paper results
  multibot_ctl.sh streams                verify Chainlink Data Streams
                  credentials (multi/.env) and discover feed IDs
  multibot_ctl.sh golive TF STRATEGY [--stake-usd N]   run ONE live worker
                  (real orders, og .env auth) alongside the paper fleet
  multibot_ctl.sh golive stop
  multibot_ctl.sh archive                move reports+logs to a timestamped
                  archive: dashboards/reports restart from a clean epoch
  multibot_ctl.sh limitless probe        map the Limitless.exchange API (run
                  this FIRST and paste the output back)
  multibot_ctl.sh limitless start|stop|status|summary|logs
                  paper arb monitor with fill-echo: Limitless books + CROSS-
                  VENUE vs Polymarket (same slots, same Chainlink oracle)
  multibot_ctl.sh launchd install|uninstall|status
                  supervise orchestrator+dashboard+limitless with a macOS
                  LaunchAgent per service: auto-restarts on crash AND after
                  login/reboot. Stops any manually-started copies first.
                  ALWAYS paper mode — live is never auto-supervised.

Notes:
- Separate contour from the og 5m bot: runtime lives in multi/runtime.
- Default mode is paper (simulated fills, no auth needed).
- live mode sources auth env from pm-hl-conservative-plus-repo/.env
  (override: BTCMULTI_REPO / BTCMULTI_ENV_FILE / BTCMULTI_PYTHON).
EOF
}

is_running() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
  else
    return 1
  fi
}

cmd_start() {
  local mode="paper"
  local config="$CONFIG_DEFAULT"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode) mode="$2"; shift 2;;
      --config) config="$2"; shift 2;;
      *) echo "Unknown arg: $1"; usage; exit 2;;
    esac
  done

  if [[ "$mode" != "paper" && "$mode" != "live" ]]; then
    echo "invalid --mode: $mode (paper|live)"; exit 2
  fi

  check_deps

  if is_running; then
    echo "already_running pid=$(cat "$PIDFILE")"
    return 0
  fi

  local ts log
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUNTIME_DIR/orchestrator_${mode}_${ts}.log"

  (
    if [[ "$mode" == "live" && -f "$ENV_FILE" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$ENV_FILE"
      set +a
    fi
    nohup "$PY" "$SCRIPT_DIR/orchestrator.py" \
      --mode "$mode" --config "$config" --repo "$REPO" --runtime-dir "$RUNTIME_DIR" \
      >"$log" 2>&1 &
    echo $! >"$PIDFILE"
  )

  ln -sfn "$log" "$ORCH_LOG_LINK"
  local pid
  pid="$(cat "$PIDFILE")"

  cat >"$METAFILE" <<JSON
{
  "startedAt": "$(date -u +%FT%TZ)",
  "pid": $pid,
  "mode": "$mode",
  "config": "$config",
  "log": "$log",
  "repo": "$REPO"
}
JSON

  sleep 1
  if ps -p "$pid" >/dev/null 2>&1; then
    echo "started pid=$pid mode=$mode log=$log"
  else
    echo "failed_to_start (check $log)"
    exit 1
  fi
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(cat "$PIDFILE")"
    echo "orchestrator running pid=$pid"
    ps -p "$pid" -o pid=,etime=,command= || true
  else
    echo "orchestrator stopped"
  fi
  if [[ -f "$RUNTIME_DIR/workers.json" ]]; then
    echo "--- workers ---"
    cat "$RUNTIME_DIR/workers.json"
  fi
  [[ -f "$METAFILE" ]] && echo "meta=$METAFILE"
}

cmd_stop() {
  if ! is_running; then
    echo "already_stopped"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" || true
  # orchestrator forwards SIGTERM to workers and waits up to 30s
  for _ in $(seq 1 35); do
    ps -p "$pid" >/dev/null 2>&1 || break
    sleep 1
  done
  if ps -p "$pid" >/dev/null 2>&1; then
    kill -9 "$pid" || true
  fi
  rm -f "$PIDFILE"
  echo "stopped pid=$pid"
}

cmd_report() {
  "$PY" "$SCRIPT_DIR/multi_report.py" --reports-dir "$RUNTIME_DIR/reports" "$@"
}

cmd_logs() {
  local target="${1:-}"
  if [[ -z "$target" ]]; then
    if [[ -L "$ORCH_LOG_LINK" ]]; then
      tail -n 120 "$(readlink "$ORCH_LOG_LINK")"
    else
      echo "no_logs"
    fi
  else
    local f="$RUNTIME_DIR/logs/${target}.log"
    if [[ -f "$f" ]]; then
      tail -n 120 "$f"
    else
      echo "no such worker log: $f"
      ls "$RUNTIME_DIR/logs/" 2>/dev/null || true
      exit 1
    fi
  fi
}

cmd_probe() {
  check_deps
  "$PY" "$SCRIPT_DIR/probe_markets.py" --config "$CONFIG_DEFAULT" "$@"
}

LIVE_PIDFILE="$RUNTIME_DIR/live_worker.pid"

live_running() {
  if [[ -f "$LIVE_PIDFILE" ]]; then
    local pid
    pid="$(cat "$LIVE_PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
  else
    return 1
  fi
}

cmd_golive() {
  if [[ "${1:-}" == "stop" ]]; then
    if live_running; then
      kill "$(cat "$LIVE_PIDFILE")" || true
      sleep 2
      if live_running; then kill -9 "$(cat "$LIVE_PIDFILE")" || true; fi
      rm -f "$LIVE_PIDFILE" "$RUNTIME_DIR/live_worker.meta.json"
      echo "live worker stopped"
    else
      rm -f "$LIVE_PIDFILE" "$RUNTIME_DIR/live_worker.meta.json"
      echo "live worker already_stopped"
    fi
    return 0
  fi
  local tf="${1:-}" strat="${2:-}"
  if [[ -z "$tf" || -z "$strat" ]]; then
    echo "Usage: multibot_ctl.sh golive TF STRATEGY [--stake-usd N]"
    echo "   eg: multibot_ctl.sh golive 15m impulse --stake-usd 2"
    exit 2
  fi
  shift 2
  local -a extra=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --stake-usd) extra+=("--stake-usd" "$2"); shift 2;;
      *) echo "Unknown arg: $1"; exit 2;;
    esac
  done
  if live_running; then
    echo "live worker already_running pid=$(cat "$LIVE_PIDFILE") — golive stop first"
    return 0
  fi
  if [[ ! -x "$REPO/.venv/bin/python" ]]; then
    echo "live mode needs the trading repo venv at $REPO (BTCMULTI_REPO to override)"
    exit 1
  fi
  check_deps
  local ts log
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUNTIME_DIR/logs/live_${tf}_${strat}.log"
  mkdir -p "$RUNTIME_DIR/logs"
  (
    if [[ -f "$ENV_FILE" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$ENV_FILE"
      set +a
    fi
    nohup "$PY" "$SCRIPT_DIR/multi_worker.py" \
      --asset btc --timeframe "$tf" --strategy "$strat" --mode live \
      --config "$CONFIG_DEFAULT" --repo "$REPO" --runtime-dir "$RUNTIME_DIR" \
      "${extra[@]}" >>"$log" 2>&1 &
    echo $! >"$LIVE_PIDFILE"
  )
  sleep 1
  if live_running; then
    cat >"$RUNTIME_DIR/live_worker.meta.json" <<JSON
{
  "pid": $(cat "$LIVE_PIDFILE"),
  "worker": "btc_${tf}_${strat}",
  "startedAt": "$(date -u +%FT%TZ)",
  "log": "$log"
}
JSON
    echo "LIVE worker started: $strat on $tf (pid=$(cat "$LIVE_PIDFILE"))"
    echo "  log:       $log"
    echo "  dashboard: switch the mode filter to 'live' to watch it"
    echo "  caps:      live guard (3 pos / \$15 exposure / \$10 daily) is active"
    if [[ "$strat" == "impulse" || "$strat" == "underdog" ]]; then
      echo "  NOTE: $strat holds to resolution — WINNING shares must be claimed"
      echo "  on Polymarket (positions page) until auto-redeem is built."
    fi
  else
    echo "live worker failed_to_start (check $log)"
    exit 1
  fi
}
cmd_archive() {
  if is_running || live_running; then
    echo "stop everything first: multibot_ctl.sh stop && multibot_ctl.sh golive stop"
    exit 1
  fi
  local ts dest moved=0
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="$RUNTIME_DIR/archive_$ts"
  mkdir -p "$dest"
  local d
  for d in reports logs; do
    if [[ -d "$RUNTIME_DIR/$d" ]]; then
      mv "$RUNTIME_DIR/$d" "$dest/$d"
      moved=1
    fi
  done
  if [[ "$moved" == 1 ]]; then
    echo "archived -> $dest"
    echo "dashboard + report now start from a clean epoch on next start."
    echo "old data stays queryable:  multibot_ctl.sh report --reports-dir $dest/reports"
  else
    rmdir "$dest" 2>/dev/null || true
    echo "nothing to archive"
  fi
  # a clean paper epoch includes the day counter — reset the PAPER guard only
  # (the live guard's daily loss memory is never touched by tooling)
  if [[ -f "$RUNTIME_DIR/portfolio_paper.json" ]]; then
    "$PY" - "$RUNTIME_DIR/portfolio_paper.json" <<'PYEOF'
import datetime, json, sys
p = sys.argv[1]
try:
    s = json.load(open(p))
except Exception:
    sys.exit(0)
s["daily"] = {"date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
              "realized_pnl_usd": 0.0, "trades": 0}
s["open_positions"] = {}
json.dump(s, open(p, "w"), indent=2)
print("paper guard daily counter reset for the new epoch")
PYEOF
  fi
}

LTL_PIDFILE="$RUNTIME_DIR/limitless.pid"

ltl_running() {
  [[ -f "$LTL_PIDFILE" ]] && ps -p "$(cat "$LTL_PIDFILE" 2>/dev/null)" >/dev/null 2>&1
}

cmd_limitless() {
  local sub="${1:-probe}"
  shift || true
  local log="$RUNTIME_DIR/limitless.log"
  case "$sub" in
    probe)
      check_deps
      "$PY" "$SCRIPT_DIR/limitless_probe.py" "$@"
      ;;
    start)
      check_deps
      if ltl_running; then
        echo "limitless monitor already_running pid=$(cat "$LTL_PIDFILE")"
        return 0
      fi
      mkdir -p "$RUNTIME_DIR"
      nohup "$PY" "$SCRIPT_DIR/limitless_arb.py" --runtime-dir "$RUNTIME_DIR" "$@" \
        >>"$log" 2>&1 &
      echo $! >"$LTL_PIDFILE"
      sleep 1
      if ltl_running; then
        echo "limitless arb monitor started pid=$(cat "$LTL_PIDFILE")"
        echo "  log:     $log"
        echo "  summary: multi/scripts/multibot_ctl.sh limitless summary"
      else
        echo "limitless monitor failed_to_start (check $log)"
        exit 1
      fi
      ;;
    stop)
      if ltl_running; then
        kill "$(cat "$LTL_PIDFILE")" || true
        sleep 1
        if ltl_running; then kill -9 "$(cat "$LTL_PIDFILE")" || true; fi
        rm -f "$LTL_PIDFILE"
        echo "limitless monitor stopped"
      else
        rm -f "$LTL_PIDFILE"
        echo "limitless monitor already_stopped"
      fi
      ;;
    status)
      if ltl_running; then
        echo "limitless monitor running pid=$(cat "$LTL_PIDFILE")"
      else
        echo "limitless monitor stopped"
      fi
      ;;
    summary)
      check_deps
      "$PY" "$SCRIPT_DIR/limitless_arb.py" --runtime-dir "$RUNTIME_DIR" --summary
      ;;
    logs)
      tail -n "${1:-50}" "$log"
      ;;
    *)
      echo "Unknown limitless subcommand: $sub"
      usage
      exit 2
      ;;
  esac
}

DASH_PIDFILE="$RUNTIME_DIR/dashboard.pid"

dash_running() {
  if [[ -f "$DASH_PIDFILE" ]]; then
    local pid
    pid="$(cat "$DASH_PIDFILE" 2>/dev/null || true)"
    [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
  else
    return 1
  fi
}

cmd_dashboard() {
  local sub="${1:-start}"
  if [[ "$sub" == "stop" ]]; then
    if dash_running; then
      kill "$(cat "$DASH_PIDFILE")" || true
      rm -f "$DASH_PIDFILE"
      echo "dashboard stopped"
    else
      rm -f "$DASH_PIDFILE"
      echo "dashboard already_stopped"
    fi
    return 0
  fi
  [[ "$sub" == "start" ]] && shift || true
  local port="8787"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port) port="$2"; shift 2;;
      *) echo "Unknown arg: $1"; usage; exit 2;;
    esac
  done
  if dash_running; then
    echo "dashboard already_running pid=$(cat "$DASH_PIDFILE") url=http://127.0.0.1:$port"
    return 0
  fi
  local log="$RUNTIME_DIR/dashboard.log"
  # dashboard.py is stdlib-only; any python3 works
  nohup "$PY" "$SCRIPT_DIR/dashboard.py" --port "$port" --runtime-dir "$RUNTIME_DIR" \
    >"$log" 2>&1 &
  echo $! >"$DASH_PIDFILE"
  sleep 1
  if dash_running; then
    echo "dashboard started pid=$(cat "$DASH_PIDFILE") -> http://127.0.0.1:$port"
  else
    echo "dashboard failed_to_start (check $log)"
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# launchd supervision (macOS): one LaunchAgent per service, KeepAlive=true so
# the OS relaunches it on crash, RunAtLoad=true so it comes back after a
# reboot/logout too — the two failure modes that killed the manual nohup
# processes before. Each plist calls back into THIS script's `run-fg`
# subcommand (not the python files directly) so it inherits the same PY/
# config/runtime-dir resolution and the multi/.env sourcing guard already at
# the top of this file, with one path per service instead of three.
#
# ALWAYS paper mode, hardcoded in run-fg — not read from multi_profiles.yaml's
# mode_default. A launchd job restarts unattended after a crash; if the
# config's mode_default were ever flipped to live for a test and forgotten,
# an unattended auto-restart placing real orders with nobody watching is
# exactly the failure this project has been careful to avoid. `golive` (run
# by hand, one arm at a time) is the only path to real money — deliberately
# not wired into launchd.
# ---------------------------------------------------------------------------

cmd_run_fg() {
  local svc="${1:-}"
  case "$svc" in
    orchestrator)
      check_deps
      exec "$PY" "$SCRIPT_DIR/orchestrator.py" \
        --mode paper --config "$CONFIG_DEFAULT" --repo "$REPO" --runtime-dir "$RUNTIME_DIR"
      ;;
    dashboard)
      exec "$PY" "$SCRIPT_DIR/dashboard.py" \
        --port "${BTCMULTI_DASH_PORT:-8787}" --runtime-dir "$RUNTIME_DIR"
      ;;
    limitless)
      check_deps
      exec "$PY" "$SCRIPT_DIR/limitless_arb.py" --runtime-dir "$RUNTIME_DIR"
      ;;
    *)
      echo "Unknown run-fg target: $svc (orchestrator|dashboard|limitless)" >&2
      exit 2
      ;;
  esac
}

LAUNCHD_SERVICES=(orchestrator dashboard limitless)
LAUNCHD_PREFIX="com.btcmulti"

launchd_label() { echo "${LAUNCHD_PREFIX}.$1"; }
launchd_plist_path() { echo "$HOME/Library/LaunchAgents/$(launchd_label "$1").plist"; }

write_plist() {
  local svc="$1" label plist out err extra=""
  label="$(launchd_label "$svc")"
  plist="$(launchd_plist_path "$svc")"
  mkdir -p "$RUNTIME_DIR/launchd" "$(dirname "$plist")"
  out="$RUNTIME_DIR/launchd/${svc}.out.log"
  err="$RUNTIME_DIR/launchd/${svc}.err.log"
  # orchestrator forwards SIGTERM to its workers and waits up to 30s for a
  # clean settle before giving up (see cmd_stop); launchd's default
  # ExitTimeOut (20s) would SIGKILL it mid-shutdown, so it gets more room.
  if [[ "$svc" == "orchestrator" ]]; then
    extra="  <key>ExitTimeOut</key>
  <integer>40</integer>
"
  fi
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SCRIPT_DIR/multibot_ctl.sh</string>
    <string>run-fg</string>
    <string>$svc</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$MULTI_ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>15</integer>
$extra  <key>StandardOutPath</key>
  <string>$out</string>
  <key>StandardErrorPath</key>
  <string>$err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLIST
}

cmd_launchd() {
  local sub="${1:-status}"
  local uid; uid="$(id -u)"
  case "$sub" in
    install)
      echo "stopping any manually-started copies first (avoids duplicate orchestrators/ports)..."
      cmd_stop || true
      cmd_dashboard stop || true
      cmd_limitless stop || true
      echo
      local errlog; errlog="$(mktemp)"
      for svc in "${LAUNCHD_SERVICES[@]}"; do
        local label plist
        label="$(launchd_label "$svc")"
        plist="$(launchd_plist_path "$svc")"
        write_plist "$svc"
        launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
        if launchctl bootstrap "gui/$uid" "$plist" 2>"$errlog"; then
          echo "installed+started $label"
        else
          echo "FAILED to bootstrap $label:"
          sed 's/^/    /' "$errlog"
          echo "  plist: $plist"
        fi
      done
      rm -f "$errlog"
      echo
      echo "these now auto-restart on crash AND on login/reboot (paper mode only)."
      echo "check:      multibot_ctl.sh launchd status"
      echo "logs:       $RUNTIME_DIR/launchd/<service>.{out,err}.log"
      echo "dashboard:  http://127.0.0.1:${BTCMULTI_DASH_PORT:-8787}"
      echo "remove:     multibot_ctl.sh launchd uninstall"
      ;;
    uninstall)
      for svc in "${LAUNCHD_SERVICES[@]}"; do
        local label plist
        label="$(launchd_label "$svc")"
        plist="$(launchd_plist_path "$svc")"
        launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
        rm -f "$plist"
        echo "removed $label"
      done
      echo "(the services stopped; nothing auto-restarts anymore — use"
      echo " start/dashboard/limitless start to run them manually again)"
      ;;
    status)
      for svc in "${LAUNCHD_SERVICES[@]}"; do
        local label
        label="$(launchd_label "$svc")"
        if launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
          echo "$svc: loaded ($label)"
          launchctl print "gui/$uid/$label" 2>/dev/null \
            | grep -E "state = |pid = " | sed 's/^/    /'
        else
          echo "$svc: NOT loaded ($label)"
        fi
      done
      ;;
    *)
      echo "Usage: multibot_ctl.sh launchd install|uninstall|status"
      exit 2
      ;;
  esac
}

main() {
  local cmd="${1:-}"
  [[ -z "$cmd" ]] && { usage; exit 2; }
  shift || true
  case "$cmd" in
    start) cmd_start "$@" ;;
    status) cmd_status ;;
    stop) cmd_stop ;;
    report) cmd_report "$@" ;;
    logs) cmd_logs "$@" ;;
    probe) cmd_probe "$@" ;;
    dashboard) cmd_dashboard "$@" ;;
    analyze) check_deps; "$PY" "$SCRIPT_DIR/analyze_wallet.py" "$@" ;;
    settle) check_deps; "$PY" "$SCRIPT_DIR/settle_backfill.py" --reports-dir "$RUNTIME_DIR/reports" "$@" ;;
    cohort) check_deps; "$PY" "$SCRIPT_DIR/cohort_scan.py" --reports-dir "$RUNTIME_DIR/reports" "$@" ;;
    streams) check_deps; "$PY" "$SCRIPT_DIR/streams_probe.py" "$@" ;;
    golive) cmd_golive "$@" ;;
    archive) cmd_archive ;;
    limitless) cmd_limitless "$@" ;;
    launchd) cmd_launchd "$@" ;;
    run-fg) cmd_run_fg "$@" ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
