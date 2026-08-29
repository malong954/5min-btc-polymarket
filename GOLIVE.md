# FLIPPOLYBOT — Go-Live Preparation Guide

Everything you need to prepare on your end before this bot places its first
real order on Polymarket. Written to be shareable: a friend with a Mac and an
hour should be able to follow it end to end.

> **Status of the code**: everything in this repo currently trades PAPER money
> (real prices, simulated fills). The live order executor is the final module
> and will only be added after the paper phase completes its validation
> (~200 official-graded trades) and the items below are ready.

---

## 1. What this bot does (one paragraph)

Polymarket runs 5-minute BTC Up/Down markets: buy the UP or DOWN contract at
price `c` (in cents), win $1 per share if you're right. This bot buys
**whichever side BTC is already leading** in a measured sweet-spot window
(~180–240 seconds before close), **only while the contract is still cheap**
(ask ≤ $0.72) and the move is decisive (≥ $10). Measured on thousands of
recorded rounds and validated live on paper: ~73% win rate at ~$0.65 average
cost → roughly **+12% expected value per trade**. Every claim above is
re-verifiable from the logs with `scripts/lab.sh analyze`.

---

## 2. Accounts and eligibility (do this first)

1. **Polymarket account** — sign up at polymarket.com and complete whatever
   verification it requires in your region.
   - **Eligibility check, not optional**: Polymarket's availability and rules
     differ by country and by US state, and they have changed over time.
     Confirm that trading is currently permitted for you specifically (and for
     each friend, in their own region) before funding anything. If the UI lets
     you deposit and trade manually, API trading uses the same account.
2. **Place one tiny manual trade in the UI** on a BTC 5m market. This proves
   your account is fully enabled end to end, and shows you the actual fee
   charged — write that fee down; the bot's economics must include it.

---

## 3. Wallet and keys (the critical part — read twice)

Polymarket runs on Polygon. Orders are signed with a private key.

**Use a dedicated, fresh wallet for the bot. Never your main wallet.**

1. Create a **new** wallet (MetaMask or any tool that exports a private key).
   This wallet will hold ONLY the bot's bankroll.
2. Connect that wallet to Polymarket once via the UI so Polymarket creates and
   links its proxy/funder wallet for your account.
3. Record, into a local `.env` file (NEVER into git, NEVER into a chat):
   - `POLY_PRIVATE_KEY` — the new wallet's private key (0x…)
   - `POLY_FUNDER` — your Polymarket proxy/funder address, shown in the
     Polymarket UI under your profile/deposit address
4. **API credentials are derived, not issued.** The official client turns your
   private key into L2 API credentials automatically:

```python
from py_clob_client.client import ClobClient
client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137)
creds = client.create_or_derive_api_key()
```

   You do not need to request anything from Polymarket support.

### Key safety rules (share these with friends verbatim)

- The private key IS the money. Anyone with the key has the funds.
- `.env` is already in this repo's `.gitignore`. Keep it that way.
- Never paste the key into a chat, a screenshot, an issue, or a script that
  prints its environment.
- Fund the wallet with only what you are prepared to lose while testing.

---

## 4. Funding

- Polymarket balances are **USDC on Polygon**. Deposit through the Polymarket
  UI (card / exchange transfer / bridge — the UI walks you through it).
- Suggested starting bankroll for the probe phase: **$50–100**. The first live
  phase uses $1–2 stakes; you are paying to measure fills, not to earn.
- Keep a few dollars of POL (Polygon gas token) in the wallet for any on-chain
  actions the deposit/withdraw flow needs. Order placement itself is gasless.

---

## 5. The `.env` file

Create `~/5min-btc-polymarket/.env` (template — fill in your values):

```
POLY_PRIVATE_KEY=0xyour_dedicated_bot_wallet_key
POLY_FUNDER=0xyour_polymarket_proxy_address
CLOB_HOST=https://clob.polymarket.com
CHAIN_ID=137

BANKROLL_LIVE=50
STAKE_LIVE=1
MAX_PRICE=0.72
LEAD_MIN_MOVE=10

CHAINLINK_API_KEY=
CHAINLINK_API_SECRET=
```

