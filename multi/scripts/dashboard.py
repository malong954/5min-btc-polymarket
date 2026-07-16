#!/usr/bin/env python3
"""
Local dashboard for the multi-timeframe bot.

Stdlib-only HTTP server that reads multi/runtime (reports, workers.json,
portfolio state) and serves a single-page dashboard: KPI tiles, cumulative
PnL per timeframe, PnL-by-timeframe comparison, worker health, open
positions, and recent trades. Auto-refreshes every 5 seconds.

Run via: multi/scripts/multibot_ctl.sh dashboard   (default port 8787)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

UTC = dt.timezone.utc
TF_ORDER = ["5m", "15m", "1h", "4h", "1d"]
ARB_THRESHOLD_DEFAULT = 0.99  # buy both sides when asks sum <= this

_cache_lock = threading.Lock()
_report_cache: dict[str, tuple[float, dict]] = {}


def default_runtime_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime")


def parse_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def scan_reports(runtime: Path) -> list[dict]:
    rdir = runtime / "reports"
    if not rdir.exists():
        return []
    out = []
    with _cache_lock:
        for p in rdir.glob("*.json"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            key = str(p)
            hit = _report_cache.get(key)
            if hit and hit[0] == mtime:
                out.append(hit[1])
                continue
            obj = load_json(p)
            if isinstance(obj, dict):
                _report_cache[key] = (mtime, obj)
                out.append(obj)
    return out


def report_close_ts(r: dict) -> float | None:
    closed = r.get("closed") or {}
    return (parse_ts(closed.get("closed_at"))
            or parse_ts(r.get("finished_at"))
            or parse_ts(r.get("started_at")))


STRATEGY_ORDER = ["favorite", "underdog", "impulse", "arb"]


def pair_key(r: dict) -> str:
    k = f"{r.get('asset') or '?'} {r.get('timeframe') or '?'}"
    strategy = str(r.get("strategy") or "favorite")
    if strategy != "favorite":
        k += f" {strategy}"
    return k


def canonical_pairs(reports: list[dict]) -> list[str]:
    """Stable series order across filters: asset x TF_ORDER x strategy.
    Color follows the entity — derived from ALL reports, never the filtered
    subset, so filtering can't repaint surviving series."""
    assets = sorted({str(r.get("asset") or "?") for r in reports})
    seen = {pair_key(r) for r in reports}
    out = []
    for a in assets:
        for tf in TF_ORDER:
            for strat in STRATEGY_ORDER:
                k = f"{a} {tf}" + ("" if strat == "favorite" else f" {strat}")
                if k in seen:
                    out.append(k)
    for k in sorted(seen - set(out)):  # unknown tfs/strategies, stable tail
        out.append(k)
    return out


