#!/usr/bin/env python3
"""FULL-ANGLE deep study over the ENTIRE pooled lab history in data/lab/.

Usage:  python3 scripts/btc_deep_study.py <asset>        # btc | sol | xrp
        python3 scripts/btc_deep_study.py <asset> combos # study + targeted combo rules

Ground rules baked in:
  * OFFICIAL labels only (result_pm events).
  * EV = winrate - avg entry ask (the only number that pays).
  * Every table is split-half validated (first vs second half of rounds by
    time); a trend only counts if it is positive in BOTH halves (BOTH+ tag).
  * Signals are re-tested inside the tradeable band (ask <= 0.85) to see
    whether the book has already priced them.

Reads data/lab/*.jsonl.gz directly -- no decompression step needed.
"""
import gzip, glob, json, sys, statistics
from datetime import datetime, timezone

ASSET = sys.argv[1] if len(sys.argv) > 1 else "btc"
MODE = sys.argv[2] if len(sys.argv) > 2 else "study"

def _lab_files(kind, asset):
    """kind: trajectory|live ; asset: btc|sol|xrp. BTC files carry no suffix."""
    pats = ([f"data/lab/{kind}-2*.jsonl.gz", f"data/lab/{kind}.jsonl.gz",
             f"data/lab/{kind}-prev.jsonl.gz", f"data/lab/{kind}-OLD*.jsonl.gz"]
            if asset == "btc" else
            [f"data/lab/{kind}-{asset}-2*.jsonl.gz", f"data/lab/{kind}-{asset}.jsonl.gz",
             f"data/lab/{kind}-{asset}-prev.jsonl.gz"])
    out = []
    for p in pats:
        out.extend(sorted(glob.glob(p)))
    return out

def iter_events(kind, asset):
    for path in _lab_files(kind, asset):
        try:
            with gzip.open(path, "rt") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue
        except Exception:
            continue

# ---------------- load ----------------
samples = {}   # round -> list of sample dicts (subset of fields)
official = {}  # round -> UP/DOWN
res_move = {}  # round -> (open, close)
for e in iter_events("trajectory", ASSET):
    t = e.get("type")
    if t == "sample":
        r = e.get("round")
        if r is None: continue
        samples.setdefault(r, []).append(e)
    elif t == "result_pm":
        if e.get("outcome") in ("UP", "DOWN"):
            official[e["round"]] = e["outcome"]
    elif t == "result":
        o, c = e.get("open"), e.get("close")
        if isinstance(o, (int, float)) and isinstance(c, (int, float)):
            res_move[e["round"]] = (o, c)

preds = {}     # round -> prediction event (first one per round)
for e in iter_events("live", ASSET):
    if e.get("type") == "prediction":
        r = e.get("round")
        if r is not None and r not in preds:
            preds[r] = e

