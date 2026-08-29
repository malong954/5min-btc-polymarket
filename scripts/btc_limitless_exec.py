#!/usr/bin/env python3
"""Limitless BTC15 executor — routes the paper engine's entries to REAL orders.

Architecture (deliberately thin):
    LivePaperEngine (btc_live_paper)  = the validated decision core, unchanged.
        fed with:  Binance spot/1m bars (signal source, venue-independent)
                   Limitless best asks  (btc_limitless.current_prices)
                   Limitless official resolutions (resolved_outcome — the
                   venue's own Chainlink settle, no label disagreement)
    ExecGuard                          = independent risk rails, checked AFTER
        the engine decides and BEFORE any real order. The engine can be wrong;
        the guard's job is to keep wrong cheap.
    Order path                         = limitless-sdk (EIP-712 signing by the
        venue's own maintained code), FAK buy with a hard price cap. This file
        never hand-rolls order signing.

DRY-RUN IS THE DEFAULT. A real order is only possible when ALL of:
    --live flag passed
    LIMITLESS_LIVE=1 in the environment
    LIMITLESS_WALLET_PK set (env or .env; never an argument, never the repo)
    LIMITLESS_API_KEY / LIMITLESS_API_SECRET set (HMAC — see btc_limitless_auth)
Anything less and every would-be order is logged as `exec_order mode=dry` with
the exact payload that WOULD have been sent, so the dry run is a full rehearsal.

Risk rails (each measured or bitterly learned on the paper side):
    per-trade cap      --max-stake (default $5)   tiered sizing above it is clamped
    daily kill switch  --daily-loss-kill ($15)    net realized UTC-day loss parks
                                                  real orders; paper continues as
                                                  shadow so the day stays graded
    open-exposure cap  --max-open ($10)           at most this much at risk at once
    one order/round                               re-fires can't double-spend
    thin-book refusal                             quoted ask depth must cover the
                                                  stake (dust asks = phantom fills)
    slip refusal       --slip-tol (0.02)          fresh ask above decision ask +
                                                  tol = the book moved against us
                                                  mid-flight; skip, don't chase
    fee accounting     0.07 x p x (1-p) per share charged on every real fill in
                       the log (TREND_RESEARCH.md; confirm rate on first ticket)

Sessions default to EUROPE ONLY — the single cell that survives the taker fee
(PLAYBOOK: BTC15-Europe +2.0c gross, ~+1.9c realistic; everything else net-dead).

Run on the Mac (the analysis container cannot reach limitless.exchange):
    .venv/bin/python scripts/btc_limitless_exec.py                    # dry-run
    LIMITLESS_LIVE=1 .venv/bin/python scripts/btc_limitless_exec.py --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Optional, TextIO

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import btc_limitless as lmts
from btc_live_paper import (LivePaperEngine, fetch_recent_1m, fetch_spot,
                            format_event, bucket_slot)

# fee = shares x RATE x price x (1-price)  (user's fee reckoning, 2026-08-01;
# ~1.3-1.5c/share at our entry prices. Confirm against the first real ticket.)
TAKER_FEE_RATE = 0.07


def taker_fee(shares: float, price: float) -> float:
    return shares * TAKER_FEE_RATE * price * (1.0 - price)


def fetch_tokens(slug: str) -> Optional[dict[str, str]]:
    """{'yes': id, 'no': id} for a market — UP buys yes, DOWN buys no
    (confirmed shape: btc_limitless.resolved_outcome reads the same dict)."""
    try:
        m = lmts._get_json(f"{lmts.REST}/markets/{slug}")
    except Exception:
        return None
    t = m.get("tokens") if isinstance(m, dict) else None
    if isinstance(t, dict) and "yes" in t and "no" in t:
        return {"yes": str(t["yes"]), "no": str(t["no"])}
    return None


class ExecGuard:
    """Hard risk rails, independent of the engine. Stateless about WHY a trade
    was chosen — it only bounds how much a wrong one can cost."""

    def __init__(self, max_stake: float = 5.0, daily_loss_kill: float = 15.0,
                 max_open: float = 10.0):
        self.max_stake = max_stake
        self.daily_loss_kill = daily_loss_kill
        self.max_open = max_open
        self.open_risk: dict[int, float] = {}     # round -> $ at risk
        self.day: Optional[str] = None            # UTC day of the running total
        self.day_pnl = 0.0                        # realized net, resets daily
        self.killed = False                       # sticky until the UTC day rolls
        self.ordered: set[int] = set()            # rounds that already sent one

    def _roll_day(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.day:
            self.day, self.day_pnl, self.killed = day, 0.0, False

    def clamp(self, stake: float) -> float:
        return round(min(stake, self.max_stake), 2)

    def allow(self, now: float, rnd: int, stake: float) -> tuple[bool, str]:
        self._roll_day(now)
        if self.killed:
            return False, "daily_kill"
        if rnd in self.ordered:
            return False, "already_ordered"
        if sum(self.open_risk.values()) + stake > self.max_open + 1e-9:
            return False, "max_open"
        return True, "ok"

    def record_order(self, rnd: int, stake: float) -> None:
        self.ordered.add(rnd)
        self.open_risk[rnd] = stake

    def record_settle(self, now: float, rnd: int, net_pnl: float) -> bool:
        """Realized net P&L for a round with a real fill. Returns True if this
        settle tripped the daily kill switch."""
        self._roll_day(now)
        self.open_risk.pop(rnd, None)
        self.day_pnl += net_pnl
        if not self.killed and self.day_pnl <= -self.daily_loss_kill:
            self.killed = True
            return True
        return False


def place_real_order(slug: str, token_id: str, price: float, shares: float
                     ) -> tuple[bool, Any]:
    """FAK buy via the official SDK (EIP-712 signed with the wallet key).
    price is a hard cap: fills at or below it, unfilled remainder is killed."""
    import asyncio

    from limitless_sdk import Client
    from limitless_sdk.types.api_tokens import HMACCredentials
    from limitless_sdk.types.orders import OrderType, Side

    key = os.environ["LIMITLESS_API_KEY"]
    sec = os.environ["LIMITLESS_API_SECRET"]
    pk = os.environ["LIMITLESS_WALLET_PK"]

    async def go():
        async with Client(hmac_credentials=HMACCredentials(
                tokenId=key, secret=sec)) as client:
            await client.markets.get_market(slug)   # cache venue for signing
            oc = client.new_order_client(pk)
            return await oc.create_order(
                token_id=token_id, side=Side.BUY, order_type=OrderType.FAK,
                market_slug=slug, price=round(price, 3), size=shares)

    try:
        resp = asyncio.run(go())
        return True, resp.model_dump() if hasattr(resp, "model_dump") else resp
    except Exception as e:  # noqa: BLE001 — a failed order must never kill the loop
        return False, f"{type(e).__name__}: {e}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Limitless BTC15 executor (dry-run by default; see --live)")
    ap.add_argument("--live", action="store_true",
                    help="arm REAL orders (also needs LIMITLESS_LIVE=1 + wallet key; "
                         "anything less falls back to dry-run)")
    ap.add_argument("--max-stake", type=float, default=5.0, help="hard per-trade cap ($)")
    ap.add_argument("--daily-loss-kill", type=float, default=15.0,
                    help="park real orders after this much net realized UTC-day loss")
    ap.add_argument("--max-open", type=float, default=10.0, help="max total $ at risk at once")
    ap.add_argument("--slip-tol", type=float, default=0.02,
                    help="refuse if the fresh ask exceeds the decision ask by more than this")
    ap.add_argument("--min-depth-x", type=float, default=1.0,
                    help="quoted ask depth must cover this many times the stake's shares")
    # Engine config — defaults are the BTC15 production cell (lab.sh values;
    # keep in sync with the paper trader so real and paper grade identically).
    ap.add_argument("--sessions", default="europe",
                    help="sessions to trade (comma list; default europe — the only "
                         "cell that survives the taker fee). '' = all")
    ap.add_argument("--lead-min-conf", type=float, default=0.60)
    ap.add_argument("--lead-hi", type=float, default=720.0, help="lead window opens (sec left; 240x3)")
    ap.add_argument("--lead-lo", type=float, default=540.0, help="lead window closes (sec left; 180x3)")
    ap.add_argument("--lead-max-price", type=float, default=0.85)
    ap.add_argument("--skip-band", default="0.60:0.80",
                    help="mid-band refusal (BTC15 ex-US: +3.9c -> +9.6c). 'none' = off")
    ap.add_argument("--ask-fall-veto", type=float, default=0.02)
    ap.add_argument("--cooldown-loss", type=int, default=0,
                    help="0 for BTC15 (it WINS after losses; +8.1c both halves)")
    ap.add_argument("--sizing", default="tiered")
    ap.add_argument("--tiers", default="0.10,0.05,0.05")
    ap.add_argument("--stake-usd", type=float, default=2.0)
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--klines-every", type=float, default=15.0)
    ap.add_argument("--history-min", type=int, default=180)
    ap.add_argument("--log", default="out/live-btc-limitless-15m.jsonl")
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    # --- arm/deny the live path (fail CLOSED: any missing piece = dry-run) ---
    have_env = os.environ.get("LIMITLESS_LIVE") == "1"
    have_pk = bool(os.environ.get("LIMITLESS_WALLET_PK"))
    have_hmac = bool(os.environ.get("LIMITLESS_API_KEY")
                     and os.environ.get("LIMITLESS_API_SECRET"))
    if not have_hmac and os.path.exists(".env"):
        for line in open(".env"):
            line = line.strip()
            if "=" in line and line.split("=", 1)[0] in (
                    "LIMITLESS_API_KEY", "LIMITLESS_API_SECRET", "LIMITLESS_WALLET_PK"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v.strip().strip('"\''))
        have_pk = bool(os.environ.get("LIMITLESS_WALLET_PK"))
        have_hmac = bool(os.environ.get("LIMITLESS_API_KEY")
                         and os.environ.get("LIMITLESS_API_SECRET"))
    live = args.live and have_env and have_pk and have_hmac
    if args.live and not live:
        missing = [n for n, ok in (("LIMITLESS_LIVE=1", have_env),
                                   ("LIMITLESS_WALLET_PK", have_pk),
                                   ("LIMITLESS_API_KEY/SECRET", have_hmac)) if not ok]
        print(f"# --live requested but missing {', '.join(missing)} -> DRY-RUN",
              file=sys.stderr)
    if live:
        try:
            import limitless_sdk  # noqa: F401 — placement dependency, fail at start not mid-trade
        except ImportError:
            print("# limitless-sdk not installed (pip install limitless-sdk) -> DRY-RUN",
                  file=sys.stderr)
            live = False

    sessions = ({s.strip().lower() for s in args.sessions.split(",") if s.strip()}
                or None) if args.sessions else None
    skip_band = None
    if args.skip_band and args.skip_band != "none":
        lo, hi = (float(x) for x in args.skip_band.replace(":", ",").split(","))
        skip_band = (lo, hi)
    tiers = tuple(float(x) for x in args.tiers.split(","))

    logf: Optional[TextIO] = None
    if args.log:
        d = os.path.dirname(args.log)
        if d:
            os.makedirs(d, exist_ok=True)
        logf = open(args.log, "a")

    # 'Decisive move' auto-scales with price (0.016% of spot — exactly the $10
    # BTC rule at ~$62k), same as the paper trader, so the two grade alike.
    lead_min_move = 10.0
    try:
        s0 = fetch_spot("binance", symbol="BTCUSDT")
        if isinstance(s0, (int, float)) and s0 > 0:
            lead_min_move = round(s0 * 1.6e-4, 6)
    except Exception:
        pass

    engine = LivePaperEngine(
        entry_rule="lead", require_market_price=True, log=logf,
        entry_window_sec=720.0, min_entry_sec=90.0,
        lead_window=(args.lead_hi, args.lead_lo),
        lead_max_price=args.lead_max_price, lead_min_conf=args.lead_min_conf,
        lead_min_move=lead_min_move, lead_persist=5.0,
        max_entry_price=args.lead_max_price,
        sessions=sessions, skip_band=skip_band,
        cooldown_loss=args.cooldown_loss, ask_fall_veto=args.ask_fall_veto,
        sizing=args.sizing, tiers=tiers, stake_usd=args.stake_usd,
        bankroll=args.bankroll, asset="BTC", window="15m",
    )
    guard = ExecGuard(args.max_stake, args.daily_loss_kill, args.max_open)
    real_fills: dict[int, dict[str, Any]] = {}   # round -> fill record

    mode = "LIVE" if live else "DRY"
    print(f"# limitless executor | BTC 15m | mode={mode} sessions="
          f"{','.join(sorted(sessions)) if sessions else 'all'} "
          f"max_stake=${args.max_stake:.2f} daily_kill=${args.daily_loss_kill:.2f} "
          f"max_open=${args.max_open:.2f} fee={TAKER_FEE_RATE}xp(1-p)/share",
          file=sys.stderr)
    engine._emit({"ts": int(time.time()), "type": "config", "venue": "limitless",
                  "mode": mode, "entry_rule": "lead",
                  "sessions": sorted(sessions) if sessions else None,
                  "lead_hi": args.lead_hi, "lead_lo": args.lead_lo,
                  "lead_max_price": args.lead_max_price,
                  "lead_min_conf": args.lead_min_conf,
                  "skip_band": list(skip_band) if skip_band else None,
                  "ask_fall_veto": args.ask_fall_veto,
                  "cooldown_loss": args.cooldown_loss,
                  "sizing": args.sizing, "tiers": list(tiers),
                  "stake_usd": args.stake_usd, "bankroll": args.bankroll,
                  "max_stake": args.max_stake,
                  "daily_loss_kill": args.daily_loss_kill,
                  "max_open": args.max_open, "slip_tol": args.slip_tol,
                  "fee_rate": TAKER_FEE_RATE})

    slot = 900
    n = 0
    bars: list = []
    last_klines = 0.0
    official: dict[int, str] = {}
    official_tries: dict[int, int] = {}
    try:
        while args.max_steps is None or n < args.max_steps:
            try:
                now = time.time()
                cur = bucket_slot(int(now), slot)
                sec_left = (cur + slot) - now

                # Official Limitless resolutions for closed rounds we still hold
                # or shadow — the venue's own Chainlink settle, so no reference
                # error is possible.
                waiting = [rs for rs in list(engine.positions) + list(engine.shadows)
                           if rs not in engine.settled and rs not in engine.shadow_settled
                           and rs not in official and now >= rs + slot + 20
                           and official_tries.get(rs, 0) < 8]
                for rs in waiting[:2]:
                    oc = None
                    try:
                        oc = lmts.resolved_outcome(lmts.current_slug(rs, "btc", "15m"))
                    except Exception:
                        pass
                    if oc in ("UP", "DOWN"):
                        official[rs] = oc
                    else:
                        official_tries[rs] = official_tries.get(rs, 0) + 1

                pending_settle = any(rs + slot <= now and rs not in engine.settled
                                     for rs in engine.positions)
                in_window = engine.min_entry_sec <= sec_left <= engine.entry_window_sec
                if (not bars or now - last_klines >= args.klines_every
                        or in_window or pending_settle):
                    bars = fetch_recent_1m("binance", args.history_min, symbol="BTCUSDT")
                    last_klines = now
                spot = fetch_spot("binance", symbol="BTCUSDT")

                entry_prices = None
                slug = None
                if in_window:
                    px = None
                    try:
                        px = lmts.current_prices(now, asset="btc", window="15m")
                    except Exception as e:
                        print(f"# limitless price error: {e}", file=sys.stderr)
                    if px:
                        slug = px.get("slug")
                        entry_prices = {"UP": px.get("UP"), "DOWN": px.get("DOWN"),
                                        "UP_size": px.get("UP_size"),
                                        "DOWN_size": px.get("DOWN_size")}

                events = engine.step(now, bars, spot=spot, entry_prices=entry_prices,
                                     official=official)
                for ev in events:
                    if not (args.quiet and ev["type"] == "heartbeat"):
                        print(format_event(ev))

                # --- route entries to the order path ---
                for ev in events:
                    if ev.get("type") != "entry":
                        continue
                    rnd = ev["round"]
                    stake = guard.clamp(float(ev["stake_usd"]))
                    ok, why = guard.allow(now, rnd, stake)
                    # Fresh book re-read: honest fill for paper AND the price
                    # cap for the real order come from the same quote.
                    px2 = None
                    try:
                        px2 = lmts.current_prices(time.time(), asset="btc",
                                                  window="15m", slug=slug)
                    except Exception:
                        pass
                    if px2:
                        rq = engine.apply_requote(
                            rnd, {"UP": px2.get("UP"), "DOWN": px2.get("DOWN")},
                            time.time())
                        if rq is not None:
                            print(format_event(rq))
                    side = ev["side"]
                    fresh = px2.get(side) if px2 else None
                    fresh_sz = px2.get(f"{side}_size") if px2 else None
                    decision = float(ev["entry_price"])
                    if ok and not isinstance(fresh, (int, float)):
                        ok, why = False, "no_fresh_quote"
                    if ok and fresh > decision + args.slip_tol:
                        ok, why = False, "slipped"
                    if ok and fresh > args.lead_max_price:
                        ok, why = False, "price_capped"
                    cap = min(fresh, decision + args.slip_tol) if ok else None
                    shares = round(stake / cap, 3) if ok else None
                    if ok and isinstance(fresh_sz, (int, float)) \
                            and fresh_sz < shares * args.min_depth_x:
                        ok, why = False, "thin_book"
                    token = None
                    if ok:
                        toks = fetch_tokens(slug) if slug else None
                        token = (toks or {}).get("yes" if side == "UP" else "no")
                        if not token:
                            ok, why = False, "no_token_id"
                    if not ok:
                        engine._emit({"ts": int(time.time()), "type": "exec_skip",
                                      "venue": "limitless", "round": rnd,
                                      "side": side, "reason": why,
                                      "stake_usd": stake,
                                      "decision_ask": decision, "fresh_ask": fresh})
                        continue
                    payload = {"market_slug": slug, "token_id": token,
                               "side": "BUY", "order_type": "FAK",
                               "price": round(cap, 3), "shares": shares,
                               "stake_usd": stake}
                    if live:
                        sent, resp = place_real_order(slug, token, cap, shares)
                    else:
                        sent, resp = True, "dry-run: not sent"
                    oev = engine._emit({"ts": int(time.time()), "type": "exec_order",
                                        "venue": "limitless", "mode": mode,
                                        "round": rnd, "side": side, "ok": sent,
                                        **payload,
                                        "est_fee": round(taker_fee(shares, cap), 4),
                                        "response": resp if isinstance(resp, str)
                                        else json.dumps(resp)[:400]})
                    print(format_event(oev))
                    if sent:
                        guard.record_order(rnd, stake)
                        real_fills[rnd] = {"side": side, "price": cap,
                                           "shares": shares, "stake": stake}

                # --- settle real fills off the engine's graded settles ---
                for ev in events:
                    if ev.get("type") != "settle" or ev["round"] not in real_fills:
                        continue
                    rnd = ev["round"]
                    f = real_fills.pop(rnd)
                    fee = taker_fee(f["shares"], f["price"])
                    win = ev.get("result") == "win"
                    gross = f["shares"] * (1.0 - f["price"]) if win else -f["stake"]
                    net = gross - fee
                    tripped = guard.record_settle(now, rnd, net)
                    sev = engine._emit({"ts": int(now), "type": "exec_settle",
                                        "venue": "limitless", "mode": mode,
                                        "round": rnd, "side": f["side"],
                                        "result": ev.get("result"),
                                        "fill_price": f["price"],
                                        "shares": f["shares"],
                                        "gross_usd": round(gross, 4),
                                        "fee_usd": round(fee, 4),
                                        "net_usd": round(net, 4),
                                        "day_net_usd": round(guard.day_pnl, 2)})
                    print(format_event(sev))
                    if tripped:
                        kev = engine._emit({"ts": int(now), "type": "exec_kill",
                                            "venue": "limitless",
                                            "day_net_usd": round(guard.day_pnl, 2),
                                            "limit": -args.daily_loss_kill,
                                            "note": "daily loss kill: real orders "
                                                    "parked until the UTC day rolls; "
                                                    "paper continues as shadow"})
                        print(format_event(kev))
            except Exception as e:  # noqa: BLE001
                print(f"# poll error: {e}", file=sys.stderr)
            n += 1
            if args.max_steps is not None and n >= args.max_steps:
                break
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    finally:
        s = engine.stats
        wr = (s["wins"] / s["trades"]) if s["trades"] else 0.0
        print(f"# summary: {s['trades']} settled, winrate {wr:.1%}, "
              f"paper ${engine.balance:.2f}, real day net ${guard.day_pnl:+.2f} "
              f"(mode={mode})", file=sys.stderr)
        if logf:
            logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