def range_cutoff(rng: str, now: float) -> float | None:
    if rng == "24h":
        return now - 86400
    if rng == "7d":
        return now - 7 * 86400
    if rng == "today":
        d = dt.datetime.fromtimestamp(now, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return d.timestamp()
    return None


def build_payload(runtime: Path, mode: str, rng: str) -> dict:
    now = time.time()
    all_reports = scan_reports(runtime)
    pairs_all = canonical_pairs(all_reports)

    cutoff = range_cutoff(rng, now)
    reports = []
    for r in all_reports:
        if str(r.get("mode") or "") != mode:
            continue
        t = report_close_ts(r)
        if t is None:
            continue
        if cutoff is not None and t < cutoff:
            continue
        reports.append((t, r))
    reports.sort(key=lambda x: x[0])

    groups: dict[str, dict] = {}
    trades = []
    series: dict[str, list[list[float]]] = {}
    cum: dict[str, float] = {}
    kpi = {"pnl_sum": 0.0, "wins": 0, "losses": 0, "flat": 0,
           "entries": 0, "slots": 0}

    for t, r in reports:
        k = pair_key(r)
        g = groups.setdefault(k, {"pair": k, "slots": 0, "entries": 0, "wins": 0,
                                  "losses": 0, "flat": 0, "pnl_sum": 0.0, "pnl_known": 0})
        g["slots"] += 1
        kpi["slots"] += 1
        opened = r.get("opened") or {}
        closed = r.get("closed") or {}
        if not opened:
            continue
        g["entries"] += 1
        kpi["entries"] += 1
        pnl = r.get("realized_cashflow_pnl_usdc")
        if isinstance(pnl, (int, float)):
            g["pnl_sum"] += pnl
            g["pnl_known"] += 1
            kpi["pnl_sum"] += pnl
            bucket = "wins" if pnl > 0 else ("losses" if pnl < 0 else "flat")
            g[bucket] += 1
            kpi[bucket] += 1
            cum[k] = cum.get(k, 0.0) + pnl
            series.setdefault(k, []).append([round(t * 1000), round(cum[k], 6)])
        trades.append({
            "t": round(t * 1000),
            "pair": k,
            "side": opened.get("side"),
            "slug": r.get("slug"),
            "entry": opened.get("entry_price"),
            "exit": closed.get("close_price") if closed.get("close_price") is not None
                    else (round(closed["close_usdc"] / closed["close_shares"], 4)
                          if closed.get("close_usdc") and closed.get("close_shares") else None),
            "reason": closed.get("close_reason"),
            "pnl": pnl if isinstance(pnl, (int, float)) else None,
        })

    decided = kpi["wins"] + kpi["losses"]
    kpi["win_rate"] = round(kpi["wins"] / decided, 4) if decided else None

    group_rows = []
    for k in pairs_all:
        if k not in groups:
            continue
        g = groups[k]
        d = g["wins"] + g["losses"]
        group_rows.append({
            **{x: g[x] for x in ("pair", "slots", "entries", "wins", "losses", "flat")},
            "entry_rate": round(g["entries"] / g["slots"], 3) if g["slots"] else None,
            "win_rate": round(g["wins"] / d, 3) if d else None,
            "pnl_sum": round(g["pnl_sum"], 6),
            "pnl_avg": round(g["pnl_sum"] / g["pnl_known"], 6) if g["pnl_known"] else None,
        })

    # runtime state
    orch_meta = load_json(runtime / "orchestrator.meta.json") or {}
    orch_pid = None
    try:
        orch_pid = int((runtime / "orchestrator.pid").read_text().strip())
    except Exception:
        pass
    workers_meta = load_json(runtime / "workers.json") or {}
    workers = []
    for wid, w in sorted((workers_meta.get("workers") or {}).items()):
        workers.append({"id": wid, "pid": w.get("pid"),
                        "alive": pid_alive(w.get("pid")),
                        "restarts": w.get("restarts"),
                        "started_at": w.get("started_at")})

    guard = load_json(runtime / f"portfolio_{mode}.json") or {}
    open_positions = []
    for wid, p in sorted((guard.get("open_positions") or {}).items()):
        open_positions.append({"worker": wid, "stake_usd": p.get("stake_usd"),
                               "reserved_at": p.get("reserved_at"),
                               "alive": pid_alive(p.get("pid"))})

    # arb progress: for the complete-set arb workers, entries are rare and the
    # deliverable is the near-miss signal — how close the two asks came to
    # summing < $1 each slot, even when no window opened. Aggregate that here.
    arb_slots = []
    for t, r in reports:  # (close_ts, report) tuples, already mode+range filtered
        if str(r.get("strategy") or "") != "arb":
            continue
        best = r.get("arb_best_combined")
        opened = r.get("opened") or {}
        pnl = r.get("realized_cashflow_pnl_usdc")
        arb_slots.append({
            "t": round(t * 1000),
            "pair": pair_key(r),
            "best": best if isinstance(best, (int, float)) else None,
            "ticks": int(r.get("arb_opportunity_ticks") or 0),
            "entered": bool(opened),
            "combined": opened.get("entry_price") if opened else None,
            "pnl": pnl if isinstance(pnl, (int, float)) else None,
        })
    arb_slots.sort(key=lambda s: s["t"])
    arb_thr = ARB_THRESHOLD_DEFAULT
    bests = [s["best"] for s in arb_slots if s["best"] is not None]
    entries = [s for s in arb_slots if s["entered"]]
    windows = [b for b in bests if b <= arb_thr]
    locked = sum(s["pnl"] for s in entries if isinstance(s["pnl"], (int, float)))
    arb = {
        "enabled": len(arb_slots) > 0,
        "threshold": arb_thr,
        "slots": len(arb_slots),
        "entries": len(entries),
        "locked_pnl_usdc": round(locked, 6),
        "best_ever": min(bests) if bests else None,
        "median_best": round(sorted(bests)[len(bests) // 2], 4) if bests else None,
        "windows_seen": len(windows),
        "window_rate": round(len(windows) / len(bests), 3) if bests else None,
        "recent": arb_slots[-80:],
    }

    return {
        "generated_at": dt.datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "range": rng,
        "orchestrator": {
            "running": bool(orch_pid and pid_alive(orch_pid)),
            "pid": orch_pid,
            "mode": orch_meta.get("mode"),
            "started_at": orch_meta.get("startedAt"),
        },
        "workers": workers,
        "open_positions": open_positions,
        "daily": guard.get("daily") or {},
        "caps": guard.get("caps") or {},
        "kpi": kpi,
        "groups": group_rows,
        "pairs_all": pairs_all,
        "series": series,
        "arb": arb,
        "trades": trades[-40:][::-1],
    }


# ---------------------------------------------------------------------------
# Page (palette + specs from the dataviz reference instance)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC Multi-Timeframe Bot</title>
<style>
:root{
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --baseline:#c3c2b7;
  --border:rgba(11,11,11,0.10);
  --good-text:#006300; --status-good:#0ca30c; --status-critical:#d03b3b;
  --pos:#2a78d6; --neg:#e34948;
  --s1:#2a78d6; --s2:#008300; --s3:#e87ba4; --s4:#eda100;
  --s5:#1baf7a; --s6:#eb6834; --s7:#4a3aa7; --s8:#e34948;
}
@media (prefers-color-scheme: dark){
  :root:where(:not([data-theme="light"])){
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --baseline:#383835;
    --border:rgba(255,255,255,0.10);
    --good-text:#0ca30c;
    --pos:#3987e5; --neg:#e66767;
    --s1:#3987e5; --s2:#008300; --s3:#d55181; --s4:#c98500;
    --s5:#199e70; --s6:#d95926; --s7:#9085e9; --s8:#e66767;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --baseline:#383835;
  --border:rgba(255,255,255,0.10);
  --good-text:#0ca30c;
  --pos:#3987e5; --neg:#e66767;
  --s1:#3987e5; --s2:#008300; --s3:#d55181; --s4:#c98500;
  --s5:#199e70; --s6:#d95926; --s7:#9085e9; --s8:#e66767;
}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:20px 20px 48px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
h1{font-size:18px;font-weight:600;margin:0}
.sub{color:var(--ink2);font-size:13px}
.chip{font-size:12px;border:1px solid var(--border);border-radius:999px;
  padding:2px 10px;color:var(--ink2);background:var(--surface)}
.chip .dot{margin-right:6px}
.live{margin-left:auto;color:var(--muted);font-size:12px}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 16px}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;
  overflow:hidden;background:var(--surface)}
.seg button{border:0;background:transparent;color:var(--ink2);padding:6px 12px;
  font:inherit;font-size:13px;cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--grid);color:var(--ink);font-weight:600}
select{font:inherit;font-size:13px;color:var(--ink);background:var(--surface);
  border:1px solid var(--border);border-radius:8px;padding:6px 8px}
.grid{display:grid;gap:14px}
.kpis{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;min-width:0}
.card h2{font-size:13px;font-weight:600;color:var(--ink2);margin:0 0 10px}
.tile .lbl{font-size:12px;color:var(--ink2)}
.tile .val{font-size:26px;font-weight:600;margin-top:2px}
.tile .sub2{font-size:12px;color:var(--muted);margin-top:2px}
.pos-t{color:var(--good-text)} .neg-t{color:var(--status-critical)}
.row2{grid-template-columns:2fr 1fr}
.row3{grid-template-columns:1fr 1fr}
@media (max-width:900px){.row2,.row3{grid-template-columns:1fr}}
main{display:grid;gap:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:500;text-align:right;padding:4px 8px;
  border-bottom:1px solid var(--grid);white-space:nowrap}
td{padding:5px 8px;text-align:right;border-bottom:1px solid var(--grid);
  font-variant-numeric:tabular-nums;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;
  vertical-align:middle;margin-right:7px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;vertical-align:baseline}
.status{display:flex;gap:8px;align-items:center;font-size:13px;padding:4px 0}
.status .nm{min-width:76px;font-variant-numeric:tabular-nums}
.status .meta{color:var(--muted);font-size:12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--ink2);margin-top:8px}
.legend span{display:inline-flex;align-items:center}
.empty{color:var(--muted);font-size:13px;padding:22px 0;text-align:center}
.chart-wrap{position:relative}
.tip{position:absolute;pointer-events:none;background:var(--surface);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12px;
  box-shadow:0 4px 14px rgba(0,0,0,.12);display:none;z-index:5;min-width:130px}
