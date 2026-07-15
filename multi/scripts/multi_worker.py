#!/usr/bin/env python3
"""
Multi-timeframe Polymarket Up/Down worker.

One worker owns one (asset, timeframe) pair and loops over consecutive market
slots. Strategy logic is ported from the proven BTC 5m runner
(scripts/test_btc_5m_session_exit_sl.py): threshold entry on CLOB best ask,
stop-loss + pre-close time exit, FAK close with GTC/force fallback. All timing
parameters are scaled per timeframe and overridable via the multi profiles
config.

Modes:
  paper (default): simulate fills against live CLOB books; no orders placed.
                   No auth or external repo required.
  live:            delegate real order placement/close to the external
                   pm_live_trade_runner.py (the same engine the og 5m bot uses).

Cross-worker safety: every worker checks a shared PortfolioGuard state file
(fcntl-locked JSON) before opening, enforcing max concurrent positions, max
total exposure, and a shared daily loss cap across all parallel workers.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import market_discovery as md  # noqa: E402

UTC = dt.timezone.utc
STOP = False


def _on_signal(signum, frame):  # noqa: ARG001
    global STOP
    STOP = True


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def log_line(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Seeded from the og 5m bot's gathered live data (conservative profile:
# threshold 0.70, stake $5, SL 25%, exit 20s before close, min-entry 60s).
# Longer timeframes scale the timing windows; entry window shrinks as a
# fraction of duration because the momentum-into-close signal concentrates
# near expiry.
DEFAULT_TF_PARAMS: dict[str, dict[str, Any]] = {
    "5m": dict(entry_window_sec_left=120, min_entry_seconds_left=60,
               exit_before_sec=20, poll_sec=3.0),
    "15m": dict(entry_window_sec_left=300, min_entry_seconds_left=120,
                exit_before_sec=30, poll_sec=5.0),
    "1h": dict(entry_window_sec_left=900, min_entry_seconds_left=300,
               exit_before_sec=60, poll_sec=15.0),
    "4h": dict(entry_window_sec_left=2700, min_entry_seconds_left=900,
               exit_before_sec=120, poll_sec=30.0),
    "1d": dict(entry_window_sec_left=10800, min_entry_seconds_left=3600,
               exit_before_sec=300, poll_sec=60.0),
}

SHARED_TF_DEFAULTS: dict[str, Any] = dict(
    enabled=True,
    strategies=["favorite"],  # which variants to run: favorite | underdog
    # --- favorite (og momentum-into-close): buy the strong side ---
    threshold=0.70,
    stake_usd=5.0,
    stop_loss_pct=0.25,
    max_spread=0.05,
    min_top_ask_notional_usd=5.0,
    favorite_min_abs_move_usd=0,  # optional: require spot to have moved this much
    # --- underdog (reversal fade, reverse-engineered from wallet analysis):
    #     buy the CHEAP side late, hold to resolution, no stop-loss ---
    underdog_max_price=0.30,
    underdog_min_price=0.03,
    underdog_stake_usd=None,      # defaults to stake_usd
    underdog_max_spread=0.15,     # longshot books are wide by nature
    underdog_max_abs_move_usd=80, # only fade when spot is CLOSE to slot open
    underdog_min_opposite_ask=0.0,  # only fade EXTREME skew: require the
                                    # favorite side's ask >= this (0 = off)
    underdog_exit_mode="resolution",  # resolution | pre_close
    # --- impulse (latency-style favorite sniping, reverse-engineered from
    #     the latency-bot cohort): when spot jerks hard, buy the impulse
    #     direction while the book's ask is still stale/cheap ---
    impulse_lookback_sec=15,      # velocity window
    impulse_min_move_usd=60,      # spot must move this much within lookback
    impulse_min_price=0.50,       # entry band: below = book disagrees,
    impulse_max_price=0.90,       #   above = repricing already done
    impulse_max_spread=0.10,      # books are in flux right after an impulse
    impulse_stake_usd=None,       # defaults to stake_usd
    impulse_entry_window_sec_left=None,  # None = watch the WHOLE slot
    impulse_min_entry_seconds_left=15,
    impulse_exit_mode="resolution",  # resolution | pre_close
    move_filter_fail_open=False,  # spot feed down: False = skip entry
    settle_grace_sec=20,
    settle_retries=6,
    settle_retry_delay_sec=5.0,
    close_retry_max=30,
    close_retry_delay_sec=2.0,
    slug_templates=None,  # falls back to market_discovery defaults
)

DEFAULT_PORTFOLIO: dict[str, Any] = dict(
    max_concurrent_positions=3,
    max_total_exposure_usd=15.0,
    daily_max_loss_usd=10.0,
)

DEFAULT_CONFIG: dict[str, Any] = dict(
    assets=["btc"],
    mode_default="paper",
    portfolio=dict(DEFAULT_PORTFOLIO),
    worker=dict(restart_on_exit=True, restart_backoff_sec=20),
    timeframes={
        tf: {**SHARED_TF_DEFAULTS, **p, "duration_sec": md.TIMEFRAME_SECONDS[tf]}
        for tf, p in DEFAULT_TF_PARAMS.items()
    },
)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str]) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if not path:
        return cfg
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"config not found: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            raise SystemExit(
                "PyYAML is required for YAML configs: pip install pyyaml "
                "(or pass a .json config instead)"
            )
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    return _deep_merge(cfg, data)


def tf_params(cfg: dict[str, Any], tf: str) -> dict[str, Any]:
    base = {**SHARED_TF_DEFAULTS, **DEFAULT_TF_PARAMS.get(tf, {}),
            "duration_sec": md.timeframe_seconds(tf)}
    return _deep_merge(base, (cfg.get("timeframes") or {}).get(tf) or {})


# ---------------------------------------------------------------------------
# Shared portfolio guard (cross-worker caps via fcntl-locked JSON state)
# ---------------------------------------------------------------------------

class PortfolioGuard:
    def __init__(self, state_path: str, max_positions: int,
                 max_exposure_usd: float, daily_max_loss_usd: float):
        self.path = Path(state_path)
        self.max_positions = int(max_positions)
        self.max_exposure_usd = float(max_exposure_usd)
        self.daily_max_loss_usd = float(daily_max_loss_usd)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self):
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                state = json.loads(f.read() or "{}")
            except Exception:
                state = {}
            state.setdefault("open_positions", {})
            state.setdefault("daily", {})
            self._roll_daily(state)
            self._prune_dead(state)
            yield state
            f.seek(0)
            f.truncate()
            f.write(json.dumps(state, ensure_ascii=False, indent=2))
            f.flush()

    @staticmethod
    def _roll_daily(state: dict) -> None:
        today = now_utc().strftime("%Y-%m-%d")
        if state["daily"].get("date") != today:
            state["daily"] = {"date": today, "realized_pnl_usd": 0.0, "trades": 0}

    @staticmethod
    def _prune_dead(state: dict) -> None:
        dead = []
        for wid, pos in state["open_positions"].items():
            pid = int(pos.get("pid") or 0)
            if pid <= 0:
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                dead.append(wid)
        for wid in dead:
            state["open_positions"].pop(wid, None)

    def try_reserve(self, worker_id: str, stake_usd: float) -> tuple[bool, str]:
        with self._locked() as state:
            open_pos = state["open_positions"]
            if worker_id in open_pos:
                return False, "worker_already_has_position"
            if len(open_pos) >= self.max_positions:
                return False, f"max_concurrent_positions_{self.max_positions}"
            exposure = sum(float(p.get("stake_usd") or 0) for p in open_pos.values())
            if exposure + stake_usd > self.max_exposure_usd:
                return False, f"max_total_exposure_{self.max_exposure_usd}"
            if float(state["daily"].get("realized_pnl_usd") or 0) <= -self.daily_max_loss_usd:
                return False, f"daily_loss_cap_{self.daily_max_loss_usd}"
            open_pos[worker_id] = {
                "stake_usd": float(stake_usd),
                "pid": os.getpid(),
                "reserved_at": ts_utc(),
            }
            return True, "reserved"

    def release(self, worker_id: str, realized_pnl_usd: Optional[float]) -> None:
        with self._locked() as state:
            state["open_positions"].pop(worker_id, None)
            if realized_pnl_usd is not None:
                state["daily"]["realized_pnl_usd"] = round(
                    float(state["daily"].get("realized_pnl_usd") or 0) + float(realized_pnl_usd), 6
                )
                state["daily"]["trades"] = int(state["daily"].get("trades") or 0) + 1


# ---------------------------------------------------------------------------
# Live execution: delegate to external pm_live_trade_runner.py (og pattern)
# ---------------------------------------------------------------------------

def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out, cur, depth = [], [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                s = "".join(cur)
                cur = []
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
    return out


def run_open(repo: str, slug: str, side: str, stake: float, execute: bool) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        ".venv/bin/python", "src/live/pm_live_trade_runner.py",
        "--market-slug", slug,
        "--force-side", side,
        "--start-equity", "100",
        "--risk-frac", str(stake / 100.0),
        "--max-notional-usd", str(stake),
    ]
    if execute:
        cmd.append("--execute")
    env = os.environ.copy()
    env.setdefault("PM_MAX_SPREAD", "1")
    env.setdefault("PM_MIN_TOP_ASK_NOTIONAL_USD", "0")
    env.setdefault("PM_ORDER_TYPE", "FAK")
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return out, parse_json_objects(out)


def run_close(repo: str, slug: str, token_id: str, shares: float, execute: bool,
              close_order_type: str = "FAK",
              close_limit_price: Optional[float] = None) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        ".venv/bin/python", "src/live/pm_live_trade_runner.py",
        "--market-slug", slug,
        "--close-token-id", token_id,
        "--close-shares", f"{shares:.8f}",
    ]
    if close_limit_price is not None and close_limit_price > 0:
        cmd += ["--close-limit-price", f"{close_limit_price:.6f}"]
    if execute:
        cmd.append("--execute")
    env = os.environ.copy()
    env["PM_CLOSE_ORDER_TYPE"] = str(close_order_type or "FAK").upper()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return out, parse_json_objects(out)


def auth_clob_client():
    """Optional authed client for force-close order polling/cancel (live only)."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        return None
    try:
        key = os.getenv("PM_PRIVATE_KEY") or ""
        funder = os.getenv("PM_FUNDER") or os.getenv("PM_ADDRESS") or None
        sig = int(os.getenv("PM_SIGNATURE_TYPE", "2"))
        v1 = os.getenv("PM_API_KEY") or ""
        v2 = os.getenv("PM_API_SECRET") or ""
        v3 = os.getenv("PM_API_PASSPHRASE") or ""
        if not key or not v1 or not v2 or not v3:
            return None
        c = ClobClient(host=md.CLOB_BASE, chain_id=POLYGON, key=key,
                       signature_type=sig, funder=funder)
        c.set_api_creds(ApiCreds(api_key=v1, api_secret=v2, api_passphrase=v3))
        return c
    except Exception:
        return None