Notes:
- The `CHAINLINK_*` values are optional. Polymarket resolves these markets on
  **Chainlink Data Streams**; our bot already grades every settle against
  Polymarket's official resolution (no key needed for that). Data Streams
  credentials would additionally let the mid-round SIGNAL use the exact
  resolution feed. Access is by sign-up with Chainlink (chain.link → Data
  Streams); when you have a key + secret, the provider gets wired in.
- Everything else the bot uses today is keyless: Binance market data via the
  open `data-api.binance.vision` mirror (works from US networks), and
  Polymarket's public gamma/CLOB endpoints for prices, sizes, and resolutions.

---

## 6. Machine setup (Mac mini)

```
git clone https://github.com/malong954/5min-btc-polymarket.git
cd 5min-btc-polymarket
git checkout claude/bot-trend-detection-06svzm
python3 -m venv .venv
.venv/bin/pip install requests py-clob-client
```

Operational must-haves:
- **Clock sync**: rounds align to exact 5-minute boundaries. System Settings →
  General → Date & Time → set automatically. A skewed clock corrupts entries.
- **Keep the Mac awake**: System Settings → Energy → prevent sleep, or run the
  bot under `caffeinate -is scripts/lab.sh`.
- **Stable network**: the bot survives brief request failures, but its edge
  is time-sensitive; flaky Wi-Fi costs real entries. Ethernet preferred.
- The processes run under `nohup` and survive closed terminals, but **not a
  reboot** — after any restart, re-run `scripts/lab.sh`.

Daily driving:

```
scripts/lab.sh            start everything + dashboard
scripts/lab.sh status     is it alive, how much data
scripts/lab.sh analyze    the full evidence battery
scripts/lab.sh history    every entry/skip/win/loss
scripts/lab.sh stop       kill switch
```

---

## 7. Fees — the go/no-go number

Polymarket charges **taker fees** on these markets. The formula scales as
`rate × p × (1-p)` per contract, which per dollar staked is `rate × (1-price)`
— at our ~$0.65 entries that is meaningful and MUST be subtracted from the
+12%/trade paper edge.

Your one manual UI trade (section 2) reveals the live fee. Before any bot
order, that number gets wired into the executor and the dashboard so P/L is
fee-true. If the measured fee ever exceeds the measured edge: no-go, and the
bot stays paper.

---

## 8. Go-live phases (do not skip steps)

**Phase 0 — now**: paper trading with official-resolution grading. Target:
200+ trades, win rate ≥ 70% at ≤ $0.68 average cost.

**Phase 1 — probe (~$20 at risk)**: $1–2 stakes, live orders, same rule. The
goal is NOT profit; it is measuring **fill quality**: how often the order
fills, at what price vs. the recorded ask, and the true fee. ~50 trades.

**Phase 2 — small (~$10 stakes)**: only if Phase 1 fills within ~2 cents of
recorded asks and the fee-adjusted edge stays positive. Flat stakes, no
compounding, withdraw profits weekly.

**Phase 3 — scale**: only after 2+ weeks of Phase 2 profitability. Size up
slowly; this market's liquidity at the cheap asks is finite (typically a few
hundred shares) — the strategy does not scale to large stakes.

**Standing rules at every phase**
- Flat stakes. Never percent-of-balance, never martingale.
- Hard cap: never buy above `MAX_PRICE` (default 0.72 for the lead rule).
- One bot instance per account.
- If live results diverge from paper by more than a few points over 50+
  trades, stop and investigate before adding money.

---

## 9. Preparation checklist (print this)

- [ ] Polymarket account created, verified, eligible in my region
- [ ] One manual UI trade done; actual fee written down: ______
- [ ] Fresh dedicated wallet created; private key stored safely offline
- [ ] Wallet connected to Polymarket once via UI; funder address recorded
- [ ] USDC deposited (probe-phase amount only) + a few POL for gas
- [ ] `.env` created on the Mac, filled in, never committed
- [ ] `py-clob-client` installed in the venv
- [ ] Mac clock auto-syncs; sleep disabled; on Ethernet
- [ ] Paper phase shows ≥ 70% win rate at ≤ $0.68 avg over 200+ official-graded trades
- [ ] (Optional) Chainlink Data Streams access requested

When every box is checked and the paper numbers hold, the live executor gets
built against your `.env` — starting at $1 stakes, measuring everything.