.tip .t{color:var(--muted);margin-bottom:4px}
.tip .r{display:flex;align-items:center;gap:7px;padding:1px 0}
.tip .v{font-weight:600;margin-left:auto;font-variant-numeric:tabular-nums}
.stale main{opacity:.6}
svg text{fill:var(--muted);font:11px system-ui,-apple-system,"Segoe UI",sans-serif}
svg .tick{font-variant-numeric:tabular-nums}
.bar:focus{outline:2px solid var(--ink2);outline-offset:1px}
footer{margin-top:18px;color:var(--muted);font-size:12px}
button.theme{margin-left:8px;font:inherit;font-size:12px;border:1px solid var(--border);
  border-radius:8px;background:var(--surface);color:var(--ink2);padding:5px 10px;cursor:pointer}
</style></head>
<body>
<div class="wrap">
<header>
  <h1>BTC Multi-Timeframe Bot</h1>
  <span class="chip" id="orchChip"><span class="dot"></span><span id="orchTxt">…</span></span>
  <span class="chip" id="modeChip">mode: …</span>
  <span class="live" id="liveTxt">connecting…</span>
  <button class="theme" id="themeBtn" title="theme">auto</button>
</header>

<div class="filters" role="group" aria-label="filters">
  <div class="seg" id="rangeSeg" role="group" aria-label="time range">
    <button data-r="today">Today</button>
    <button data-r="24h">24h</button>
    <button data-r="7d">7d</button>
    <button data-r="all" aria-pressed="true">All</button>
  </div>
  <select id="modeSel" aria-label="mode">
    <option value="paper">paper</option>
    <option value="live">live</option>
  </select>
</div>

<main>
  <div class="grid kpis" id="kpis"></div>

  <div class="grid row2">
    <div class="card">
      <h2>Cumulative PnL (USDC)</h2>
      <div class="chart-wrap" id="lineWrap">
        <div id="lineChart"></div>
        <div class="tip" id="lineTip"></div>
      </div>
      <div class="legend" id="lineLegend"></div>
    </div>
    <div class="card">
      <h2>PnL by timeframe (USDC)</h2>
      <div class="chart-wrap" id="barWrap">
        <div id="barChart"></div>
        <div class="tip" id="barTip"></div>
      </div>
    </div>
  </div>

  <div class="card" id="arbCard" style="display:none">
    <h2>Arb windows — best combined ask per slot (buy both sides when &le; $1.00)</h2>
    <div class="grid kpis" id="arbKpis" style="margin-bottom:12px"></div>
    <div class="chart-wrap" id="arbWrap">
      <div id="arbChart"></div>
      <div class="tip" id="arbTip"></div>
    </div>
  </div>

  <div class="grid row3">
    <div class="card">
      <h2>Timeframe comparison</h2>
      <div id="groupsTbl"></div>
    </div>
    <div class="card">
      <h2>Workers &amp; open positions</h2>
      <div id="workers"></div>
      <div id="positions" style="margin-top:10px"></div>
    </div>
  </div>

  <div class="card">
    <h2>Recent trades</h2>
    <div id="tradesTbl"></div>
  </div>
