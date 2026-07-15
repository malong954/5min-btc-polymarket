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
    *) usage; exit 2 ;;
  esac
}

main "$@"