def poll_order_status(client, order_id: str, wait_sec: float = 6.0,
                      step_sec: float = 1.0) -> str:
    if client is None or not order_id:
        return ""
    deadline = time.time() + max(0.0, float(wait_sec))
    last = None
    while time.time() <= deadline:
        try:
            last = client.get_order(order_id)
            st = str((last or {}).get("status") or "").upper()
            if st and st not in ("LIVE", "OPEN"):
                return st
        except Exception:
            pass
        time.sleep(max(0.2, float(step_sec)))
    try:
        last = client.get_order(order_id)
    except Exception:
        pass
    return str((last or {}).get("status") or "").upper()


def cancel_token_orders(client, token_id: str) -> Optional[dict[str, Any]]:
    if client is None:
        return None
    try:
        return client.cancel_market_orders(asset_id=str(token_id))
    except Exception as e:
        return {"error": str(e)}


def live_close_position(repo: str, opened: dict[str, Any], params: dict[str, Any],
                        execute: bool, report: dict[str, Any]) -> dict[str, Any]:
    """FAK close with GTC-limit fallback and force-close ladder (og-ported)."""
    close_debug: list[dict[str, Any]] = []
    close_obj: dict[str, Any] = {}
    out = ""
    fallback_used = None
    force_close_used = None
    client = auth_clob_client()

    for i in range(max(1, int(params["close_retry_max"]))):
        if i > 0 and STOP and i > 3:
            break  # allow a few attempts even during shutdown, then bail
        out, objs = run_close(repo, opened["market_slug"], opened["token_id"],
                              opened["shares"], execute, close_order_type="FAK")
        close_obj = objs[-1] if objs else {}
        post = close_obj.get("order_post_result") or {}
        status = str(post.get("status") or "").lower()
        skipped = str(close_obj.get("close_skipped") or "")
        close_debug.append({"ts": ts_utc(), "attempt": i + 1, "order_type": "FAK",
                            "status": status, "close_skipped": skipped})
        if post.get("success") is True and status == "matched":
            break

        if skipped == "zero_effective_shares":
            time.sleep(float(params["close_retry_delay_sec"]))
            continue

        txt = ((out or "") + "\n" + json.dumps(close_obj, ensure_ascii=False)).lower()
        if "no orders found to match with fak order" in txt:
            px = md.get_side_price(opened["market_slug"], opened["side"])
            if px is None:
                px = report.get("last_side_price") or opened["entry_price"]
            bb = None
            try:
                bb, _, _, _ = md.top_of_book(opened["token_id"])
            except Exception:
                bb = None
            limit_px = max(0.01, min(0.99, float((bb - 0.01) if bb is not None else px)))
            fallback_used = {"type": "GTC_LIMIT", "price": limit_px}
            out2, objs2 = run_close(repo, opened["market_slug"], opened["token_id"],
                                    opened["shares"], execute,
                                    close_order_type="GTC", close_limit_price=limit_px)
            close_obj2 = objs2[-1] if objs2 else {}
            post2 = close_obj2.get("order_post_result") or {}
            status2 = str(post2.get("status") or "").lower()
            close_debug.append({"ts": ts_utc(), "attempt": i + 1, "order_type": "GTC",
                                "status": status2,
                                "close_skipped": str(close_obj2.get("close_skipped") or ""),
                                "limit_price": limit_px})
            close_obj, out = close_obj2, out2
            if post2.get("success") is True and status2 == "matched":
                break

            if post2.get("success") is True and status2 == "live":
                oid2 = str(post2.get("orderID") or "")
                st_upd = poll_order_status(
                    client, oid2,
                    wait_sec=min(8.0, max(2.0, float(params["close_retry_delay_sec"]) * 2)))
                close_debug.append({"ts": ts_utc(), "attempt": i + 1,
                                    "order_type": "GTC_POLL",
                                    "status": st_upd.lower(), "order_id": oid2})
                if st_upd == "MATCHED":
                    post2["status"] = "matched"
                    close_obj["order_post_result"] = post2
                    break

                cancel_info = cancel_token_orders(client, opened["token_id"])
                bb2 = None
                try:
                    bb2, _, _, _ = md.top_of_book(opened["token_id"])
                except Exception:
                    bb2 = None
                force_px = max(0.01, min(0.99, float((bb2 - 0.02) if bb2 is not None else 0.01)))
                force_close_used = {"type": "FORCE_GTC_LIMIT", "price": force_px,
                                    "cancel_info": cancel_info}
                out3, objs3 = run_close(repo, opened["market_slug"], opened["token_id"],
                                        opened["shares"], execute,
                                        close_order_type="GTC", close_limit_price=force_px)
                close_obj3 = objs3[-1] if objs3 else {}
                post3 = close_obj3.get("order_post_result") or {}
                status3 = str(post3.get("status") or "").lower()
                close_debug.append({"ts": ts_utc(), "attempt": i + 1,
                                    "order_type": "FORCE_GTC", "status": status3,
                                    "close_skipped": str(close_obj3.get("close_skipped") or ""),
                                    "limit_price": force_px})
                close_obj, out = close_obj3, out3
                if post3.get("success") is True and status3 == "matched":
                    break

        time.sleep(float(params["close_retry_delay_sec"]))

    post = close_obj.get("order_post_result") or {}
    post_status = str(post.get("status") or "").lower()
    close_usdc = float(post.get("takingAmount") or 0)
    report["close_debug"] = close_debug
    if fallback_used:
        report["close_fallback"] = fallback_used
    if force_close_used:
        report["close_force"] = force_close_used
    report["close_raw"] = out[-4000:]
    return {
        "closed_at": ts_utc(),
        "close_success": bool(post.get("success") is True and (post_status == "matched" or close_usdc > 0)),
        "close_status": post.get("status"),
        "close_order_id": post.get("orderID"),
        "close_tx": (post.get("transactionsHashes") or [None])[0],
        "close_shares": float(post.get("makingAmount") or 0),
        "close_usdc": close_usdc,
        "close_skipped": close_obj.get("close_skipped"),
    }