</main>
<footer>Paper fills simulate CLOB top-of-book. Data refreshes every 5s from multi/runtime.</footer>
</div>

<script>
"use strict";
const SLOTS = ["--s1","--s2","--s3","--s4","--s5","--s6","--s7","--s8"];
const qs = new URLSearchParams(location.search);
const state = {
  mode: qs.get("mode") === "live" ? "live" : "paper",
  range: ["today","24h","7d","all"].includes(qs.get("range")) ? qs.get("range") : "all",
  data: null,
  theme: ["light","dark"].includes(qs.get("theme")) ? qs.get("theme") : "auto",
};

const $ = id => document.getElementById(id);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const el = (tag, attrs, text) => {
  const n = document.createElement(tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (text !== undefined) n.textContent = text;
  return n;
};
const svgel = (tag, attrs) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  if (attrs) for (const [k,v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
function colorOf(pair){
  const i = (state.data.pairs_all || []).indexOf(pair);
  // 8 palette slots; overflow series get the de-emphasis gray rather than a
  // recycled hue (legend + tables still identify them unambiguously)
  if (i >= 0 && i < SLOTS.length) return css(SLOTS[i]);
  return css("--muted");
}
function fmtUsd(v, signed=true){
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  const dec = a >= 100 ? 0 : (a >= 1 ? 2 : 3);
  const s = (v < 0 ? "-" : (signed && v > 0 ? "+" : "")) + "$" + a.toFixed(dec);
  return s;
}
const fmtPct = v => (v === null || v === undefined) ? "–" : Math.round(v*100) + "%";
const fmtTime = ms => {
  const d = new Date(ms);
  return d.toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
};

/* ---------- theme ---------- */
function applyTheme(){
  const r = document.documentElement;
  if (state.theme === "auto") r.removeAttribute("data-theme");
  else r.setAttribute("data-theme", state.theme);
  $("themeBtn").textContent = state.theme;
  if (state.data) render(state.data);
}
$("themeBtn").addEventListener("click", () => {
  state.theme = state.theme === "auto" ? "light" : state.theme === "light" ? "dark" : "auto";
  applyTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
if (state.theme !== "auto") applyTheme();
if (state.mode !== "paper") $("modeSel").value = state.mode;
if (state.range !== "all")
  for (const x of $("rangeSeg").querySelectorAll("button"))
    x.setAttribute("aria-pressed", x.dataset.r === state.range ? "true" : "false");

/* ---------- filters ---------- */
$("rangeSeg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state.range = b.dataset.r;
  for (const x of $("rangeSeg").querySelectorAll("button"))
    x.setAttribute("aria-pressed", x === b ? "true" : "false");
  tick();
});
$("modeSel").addEventListener("change", () => { state.mode = $("modeSel").value; tick(); });

/* ---------- fetch loop ---------- */
let lastOk = 0;
async function tick(){
  try {
    const r = await fetch(`/data.json?mode=${state.mode}&range=${state.range}`, {cache:"no-store"});
    if (!r.ok) throw new Error(r.status);
    state.data = await r.json();
    lastOk = Date.now();
    document.body.classList.remove("stale");
    $("liveTxt").textContent = "live · updated 0s ago";
    render(state.data);
  } catch (e) {
    document.body.classList.add("stale");
    $("liveTxt").textContent = "connection lost — retrying";
  }
}
setInterval(tick, 5000);
setInterval(() => {
  if (!lastOk) return;
  if (!document.body.classList.contains("stale"))
    $("liveTxt").textContent = "live · updated " + Math.max(0, Math.round((Date.now()-lastOk)/1000)) + "s ago";
}, 1000);
addEventListener("resize", () => { if (state.data) render(state.data); });

/* ---------- render ---------- */
function render(d){
  const orch = d.orchestrator || {};
  $("orchTxt").textContent = orch.running ? "running" : "stopped";
  const dotColor = orch.running ? css("--status-good") : css("--status-critical");
  $("orchChip").querySelector(".dot").style.background = dotColor;
  $("modeChip").textContent = "mode: " + d.mode;

  renderKpis(d);
  renderLine(d);
  renderBars(d);
  renderArb(d);
  renderGroups(d);
  renderWorkers(d);
  renderTrades(d);
}

function tile(lbl, val, cls, sub){
  const c = el("div", {class:"card tile"});
  c.appendChild(el("div", {class:"lbl"}, lbl));
  const v = el("div", {class:"val" + (cls ? " " + cls : "")}, val);
  c.appendChild(v);
  if (sub) c.appendChild(el("div", {class:"sub2"}, sub));
  return c;
}
function renderKpis(d){
  const k = d.kpi || {}, box = $("kpis");
  box.replaceChildren();
  const pnl = k.pnl_sum || 0;
  box.appendChild(tile("Total PnL (" + d.range + ")", fmtUsd(pnl),
    pnl > 0 ? "pos-t" : pnl < 0 ? "neg-t" : "", d.mode + " mode"));
  box.appendChild(tile("Win rate", fmtPct(k.win_rate),
    "", (k.wins||0) + "W · " + (k.losses||0) + "L" + ((k.flat||0) ? " · " + k.flat + " flat" : "")));
  box.appendChild(tile("Entries / slots", (k.entries||0) + " / " + (k.slots||0),
    "", k.slots ? Math.round(100*(k.entries||0)/k.slots) + "% entry rate" : "no completed slots yet"));
  box.appendChild(tile("Open positions", String((d.open_positions||[]).length),
    "", "cap-shared across workers"));
  const daily = d.daily || {};
  const dp = daily.realized_pnl_usd;
  const cap = (d.caps || {}).daily_max_loss_usd;
  const capHit = (cap && dp !== undefined && dp <= -cap);
  box.appendChild(tile("Daily PnL (guard)", dp === undefined ? "–" : fmtUsd(dp),
    dp > 0 ? "pos-t" : dp < 0 ? "neg-t" : "",
    capHit
      ? "⚠ DAILY CAP $" + cap + " HIT — entries paused until 00:00 UTC"
      : (daily.trades || 0) + " trades · " + (daily.date || "no state yet")
        + (cap ? " · cap $" + cap : "")));
}

/* ---------- cumulative PnL line chart ---------- */
function renderLine(d){
  const holder = $("lineChart"), tip = $("lineTip"), legend = $("lineLegend");
  holder.replaceChildren(); legend.replaceChildren(); tip.style.display = "none";
  const series = (d.pairs_all || [])
    .filter(p => (d.series && d.series[p] || []).length)
    .map(p => ({key:p, color:colorOf(p), pts:d.series[p]}));
  if (!series.length){
    holder.appendChild(el("div", {class:"empty"},
      "No closed trades in this range yet — entries appear once a slot's entry window triggers."));
    return;
  }
  const W = Math.max(320, holder.clientWidth || holder.parentNode.clientWidth || 600);
  const H = 260, mL = 46, mR = 14, mT = 10, mB = 26;
  const iw = W - mL - mR, ih = H - mT - mB;
  const allT = [], allV = [0];
  for (const s of series) for (const [t,v] of s.pts){ allT.push(t); allV.push(v); }
  let t0 = Math.min(...allT), t1 = Math.max(...allT);
  if (t1 - t0 < 60000){ t0 -= 60000; t1 += 60000; }
  const v0 = Math.min(...allV), v1 = Math.max(...allV);
  const pad = Math.max((v1 - v0) * 0.1, 0.01);
  const y0 = v0 - pad, y1 = v1 + pad;
  const X = t => mL + (t - t0) / (t1 - t0) * iw;
  const Y = v => mT + (y1 - v) / (y1 - y0) * ih;

  const svg = svgel("svg", {width:W, height:H, viewBox:`0 0 ${W} ${H}`, role:"img",
    "aria-label":"Cumulative PnL per timeframe"});
  // y grid: 4 clean ticks
  const step = niceStep((y1 - y0) / 4);
  for (let v = Math.ceil(y0/step)*step; v <= y1 + 1e-12; v += step){
    svg.appendChild(svgel("line", {x1:mL, x2:W-mR, y1:Y(v), y2:Y(v),
      stroke:css("--grid"), "stroke-width":1}));
    const tx = svgel("text", {x:mL-8, y:Y(v)+3.5, "text-anchor":"end", class:"tick"});
    tx.textContent = v.toFixed(Math.abs(step) >= 1 ? 0 : 2);
    svg.appendChild(tx);
  }
  // zero baseline
  if (y0 < 0 && y1 > 0)
    svg.appendChild(svgel("line", {x1:mL, x2:W-mR, y1:Y(0), y2:Y(0),
      stroke:css("--baseline"), "stroke-width":1}));
  // x ticks
  for (let i = 0; i <= 3; i++){
    const t = t0 + (t1 - t0) * i / 3;
    const tx = svgel("text", {x:X(t), y:H-8, "text-anchor": i===0?"start":i===3?"end":"middle", class:"tick"});
    tx.textContent = fmtTime(t);
    svg.appendChild(tx);
  }
  // step-after lines (cumulative PnL is a step function)
  for (const s of series){
    let dstr = "";
    s.pts.forEach(([t,v], i) => {
      const x = X(t), y = Y(v);
      if (i === 0){ dstr = `M ${x} ${Y(0)} L ${x} ${y}`; }
      else { dstr += ` H ${x} V ${y}`; }
    });
    svg.appendChild(svgel("path", {d:dstr, fill:"none", stroke:s.color,
      "stroke-width":2, "stroke-linejoin":"round", "stroke-linecap":"round"}));
    const [lt, lv] = s.pts[s.pts.length-1];
    svg.appendChild(svgel("circle", {cx:X(lt), cy:Y(lv), r:4, fill:s.color,
      stroke:css("--surface"), "stroke-width":2}));
  }
  // crosshair
  const hair = svgel("line", {y1:mT, y2:H-mB, stroke:css("--baseline"),
    "stroke-width":1, visibility:"hidden"});
  svg.appendChild(hair);
  const overlay = svgel("rect", {x:mL, y:mT, width:iw, height:ih, fill:"transparent"});
  svg.appendChild(overlay);
  const times = [...new Set(allT)].sort((a,b)=>a-b);
  overlay.addEventListener("pointermove", ev => {
    const box = svg.getBoundingClientRect();
    const px = ev.clientX - box.left;
    let best = times[0];
    for (const t of times) if (Math.abs(X(t)-px) < Math.abs(X(best)-px)) best = t;
    hair.setAttribute("x1", X(best)); hair.setAttribute("x2", X(best));
    hair.setAttribute("visibility", "visible");
    tip.replaceChildren(el("div", {class:"t"}, fmtTime(best)));
    for (const s of series){
      let v = null;
      for (const [t,val] of s.pts){ if (t <= best) v = val; else break; }
      if (v === null) continue;
      const row = el("div", {class:"r"});
      const key = el("span", {class:"key"}); key.style.background = s.color;
      row.appendChild(key);
      row.appendChild(el("span", {}, s.key));
      row.appendChild(el("span", {class:"v"}, fmtUsd(v)));
      tip.appendChild(row);
    }
    tip.style.display = "block";
    const wrap = $("lineWrap").getBoundingClientRect();
    let lx = ev.clientX - wrap.left + 14;
    if (lx + tip.offsetWidth > wrap.width - 4) lx = ev.clientX - wrap.left - tip.offsetWidth - 14;
    tip.style.left = lx + "px";
    tip.style.top = Math.max(0, ev.clientY - wrap.top - 20) + "px";
  });
  overlay.addEventListener("pointerleave", () => {
    tip.style.display = "none"; hair.setAttribute("visibility", "hidden");
  });
  holder.appendChild(svg);
  for (const s of series){
    const it = el("span");
    const key = el("span", {class:"key"}); key.style.background = s.color;
    it.appendChild(key); it.appendChild(document.createTextNode(s.key));
    legend.appendChild(it);
  }
}
function niceStep(raw){
  const p = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-9))));
  for (const m of [1,2,2.5,5,10]) if (m*p >= raw) return m*p;
  return 10*p;
}

