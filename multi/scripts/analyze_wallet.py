#!/usr/bin/env python3
"""
Analyze a Polymarket wallet's public trade history to reverse-engineer its
Up/Down strategy, and cross-match it against this bot's own logs.

Pulls the wallet's activity from the public Data API (no auth), classifies
every trade into asset/timeframe/slot, then profiles:

  - which timeframes and assets they trade, sizes and volume
  - WHEN they enter: seconds-before-close distribution per timeframe
  - WHAT they buy: price-level buckets (favorites vs longshots)
  - both-sides buys per market (complete-set arbitrage detection)
  - exit style: sell-before-close vs hold-to-redeem, cashflow PnL
  - their active windows: hour-of-day (ET) histogram and trading sessions
  - log match: for markets our workers also watched, what our CLOB
    heartbeats saw at their entry moment and what our bot did

Usage (run in your trading environment):
  python multi/scripts/analyze_wallet.py --wallet 0x45230b...
  python multi/scripts/analyze_wallet.py --wallet 0x... --dump raw.json
  python multi/scripts/analyze_wallet.py --input raw.json   # offline re-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402

DATA_API = "https://data-api.polymarket.com"
UTC = dt.timezone.utc

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
NAME_TO_ASSET = {v: k for k, v in md.ASSET_NAMES.items()}

UPDOWN_RE = re.compile(r"^(?P<asset>[a-z0-9]+)-updown-(?P<tf>5m|15m|1h|4h|1d)-(?P<bucket>\d+)$")
ET_HOUR_RE = re.compile(
    r"^(?P<name>[a-z]+)-up-or-down-(?P<month>[a-z]+)-(?P<day>\d{1,2})"
    r"(?:-(?P<year>\d{4}))?-(?P<h>\d{1,2})(?P<ampm>am|pm)-et$")
ET_DAY_RE = re.compile(
    r"^(?P<name>[a-z]+)-up-or-down(?:-on)?-(?P<month>[a-z]+)-(?P<day>\d{1,2})"
    r"(?:-(?P<year>\d{4}))?$")


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_activity(wallet: str, max_pages: int = 40, page_size: int = 500) -> list[dict]:
    import requests
    out: list[dict] = []
    for page in range(max_pages):
        r = requests.get(f"{DATA_API}/activity",
                         params={"user": wallet, "limit": page_size,
                                 "offset": page * page_size},
                         timeout=20)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        time.sleep(0.25)
    return out


# ---------------------------------------------------------------------------
# Normalize + classify
# ---------------------------------------------------------------------------

def norm_ts(v: Any) -> Optional[float]:
    try:
        t = float(v)
    except (TypeError, ValueError):
        return None
    if t > 1e12:  # ms
        t /= 1000.0
    return t


def infer_year(ts: float, month: int, day: int) -> int:
    """Year for a year-less ET slug: the candidate nearest the trade time."""
    base = dt.datetime.fromtimestamp(ts, md.ET)
    best, best_d = base.year, None
    for y in (base.year - 1, base.year, base.year + 1):
        try:
            cand = dt.datetime(y, month, day, 12, 0, tzinfo=md.ET)
        except ValueError:
            continue
        d = abs((cand - base).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = y, d
    return best


def classify_slug(slug: str, ts: float) -> Optional[dict]:
    """Return {asset, tf, close_ts, open_ts} for an Up/Down slug, else None."""
    s = (slug or "").lower()
    m = UPDOWN_RE.match(s)
    if m:
        tf = m.group("tf")
        bucket = int(m.group("bucket"))
        dur = md.TIMEFRAME_SECONDS[tf]
        if tf == "1d":
            close = md._slot_end_ts("1d", bucket)
        else:
            close = bucket + dur
        return {"asset": m.group("asset"), "tf": tf,
                "open_ts": bucket, "close_ts": close}
    m = ET_HOUR_RE.match(s)
    if m:
        mon = MONTHS.get(m.group("month"))
        if not mon:
            return None
        day = int(m.group("day"))
        year = int(m.group("year")) if m.group("year") else infer_year(ts, mon, day)
        h = int(m.group("h")) % 12
        if m.group("ampm") == "pm":
            h += 12
        try:
            start = dt.datetime(year, mon, day, h, 0, tzinfo=md.ET)
        except ValueError:
            return None
        return {"asset": NAME_TO_ASSET.get(m.group("name"), m.group("name")),
                "tf": "1h", "open_ts": int(start.timestamp()),
                "close_ts": int(start.timestamp()) + 3600}
    m = ET_DAY_RE.match(s)
    if m:
        mon = MONTHS.get(m.group("month"))
        if not mon:
            return None
        day = int(m.group("day"))
        year = int(m.group("year")) if m.group("year") else infer_year(ts, mon, day)
        try:
            # daily markets close at NOON ET of the named day
            close = dt.datetime(year, mon, day, 12, 0, tzinfo=md.ET)
        except ValueError:
            return None
        close_ts = int(close.timestamp())
        return {"asset": NAME_TO_ASSET.get(m.group("name"), m.group("name")),
                "tf": "1d", "open_ts": close_ts - 86400, "close_ts": close_ts}
    return None


def normalize(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ts = norm_ts(it.get("timestamp"))
        if ts is None:
            continue
        typ = str(it.get("type") or "").upper()
        slug = str(it.get("slug") or it.get("eventSlug") or "")
        usdc = it.get("usdcSize", it.get("usdc_size"))
        price = it.get("price")
        size = it.get("size")
        try:
            usdc = float(usdc) if usdc is not None else None
        except (TypeError, ValueError):
            usdc = None
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError):
            size = None
        if usdc is None and price is not None and size is not None:
            usdc = price * size
        rec = {
            "ts": ts,
            "type": typ,
            "side": str(it.get("side") or "").upper(),
            "slug": slug,
            "event_slug": str(it.get("eventSlug") or ""),
            "title": it.get("title"),
            "outcome": str(it.get("outcome") or "").upper(),
            "outcome_index": it.get("outcomeIndex"),
            "price": price,
            "size": size,
            "usdc": usdc,
            "condition_id": it.get("conditionId"),
        }
        rec["cls"] = classify_slug(slug, ts) or classify_slug(rec["event_slug"], ts)
        out.append(rec)
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def pct(vals: list[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


PRICE_BUCKETS = [(0.0, 0.2, "<0.20"), (0.2, 0.5, "0.20-0.50"),
                 (0.5, 0.7, "0.50-0.70"), (0.7, 0.9, "0.70-0.90"),
                 (0.9, 1.01, ">=0.90")]


def bucket_price(p: float) -> str:
    for lo, hi, name in PRICE_BUCKETS:
        if lo <= p < hi:
            return name
    return "?"


def analyze(records: list[dict]) -> dict:
    updown = [r for r in records if r["cls"]]
    trades = [r for r in updown if r["type"] == "TRADE"]
    buys = [r for r in trades if r["side"] == "BUY"]
    sells = [r for r in trades if r["side"] == "SELL"]
    redeems = [r for r in updown if r["type"] == "REDEEM"]

    by_tf: dict[str, dict] = {}
    markets: dict[str, dict] = {}
    for r in updown:
        key = r["slug"] or r["event_slug"]
        mkt = markets.setdefault(key, {
            "slug": key, "tf": r["cls"]["tf"], "asset": r["cls"]["asset"],
            "close_ts": r["cls"]["close_ts"],
            "buy_usdc": 0.0, "sell_usdc": 0.0, "redeem_usdc": 0.0,
            "buy_sides": set(), "buy_px_shares": defaultdict(list),
            "n_buys": 0, "n_sells": 0, "sell_secs_before_close": [],
        })
        if r["type"] == "TRADE" and r["side"] == "BUY":
            mkt["buy_usdc"] += r["usdc"] or 0.0
            mkt["n_buys"] += 1
            if r["outcome"]:
                mkt["buy_sides"].add(r["outcome"])
                if r["price"] is not None and r["size"]:
                    mkt["buy_px_shares"][r["outcome"]].append((r["price"], r["size"]))
        elif r["type"] == "TRADE" and r["side"] == "SELL":
            mkt["sell_usdc"] += r["usdc"] or 0.0
            mkt["n_sells"] += 1
            mkt["sell_secs_before_close"].append(mkt["close_ts"] - r["ts"])
        elif r["type"] == "REDEEM":
            mkt["redeem_usdc"] += r["usdc"] or 0.0

    now = time.time()
    for key, mkt in markets.items():
        mkt["cash_pnl"] = round(mkt["sell_usdc"] + mkt["redeem_usdc"] - mkt["buy_usdc"], 4)
        mkt["resolved_past"] = mkt["close_ts"] < now - 600
        # complete-set: bought both sides — combined VWAP sum tells if arb
        both = {"UP", "DOWN"} <= mkt["buy_sides"] or {"YES", "NO"} <= mkt["buy_sides"]
        mkt["both_sides"] = both
        if both:
            sums = []
            for side, lst in mkt["buy_px_shares"].items():
                tot_sh = sum(s for _, s in lst)
                if tot_sh > 0:
                    sums.append(sum(p * s for p, s in lst) / tot_sh)
            mkt["combined_vwap_sum"] = round(sum(sums), 4) if len(sums) >= 2 else None
        else:
            mkt["combined_vwap_sum"] = None

    for tf in list(md.TIMEFRAME_SECONDS) + ["other"]:
        tf_buys = [r for r in buys if r["cls"]["tf"] == tf]
        if not tf_buys and not any(m["tf"] == tf for m in markets.values()):
            continue
        dur = md.TIMEFRAME_SECONDS.get(tf, 0)
        secs = [r["cls"]["close_ts"] - r["ts"] for r in tf_buys
                if r["cls"]["close_ts"] >= r["ts"]]
        pos = [1.0 - s / dur for s in secs if dur] if dur else []
        tf_markets = [m for m in markets.values() if m["tf"] == tf]
        resolved = [m for m in tf_markets if m["resolved_past"]]
        wins = [m for m in resolved if m["cash_pnl"] > 0]
        arbs = [m for m in tf_markets if m["both_sides"]]
        arb_sums = [m["combined_vwap_sum"] for m in arbs if m["combined_vwap_sum"]]
        prices = [r["price"] for r in tf_buys if r["price"] is not None]
        sizes = [r["usdc"] for r in tf_buys if r["usdc"]]
        sells_before = [s for m in tf_markets for s in m["sell_secs_before_close"] if s > 0]
        by_tf[tf] = {
            "markets": len(tf_markets),
            "buys": len(tf_buys),
            "sells": sum(m["n_sells"] for m in tf_markets),
            "volume_usdc": round(sum(sizes), 2),
            "avg_buy_usdc": round(sum(sizes) / len(sizes), 2) if sizes else None,
            "median_buy_price": pct(prices, 0.5),
            "price_buckets": dict(Counter(bucket_price(p) for p in prices)),
            "entry_secs_before_close": {
                "p25": pct(secs, 0.25), "median": pct(secs, 0.5), "p75": pct(secs, 0.75),
            },
            "entry_position_in_window_pct": (
                round(100 * pct(pos, 0.5), 1) if pos else None),
            "sides": dict(Counter(r["outcome"] for r in tf_buys if r["outcome"])),
            "both_sides_markets": len(arbs),
            "arb_avg_combined_price": round(sum(arb_sums) / len(arb_sums), 4) if arb_sums else None,
            "sold_before_close_markets": sum(1 for m in tf_markets if m["n_sells"]),
            "resolved_markets": len(resolved),
            "win_markets": len(wins),
            "win_rate_resolved": round(len(wins) / len(resolved), 3) if resolved else None,
            "cash_pnl_usdc": round(sum(m["cash_pnl"] for m in tf_markets), 2),
            "cash_pnl_resolved_usdc": round(sum(m["cash_pnl"] for m in resolved), 2),
        }

    # activity windows (the "only certain windows" question)
    hours = Counter(dt.datetime.fromtimestamp(r["ts"], md.ET).hour for r in buys)
    days = Counter(dt.datetime.fromtimestamp(r["ts"], md.ET).strftime("%a") for r in buys)
    sessions = []
    cur = None
    for r in buys:
        if cur and r["ts"] - cur["end_ts"] <= 45 * 60:
            cur["end_ts"] = r["ts"]
            cur["trades"] += 1
        else:
            if cur:
                sessions.append(cur)
            cur = {"start_ts": r["ts"], "end_ts": r["ts"], "trades": 1}
    if cur:
        sessions.append(cur)
    fmt_et = lambda t: dt.datetime.fromtimestamp(t, md.ET).strftime("%Y-%m-%d %H:%M ET")
    sessions_fmt = [{"start": fmt_et(s["start_ts"]), "end": fmt_et(s["end_ts"]),
                     "trades": s["trades"]} for s in sessions[-12:]]

    total_cash = round(sum(m["cash_pnl"] for m in markets.values()), 2)
    edge = detect_edge(by_tf, hours, markets)

    return {
        "records_total": len(records),
        "updown_events": len(updown),
        "updown_buys": len(buys),
        "updown_sells": len(sells),
        "redeems": len(redeems),
        "non_updown_events": len(records) - len(updown),
        "date_range": {
            "first": fmt_et(records[0]["ts"]) if records else None,
            "last": fmt_et(records[-1]["ts"]) if records else None,
        },
        "by_timeframe": by_tf,
        "windows": {
            "hour_of_day_et_buys": {h: hours.get(h, 0) for h in range(24) if hours.get(h)},
            "day_of_week_buys": dict(days),
            "recent_sessions": sessions_fmt,
            "n_sessions": len(sessions),
        },
        "cash_pnl_total_usdc": total_cash,
        "note": ("cash PnL = sells + redemptions - buys; markets closed >10min ago "
                 "with no redemption count as losses (unredeemed wins would "
                 "understate PnL)"),
        "edge_summary": edge,
        "markets_sample": sorted(
            ({k: m[k] for k in ("slug", "tf", "cash_pnl", "both_sides",
                                "combined_vwap_sum", "n_buys", "n_sells")}
             for m in markets.values()),
            key=lambda m: -abs(m["cash_pnl"]))[:15],
    }


def detect_edge(by_tf: dict, hours: Counter, markets: dict) -> list[str]:
    out = []
    total_mkts = sum(g["markets"] for g in by_tf.values()) or 1
    arb_mkts = sum(g["both_sides_markets"] for g in by_tf.values())
    arb_prices = [g["arb_avg_combined_price"] for g in by_tf.values()
                  if g["arb_avg_combined_price"]]
    if arb_mkts / total_mkts > 0.2 and arb_prices and min(arb_prices) < 0.995:
        out.append(f"COMPLETE-SET ARBITRAGE: buys BOTH sides in {arb_mkts}/{total_mkts} "
                   f"markets at a combined price < $1 (avg "
                   f"{sum(arb_prices)/len(arb_prices):.3f}) — locks in the spread "
                   "risk-free and waits for resolution.")
    for tf, g in by_tf.items():
        if tf == "other" or not g["buys"]:
            continue
        mp = g.get("median_buy_price")
        posn = g.get("entry_position_in_window_pct")
        if mp is not None and posn is not None:
            if mp >= 0.8 and posn >= 80:
                out.append(f"{tf}: LATE FAVORITE ENTRIES — median buy at "
                           f"{mp:.2f} with ~{posn:.0f}% of the window elapsed "
                           "(momentum-into-close, same family as our strategy).")
            elif mp <= 0.35:
                out.append(f"{tf}: LONGSHOT/REVERSAL BUYING — median buy price {mp:.2f}.")
        if g["markets"] and g["sold_before_close_markets"] / g["markets"] > 0.5:
            out.append(f"{tf}: scalper profile — exits before close in "
                       f"{g['sold_before_close_markets']}/{g['markets']} markets.")
        elif g["markets"] >= 5 and g["sold_before_close_markets"] / g["markets"] < 0.2:
            out.append(f"{tf}: holds to resolution (redeems rather than selling early).")
    if hours:
        top = [h for h, _ in hours.most_common()]
        cum, need, got = 0, 0.8 * sum(hours.values()), []
        for h in top:
            got.append(h)
            cum += hours[h]
            if cum >= need:
                break
        got.sort()
        out.append(f"ACTIVE WINDOW: 80% of buys fall in ET hours {got} — "
                   "not a 24/7 bot; likely manual sessions or a scheduled window.")
    if not out:
        out.append("No dominant pattern detected — inspect markets_sample and rerun "
                   "with --dump to share raw data.")
    return out


# ---------------------------------------------------------------------------
# Match against our runtime logs
# ---------------------------------------------------------------------------

def match_logs(records: list[dict], reports_dir: Path) -> dict:
    reports = {}
    if reports_dir.exists():
        for p in reports_dir.glob("*.json"):
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if obj.get("slug"):
                prev = reports.get(obj["slug"])
                # both strategies may report the same slot; favorite carries
                # the threshold context we compare against
                if prev is None or str(obj.get("strategy") or "favorite") == "favorite":
                    reports[obj["slug"]] = obj

    rows = []
    agree = 0
    for r in records:
        if r["type"] != "TRADE" or r["side"] != "BUY" or not r["cls"]:
            continue
        rep = reports.get(r["slug"]) or reports.get(r["event_slug"])
        if not rep:
            continue
        best_hb, best_d = None, None
        for hb in rep.get("attempts_tail") or []:
            t = None
            try:
                t = dt.datetime.fromisoformat(str(hb.get("ts")).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            d = abs(t - r["ts"])
            if best_d is None or d < best_d:
                best_hb, best_d = hb, d
        our_open = (rep.get("opened") or {})
        thr = ((rep.get("params") or {}).get("threshold")) or 0.70
        their_side = r["outcome"] if r["outcome"] in ("UP", "DOWN") else None
        our_ask = None
        if best_hb and their_side:
            our_ask = best_hb.get("clob_up_ask" if their_side == "UP" else "clob_down_ask")
        would_trigger = (our_ask is not None and our_ask >= thr)
        same_side = bool(our_open and their_side and our_open.get("side") == their_side)
        if same_side:
            agree += 1
        rows.append({
            "slug": r["slug"],
            "their_ts_et": dt.datetime.fromtimestamp(r["ts"], md.ET).strftime("%m-%d %H:%M:%S"),
            "their_side": their_side,
            "their_price": r["price"],
            "secs_before_close": round(r["cls"]["close_ts"] - r["ts"], 1),
            "our_nearest_hb_gap_s": round(best_d, 1) if best_d is not None else None,
            "our_ask_their_side": our_ask,
            "our_threshold_would_trigger": would_trigger,
            "our_action": (our_open.get("side") + f"@{our_open.get('entry_price')}"
                           if our_open else rep.get("result")),
            "same_side_as_us": same_side,
        })
    return {
        "overlapping_buy_trades": len(rows),
        "same_side_entries": agree,
        "rows": rows[:60],
        "note": ("overlap grows the longer the multi bot runs; heartbeats keep "
                 "only the last ~30 ticks per slot, so gap_s can be large for "
                 "early-window trades"),
    }


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", default=None, help="0x wallet address")
    ap.add_argument("--input", default=None, help="analyze a saved --dump file instead of fetching")
    ap.add_argument("--dump", default=None, help="save raw activity JSON here")
    ap.add_argument("--max-pages", type=int, default=40, help="500 events per page")
    ap.add_argument("--reports-dir",
                    default=str(Path(__file__).resolve().parents[1] / "runtime" / "reports"))
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    if args.input:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    elif args.wallet:
        raw = fetch_activity(args.wallet.strip().lower(), args.max_pages)
    else:
        ap.error("--wallet or --input required")
        return 2

    if args.dump:
        Path(args.dump).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    records = normalize(raw)
    result = analyze(records)
    result["wallet"] = args.wallet
    result["log_match"] = match_logs(records, Path(args.reports_dir))

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if not args.json_only:
        print("\n=== EDGE SUMMARY ===", file=sys.stderr)
        for line in result["edge_summary"]:
            print(" • " + line, file=sys.stderr)
        lm = result["log_match"]
        print(f"\nlog match: {lm['overlapping_buy_trades']} of their buys landed in "
              f"markets our bot also watched; {lm['same_side_entries']} matched our "
              "entry side.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