def resolve_outcome(slug: str, side: str, retries: int, delay: float) -> tuple[Optional[bool], Optional[float]]:
    """After market end, poll gamma until outcome prices settle to ~1/0.
    Returns (won, settled_price) — (None, None) if still unresolved."""
    for i in range(max(1, int(retries))):
        try:
            ev = md.fetch_event(slug)
            mkts = (ev or {}).get("markets") or []
            if mkts:
                info = md.market_side_info(mkts[0])
                px = info["up_price"] if side == "UP" else info["down_price"]
                if px >= 0.95:
                    return True, px
                if px <= 0.05:
                    return False, px
        except Exception:
            pass
        if i < retries - 1:
            time.sleep(max(1.0, float(delay)))
    return None, None


def spot_settle(asset: str, slot_start: int, end_ts: float) -> tuple[Optional[bool], Optional[float], Optional[float]]:
    """Settle from the spot feed's 1m klines when gamma hasn't flipped yet:
    market rule is Up iff close >= open. Both prices are historical candle
    opens (at slot start and at slot end), so there is no sampling race.
    Returns (up_won, open_px, close_px)."""
    open_px = md.spot_open_at(asset, int(slot_start))
    close_px = md.spot_open_at(asset, int(end_ts))
    if open_px is None or close_px is None:
        return None, None, None
    return close_px >= open_px, open_px, close_px