/* ---------- PnL by timeframe: diverging bars ---------- */
function renderBars(d){
  const holder = $("barChart"), tip = $("barTip");
  holder.replaceChildren(); tip.style.display = "none";
  const rows = (d.groups || []).filter(g => g.pnl_sum !== null && g.entries > 0);
  if (!rows.length){
    holder.appendChild(el("div", {class:"empty"}, "No entries in this range yet."));
    return;
  }
  const W = Math.max(280, holder.clientWidth || 400);
  const barH = 18, gap = 10, mT = 6, mB = 24, mL = 78, mR = 56;
  const H = mT + rows.length*(barH+gap) - gap + mB;
  const vmax = Math.max(...rows.map(r => Math.abs(r.pnl_sum)), 0.01);
  // reserve tip-label room on the negative side so a max-loss bar's value
  // label never collides with the category column
  const lp = rows.some(r => r.pnl_sum < 0) ? 52 : 0;
  const iw = W - mL - mR - lp;
  const X = v => mL + lp + (v + vmax) / (2*vmax) * iw;
  const svg = svgel("svg", {width:W, height:H, viewBox:`0 0 ${W} ${H}`, role:"img",
    "aria-label":"PnL by timeframe"});
  svg.appendChild(svgel("line", {x1:X(0), x2:X(0), y1:mT, y2:H-mB,
    stroke:css("--baseline"), "stroke-width":1}));
  rows.forEach((g, i) => {
    const y = mT + i*(barH+gap);
    const v = g.pnl_sum;
    const x0 = X(0), x1 = X(v);
    const w = Math.max(Math.abs(x1-x0), 1);
    const r = Math.min(4, w);
    const color = v >= 0 ? css("--pos") : css("--neg");
    // rounded data-end, square at the baseline
    const path = v >= 0
      ? `M ${x0} ${y} H ${x0+w-r} Q ${x0+w} ${y} ${x0+w} ${y+r} V ${y+barH-r} Q ${x0+w} ${y+barH} ${x0+w-r} ${y+barH} H ${x0} Z`
      : `M ${x0} ${y} H ${x0-w+r} Q ${x0-w} ${y} ${x0-w} ${y+r} V ${y+barH-r} Q ${x0-w} ${y+barH} ${x0-w+r} ${y+barH} H ${x0} Z`;
    const bar = svgel("path", {d:path, fill:color, class:"bar", tabindex:"0", role:"img",
      "aria-label": g.pair + " " + fmtUsd(v)});
    const lift = on => bar.setAttribute("opacity", on ? "0.85" : "1");
    const show = ev => {
      tip.replaceChildren(el("div", {class:"t"}, g.pair));
      const row = el("div", {class:"r"});
      row.appendChild(el("span", {}, g.entries + " trades"));
      row.appendChild(el("span", {class:"v"}, fmtUsd(v)));
      tip.appendChild(row);
      tip.style.display = "block";
      const wrap = $("barWrap").getBoundingClientRect();
      const bb = bar.getBoundingClientRect();
      const cx = (ev && ev.clientX) ? ev.clientX : bb.left + bb.width/2;
      let lx = cx - wrap.left + 12;
      if (lx + tip.offsetWidth > wrap.width - 4) lx = wrap.width - tip.offsetWidth - 4;
      tip.style.left = lx + "px";
      tip.style.top = (bb.top - wrap.top - 8) + "px";
      lift(true);
    };
    const hide = () => { tip.style.display = "none"; lift(false); };
    bar.addEventListener("pointermove", show);
    bar.addEventListener("pointerleave", hide);
    bar.addEventListener("focus", show);
    bar.addEventListener("blur", hide);
    svg.appendChild(bar);
    const cat = svgel("text", {x:mL-8, y:y+barH/2+4, "text-anchor":"end"});
    cat.textContent = g.pair;
    svg.appendChild(cat);
    const valT = svgel("text", {x: v >= 0 ? x0+w+6 : x0-w-6, y:y+barH/2+4,
      "text-anchor": v >= 0 ? "start" : "end", class:"tick"});
    valT.textContent = fmtUsd(v);
    svg.appendChild(valT);
  });
  const zt = svgel("text", {x:X(0), y:H-8, "text-anchor":"middle", class:"tick"});
  zt.textContent = "0";
  svg.appendChild(zt);
  holder.appendChild(svg);
}

