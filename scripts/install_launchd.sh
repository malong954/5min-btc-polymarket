#!/usr/bin/env bash
#
# Install / remove a macOS launchd agent that runs the live paper trader 24/7.
# It auto-detects this repo and its .venv, restarts on crash, and starts again
# after a reboot/login. The JSONL stream keeps flowing to out/live.jsonl.
#
# Usage:
#   scripts/install_launchd.sh install     # write plist + load it (default)
#   scripts/install_launchd.sh status      # show whether it's running
#   scripts/install_launchd.sh uninstall   # stop + remove it
#   scripts/install_launchd.sh print       # print the plist (no changes)
#
# Tune via env vars before running install:
#   PROVIDER=binance|cryptocompare  THRESHOLD=0.60  PRICE=0.85  POLL=2
#   STAKE=10  SIZING=flat|confidence|kelly  BANKROLL=100
#   e.g.  STAKE=15 SIZING=confidence scripts/install_launchd.sh install
#
set -euo pipefail

LABEL="com.btc5m.livepaper"
CMD="${1:-install}"

# Resolve repo root = parent of this script's dir (works from anywhere).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$REPO/.venv/bin/python3"
[ -x "$PY" ] || PY="$REPO/.venv/bin/python"

PROVIDER="${PROVIDER:-binance}"
THRESHOLD="${THRESHOLD:-0.60}"
PRICE="${PRICE:-0.85}"
POLL="${POLL:-2}"
STAKE="${STAKE:-10}"
SIZING="${SIZING:-flat}"
BANKROLL="${BANKROLL:-100}"
PRICE_SOURCE="${PRICE_SOURCE:-polymarket}"  # polymarket (real ask) | fixed

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"

gen_plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$REPO/scripts/btc_live_paper.py</string>
        <string>--provider</string><string>$PROVIDER</string>
        <string>--poll</string><string>$POLL</string>
        <string>--entry-threshold</string><string>$THRESHOLD</string>
        <string>--entry-price</string><string>$PRICE</string>
        <string>--stake-usd</string><string>$STAKE</string>
        <string>--sizing</string><string>$SIZING</string>
        <string>--bankroll</string><string>$BANKROLL</string>
        <string>--entry-price-source</string><string>$PRICE_SOURCE</string>
        <string>--log</string><string>$REPO/out/live.jsonl</string>
        <string>--quiet</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$REPO/out/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO/out/launchd.err.log</string>
</dict>
</plist>
EOF
}

case "$CMD" in
  print)
    gen_plist
    ;;

  install)
    [ -x "$PY" ] || { echo "error: venv python not found at $REPO/.venv — create it first:"; \
                      echo "  python3 -m venv .venv && source .venv/bin/activate && pip install requests"; exit 1; }
    mkdir -p "$PLIST_DIR" "$REPO/out"
    gen_plist > "$PLIST"
    echo "wrote $PLIST"
    uid="$(id -u)"
    # Clear any prior copy (both modern and legacy).
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    # Try modern bootstrap; fall back to legacy load -w (more tolerant, e.g. when
    # HOME is on an external volume where bootstrap throws EIO); then to nohup.
    if launchctl bootstrap "gui/$uid" "$PLIST" 2>/dev/null; then
      launchctl enable "gui/$uid/$LABEL" 2>/dev/null || true
      launchctl kickstart -k "gui/$uid/$LABEL" 2>/dev/null || true
      echo "loaded and started via launchctl bootstrap: $LABEL"
    elif launchctl load -w "$PLIST" 2>/dev/null; then
      echo "loaded and started via legacy launchctl load: $LABEL"
    else
      echo ""
      echo "launchctl could not load the agent (common when HOME is on an external"
      echo "volume like /Volumes/MASTER). Starting it directly in the background instead:"
      mkdir -p "$REPO/out"
      nohup "$PY" "$REPO/scripts/btc_live_paper.py" \
        --provider "$PROVIDER" --poll "$POLL" --entry-threshold "$THRESHOLD" \
        --entry-price "$PRICE" --stake-usd "$STAKE" --sizing "$SIZING" \
        --bankroll "$BANKROLL" --entry-price-source "$PRICE_SOURCE" \
        --log "$REPO/out/live.jsonl" --quiet \
        >> "$REPO/out/nohup.log" 2>&1 &
      echo "started with nohup (pid $!). Note: nohup does NOT survive a reboot."
      echo "  to add reboot-survival, add a crontab @reboot line (see 'run' below)."
    fi
    echo "  stream : tail -f $REPO/out/live.jsonl"
    echo "  status : scripts/install_launchd.sh status"
    echo "  stop   : scripts/install_launchd.sh uninstall"
    ;;

  run)
    # Dependency-free always-on: run in the foreground background via nohup.
    [ -x "$PY" ] || { echo "error: venv python not found at $REPO/.venv"; exit 1; }
    mkdir -p "$REPO/out"
    nohup "$PY" "$REPO/scripts/btc_live_paper.py" \
      --provider "$PROVIDER" --poll "$POLL" --entry-threshold "$THRESHOLD" \
      --entry-price "$PRICE" --stake-usd "$STAKE" --sizing "$SIZING" \
      --bankroll "$BANKROLL" --entry-price-source "$PRICE_SOURCE" \
      --log "$REPO/out/live.jsonl" --quiet \
      >> "$REPO/out/nohup.log" 2>&1 &
    echo "started (pid $!). stream: tail -f $REPO/out/live.jsonl ; stop: scripts/install_launchd.sh uninstall"
    ;;

  status)
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      echo "RUNNING (launchd): $LABEL"
      launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =|last exit" || true
    elif pgrep -f "btc_live_paper.py" >/dev/null 2>&1; then
      echo "RUNNING (nohup): pids $(pgrep -f btc_live_paper.py | tr '\n' ' ')"
    else
      echo "NOT running: $LABEL"
    fi
    ;;

  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    pkill -f "btc_live_paper.py" 2>/dev/null || true
    rm -f "$PLIST"
    echo "stopped and removed: $LABEL (launchd + any nohup process)"
    ;;

  *)
    echo "usage: scripts/install_launchd.sh [install|run|status|uninstall|print]"; exit 1
    ;;
esac