rounds = sorted(r for r in official if r in samples)
half_ts = rounds[len(rounds)//2] if rounds else 0

def near(r, sec, lo=None, hi=None):
    """sample nearest to `sec` seconds left (optionally bounded)."""
    best, bd = None, 1e9
    for s in samples.get(r, []):
        sl = s.get("sec_left")
        if sl is None: continue
        if lo is not None and not (lo <= sl <= hi): continue
        d = abs(sl - sec)
        if d < bd: best, bd = s, d
    return best if bd <= 60 else None

def ask_of(s, side):
    return s.get("up_ask") if side == "UP" else s.get("dn_ask")

def session(r):
    h = datetime.fromtimestamp(r, tz=timezone.utc).hour
    return "Asia" if h < 8 else ("Europe" if h < 16 else "US")

def is_wd(r):
    return datetime.fromtimestamp(r, tz=timezone.utc).weekday() < 5

def report(recs, label, min_n=8):
    """recs: list of (win_bool, price, round). prints pooled + split-half."""
    if len(recs) < min_n:
        print(f"   {label:<34} n={len(recs)} (<{min_n})"); return None
    n = len(recs); wr = sum(w for w,_,_ in recs)/n; ap = sum(p for _,p,_ in recs)/n
    h1 = [(w,p) for w,p,r in recs if r < half_ts]; h2 = [(w,p) for w,p,r in recs if r >= half_ts]
    def ev(rs): return (sum(w for w,_ in rs)/len(rs) - sum(p for _,p in rs)/len(rs)) if rs else None
    e1, e2 = ev(h1), ev(h2)
    both = "BOTH+" if (e1 is not None and e2 is not None and e1 > 0 and e2 > 0) else "     "
    f = lambda x: f"{x:+.3f}" if x is not None else "  -  "
    print(f"   {label:<34} n={n:<4d} win {wr:5.1%} @ {ap:.3f}  EV {wr-ap:+.3f}  [h1 {f(e1)} h2 {f(e2)}] {both}")
    return wr-ap

print(f"\n{'#'*74}\n##### {ASSET.upper()} — {len(rounds)} official rounds, {len(preds)} predictions joinable, split at {half_ts}\n{'#'*74}")

# =============== A. divergence type x EV ===============
print("\n=== A. DIVERGENCE TYPE (prediction side taken at its decision-time ask; official labels) ===")
div_recs = {}
for r in rounds:
    p = preds.get(r)
    if not p: continue
    feats = p.get("features") or {}
    dv = feats.get("divergence") or "none"
    side = p.get("direction")
    if side not in ("UP","DOWN"): continue
    s = near(r, p.get("seconds_left") or 150)
    if not s: continue
    px = ask_of(s, side)
    if not isinstance(px,(int,float)) or not (0 < px < 1): continue
    div_recs.setdefault(dv, []).append((side == official[r], float(px), r))
for dv, recs in sorted(div_recs.items(), key=lambda kv: -len(kv[1])):
    report(recs, f"div={dv}")
print("  -- same, only when div AGREES with prediction side (bullish->UP / bearish->DOWN):")
agree_recs, oppose_recs = [], []
for dv, recs in div_recs.items():
    if dv == "none": continue
    for rec in recs:
        r = rec[2]; side = preds[r]["direction"]
        bull = "bullish" in dv
        (agree_recs if (bull and side=="UP") or ((not bull) and side=="DOWN") else oppose_recs).append(rec)
report(agree_recs, "div agrees with side")
report(oppose_recs, "div opposes side")

# =============== B/C. every feature, bucketed, and inside ask<=0.85 ===============
print("\n=== B. SCALAR FEATURES (prediction side; terciles over pooled values) ===")
FEATS = ["rsi_1m","macd_hist_1m","bb_pctb_1m","roc_1m","rel_volume_1m",
         "agreement","vol_factor","ema_gap_5m","div_signal","score","confidence"]
frecs = []   # (featdict, side, win, px, r)
for r in rounds:
    p = preds.get(r)
    if not p: continue
    side = p.get("direction")
    if side not in ("UP","DOWN"): continue
    s = near(r, p.get("seconds_left") or 150)
    if not s: continue
    px = ask_of(s, side)
    if not isinstance(px,(int,float)) or not (0 < px < 1): continue
    fd = dict(p.get("features") or {})
    fd["confidence"] = p.get("confidence")
    frecs.append((fd, side, side == official[r], float(px), r))
for ft in FEATS:
    vals = sorted(fd.get(ft) for fd,_,_,_,_ in frecs if isinstance(fd.get(ft),(int,float)))
    if len(vals) < 30:
        print(f"   {ft:<14} insufficient data"); continue
    t1, t2 = vals[len(vals)//3], vals[2*len(vals)//3]
    print(f"   {ft} (terciles at {t1:g} / {t2:g}):")
    for name, cond in (("LOW", lambda v: v <= t1), ("MID", lambda v: t1 < v <= t2), ("HIGH", lambda v: v > t2)):
        recs = [(w,p,r) for fd,_,w,p,r in frecs if isinstance(fd.get(ft),(int,float)) and cond(fd[ft])]
        report(recs, f"  {name}")

print("\n=== C. FEATURES INSIDE THE TRADEABLE BAND (ask <= 0.85 only) — is anything unpriced? ===")
band = [(fd,sd,w,p,r) for fd,sd,w,p,r in frecs if p <= 0.85]
print(f"   ({len(band)} of {len(frecs)} prediction-rounds have side ask <= 0.85)")
for ft in FEATS:
    vals = sorted(fd.get(ft) for fd,_,_,_,_ in band if isinstance(fd.get(ft),(int,float)))
    if len(vals) < 30: continue
    t2 = vals[2*len(vals)//3]
    hi = [(w,p,r) for fd,_,w,p,r in band if isinstance(fd.get(ft),(int,float)) and fd[ft] > t2]
    lo = [(w,p,r) for fd,_,w,p,r in band if isinstance(fd.get(ft),(int,float)) and fd[ft] <= t2]
    ehi = report(hi, f"{ft} TOP-tercile")
    elo = report(lo, f"{ft} rest")

# =============== D. leading-side ask band x time ===============
print("\n=== D. LEADING SIDE by ask band x time window (buy leader at that ask; official) ===")
for wlo, whi in ((60,120),(120,180),(180,240)):
    print(f"  window {wlo}-{whi}s left:")
    for blo, bhi in ((0.50,0.65),(0.65,0.75),(0.75,0.85),(0.85,0.95)):
        recs = []
        for r in rounds:
            for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
                sl = s.get("sec_left") or 0
                if not (wlo <= sl <= whi): continue
                mv = s.get("move") or 0
                if abs(mv) < 1e-12: continue
                side = "UP" if mv > 0 else "DOWN"
                px = ask_of(s, side)
                if isinstance(px,(int,float)) and blo <= px < bhi:
                    recs.append((side == official[r], float(px), r)); break
        report(recs, f"ask {blo:.2f}-{bhi:.2f}")

# =============== E. session ===============
print("\n=== E. SESSION (leading side at 120-180s, ask <= 0.85) ===")
for sess in ("Asia","Europe","US"):
    recs = []
    for r in rounds:
        if session(r) != sess: continue
        for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
            sl = s.get("sec_left") or 0
            if not (120 <= sl <= 180): continue
            mv = s.get("move") or 0
            if abs(mv) < 1e-12: continue
            side = "UP" if mv > 0 else "DOWN"
            px = ask_of(s, side)
            if isinstance(px,(int,float)) and 0 < px <= 0.85:
                recs.append((side == official[r], float(px), r)); break
    report(recs, f"{sess}")
recs = [ ]
print("  weekday vs weekend (same rule):")
for wd in (True, False):
    recs = []
    for r in rounds:
        if is_wd(r) != wd: continue
        for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
            sl = s.get("sec_left") or 0
            if not (120 <= sl <= 180): continue
            mv = s.get("move") or 0
            if abs(mv) < 1e-12: continue
            side = "UP" if mv > 0 else "DOWN"
            px = ask_of(s, side)
            if isinstance(px,(int,float)) and 0 < px <= 0.85:
                recs.append((side == official[r], float(px), r)); break
    report(recs, "weekday" if wd else "weekend")

# =============== F. streaks ===============
print("\n=== F. ROUND-LEVEL MOMENTUM (does last official outcome predict the next?) ===")
seq = [(r, official[r]) for r in rounds]
follow = same = 0
for i in range(1, len(seq)):
    if seq[i][0] - seq[i-1][0] > 1800: continue   # only adjacent-ish rounds
    same += 1
    if seq[i][1] == seq[i-1][1]: follow += 1
if same:
    print(f"   adjacent pairs: {same}   repeat rate: {follow/same:.1%}  (50% = coin flip)")
# streak length 2:
rep2 = tot2 = 0
for i in range(2, len(seq)):
    if seq[i][0]-seq[i-1][0] > 1800 or seq[i-1][0]-seq[i-2][0] > 1800: continue
    if seq[i-1][1] == seq[i-2][1]:
        tot2 += 1
        if seq[i][1] == seq[i-1][1]: rep2 += 1
if tot2:
    print(f"   after 2 same in a row: {tot2} cases, third same: {rep2/tot2:.1%}")
# and can you BUY the repeat cheaply? price of "same as last" side at 150s:
recs = []
for i in range(1, len(seq)):
    if seq[i][0] - seq[i-1][0] > 1800: continue
    r, side = seq[i][0], seq[i-1][1]
    s = near(r, 150)
    if not s: continue
    px = ask_of(s, side)
    if isinstance(px,(int,float)) and 0 < px < 1:
        recs.append((side == official[r], float(px), r))
report(recs, "buy last-round's winner @150s")

# =============== H. book-size imbalance ===============
print("\n=== H. BOOK-DEPTH IMBALANCE at ~150s (never analyzed before) ===")
print("   rule: side whose ASK depth is thinner (scarcer supply) — and bid-depth variants")
for name, pick in (
    ("thin-ask side (supply scarce)", lambda s: ("UP" if (s.get("up_sz") or 0) < (s.get("dn_sz") or 0) else "DOWN")),
    ("thick-bid side (demand heavy)", lambda s: ("UP" if (s.get("up_bid_sz") or 0) > (s.get("dn_bid_sz") or 0) else "DOWN")),
):
    recs = []
    for r in rounds:
        s = near(r, 150)
        if not s: continue
        if not all(isinstance(s.get(k),(int,float)) for k in ("up_sz","dn_sz","up_bid_sz","dn_bid_sz")): continue
        side = pick(s)
        px = ask_of(s, side)
        if isinstance(px,(int,float)) and 0 < px < 1:
            recs.append((side == official[r], float(px), r))
    report(recs, name)
    # same but only when imbalance is extreme (>3x)
    recs3 = []
    for r in rounds:
        s = near(r, 150)
        if not s: continue
        if not all(isinstance(s.get(k),(int,float)) for k in ("up_sz","dn_sz","up_bid_sz","dn_bid_sz")): continue
        a, b = (s.get("up_sz") or 0), (s.get("dn_sz") or 0)
        if name.startswith("thin"):
            if min(a,b) <= 0 or max(a,b)/max(min(a,b),1e-9) < 3: continue
        else:
            a, b = (s.get("up_bid_sz") or 0), (s.get("dn_bid_sz") or 0)
            if min(a,b) <= 0 or max(a,b)/max(min(a,b),1e-9) < 3: continue
        side = pick(s)
        px = ask_of(s, side)
        if isinstance(px,(int,float)) and 0 < px < 1:
            recs3.append((side == official[r], float(px), r))
    report(recs3, f"  ...only when >3x imbalance")

# =============== I. conf crossing + cap (current live config) ===============
print("\n=== I. CONF >=0.70 CROSSING + CAP 0.85 (the config running right now; pooled validation) ===")
for cap in (1.0, 0.85):
    recs = []
    for r in rounds:
        for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
            c, side = s.get("ind_conf"), s.get("ind_dir")
            if c is None or side not in ("UP","DOWN") or c < 0.70: continue
            px = ask_of(s, side)
            if isinstance(px,(int,float)) and 0 < px < 1:
                if px <= cap:
                    recs.append((side == official[r], float(px), r))
                break   # first crossing only; if too pricey, no trade
    report(recs, f"crossing conf>=0.70, cap {cap:.2f}")

if MODE == 'combos':
    print(f"===== {ASSET.upper()} combos ({len(rounds)} rounds) =====")

    # -- 1. crossing conf>=0.70 cap 0.85 baseline + divergence overlays (BTC idea)
    def crossing(cap, want_div=None, sess_not=None, cross_lo=None):
        recs=[]
        for r in rounds:
            p = preds.get(r); feats = (p.get("features") or {}) if p else {}
            dv = feats.get("divergence") or "none"
            if want_div == "present" and dv == "none": continue
            if want_div == "absent" and dv != "none": continue
            if sess_not and session(r) == sess_not: continue
            for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
                c, side = s.get("ind_conf"), s.get("ind_dir")
                if c is None or side not in ("UP","DOWN") or c < 0.70: continue
                if cross_lo is not None and (s.get("sec_left") or 0) < cross_lo: break
                px = ask_of(s, side)
                if isinstance(px,(int,float)) and 0 < px < 1:
                    if px <= cap: recs.append((side == official[r], float(px), r))
                    break
        return recs

    report(crossing(0.85), "crossing conf>=0.70 cap0.85 (LIVE CONFIG)")
    report(crossing(0.85, want_div="present"), "  + divergence present")
    report(crossing(0.85, want_div="absent"), "  + divergence absent")
    report(crossing(0.85, cross_lo=120), "  + crossing must fire >=120s left")

    # -- 2. leading side, EARLY window 180-240s, band 0.75-0.85 (sweet-spot refresh)
    def lead_band(wlo, whi, blo, bhi, quiet=None, no_div=False):
        recs=[]
        for r in rounds:
            if no_div:
                p = preds.get(r); feats=(p.get("features") or {}) if p else {}
                if (feats.get("divergence") or "none") != "none": continue
            if quiet is not None:
                p = preds.get(r); feats=(p.get("features") or {}) if p else {}
                vf = feats.get("vol_factor")
                if not isinstance(vf,(int,float)) or vf > quiet: continue
            for s in sorted(samples[r], key=lambda q: -(q.get("sec_left") or 0)):
                sl = s.get("sec_left") or 0
                if not (wlo <= sl <= whi): continue
                mv = s.get("move") or 0
                if abs(mv) < 1e-12: continue
                side = "UP" if mv > 0 else "DOWN"
                px = ask_of(s, side)
                if isinstance(px,(int,float)) and blo <= px < bhi:
                    recs.append((side == official[r], float(px), r)); break
        return recs

    report(lead_band(180,240,0.75,0.85), "lead 180-240s ask 0.75-0.85 (new sweet spot)")
    report(lead_band(180,240,0.75,0.85,no_div=True), "  + no divergence")
    report(lead_band(180,270,0.75,0.90), "lead 180-270s ask 0.75-0.90 (wider)")

    # -- 3. SOL quiet-round rule: prediction side, vol_factor low, no div, ask<=0.85
    def quiet_rule(vf_max, no_div, cap):
        recs=[]
        for r in rounds:
            p = preds.get(r)
            if not p: continue
            feats = p.get("features") or {}
            vf = feats.get("vol_factor")
            if not isinstance(vf,(int,float)) or vf > vf_max: continue
            if no_div and (feats.get("divergence") or "none") != "none": continue
            side = p.get("direction")
            if side not in ("UP","DOWN"): continue
            s = near(r, p.get("seconds_left") or 150)
            if not s: continue
            px = ask_of(s, side)
            if isinstance(px,(int,float)) and 0 < px <= cap:
                recs.append((side == official[r], float(px), r))
        return recs

    report(quiet_rule(0.77, True, 0.85), "QUIET rule: vol_factor<=0.77 + no div + ask<=0.85")
    report(quiet_rule(0.77, False, 0.85), "  same without div veto")

    # -- 4. score-top rule (XRP idea): score top tercile + ask<=0.85
    svals = sorted((p.get("features") or {}).get("score") for p in preds.values()
                   if isinstance((p.get("features") or {}).get("score"),(int,float)))
    if svals:
        t2 = svals[2*len(svals)//3]
        recs=[]
        for r in rounds:
            p = preds.get(r)
            if not p: continue
            sc = (p.get("features") or {}).get("score")
            if not isinstance(sc,(int,float)) or sc <= t2: continue
            side = p.get("direction")
            if side not in ("UP","DOWN"): continue
            s = near(r, p.get("seconds_left") or 150)
            if not s: continue
            px = ask_of(s, side)
            if isinstance(px,(int,float)) and 0 < px <= 0.85:
                recs.append((side == official[r], float(px), r))
        report(recs, f"SCORE-TOP rule: score>{t2:.3f} + ask<=0.85")