/* ---------- arb windows: best combined ask per slot vs the $1 line ---------- */
function renderArb(d){
  const a = d.arb || {};
  const card = $("arbCard");
  if (!a.enabled){ card.style.display = "none"; return; }
  card.style.display = "";

  const box = $("arbKpis");
  box.replaceChildren();
  const thr = a.threshold ?? 0.99;
  const bestCls = (a.best_ever != null && a.best_ever <= thr) ? "pos-t" : "";
  box.appendChild(tile("Best combined seen",
    a.best_ever != null ? "$" + a.best_ever.toFixed(3) : "–", bestCls,
    "buy threshold $" + thr.toFixed(2)));
  box.appendChild(tile("Window rate", fmtPct(a.window_rate),
    "", (a.windows_seen||0) + " of " + (a.slots||0) + " slots dipped ≤ $" + thr.toFixed(2)));
  box.appendChild(tile("Windows captured", String(a.entries||0),
    "", "median best/slot $" + (a.median_best != null ? a.median_best.toFixed(3) : "–")));
  box.appendChild(tile("Locked arb PnL",
    fmtUsd(a.locked_pnl_usdc||0), (a.locked_pnl_usdc||0) > 0 ? "pos-t" : "",
    "profit fixed at fill"));

  const holder = $("arbChart"), tip = $("arbTip");
  holder.replaceChildren(); tip.style.display = "none";
  const pts = (a.recent || []).filter(s => s.best != null);
  if (!pts.length){
    holder.appendChild(el("div", {class:"empty"},
      "No combined-ask samples yet — the arb worker logs the best per slot as it runs."));
    return;
  }
  const W = Math.max(320, holder.clientWidth || 600);
  const H = 200, mL = 52, mR = 12, mT = 10, mB = 24;
  const iw = W - mL - mR, ih = H - mT - mB;
  const vals = pts.map(p => p.best);
  let y0 = Math.min(...vals, thr) - 0.01, y1 = Math.max(...vals, 1.0) + 0.01;
  const X = i => mL + (pts.length === 1 ? iw/2 : i/(pts.length-1)*iw);
  const Y = v => mT + (y1 - v)/(y1 - y0)*ih;
  const svg = svgel("svg", {width:W, height:H, viewBox:`0 0 ${W} ${H}`, role:"img",
    "aria-label":"Best combined ask per slot"});
  // y gridlines at clean 0.02 steps
  for (let v = Math.ceil(y0/0.02)*0.02; v <= y1; v += 0.02){
    svg.appendChild(svgel("line", {x1:mL, x2:W-mR, y1:Y(v), y2:Y(v),
      stroke:css("--grid"), "stroke-width":1}));
    const tx = svgel("text", {x:mL-8, y:Y(v)+3.5, "text-anchor":"end", class:"tick"});
    tx.textContent = "$" + v.toFixed(2);
    svg.appendChild(tx);
  }
  // the $1.00 arb boundary — solid baseline; anything below it is a window
  const yb = Y(thr);
  svg.appendChild(svgel("line", {x1:mL, x2:W-mR, y1:yb, y2:yb,
    stroke:css("--baseline"), "stroke-width":1}));
  const lbl = svgel("text", {x:W-mR, y:yb-4, "text-anchor":"end", class:"tick"});
  lbl.textContent = "≤ $" + thr.toFixed(2) + " = arb";
  svg.appendChild(lbl);
  // dots: green when a window existed (best <= threshold), muted otherwise
  pts.forEach((p, i) => {
    const win = p.best <= thr;
    const dot = svgel("circle", {cx:X(i), cy:Y(p.best), r: win ? 4.5 : 3.5,
      fill: win ? css("--s2") : css("--muted"),
      stroke: css("--surface"), "stroke-width":2});
    dot.style.cursor = "pointer";
    const show = () => {
      tip.replaceChildren(el("div", {class:"t"}, fmtTime(p.t)));
      const mk = (lab, val) => { const r = el("div", {class:"r"});
        r.appendChild(el("span", {}, lab));
        r.appendChild(el("span", {class:"v"}, val)); tip.appendChild(r); };
      mk("best combined", "$" + p.best.toFixed(3));
      mk("window", win ? "yes" : "no");
      if (p.entered) mk("entered @", "$" + Number(p.combined).toFixed(3));
      if (p.pnl != null) mk("locked pnl", fmtUsd(p.pnl));
      tip.style.display = "block";
      const wrap = $("arbWrap").getBoundingClientRect();
      const bb = dot.getBoundingClientRect();
      let lx = bb.left - wrap.left + 10;
      if (lx + 150 > wrap.width) lx = wrap.width - 154;
      tip.style.left = lx + "px";
      tip.style.top = Math.max(0, bb.top - wrap.top - 8) + "px";
    };
    dot.addEventListener("pointerenter", show);
    dot.addEventListener("pointerleave", () => tip.style.display = "none");
    svg.appendChild(dot);
  });
  holder.appendChild(svg);
}

