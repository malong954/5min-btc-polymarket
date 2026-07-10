# BTC 5m Odds Improvement Plan

Status: **PLAN ONLY — no runtime behavior changes are authorized by this document.**

Goal: improve the win rate and net PnL of the existing momentum-into-close strategy
(entry ≥ 0.70 on the stronger side, ~2 minutes before close, stop-loss, exit at T-20s,
optional micro-hedge) without changing what currently works.

Source of ideas: code review of [alsk1992/CloddsBot](https://github.com/alsk1992/CloddsBot)
(MIT). We port **concepts and protocol knowledge** to our Python stack; we do not import
its TypeScript code or run its engine (its live loop has known wiring defects: the
orderbook feed is never connected to the strategy engine, and live fills are never
registered by its position tracker).

## Guiding rules (apply to every item)

1. **Additive only.** New code lives in new modules/flags. The current entry/exit logic
   path stays byte-for-byte identical when flags are off.
2. **Default off.** Every feature ships behind a profile flag that defaults to the
   current behavior.
3. **Shadow before live.** Each feature must first run in *shadow mode*: it logs what it
   *would* have done alongside the live bot for a fixed evaluation window, and we compare
   against the baseline before enabling.
4. **Baseline first.** Before any feature is evaluated we freeze a baseline report
   (win rate, avg entry price, avg exit price, PnL/trade, fee drag, missed-entry count)
   from `scripts/btc5m_report.py` over a defined window, so "improved odds" is measured,
   not assumed.
5. **Kill switch.** Any enabled feature must be revertible by flipping its flag, with no
   state migration.

Rollout gate per feature: `shadow → dry-run → live at minimum stake → live at profile stake`,
advancing only if the measured metric does not regress.

---

## Item 1 — WebSocket market data (replace 5s REST polling)

**Problem.** The runner polls the CLOB via REST every ~5s (`--poll-sec 5`). In a market
whose entire life is 300s and whose edge window is the last ~120s, a 5s cadence means we
see the 0.70 trigger up to 5s late: worse entry prices, and stop-loss/T-20s exits that
react late. Our own profile config already guards against this weakness
(`skip_if_quote_stale_sec_gt: 8`).

**Change.** Add a market-data module (`scripts/lib/md_ws.py` or similar) that subscribes to
Polymarket's CLOB market channel — `wss://ws-subscriptions-clob.polymarket.com/ws/market`
— for the active market's token IDs, maintaining an in-memory best bid/ask and book.
Message types to handle: `book`, `price_change` (post-2025-09-15 schema with legacy
fallback), `best_bid_ask`, `last_trade_price`, `tick_size_change`, `market_resolved`.
Reconnect with exponential backoff; **on any WS staleness, fall back to the existing REST
poll automatically** (REST path is retained permanently as fallback, not deleted).

Optionally (phase 1b), subscribe to the authenticated *user channel*
(`.../ws/user`, L2 HMAC creds in the subscribe payload, JSON ping every 10s) to get
push-based fill confirmations instead of inferring fills.

**Why it improves odds.** Same strategy, earlier trigger detection → better average entry
price at the 0.70 crossing, tighter stop-loss and T-20s execution. No decision logic changes.

**Break risk & mitigation.** Medium if rushed (new failure mode: silently stale WS feeding
the trigger). Mitigations: freshness timestamp on every quote; if `now - last_ws_msg >
stale_threshold`, the runner transparently reverts to REST polling and logs it; shadow mode
runs WS and REST side by side and logs quote deltas and trigger-time differences before the
WS path is ever allowed to drive entries.

**Flag.** `market_data.source: rest_poll | ws_with_rest_fallback` (default `rest_poll`).

**Acceptance.** ≥ 1 week shadow: WS trigger fires earlier than REST on ≥ X% of entries with
zero false triggers vs. REST-confirmed prices; no unhandled disconnect gaps > stale threshold.

---

## Item 2 — Dual price feed: Binance spot (early signal) + Chainlink RTDS (settlement truth)

**Problem.** Our entry requires "BTC moved ~$70–100 in the interval," but the market
resolves on Polymarket's oracle (Chainlink), not on whatever source we measure the move
against. Near the boundary, feed mismatch converts a "confirmed move" into a loss —
exactly the late-entry failure mode: a $75 move on our feed can be a $60 move on the
resolution feed.

**Change.** Add a price-feed module with two sources:

- **Binance WS** (`wss://stream.binance.com:9443/ws`, `btcusdt@ticker` or trade stream)
  as the low-latency *signal* feed (REST fallback `api.binance.com/api/v3`).
- **Polymarket RTDS** (`wss://ws-live-data.polymarket.com`), topic
  `crypto_prices_chainlink`, as the *settlement-truth* feed — the same oracle family that
  resolves the market.

Entry rule becomes two-stage (flagged): detect the move on Binance, **confirm** the move
size against the Chainlink feed before entering. If feeds disagree beyond a configured
tolerance near the threshold, skip the trade (a skipped marginal trade is the intended
behavior, not a regression).

**Why it improves odds.** Filters out precisely the marginal entries where the move exists
on spot but not on the resolution feed — those are disproportionately the losers. Also
gives an earlier, tick-level move detection than any REST source.

**Break risk & mitigation.** Low-medium: strictly a *filter* (it can only remove entries,
never add or resize). Shadow mode first: log Binance-vs-Chainlink deltas at every
would-be entry for a week; set the tolerance from that data; then enable as filter-only.

**Flag.** `signal.confirm_with_chainlink: false` (default), `signal.feed_disagree_tolerance_usd`.

**Acceptance.** Shadow data shows the filter would have skipped more losing entries than
winning ones over the window (net expected PnL of skipped set < 0).

---

## Item 3 — Exit ladder: ratchet floor + time-aware trailing stop

**Problem.** Current exits: fixed stop-loss (25–30% from entry) and hard exit at T-20s.
Between entry (~0.70) and exit there is no profit protection: a position that runs to
0.90 and reverses to 0.72 exits near flat despite having been well in profit.

**Change.** Add an exit engine evaluated on every quote (works with Item 1, but also with
REST polling) implementing, in priority order:

1. `force_exit` — existing T-20s rule (unchanged, always wins).
2. `stop_loss` — existing rule (unchanged).
3. `ratchet_floor` — once unrealized profit crosses defined rungs, raise a floor that
   locks in a fraction of the peak (e.g. peak +20% → floor +12%; peak +10% → floor +5%;
   table tuned in shadow). Exit if price falls to the floor.
4. `trailing_stop` — trailing distance tightens as time-to-expiry shrinks (wide at
   T-120s, tight at T-40s), since recovery time no longer exists late in the round.

CloddsBot's `positions.ts` exit set (9 tiers incl. depth-collapse and stale-profit exits)
is the reference; we adopt only these two additions first — smallest surface, clearest win.

**Why it improves odds.** Directly converts already-earned unrealized profit into realized
profit on reversals; does not change entries at all.

**Break risk & mitigation.** Medium — this is the only item that changes *when we exit*,
so it can genuinely reduce PnL if mis-tuned (exiting winners too early that would have
resolved in-the-money). Mitigations: shadow mode replays every live position and logs
"ladder would have exited here at X vs. actual outcome Y"; enable only if shadow shows
net improvement including the trades it would have cut short; rungs configured per
profile; single flag reverts to the current two-exit behavior.

**Flag.** `exits.ladder_enabled: false` (default).

**Acceptance.** ≥ 2 weeks shadow: ladder-simulated PnL > actual PnL over the same
positions, and the ladder never violates the T-20s or stop-loss guarantees.

---

## Item 4 — Fee-aware PnL and edge accounting

**Problem.** Polymarket charges a taker fee on these crypto markets, well-approximated by
`fee = 0.125 · (p·(1−p))²` per share (verify the current coefficient against live fills
before hardcoding — CloddsBot's own code and comments disagree, 0.125 vs 0.25). At our
p≈0.70 entry it's small (~0.5%), but a strategy netting a few percent per trade, plus a
stop-loss that re-crosses the spread, should not treat it as zero. Reports currently
overstate the edge.

**Change.** Reporting/accounting only:

- Add the fee model to `btc5m_report.py` / `btc5m_latest_report.py`: per-trade gross PnL,
  fees paid (entry + any exit that takes), spread paid, net PnL. Calibrate against actual
  fill data from the runner logs.
- Add a `min_net_edge` note to the profile YAML documenting the true breakeven at 0.70
  entry including fee + spread (informational; the threshold itself is not changed).

**Why it improves odds.** Doesn't change trading directly — it makes every other item's
evaluation honest, and tells us whether marginal setups (thin edge at high entry price)
are actually positive after costs. Decisions made on gross PnL are how a slightly winning
strategy quietly becomes a losing one.

**Break risk & mitigation.** **None to the live path** — pure reporting. This is the one
item that can be merged without a shadow phase. Risk is only misreporting; validate the
fee model against a sample of real fills first.

**Flag.** None needed (reporting only). Ship first.

**Acceptance.** Fee column matches actual charged fees on ≥ 20 live fills within rounding.

---

## Item 5 — Optional expiry-fade mode (flat-market complement) — backtest-gated

**Problem/opportunity.** Our momentum trigger only fires when BTC has already moved.
In flat windows the bot sits idle. CloddsBot's `expiry_fade` trades exactly that regime:
in the last 1–5 minutes, if spot is flat but the book is skewed ≥ 0.15 from 0.50, buy the
*cheap* side (mean reversion on the odds, not the price). It is the mirror-image of our
strategy and would raise trade frequency without competing for the same entries.

**Change (eventually).** A separate strategy mode in the runner, mutually exclusive per
round with the momentum entry (never both in the same round in phase 1), with its own
small stake cap, its own daily loss cap, and its own report bucket.

**Why it might improve odds.** More rounds traded with an independent edge → higher total
PnL *if* the edge is real on today's markets. That "if" is unproven for us.

**Break risk & mitigation.** Highest uncertainty of the five — this is a *new bet*, not an
optimization, and it loses in exactly the environment where our momentum strategy wins
(late moves). Therefore: **strictly backtest/paper-gated.** Order of proof:
1. Collect flat-window skew data via the Item 1/2 feeds (no orders).
2. Paper-trade the rule in shadow for ≥ 2 weeks; require positive net PnL after Item 4 fees.
3. Only then propose live enablement as a separate decision — not covered by this plan's
   approval.

**Flag.** `strategies.expiry_fade.enabled: false` (default; live enablement out of scope).

---

## Sequencing

| Phase | Item | Touches live behavior? | Gate |
|-------|------|------------------------|------|
| 0 | Freeze baseline report window | No | — |
| 1 | Item 4: fee-aware reporting | No | Fill-data validation |
| 2 | Item 1: WS market data (shadow → fallback-guarded live) | Yes, execution timing only | 1-week shadow, no regressions |
| 3 | Item 2: Chainlink confirm filter (shadow → filter-only live) | Yes, removes entries only | Shadow shows skipped set is net-negative |
| 4 | Item 3: exit ladder (shadow → live) | Yes, exits only | 2-week shadow beats actual PnL |
| 5 | Item 5: expiry fade (data → paper only) | No (live out of scope) | Separate future decision |

Ordering rationale: Item 4 makes measurement honest before anything else changes; Item 1
improves plumbing without touching decisions; Items 2–3 change decisions and therefore sit
behind the longest gates; Item 5 is research until proven.

## Reference map (CloddsBot, for implementation phase)

| Topic | CloddsBot location |
|-------|--------------------|
| CLOB market WS handling, reconnect, schema fallback | `src/feeds/polymarket/index.ts` |
| User channel (fills) WS + HMAC subscribe payload | `src/feeds/polymarket/user-ws.ts` |
| RTDS Chainlink crypto prices | `src/feeds/polymarket/rtds.ts` |
| Binance WS/REST feed with fallbacks | `src/feeds/crypto/index.ts` |
| Exit ladder (ratchet/trailing tables) | `src/strategies/crypto-hft/positions.ts` |
| Taker fee formula | `src/strategies/crypto-hft/types.ts` |
| Market discovery slug logic (validates ours) | `src/strategies/crypto-hft/market-scanner.ts` |

Known CloddsBot defects — do not copy: engine never receives orderbook events
(`onOrderbook` uncalled); live order path never reports `filledSize`, so its position
tracker misses live fills; Gamma response parsing expects snake_case fields; `aggressive`
preset defaults to live trading.