# ---------------------------------------------------------------------------
# Slot session
# ---------------------------------------------------------------------------

def sleep_until(target_ts: float, max_chunk: float = 30.0) -> None:
    while not STOP:
        remaining = target_ts - time.time()
        if remaining <= 0:
            return
        time.sleep(min(max_chunk, max(0.2, remaining)))


class AttemptLog:
    """Bounded attempt log: keeps status counters plus last N raw entries."""

    def __init__(self, keep_last: int = 30):
        self.counts: dict[str, int] = {}
        self.last: list[dict[str, Any]] = []
        self.keep_last = keep_last

    def add(self, entry: dict[str, Any]) -> None:
        st = str(entry.get("status") or "unknown")
        self.counts[st] = self.counts.get(st, 0) + 1
        self.last.append(entry)
        if len(self.last) > self.keep_last:
            self.last.pop(0)


def run_slot_session(args, cfg: dict[str, Any], params: dict[str, Any],
                     guard: PortfolioGuard, worker_id: str) -> Optional[dict[str, Any]]:
    """Run one full slot session. Returns a report dict, or None when there was
    nothing to do (no market resolvable right now)."""
    m = md.resolve_market(args.asset, args.timeframe, "current",
                          templates=params.get("slug_templates"))
    if m is None:
        log_line({"ts": ts_utc(), "status": "no_market_resolvable",
                  "asset": args.asset, "tf": args.timeframe})
        time.sleep(float(params["poll_sec"]))
        return None

    slot_start = int(m["_slot_start"])
    end_ts = float(m["_end_ts"])
    slug = m["_slug"]
    sec_left = float(m["_seconds_left"])

    strategy = getattr(args, "strategy", "favorite")
    if strategy == "underdog":
        stake = float(params.get("underdog_stake_usd") or params["stake_usd"])
    elif strategy == "impulse":
        stake = float(params.get("impulse_stake_usd") or params["stake_usd"])
    else:
        stake = float(params["stake_usd"])
    hold_to_resolution = (
        (strategy == "underdog" and str(params.get("underdog_exit_mode")) == "resolution")
        or (strategy == "impulse" and str(params.get("impulse_exit_mode")) == "resolution"))
    if strategy == "impulse":
        eff_entry_window = float(params.get("impulse_entry_window_sec_left")
                                 or params["duration_sec"])
        eff_min_entry = float(params.get("impulse_min_entry_seconds_left") or 15)
    else:
        eff_entry_window = float(params["entry_window_sec_left"])
        eff_min_entry = float(params["min_entry_seconds_left"])

    # Too late to enter this slot: wait for the next one, no report.
    if sec_left < eff_min_entry:
        log_line({"ts": ts_utc(), "status": "slot_too_late", "slug": slug,
                  "seconds_left": round(sec_left, 1)})
        sleep_until(end_ts + 2)
        return None

    report: dict[str, Any] = {
        "started_at": ts_utc(),
        "asset": args.asset,
        "timeframe": args.timeframe,
        "mode": args.mode,
        "strategy": strategy,
        "slot_start": slot_start,
        "slug": slug,
        "params": {k: params.get(k) for k in (
            "threshold", "stake_usd", "stop_loss_pct", "entry_window_sec_left",
            "min_entry_seconds_left", "exit_before_sec", "poll_sec",
            "max_spread", "min_top_ask_notional_usd",
            "underdog_max_price", "underdog_min_price", "underdog_max_spread",
            "underdog_max_abs_move_usd", "underdog_exit_mode",
            "impulse_lookback_sec", "impulse_min_move_usd",
            "impulse_min_price", "impulse_max_price", "impulse_exit_mode",
            "favorite_min_abs_move_usd")},
    }
    report["params"]["stake_usd_effective"] = stake
    attempts = AttemptLog()
    slot_open_spot: Optional[float] = None
    spot_samples: list[tuple[float, float]] = []

    # Wait until the entry window opens (long timeframes sleep here for a while).
    window_open_ts = end_ts - eff_entry_window
    if time.time() < window_open_ts:
        log_line({"ts": ts_utc(), "status": "waiting_for_entry_window", "slug": slug,
                  "window_opens_in_sec": round(window_open_ts - time.time(), 1)})
        sleep_until(window_open_ts)

    # --- entry phase ---
    opened = None
    guard_reserved = False
    while not STOP:
        sec_left = end_ts - time.time()
        if sec_left < eff_min_entry:
            break
        try:
            up_bid, up_ask, _, up_ask_sz = md.top_of_book(m["up_token"])
            dn_bid, dn_ask, _, dn_ask_sz = md.top_of_book(m["down_token"])
        except Exception as e:
            attempts.add({"ts": ts_utc(), "status": "skip_clob_unavailable", "error": str(e)})
            time.sleep(float(params["poll_sec"]))
            continue

        attempts.add({"ts": ts_utc(), "status": "heartbeat",
                      "clob_up_ask": up_ask, "clob_down_ask": dn_ask,
                      "seconds_left": round(sec_left, 1)})

        # spot move filter (underdog fades SMALL moves; favorite may require a
        # minimum move per the og strategy doc)
        move_cap = float(params.get("underdog_max_abs_move_usd") or 0) \
            if strategy == "underdog" else 0.0
        move_floor = float(params.get("favorite_min_abs_move_usd") or 0) \
            if strategy == "favorite" else 0.0
        if move_cap > 0 or move_floor > 0:
            if slot_open_spot is None:
                slot_open_spot = md.spot_open_at(args.asset, slot_start)
            spot_now = md.spot_price(args.asset)
            if slot_open_spot is None or spot_now is None:
                if not params.get("move_filter_fail_open"):
                    attempts.add({"ts": ts_utc(), "status": "skip_move_unavailable"})
                    time.sleep(float(params["poll_sec"]))
                    continue
            else:
                move = abs(spot_now - slot_open_spot)
                report["last_spot_move_usd"] = round(move, 2)
                if move_cap > 0 and move > move_cap:
                    attempts.add({"ts": ts_utc(), "status": "skip_move_too_large",
                                  "move_usd": round(move, 2)})
                    time.sleep(float(params["poll_sec"]))
                    continue
                if move_floor > 0 and move < move_floor:
                    attempts.add({"ts": ts_utc(), "status": "skip_move_too_small",
                                  "move_usd": round(move, 2)})
                    time.sleep(float(params["poll_sec"]))
                    continue

        candidates: list[tuple[str, float, Optional[float], float]] = []
        if strategy == "impulse":
            # velocity trigger: spot moved hard within the lookback window and
            # the book's ask for the impulse direction is still in the stale band
            now_t = time.time()
            spot_now = md.spot_price(args.asset)
            if spot_now is None:
                attempts.add({"ts": ts_utc(), "status": "skip_move_unavailable"})
                time.sleep(float(params["poll_sec"]))
                continue
            lookback = float(params["impulse_lookback_sec"])
            spot_samples.append((now_t, spot_now))
            spot_samples[:] = [(t, v) for t, v in spot_samples
                               if t >= now_t - 3 * lookback]
            ref = None
            for t, v in reversed(spot_samples):
                if t <= now_t - lookback:
                    ref = v
                    break
            if ref is None:
                attempts.add({"ts": ts_utc(), "status": "skip_impulse_warmup"})
                time.sleep(float(params["poll_sec"]))
                continue
            vel = spot_now - ref
            report["last_impulse_usd"] = round(vel, 2)
            if abs(vel) >= float(params["impulse_min_move_usd"]):
                side = "UP" if vel > 0 else "DOWN"
                ask = up_ask if side == "UP" else dn_ask
                bid = up_bid if side == "UP" else dn_bid
                sz = up_ask_sz if side == "UP" else dn_ask_sz
                lo, hi = float(params["impulse_min_price"]), float(params["impulse_max_price"])
                if ask is not None and lo <= ask <= hi:
                    candidates.append((side, ask, bid, sz))
                else:
                    attempts.add({"ts": ts_utc(), "status": "skip_impulse_price_out_of_band",
                                  "side": side, "ask": ask, "move_usd": round(vel, 2)})
            spread_cap = float(params["impulse_max_spread"])
        elif strategy == "underdog":
            lo = float(params["underdog_min_price"])
            hi = float(params["underdog_max_price"])
            opp_min = float(params.get("underdog_min_opposite_ask") or 0)
            if (up_ask is not None and lo <= up_ask <= hi
                    and (opp_min <= 0 or (dn_ask or 0) >= opp_min)):
                candidates.append(("UP", up_ask, up_bid, up_ask_sz))
            if (dn_ask is not None and lo <= dn_ask <= hi
                    and (opp_min <= 0 or (up_ask or 0) >= opp_min)):
                candidates.append(("DOWN", dn_ask, dn_bid, dn_ask_sz))
            # fade the move: buy the CHEAPER (trailing) side
            candidates.sort(key=lambda x: x[1])
            spread_cap = float(params["underdog_max_spread"])
        else:
            if up_ask is not None and up_ask >= float(params["threshold"]):
                candidates.append(("UP", up_ask, up_bid, up_ask_sz))
            if dn_ask is not None and dn_ask >= float(params["threshold"]):
                candidates.append(("DOWN", dn_ask, dn_bid, dn_ask_sz))
            candidates.sort(key=lambda x: x[1], reverse=True)
            spread_cap = float(params["max_spread"])

        picked = None
        for side, ask, bid, ask_sz in candidates:
            if bid is not None and (ask - bid) > spread_cap:
                attempts.add({"ts": ts_utc(), "status": "skip_spread_too_wide",
                              "side": side, "spread": round(ask - bid, 4)})
                continue
            if ask * ask_sz < float(params["min_top_ask_notional_usd"]):
                attempts.add({"ts": ts_utc(), "status": "skip_thin_top_ask",
                              "side": side, "top_ask_notional": round(ask * ask_sz, 2)})
                continue
            picked = (side, ask)
            break

        if picked is None:
            time.sleep(float(params["poll_sec"]))
            continue

        side, trigger_price = picked
        ok, reason = guard.try_reserve(worker_id, stake)
        if not ok:
            attempts.add({"ts": ts_utc(), "status": "guard_denied", "reason": reason})
            time.sleep(float(params["poll_sec"]))
            continue
        guard_reserved = True

        if args.mode == "live":
            out, objs = run_open(args.repo, slug, side, stake, True)
            post, runner = None, None
            for o in objs:
                if isinstance(o, dict) and "order_post_result" in o:
                    runner = o
                    post = o.get("order_post_result") or {}
            if post and post.get("success") is True and str(post.get("status", "")).lower() == "matched":
                token_id = str(runner.get("token_id") or (m["up_token"] if side == "UP" else m["down_token"]))
                opened = {
                    "opened_at": ts_utc(),
                    "market_slug": slug,
                    "side": side,
                    "token_id": token_id,
                    "entry_price": float(runner.get("entry_price") or trigger_price),
                    "shares": float(post.get("takingAmount") or 0),
                    "cost_usdc": float(post.get("makingAmount") or 0),
                    "open_order_id": post.get("orderID"),
                    "open_tx": (post.get("transactionsHashes") or [None])[0],
                }
                report["open_raw"] = out[-4000:]
                break
            report["last_open_try"] = out[-2000:]
            guard.release(worker_id, None)
            guard_reserved = False
            time.sleep(float(params["poll_sec"]))
        else:  # paper fill at best ask
            shares = round(stake / trigger_price, 6)
            opened = {
                "opened_at": ts_utc(),
                "market_slug": slug,
                "side": side,
                "token_id": str(m["up_token"] if side == "UP" else m["down_token"]),
                "entry_price": trigger_price,
                "shares": shares,
                "cost_usdc": stake,
                "paper": True,
            }
            break

    report["attempt_counts"] = attempts.counts
    report["attempts_tail"] = attempts.last

    if not opened:
        if guard_reserved:
            guard.release(worker_id, None)
        report["finished_at"] = ts_utc()
        report["result"] = "stopped_before_entry" if STOP else "no_entry"
        # Skip ahead to next slot so we don't reprocess this one.
        if not STOP:
            sleep_until(end_ts + 2)
        return report

    report["opened"] = opened
    log_line({"ts": ts_utc(), "status": "opened", "slug": slug,
              "side": opened["side"], "entry_price": opened["entry_price"],
              "strategy": strategy, "mode": args.mode})

    # --- hold-to-resolution path (underdog): no stop-loss, no pre-close exit;
    #     max loss is the stake, winners settle at $1/share ---
    if hold_to_resolution:
        sleep_until(end_ts + float(params["settle_grace_sec"]))
        won, settled_px = resolve_outcome(slug, opened["side"],
                                          params["settle_retries"],
                                          params["settle_retry_delay_sec"])
        settle_source = "gamma" if won is not None else None
        spot_open_px = spot_close_px = None
        if won is None:
            # gamma often takes minutes to flip to 0/1; the spot feed's
            # historical klines settle it immediately (Up iff close >= open)
            up_won, spot_open_px, spot_close_px = spot_settle(
                args.asset, slot_start, end_ts)
            if up_won is not None:
                won = up_won if opened["side"] == "UP" else (not up_won)
                settle_source = "spot_klines"
        if won is None:
            proceeds, pnl = None, None
        else:
            proceeds = round(opened["shares"] * (1.0 if won else 0.0), 6)
            pnl = round(proceeds - opened["cost_usdc"], 6)
        closed = {
            "close_reason": "resolution",
            "closed_at": ts_utc(),
            "close_success": won is not None,
            "won": won,
            "close_price": (1.0 if won else 0.0) if won is not None else None,
            "settle_source": settle_source,
            "settled_gamma_price": settled_px,
            "spot_open": spot_open_px,
            "spot_close": spot_close_px,
            "close_shares": opened["shares"],
            "close_usdc": proceeds,
            "paper": args.mode != "live",
        }
        if args.mode == "live":
            # no sell order is sent; winning shares must be redeemed on-chain
            closed["live_redemption_manual"] = True
        guard.release(worker_id, pnl)
        report["closed"] = closed
        report["realized_cashflow_pnl_usdc"] = pnl
        report["finished_at"] = ts_utc()
        report["result"] = "done" if won is not None else "settle_unresolved"
        log_line({"ts": ts_utc(), "status": "settled", "slug": slug,
                  "won": won, "pnl_usdc": pnl, "strategy": strategy})
        return report

    # --- monitor phase: stop-loss or pre-close time exit ---
    sl_price = opened["entry_price"] * (1.0 - float(params["stop_loss_pct"]))
    report["stop_loss_price"] = round(sl_price, 6)

    close_reason = None
    while True:
        if STOP:
            close_reason = "stop_requested"
            break
        if time.time() >= (end_ts - float(params["exit_before_sec"])):
            close_reason = f"time_exit_{int(params['exit_before_sec'])}s_before_end"
            break
        side_px = None
        try:
            bb, _, _, _ = md.top_of_book(opened["token_id"])
            side_px = bb
        except Exception:
            pass
        if side_px is None:
            side_px = md.get_side_price(slug, opened["side"])
        report["last_side_price"] = side_px
        report["last_check_at"] = ts_utc()
        if side_px is not None and side_px <= sl_price:
            close_reason = f"stop_loss_{int(float(params['stop_loss_pct']) * 100)}pct"
            break
        time.sleep(float(params["poll_sec"]))

    # --- close phase ---
    if args.mode == "live":
        closed = live_close_position(args.repo, opened, params, True, report)
        closed["close_reason"] = close_reason
        pnl = None
        if closed.get("close_usdc"):
            pnl = round(float(closed["close_usdc"]) - float(opened["cost_usdc"]), 6)
    else:
        exit_px = None
        try:
            bb, _, _, _ = md.top_of_book(opened["token_id"])
            exit_px = bb
        except Exception:
            pass
        if exit_px is None:
            exit_px = md.get_side_price(slug, opened["side"])
        proceeds = round(float(exit_px) * opened["shares"], 6) if exit_px is not None else None
        closed = {
            "close_reason": close_reason,
            "closed_at": ts_utc(),
            "close_success": exit_px is not None,
            "close_price": exit_px,
            "close_shares": opened["shares"],
            "close_usdc": proceeds,
            "paper": True,
        }
        pnl = round(proceeds - opened["cost_usdc"], 6) if proceeds is not None else None

    guard.release(worker_id, pnl)
    report["closed"] = closed
    report["realized_cashflow_pnl_usdc"] = pnl
    report["finished_at"] = ts_utc()
    report["result"] = "done"
    log_line({"ts": ts_utc(), "status": "closed", "slug": slug,
              "reason": close_reason, "pnl_usdc": pnl, "mode": args.mode})

    if not STOP:
        sleep_until(end_ts + 2)
    return report


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def default_repo_path() -> str:
    env_repo = os.environ.get("BTCMULTI_REPO") or os.environ.get("BTC5M_REPO")
    if env_repo:
        return env_repo
    return str(Path(__file__).resolve().parents[4] / "pm-hl-conservative-plus-repo")