/* ---------- tables & status ---------- */
function mkTable(headers, rows){
  const t = el("table"), tr = el("tr");
  for (const h of headers) tr.appendChild(el("th", {}, h));
  t.appendChild(tr);
  for (const r of rows){
    const row = el("tr");
    for (const c of r) row.appendChild(c);
    t.appendChild(row);
  }
  return t;
}
function pairCell(pair){
  const td = el("td");
  const key = el("span", {class:"key"}); key.style.background = colorOf(pair);
  td.appendChild(key); td.appendChild(document.createTextNode(pair));
  return td;
}
function pnlCell(v){
  const td = el("td", {}, fmtUsd(v));
  if (v > 0) td.classList.add("pos-t");
  if (v < 0) td.classList.add("neg-t");
  return td;
}
function renderGroups(d){
  const box = $("groupsTbl"); box.replaceChildren();
  const rows = d.groups || [];
  if (!rows.length){
    box.appendChild(el("div", {class:"empty"}, "No completed slots in this range yet."));
    return;
  }
  box.appendChild(mkTable(
    ["series","slots","entries","W–L","win rate","PnL","avg"],
    rows.map(g => [
      pairCell(g.pair),
      el("td", {}, String(g.slots)),
      el("td", {}, g.entries + (g.entry_rate !== null ? " (" + Math.round(g.entry_rate*100) + "%)" : "")),
      el("td", {}, (g.wins||0) + "–" + (g.losses||0)),
      el("td", {}, fmtPct(g.win_rate)),
      pnlCell(g.pnl_sum),
      el("td", {}, g.pnl_avg === null ? "–" : fmtUsd(g.pnl_avg)),
    ])));
}
function renderWorkers(d){
  const box = $("workers"); box.replaceChildren();
  const ws = d.workers || [];
  if (!ws.length){
    box.appendChild(el("div", {class:"empty"}, "No workers yet — start the bot."));
  }
  for (const w of ws){
    const row = el("div", {class:"status"});
    const dot = el("span", {class:"dot"});
    dot.style.background = w.alive ? css("--status-good") : css("--status-critical");
    row.appendChild(dot);
    row.appendChild(el("span", {class:"nm"}, w.id));
    row.appendChild(el("span", {}, w.alive ? "running" : "stopped"));
    row.appendChild(el("span", {class:"meta"},
      "pid " + (w.pid ?? "–") + (w.restarts ? " · " + w.restarts + " restarts" : "")));
    box.appendChild(row);
  }
  const pbox = $("positions"); pbox.replaceChildren();
  const ps = d.open_positions || [];
  pbox.appendChild(el("h2", {}, "Open positions (" + ps.length + ")"));
  if (!ps.length){
    pbox.appendChild(el("div", {class:"empty"}, "None — capacity free."));
    return;
  }
  pbox.appendChild(mkTable(["worker","stake","since"],
    ps.map(p => [
      el("td", {}, p.worker),
      el("td", {}, fmtUsd(p.stake_usd, false)),
      el("td", {}, p.reserved_at ? fmtTime(Date.parse(p.reserved_at)) : "–"),
    ])));
}
function renderTrades(d){
  const box = $("tradesTbl"); box.replaceChildren();
  const ts = d.trades || [];
  if (!ts.length){
    box.appendChild(el("div", {class:"empty"}, "No trades in this range yet."));
    return;
  }
  box.appendChild(mkTable(
    ["time","series","side","entry","exit","close reason","PnL"],
    ts.map(t => [
      el("td", {}, fmtTime(t.t)),
      pairCell(t.pair),
      el("td", {}, t.side || "–"),
      el("td", {}, t.entry !== null && t.entry !== undefined ? Number(t.entry).toFixed(3) : "–"),
      el("td", {}, t.exit !== null && t.exit !== undefined ? Number(t.exit).toFixed(3) : "–"),
      el("td", {}, t.reason || "–"),
      pnlCell(t.pnl),
    ])));
}

tick();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    runtime: Path = Path(".")

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/data.json":
            q = parse_qs(u.query)
            mode = (q.get("mode") or ["paper"])[0]
            rng = (q.get("range") or ["all"])[0]
            if mode not in ("paper", "live"):
                mode = "paper"
            if rng not in ("today", "24h", "7d", "all"):
                rng = "all"
            try:
                payload = build_payload(self.runtime, mode, rng)
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode("utf-8"),
                           "application/json")
            return
        self._send(404, b"not found", "text/plain")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--runtime-dir", default=default_runtime_dir())
    args = ap.parse_args()

    Handler.runtime = Path(args.runtime_dir)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"status": "dashboard_started",
                      "url": f"http://{args.host}:{args.port}",
                      "runtime": str(Handler.runtime)}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
