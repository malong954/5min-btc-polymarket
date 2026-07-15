#!/usr/bin/env python3
"""
Multi-timeframe orchestrator: spawns one multi_worker.py process per enabled
(asset, timeframe) pair from the multi profiles config, supervises them
(optional restart on crash), and shuts them all down cleanly on SIGTERM.

Run via multibot_ctl.sh in normal use.
"""
from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from multi_worker import load_config, tf_params, ts_utc, default_repo_path, default_runtime_dir  # noqa: E402

STOP = False


def _on_signal(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--mode", choices=["paper", "live"], default=None,
                    help="defaults to mode_default from config (paper)")
    ap.add_argument("--repo", default=default_repo_path())
    ap.add_argument("--runtime-dir", default=default_runtime_dir())
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    cfg = load_config(args.config)
    mode = args.mode or str(cfg.get("mode_default") or "paper")
    assets = [str(a).lower() for a in (cfg.get("assets") or ["btc"])]
    worker_cfg = cfg.get("worker") or {}
    restart_on_exit = bool(worker_cfg.get("restart_on_exit", True))
    backoff = float(worker_cfg.get("restart_backoff_sec", 20))

    pairs: list[tuple[str, str, str]] = []
    for asset in assets:
        for tf in ("5m", "15m", "1h", "4h", "1d"):
            p = tf_params(cfg, tf)
            if not p.get("enabled", True):
                continue
            for strategy in (p.get("strategies") or ["favorite"]):
                if strategy not in ("favorite", "underdog", "impulse", "arb"):
                    continue
                if strategy == "arb" and mode == "live":
                    print(json.dumps({"ts": ts_utc(), "status": "skip_arb_live",
                                      "note": "arb is paper-only for now"}),
                          flush=True)
                    continue
                pairs.append((asset, tf, strategy))

    if not pairs:
        print("no enabled (asset, timeframe) pairs in config", file=sys.stderr)
        return 2

    runtime = Path(args.runtime_dir)
    logs_dir = runtime / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    workers_meta_path = runtime / "workers.json"
    worker_script = str(Path(__file__).resolve().with_name("multi_worker.py"))

    procs: dict[str, dict] = {}

    def spawn(asset: str, tf: str, strategy: str = "favorite") -> None:
        wid = f"{asset}_{tf}" if strategy == "favorite" else f"{asset}_{tf}_{strategy}"
        log_path = logs_dir / f"{wid}.log"
        cmd = [sys.executable, worker_script,
               "--asset", asset, "--timeframe", tf, "--mode", mode,
               "--strategy", strategy,
               "--repo", args.repo, "--runtime-dir", str(runtime)]
        if args.config:
            cmd += ["--config", args.config]
        logf = open(log_path, "a", encoding="utf-8")
        p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)
        prev = procs.get(wid) or {}
        procs[wid] = {"proc": p, "logf": logf, "asset": asset, "tf": tf,
                      "strategy": strategy,
                      "log": str(log_path), "started_at": ts_utc(),
                      "restarts": int(prev.get("restarts") or 0)}
        print(json.dumps({"ts": ts_utc(), "status": "spawned", "worker": wid,
                          "pid": p.pid, "log": str(log_path)}), flush=True)

    def write_workers_meta() -> None:
        meta = {}
        for wid, info in procs.items():
            p = info["proc"]
            meta[wid] = {"pid": p.pid, "alive": p.poll() is None,
                         "restarts": info["restarts"], "log": info["log"],
                         "started_at": info["started_at"]}
        workers_meta_path.write_text(json.dumps(
            {"mode": mode, "updated_at": ts_utc(), "workers": meta},
            ensure_ascii=False, indent=2), encoding="utf-8")

    for asset, tf, strategy in pairs:
        spawn(asset, tf, strategy)
    write_workers_meta()

    pending_restart: dict[str, float] = {}
    while not STOP:
        time.sleep(3)
        changed = False
        for wid, info in list(procs.items()):
            p = info["proc"]
            if p.poll() is None:
                continue
            changed = True
            print(json.dumps({"ts": ts_utc(), "status": "worker_exited",
                              "worker": wid, "code": p.returncode}), flush=True)
            if restart_on_exit and wid not in pending_restart:
                pending_restart[wid] = time.time() + backoff
        now = time.time()
        for wid, due in list(pending_restart.items()):
            if now >= due:
                info = procs[wid]
                info["restarts"] += 1
                restarts = info["restarts"]
                info["logf"].close()
                spawn(info["asset"], info["tf"], info.get("strategy", "favorite"))
                procs[wid]["restarts"] = restarts
                pending_restart.pop(wid, None)
                changed = True
        if changed:
            write_workers_meta()

    # graceful shutdown: SIGTERM to all workers, wait, then SIGKILL stragglers
    print(json.dumps({"ts": ts_utc(), "status": "shutting_down"}), flush=True)
    for info in procs.values():
        if info["proc"].poll() is None:
            info["proc"].terminate()
    deadline = time.time() + 30
    for info in procs.values():
        p = info["proc"]
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.5)
        if p.poll() is None:
            p.kill()
        info["logf"].close()
    write_workers_meta()
    print(json.dumps({"ts": ts_utc(), "status": "stopped"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