def default_runtime_dir() -> str:
    return str(Path(__file__).resolve().parents[1] / "runtime")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--timeframe", required=True, choices=sorted(md.TIMEFRAME_SECONDS))
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--strategy", choices=["favorite", "underdog", "impulse"],
                    default="favorite")
    ap.add_argument("--config", default=None, help="multi profiles yaml/json")
    ap.add_argument("--repo", default=default_repo_path(),
                    help="external pm-hl-conservative-plus-repo path (live mode)")
    ap.add_argument("--runtime-dir", default=default_runtime_dir())
    ap.add_argument("--max-slots", type=int, default=0,
                    help="stop after N completed slot sessions (0 = run until stopped)")
    ap.add_argument("--duration-min", type=int, default=0,
                    help="stop after N minutes (0 = unlimited)")
    ap.add_argument("--stake-usd", type=float, default=None, help="override config stake")
    ap.add_argument("--threshold", type=float, default=None, help="override config threshold")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    cfg = load_config(args.config)
    params = tf_params(cfg, args.timeframe)
    if args.stake_usd is not None:
        params["stake_usd"] = args.stake_usd
    if args.threshold is not None:
        params["threshold"] = args.threshold

    if args.mode == "live" and not Path(args.repo).exists():
        log_line({"ts": ts_utc(), "status": "fatal",
                  "error": f"live mode requires external repo at {args.repo}"})
        return 2

    runtime = Path(args.runtime_dir)
    reports_dir = runtime / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    pf = cfg.get("portfolio") or {}
    guard = PortfolioGuard(
        str(runtime / f"portfolio_{args.mode}.json"),
        pf.get("max_concurrent_positions", DEFAULT_PORTFOLIO["max_concurrent_positions"]),
        pf.get("max_total_exposure_usd", DEFAULT_PORTFOLIO["max_total_exposure_usd"]),
        pf.get("daily_max_loss_usd", DEFAULT_PORTFOLIO["daily_max_loss_usd"]),
    )
    worker_id = f"{args.asset}-{args.timeframe}" if args.strategy == "favorite" \
        else f"{args.asset}-{args.timeframe}-{args.strategy}"

    log_line({"ts": ts_utc(), "status": "worker_started", "worker": worker_id,
              "strategy": args.strategy,
              "mode": args.mode, "params": {k: params.get(k) for k in (
                  "threshold", "stake_usd", "stop_loss_pct", "entry_window_sec_left",
                  "min_entry_seconds_left", "exit_before_sec", "poll_sec",
                  "underdog_max_price", "underdog_max_abs_move_usd",
                  "underdog_exit_mode")}})

    deadline = time.time() + args.duration_min * 60 if args.duration_min > 0 else None
    slots_done = 0
    while not STOP:
        if deadline and time.time() >= deadline:
            log_line({"ts": ts_utc(), "status": "duration_reached"})
            break
        if args.max_slots and slots_done >= args.max_slots:
            log_line({"ts": ts_utc(), "status": "max_slots_reached", "slots": slots_done})
            break
        try:
            report = run_slot_session(args, cfg, params, guard, worker_id)
        except Exception as e:
            log_line({"ts": ts_utc(), "status": "slot_error", "error": str(e)})
            guard.release(worker_id, None)
            time.sleep(float(params["poll_sec"]))
            continue
        if report is not None:
            strat_tag = "" if args.strategy == "favorite" else f"_{args.strategy}"
            fname = f"{args.asset}_{args.timeframe}{strat_tag}_{report['slot_start']}.json"
            (reports_dir / fname).write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            slots_done += 1

    log_line({"ts": ts_utc(), "status": "worker_stopped", "worker": worker_id,
              "slots_done": slots_done})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
