#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import subprocess
import time
from typing import Any, Optional
from pathlib import Path

import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds

UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def ts_utc() -> str:
    return now_utc().isoformat().replace('+00:00', 'Z')


def parse_json_objects(text: str) -> list[dict[str, Any]]:
    out = []
    cur = []
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        if depth > 0:
            cur.append(ch)
        if ch == '}' and depth > 0:
            depth -= 1
            if depth == 0:
                s = ''.join(cur)
                cur = []
                try:
                    out.append(json.loads(s))
                except Exception:
                    pass
    return out


def bucket_5m(ts: int) -> int:
    return ts - (ts % 300)


def fetch_event(slug: str) -> Optional[dict[str, Any]]:
    r = requests.get('https://gamma-api.polymarket.com/events', params={'slug': slug}, timeout=12)
    r.raise_for_status()
    arr = r.json()
    return arr[0] if arr else None


def resolve_active_current_5m_market() -> Optional[dict[str, Any]]:
    """Return active BTC 5m market for the current slot only."""
    now = int(time.time())
    cur = bucket_5m(now)
    slug = f'btc-updown-5m-{cur}'

    try:
        ev = fetch_event(slug)
    except Exception:
        return None
    if not ev:
        return None

    mkts = ev.get('markets') or []
    if not mkts:
        return None

    m = mkts[0]
    if m.get('closed') is True:
        return None
    if m.get('active') is False:
        return None

    end_iso = str(m.get('endDate') or m.get('endDateIso') or '')
    try:
        end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None

    sec_left = end_ts - time.time()
    if sec_left <= 5:
        return None

    mm = dict(m)
    mm['_event_slug'] = slug
    mm['_seconds_left'] = sec_left
    return mm


