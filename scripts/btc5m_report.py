#!/usr/bin/env python3
import argparse
import glob
import json
import os
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from btc5m_fees import estimate_run_fees, fee_coef_from_env, fee_model_meta


def default_runtime_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime")


def load_tail_json(path: str):
    try:
        txt = open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return None
    i = txt.rfind("\n{")
    if i == -1 and txt.startswith("{"):
        i = 0
    if i == -1:
        return None
    blob = txt[i + 1 :] if txt[i : i + 1] == "\n" else txt[i:]
    try:
        return json.loads(blob)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime-dir", default=default_runtime_dir())
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--fee-coef", type=float, default=fee_coef_from_env())
    args = ap.parse_args()

    pats = [
        os.path.join(args.runtime_dir, "btc5m_one_trade*.log"),
        os.path.join(args.runtime_dir, "btc5m_live_until_*.log"),
        os.path.join(args.runtime_dir, "btc5m_*_*.log"),
    ]
    files = []
    for p in pats:
        files.extend(glob.glob(p))
    files = sorted(set(files), key=lambda p: os.path.getmtime(p), reverse=True)[: args.limit]

    rows = []
    total_pnl = 0.0
    pnl_count = 0
    total_fees = 0.0
    total_net = 0.0
    net_count = 0
    net_wins = 0
    entry_prices = []
    close_status = Counter()
    results = Counter()

    for f in files:
        obj = load_tail_json(f)
        if not obj:
            continue
        r = obj.get("result")
        results[r] += 1
        op = obj.get("opened") or {}
        cl = obj.get("closed") or {}
        pnl = obj.get("realized_cashflow_pnl_usdc")
        if isinstance(pnl, (int, float)):
            total_pnl += float(pnl)
            pnl_count += 1
        fees = estimate_run_fees(obj, args.fee_coef)
        if isinstance(fees.get("entry_price"), (int, float)):
            entry_prices.append(float(fees["entry_price"]))
        total_fees += fees["est_fees_total_usdc"]
        net = fees.get("est_net_pnl_usdc")
        if isinstance(net, (int, float)):
            total_net += float(net)
            net_count += 1
            if net > 0:
                net_wins += 1
        close_status[str(cl.get("close_status") or cl.get("close_skipped") or "none")] += 1
        rows.append(
            {
                "file": os.path.basename(f),
                "result": r,
                "side": op.get("side"),
                "market": op.get("market_slug"),
                "open_tx": op.get("open_tx"),
                "close_tx": cl.get("close_tx"),
                "close_status": cl.get("close_status"),
                "close_skipped": cl.get("close_skipped"),
                "pnl": pnl,
                "entry_price": fees.get("entry_price"),
                "exit_price": fees.get("exit_price"),
                "est_fees_usdc": fees.get("est_fees_total_usdc"),
                "est_net_pnl_usdc": fees.get("est_net_pnl_usdc"),
            }
        )

    out = {
        "logs_scanned": len(files),
        "runs_parsed": len(rows),
        "results": dict(results),
        "close_status": dict(close_status),
        "realized_pnl_sum_usdc": round(total_pnl, 6) if pnl_count else None,
        "realized_pnl_count": pnl_count,
        "est_fees_sum_usdc": round(total_fees, 6) if rows else None,
        "est_net_pnl_sum_usdc": round(total_net, 6) if net_count else None,
        "est_net_pnl_count": net_count,
        "est_net_win_rate": round(net_wins / net_count, 4) if net_count else None,
        "avg_entry_price": round(sum(entry_prices) / len(entry_prices), 4) if entry_prices else None,
        "fee_model": fee_model_meta(args.fee_coef),
        "runs": rows,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