def parse_json_field(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def market_side_prices(market: dict[str, Any]) -> tuple[float, float, str, str, str, str]:
    outcomes = parse_json_field(market.get('outcomes')) or []
    prices = parse_json_field(market.get('outcomePrices')) or []
    token_ids = parse_json_field(market.get('clobTokenIds')) or []
    if len(prices) < 2 or len(token_ids) < 2:
        raise RuntimeError('missing outcomePrices/clobTokenIds')

    up_i, down_i = 0, 1
    labs = [str(x).lower() for x in outcomes[:2]] if isinstance(outcomes, list) else []
    if len(labs) >= 2 and ('up' in labs[1] or 'yes' in labs[1]):
        up_i, down_i = 1, 0

    up_p = float(prices[up_i])
    dn_p = float(prices[down_i])
    up_t = str(token_ids[up_i])
    dn_t = str(token_ids[down_i])
    return up_p, dn_p, up_t, dn_t, str(market.get('slug') or market.get('_event_slug') or ''), str(market.get('endDate') or market.get('endDateIso') or '')


def _best_bid_ask(book) -> tuple[Optional[float], Optional[float]]:
    bids = getattr(book, 'bids', []) or []
    asks = getattr(book, 'asks', []) or []
    best_bid = None
    best_ask = None
    for b in bids:
        p = float(getattr(b, 'price', 0) or 0)
        if best_bid is None or p > best_bid:
            best_bid = p
    for a in asks:
        p = float(getattr(a, 'price', 0) or 0)
        if best_ask is None or p < best_ask:
            best_ask = p
    return best_bid, best_ask


def clob_side_prices(up_token: str, down_token: str, clob_base: str = 'https://clob.polymarket.com') -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return trigger prices from CLOB orderbooks: UP ask, DOWN ask, spread of picked side when available."""
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    up_book = pub.get_order_book(str(up_token))
    dn_book = pub.get_order_book(str(down_token))
    up_bid, up_ask = _best_bid_ask(up_book)
    dn_bid, dn_ask = _best_bid_ask(dn_book)

    picked_spread = None
    # Side picked later by max ask; keep a generic sanity spread estimate
    if up_ask is not None and up_bid is not None:
        picked_spread = max(0.0, up_ask - up_bid)
    if dn_ask is not None and dn_bid is not None:
        s = max(0.0, dn_ask - dn_bid)
        picked_spread = s if picked_spread is None else min(picked_spread, s)

    return up_ask, dn_ask, picked_spread


def clob_best_bid(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> Optional[float]:
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    book = pub.get_order_book(str(token_id))
    best_bid, _ = _best_bid_ask(book)
    return best_bid


def clob_best_bid_ask(token_id: str, clob_base: str = 'https://clob.polymarket.com') -> tuple[Optional[float], Optional[float]]:
    pub = ClobClient(host=clob_base, chain_id=POLYGON)
    book = pub.get_order_book(str(token_id))
    return _best_bid_ask(book)


def auth_clob_client(clob_base: str = 'https://clob.polymarket.com') -> Optional[ClobClient]:
    try:
        key = os.getenv('PM_PRIVATE_KEY') or ''
        funder = os.getenv('PM_FUNDER') or os.getenv('PM_ADDRESS') or None
        sig = int(os.getenv('PM_SIGNATURE_TYPE', '2'))
        v1 = os.getenv('PM_API_KEY') or ''
        v2 = os.getenv('PM_API_SECRET') or ''
        v3 = os.getenv('PM_API_PASSPHRASE') or ''
        if not key or not v1 or not v2 or not v3:
            return None
        c = ClobClient(host=clob_base, chain_id=POLYGON, key=key, signature_type=sig, funder=funder)
        creds = {
            f"api_{'key'}": v1,
            f"api_{'secret'}": v2,
            f"api_{'passphrase'}": v3,
        }
        c.set_api_creds(ApiCreds(**creds))
        return c
    except Exception:
        return None


def poll_order_status(client: Optional[ClobClient], order_id: str, wait_sec: float = 6.0, step_sec: float = 1.0) -> tuple[str, Optional[dict[str, Any]]]:
    if client is None or not order_id:
        return '', None
    deadline = time.time() + max(0.0, float(wait_sec))
    last = None
    while time.time() <= deadline:
        try:
            last = client.get_order(order_id)
            st = str((last or {}).get('status') or '').upper()
            if st and st not in ('LIVE', 'OPEN'):
                return st, last
        except Exception:
            pass
        time.sleep(max(0.2, float(step_sec)))
    try:
        last = client.get_order(order_id)
    except Exception:
        pass
    st = str((last or {}).get('status') or '').upper()
    return st, last


def cancel_token_orders(client: Optional[ClobClient], token_id: str) -> Optional[dict[str, Any]]:
    if client is None:
        return None
    try:
        return client.cancel_market_orders(asset_id=str(token_id))
    except Exception as e:
        return {'error': str(e)}


def run_open(repo: str, slug: str, side: str, stake: float, execute: bool) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--force-side', side,
        '--start-equity', '100',
        '--risk-frac', str(stake / 100.0),
        '--max-notional-usd', str(stake),
    ]
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env.setdefault('PM_MAX_SPREAD', '1')
    env.setdefault('PM_MIN_TOP_ASK_NOTIONAL_USD', '0')
    env.setdefault('PM_ORDER_TYPE', 'FAK')
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def run_close(
    repo: str,
    slug: str,
    token_id: str,
    shares: float,
    execute: bool,
    close_order_type: str = 'FAK',
    close_limit_price: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    cmd = [
        '.venv/bin/python',
        'src/live/pm_live_trade_runner.py',
        '--market-slug', slug,
        '--close-token-id', token_id,
        '--close-shares', f'{shares:.8f}',
    ]
    if close_limit_price is not None and close_limit_price > 0:
        cmd += ['--close-limit-price', f'{close_limit_price:.6f}']
    if execute:
        cmd.append('--execute')
    env = os.environ.copy()
    env['PM_CLOSE_ORDER_TYPE'] = str(close_order_type or 'FAK').upper()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    out = (p.stdout or '') + '\n' + (p.stderr or '')
    return out, parse_json_objects(out)


def get_side_price_from_slug(slug: str, side: str) -> Optional[float]:
    try:
        ev = fetch_event(slug)
        if not ev:
            return None
        mkts = ev.get('markets') or []
        if not mkts:
            return None
        up, dn, *_ = market_side_prices(mkts[0])
        return up if side == 'UP' else dn
    except Exception:
        return None


PROFILES: dict[str, dict[str, Any]] = {
    'conservative': {
        'threshold': 0.70,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.25,
        'exit_before_sec': 20,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        'scale_enabled': 0,
        'scale_stake_usd': 5.0,
        'scale_trigger_delta': 0.08,
        'scale_max_price': 0.94,
        'scale_max_adds': 1,
        'scale_min_seconds_left': 45,
        'max_total_notional_usd': 10.0,
        'hedge_exit': 1,
        'hedge_min_edge': 0.02,
    },
    'aggressive': {
        'threshold': 0.70,
        'stake_usd': 5.0,
        'stop_loss_pct': 0.30,
        'exit_before_sec': 20,
        'min_entry_seconds_left': 60,
        'entry_timeout_min': 60,
        'poll_sec': 5.0,
        'scale_enabled': 1,
        'scale_stake_usd': 5.0,
        'scale_trigger_delta': 0.06,
        'scale_max_price': 0.95,
        'scale_max_adds': 1,
        'scale_min_seconds_left': 40,
        'max_total_notional_usd': 15.0,
        'hedge_exit': 1,
        'hedge_min_edge': 0.015,
    },
}


def apply_profile(args: argparse.Namespace) -> argparse.Namespace:
    prof = PROFILES.get(args.profile or 'conservative', PROFILES['conservative'])
    if args.threshold is None:
        args.threshold = float(prof['threshold'])
    if args.stake_usd is None:
        args.stake_usd = float(prof['stake_usd'])
    if args.stop_loss_pct is None:
        args.stop_loss_pct = float(prof['stop_loss_pct'])
    if args.exit_before_sec is None:
        args.exit_before_sec = int(prof['exit_before_sec'])
    if args.min_entry_seconds_left is None:
        args.min_entry_seconds_left = int(prof['min_entry_seconds_left'])
    if args.entry_timeout_min is None:
        args.entry_timeout_min = int(prof['entry_timeout_min'])
    if args.poll_sec is None:
        args.poll_sec = float(prof['poll_sec'])
    for k in (
        'scale_enabled', 'scale_stake_usd', 'scale_trigger_delta', 'scale_max_price',
        'scale_max_adds', 'scale_min_seconds_left', 'max_total_notional_usd',
        'hedge_exit', 'hedge_min_edge',
    ):
        if getattr(args, k) is None:
            setattr(args, k, prof[k])
    return args


def default_repo_path() -> str:
    env_repo = os.environ.get('BTC5M_REPO')
    if env_repo:
        return env_repo
    return str(Path(__file__).resolve().parents[3] / 'pm-hl-conservative-plus-repo')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=default_repo_path())
    ap.add_argument('--profile', choices=['conservative', 'aggressive'], default='conservative')
    ap.add_argument('--threshold', type=float, default=None)
    ap.add_argument('--stake-usd', type=float, default=None)
    ap.add_argument('--stop-loss-pct', type=float, default=None, help='0.30 means -30%% from entry price')
    ap.add_argument('--exit-before-sec', type=int, default=None)
    ap.add_argument('--min-entry-seconds-left', type=int, default=None, help='Do not open if less seconds remain in current 5m slot')
    ap.add_argument('--entry-timeout-min', type=int, default=None)
    ap.add_argument('--poll-sec', type=float, default=None)
    ap.add_argument('--close-retry-max', type=int, default=18, help='Max close retries when position is not yet visible / not immediately closable')
    ap.add_argument('--close-retry-delay-sec', type=float, default=2.0, help='Delay between close retries')
    ap.add_argument('--scale-enabled', type=int, choices=[0, 1], default=None, help='1 = allow adding to a winning position once price confirms above entry')
    ap.add_argument('--scale-stake-usd', type=float, default=None, help='Stake for each scale-in add')
    ap.add_argument('--scale-trigger-delta', type=float, default=None, help='Add only after side price >= entry_price + delta')
    ap.add_argument('--scale-max-price', type=float, default=None, help='Never scale-in above this price (risk/reward guard)')
    ap.add_argument('--scale-max-adds', type=int, default=None, help='Max scale-in attempts per session')
    ap.add_argument('--scale-min-seconds-left', type=int, default=None, help='Do not scale-in with fewer seconds left in the slot')
    ap.add_argument('--max-total-notional-usd', type=float, default=None, help='Cap on total position cost including scale-ins')
    ap.add_argument('--hedge-exit', type=int, choices=[0, 1], default=None, help='1 = at exit, buy equal opposite shares instead of selling when that exits richer')
    ap.add_argument('--hedge-min-edge', type=float, default=None, help='Required per-share advantage of hedge exit (1 - opp ask) over own best bid')
    ap.add_argument('--execute', action='store_true')
    args = apply_profile(ap.parse_args())

    report: dict[str, Any] = {
        'started_at': ts_utc(),
        'params': {
            'profile': args.profile,
            'threshold': args.threshold,
            'stake_usd': args.stake_usd,
            'stop_loss_pct': args.stop_loss_pct,
            'exit_before_sec': args.exit_before_sec,
            'min_entry_seconds_left': args.min_entry_seconds_left,
            'entry_timeout_min': args.entry_timeout_min,
            'poll_sec': args.poll_sec,
            'close_retry_max': args.close_retry_max,
            'close_retry_delay_sec': args.close_retry_delay_sec,
            'scale_enabled': args.scale_enabled,
            'scale_stake_usd': args.scale_stake_usd,
            'scale_trigger_delta': args.scale_trigger_delta,
            'scale_max_price': args.scale_max_price,
            'scale_max_adds': args.scale_max_adds,
            'scale_min_seconds_left': args.scale_min_seconds_left,
            'max_total_notional_usd': args.max_total_notional_usd,
            'hedge_exit': args.hedge_exit,
            'hedge_min_edge': args.hedge_min_edge,
            'execute': args.execute,
        },
        'attempts': [],
    }

    deadline = time.time() + args.entry_timeout_min * 60
    opened = None

    while time.time() < deadline:
        try:
            m = resolve_active_current_5m_market()
            if not m:
                report['attempts'].append({'ts': ts_utc(), 'status': 'heartbeat_no_current_market'})
                time.sleep(args.poll_sec)
                continue

            g_up, g_dn, up_t, dn_t, slug, end_iso = market_side_prices(m)

            end_ts = None
            sec_left = None
            try:
                end_ts = dt.datetime.fromisoformat(end_iso.replace('Z', '+00:00')).timestamp()
                sec_left = max(0.0, end_ts - time.time())
            except Exception:
                pass

            if sec_left is None:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'heartbeat_bad_market_end'})
                time.sleep(args.poll_sec)
                continue

            # Do not open if less than N seconds remain in current slot.
            if sec_left < args.min_entry_seconds_left:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': 'skip_too_late_to_enter',
                    'seconds_left': sec_left,
                    'min_entry_seconds_left': args.min_entry_seconds_left,
                })
                time.sleep(args.poll_sec)
                continue

            # CLOB-based trigger price (best ask of selected side), not Gamma outcomePrices.
            try:
                up_ask, dn_ask, min_spread = clob_side_prices(up_t, dn_t)
            except Exception as e:
                report['attempts'].append({'ts': ts_utc(), 'slug': slug, 'status': 'skip_clob_unavailable', 'error': str(e)})
                time.sleep(args.poll_sec)
                continue

            report['attempts'].append({
                'ts': ts_utc(),
                'slug': slug,
                'status': 'heartbeat',
                'gamma_up': g_up,
                'gamma_down': g_dn,
                'clob_up_ask': up_ask,
                'clob_down_ask': dn_ask,
                'seconds_left': sec_left,
                'min_spread': min_spread,
            })

            candidates: list[tuple[str, float]] = []
            if up_ask is not None and float(up_ask) >= args.threshold:
                candidates.append(('UP', float(up_ask)))
            if dn_ask is not None and float(dn_ask) >= args.threshold:
                candidates.append(('DOWN', float(dn_ask)))

            if not candidates:
                report['attempts'].append({
                    'ts': ts_utc(),
                    'slug': slug,
                    'status': 'skip_price_below_threshold',
                    'threshold': args.threshold,
                    'clob_up_ask': up_ask,
                    'clob_down_ask': dn_ask,
                    'seconds_left': sec_left,
                })
                time.sleep(args.poll_sec)
                continue

            side, trigger_price = sorted(candidates, key=lambda x: x[1], reverse=True)[0]

            out, objs = run_open(args.repo, slug, side, args.stake_usd, args.execute)
            post = None
            runner = None
            for o in objs:
                if isinstance(o, dict) and 'order_post_result' in o:
                    runner = o
                    post = o.get('order_post_result') or {}
            if post and post.get('success') is True and str(post.get('status', '')).lower() == 'matched':
                token_id = str(runner.get('token_id') or (up_t if side == 'UP' else dn_t))
                shares = float(post.get('takingAmount') or 0)
                cost = float(post.get('makingAmount') or 0)
                entry_price = float(runner.get('entry_price') or trigger_price)
                opened = {
                    'opened_at': ts_utc(),
                    'market_slug': slug,
                    'market_end_iso': end_iso,
                    'side': side,
                    'token_id': token_id,
                    'opp_token_id': dn_t if side == 'UP' else up_t,
                    'entry_price': entry_price,
                    'shares': shares,
                    'cost_usdc': cost,
                    'open_order_id': post.get('orderID'),
                    'open_tx': (post.get('transactionsHashes') or [None])[0],
                }
                report['open_raw'] = out[-4000:]
                break
            else:
                report['last_open_try'] = out[-2000:]
        except Exception as e:
            report['attempts'].append({'ts': ts_utc(), 'status': 'error', 'error': str(e)})
        time.sleep(args.poll_sec)

    if not opened:
        report['finished_at'] = ts_utc()
        report['result'] = 'no_entry_timeout'
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    report['opened'] = opened

    # monitor after open: stop-loss or time exit
    end_ts = None
    try:
        end_ts = dt.datetime.fromisoformat(opened['market_end_iso'].replace('Z', '+00:00')).timestamp()
    except Exception:
        end_ts = time.time() + 300

    sl_price = opened['entry_price'] * (1.0 - args.stop_loss_pct)
    report['stop_loss_price'] = sl_price
    report['scale_adds'] = []

    close_reason = None
    scale_attempts = 0
    while True:
        now = time.time()
        sec_left = max(0.0, end_ts - now)
        if now >= (end_ts - args.exit_before_sec):
            close_reason = f'time_exit_{args.exit_before_sec}s_before_end'
            break

        side_px = get_side_price_from_slug(opened['market_slug'], opened['side'])
        report['last_side_price'] = side_px
        report['last_check_at'] = ts_utc()
        if side_px is not None and side_px <= sl_price:
            close_reason = f"stop_loss_{int(args.stop_loss_pct * 100)}pct"
            break

        # scale-in: add to a confirmed winner only, inside hard risk caps
        if (
            int(args.scale_enabled or 0) == 1
            and scale_attempts < int(args.scale_max_adds)
            and side_px is not None
            and side_px >= opened['entry_price'] + float(args.scale_trigger_delta)
            and side_px <= float(args.scale_max_price)
            and sec_left >= float(args.scale_min_seconds_left)
            and (opened['cost_usdc'] + float(args.scale_stake_usd)) <= float(args.max_total_notional_usd)
        ):
            scale_attempts += 1
            out_s, objs_s = run_open(args.repo, opened['market_slug'], opened['side'], float(args.scale_stake_usd), args.execute)
            post_s = None
            runner_s = None
            for o in objs_s:
                if isinstance(o, dict) and 'order_post_result' in o:
                    runner_s = o
                    post_s = o.get('order_post_result') or {}
            add: dict[str, Any] = {
                'ts': ts_utc(),
                'attempt': scale_attempts,
                'trigger_price': side_px,
                'seconds_left': sec_left,
                'stake_usd': float(args.scale_stake_usd),
                'matched': bool(post_s and post_s.get('success') is True and str(post_s.get('status', '')).lower() == 'matched'),
            }
            if add['matched']:
                add_shares = float(post_s.get('takingAmount') or 0)
                add_cost = float(post_s.get('makingAmount') or 0)
                add['shares'] = add_shares
                add['cost_usdc'] = add_cost
                add['add_price'] = float((runner_s or {}).get('entry_price') or side_px)
                add['order_id'] = post_s.get('orderID')
                add['tx'] = (post_s.get('transactionsHashes') or [None])[0]
                opened['shares'] += add_shares
                opened['cost_usdc'] += add_cost
                if opened['shares'] > 0:
                    # re-anchor stop-loss to blended average entry so the larger position keeps the same risk profile
                    avg_entry = opened['cost_usdc'] / opened['shares']
                    sl_price = avg_entry * (1.0 - args.stop_loss_pct)
                    report['stop_loss_price'] = sl_price
                    add['avg_entry_price'] = avg_entry
                    add['new_stop_loss_price'] = sl_price
            report['scale_adds'].append(add)
        time.sleep(args.poll_sec)

    # Exit-price optimization ("loss minimize"): selling our side at its best bid and
    # buying an equal number of opposite shares at their best ask are the same exit
    # economically (UP + DOWN = $1 at resolution), but they fill from different books.
    # When (1 - opp_ask) beats own_bid by hedge_min_edge, buying the opposite side
    # locks a richer exit than dumping into our own side's (often thin) bid.
    shares_to_close = float(opened['shares'])
    hedge_exit_info: Optional[dict[str, Any]] = None
    if int(args.hedge_exit or 0) == 1 and shares_to_close > 0:
        own_bid = None
        opp_ask = None
        try:
            own_bid = clob_best_bid(opened['token_id'])
        except Exception:
            pass
        try:
            _, opp_ask = clob_best_bid_ask(opened['opp_token_id'])
        except Exception:
            pass
        if own_bid is not None and opp_ask is not None and 0.0 < float(opp_ask) < 1.0:
            lock_value = 1.0 - float(opp_ask)
            hedge_notional = round(min(shares_to_close * float(opp_ask) * 1.02, shares_to_close * 0.99), 2)
            hedge_exit_info = {
                'considered_at': ts_utc(),
                'own_bid': float(own_bid),
                'opp_ask': float(opp_ask),
                'lock_value_per_share': lock_value,
                'min_edge': float(args.hedge_min_edge),
                'taken': False,
            }
            if lock_value >= float(own_bid) + float(args.hedge_min_edge) and hedge_notional >= 1.0:
                opp_side = 'DOWN' if opened['side'] == 'UP' else 'UP'
                out_h, objs_h = run_open(args.repo, opened['market_slug'], opp_side, hedge_notional, args.execute)
                post_h = None
                for o in objs_h:
                    if isinstance(o, dict) and 'order_post_result' in o:
                        post_h = o.get('order_post_result') or {}
                if post_h and post_h.get('success') is True and str(post_h.get('status', '')).lower() == 'matched':
                    hedged_shares = float(post_h.get('takingAmount') or 0)
                    hedge_cost = float(post_h.get('makingAmount') or 0)
                    covered = min(shares_to_close, hedged_shares)
                    hedge_exit_info.update({
                        'taken': True,
                        'side': opp_side,
                        'shares': hedged_shares,
                        'cost_usdc': hedge_cost,
                        'covered_shares': covered,
                        'order_id': post_h.get('orderID'),
                        'tx': (post_h.get('transactionsHashes') or [None])[0],
                    })
                    shares_to_close = max(0.0, shares_to_close - hedged_shares)
                    # residual too small to sell at min order size: hold it to resolution
                    if shares_to_close > 0 and shares_to_close * max(float(own_bid), 0.01) < 1.0:
                        hedge_exit_info['residual_held_shares'] = shares_to_close
                        shares_to_close = 0.0
                    report['locked_min_payout_usdc'] = round(covered * 1.0, 6)
    if hedge_exit_info:
        report['hedge_exit'] = hedge_exit_info

    close_debug: list[dict[str, Any]] = []
    close_obj: dict[str, Any] = {}
    out = ''
    fallback_used = None
    force_close_used = None
    client = auth_clob_client()

    for i in range(max(1, int(args.close_retry_max)) if shares_to_close > 0 else 0):
        out, objs = run_close(
            args.repo,
            opened['market_slug'],
            opened['token_id'],
            shares_to_close,
            args.execute,
            close_order_type='FAK',
        )
        close_obj = objs[-1] if objs else {}
        post = close_obj.get('order_post_result') or {}
        status = str(post.get('status') or '').lower()
        skipped = str(close_obj.get('close_skipped') or '')
        close_debug.append({
            'ts': ts_utc(),
            'attempt': i + 1,
            'order_type': 'FAK',
            'status': status,
            'close_skipped': skipped,
        })
        if post.get('success') is True and status == 'matched':
            break

        # common transient path right after open: token balance not yet visible
        if skipped == 'zero_effective_shares':
            time.sleep(float(args.close_retry_delay_sec))
            continue

        # fallback: if FAK has no instant match, try a GTC limit close near current side price
        txt = ((out or '') + '\n' + json.dumps(close_obj, ensure_ascii=False)).lower()
        if 'no orders found to match with fak order' in txt:
            px = get_side_price_from_slug(opened['market_slug'], opened['side'])
            if px is None:
                px = report.get('last_side_price')
            if px is None:
                px = opened['entry_price']
            bb = None
            try:
                bb = clob_best_bid(opened['token_id'])
            except Exception:
                bb = None
            limit_px = max(0.01, min(0.99, float((bb - 0.01) if bb is not None else px)))
            fallback_used = {'type': 'GTC_LIMIT', 'price': limit_px}
            out2, objs2 = run_close(
                args.repo,
                opened['market_slug'],
                opened['token_id'],
                shares_to_close,
                args.execute,
                close_order_type='GTC',
                close_limit_price=limit_px,
            )
            close_obj2 = objs2[-1] if objs2 else {}
            post2 = close_obj2.get('order_post_result') or {}
            status2 = str(post2.get('status') or '').lower()
            close_debug.append({
                'ts': ts_utc(),
                'attempt': i + 1,
                'order_type': 'GTC',
                'status': status2,
                'close_skipped': str(close_obj2.get('close_skipped') or ''),
                'limit_price': limit_px,
            })
            close_obj = close_obj2
            out = out2
            if post2.get('success') is True and status2 == 'matched':
                break

            # If GTC is accepted but still live, force-close flow: poll status, cancel, repost aggressive.
            if post2.get('success') is True and status2 == 'live':
                oid2 = str(post2.get('orderID') or '')
                st_upd, ord_upd = poll_order_status(client, oid2, wait_sec=min(8.0, max(2.0, float(args.close_retry_delay_sec) * 2)), step_sec=1.0)
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'GTC_POLL',
                    'status': st_upd.lower() if st_upd else '',
                    'order_id': oid2,
                })
                if st_upd == 'MATCHED':
                    post2['status'] = 'matched'
                    close_obj['order_post_result'] = post2
                    break

                cancel_info = cancel_token_orders(client, opened['token_id'])
                bb2 = None
                try:
                    bb2 = clob_best_bid(opened['token_id'])
                except Exception:
                    bb2 = None
                force_px = max(0.01, min(0.99, float((bb2 - 0.02) if bb2 is not None else 0.01)))
                force_close_used = {
                    'type': 'FORCE_GTC_LIMIT',
                    'price': force_px,
                    'cancel_info': cancel_info,
                }
                out3, objs3 = run_close(
                    args.repo,
                    opened['market_slug'],
                    opened['token_id'],
                    shares_to_close,
                    args.execute,
                    close_order_type='GTC',
                    close_limit_price=force_px,
                )
                close_obj3 = objs3[-1] if objs3 else {}
                post3 = close_obj3.get('order_post_result') or {}
                status3 = str(post3.get('status') or '').lower()
                close_debug.append({
                    'ts': ts_utc(),
                    'attempt': i + 1,
                    'order_type': 'FORCE_GTC',
                    'status': status3,
                    'close_skipped': str(close_obj3.get('close_skipped') or ''),
                    'limit_price': force_px,
                })
                close_obj = close_obj3
                out = out3
                if post3.get('success') is True and status3 == 'matched':
                    break

        time.sleep(float(args.close_retry_delay_sec))

    fully_hedged = bool(hedge_exit_info and hedge_exit_info.get('taken') and shares_to_close <= 0)
    if fully_hedged:
        closed = {
            'close_reason': f'{close_reason}_hedged_hold_to_resolution',
            'closed_at': ts_utc(),
            'close_success': True,
            'close_status': 'hedged',
            'close_order_id': hedge_exit_info.get('order_id'),
            'close_tx': hedge_exit_info.get('tx'),
            'close_shares': 0.0,
            'close_usdc': 0.0,
            'close_skipped': None,
        }
    else:
        post = close_obj.get('order_post_result') or {}
        post_status = str(post.get('status') or '').lower()
        close_usdc = float(post.get('takingAmount') or 0)
        closed = {
            'close_reason': close_reason,
            'closed_at': ts_utc(),
            'close_success': bool(post.get('success') is True and (post_status == 'matched' or close_usdc > 0)),
            'close_status': post.get('status'),
            'close_order_id': post.get('orderID'),
            'close_tx': (post.get('transactionsHashes') or [None])[0],
            'close_shares': float(post.get('makingAmount') or 0),
            'close_usdc': close_usdc,
            'close_skipped': close_obj.get('close_skipped'),
        }
    report['close_debug'] = close_debug
    if fallback_used:
        report['close_fallback'] = fallback_used
    if force_close_used:
        report['close_force'] = force_close_used
    report['close_raw'] = out[-4000:]
    report['closed'] = closed

    # PnL: with a hedge exit, covered shares pay a guaranteed $1 each at resolution
    # (UP + DOWN pair), so report the locked minimum rather than raw cashflow.
    hedge = report.get('hedge_exit') or {}
    hedge_cost = float(hedge.get('cost_usdc') or 0.0)
    covered = float(hedge.get('covered_shares') or 0.0)
    pnl = None
    if closed['close_usdc'] or covered > 0:
        pnl = round(covered * 1.0 + closed['close_usdc'] - opened['cost_usdc'] - hedge_cost, 6)
    report['pnl_basis'] = 'locked_min_at_resolution' if covered > 0 else 'cashflow'
    if covered > 0:
        report['pnl_note'] = 'hedged pair pays $1/share at resolution; residual unhedged shares add upside if the original side wins; ensure winnings redemption is handled'
    report['realized_cashflow_pnl_usdc'] = pnl
    report['finished_at'] = ts_utc()
    report['result'] = 'done_hedged' if covered > 0 else 'done'

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
