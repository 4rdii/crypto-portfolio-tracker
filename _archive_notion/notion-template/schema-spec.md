# Crypto Portfolio Tracker — Notion Template Specification

**Target user:** DeFi-native crypto power users (multi-chain, multi-wallet, yield farmers, airdrop hunters, NFT collectors). Not generic "buy BTC and hold" investors.

**Stack:** Notion (front-end) + n8n (sync engine) + Moralis / Helius / mempool.space (data sources).

**Design principle:** Notion cannot fetch live prices natively. We solve this with a dedicated `Prices` database that n8n writes to every N minutes. All other databases pull price data via `relation + rollup`, never via hardcoded numbers. This keeps Holdings, DeFi Positions, NFT Holdings, and Watchlist all reactive to a single source of truth.

**Write-safety principle:** Notion formula properties are read-only via the API. Any field n8n needs to write MUST be a `number`, `rich_text`, `date`, `select`, `checkbox`, `url`, or `relation` — never a formula. Every schema below marks each field as one of:

- `MANUAL` — user edits in the UI, n8n never touches
- `N8N` — n8n writes via API, user should not edit manually
- `FORMULA` — computed by Notion, nobody writes
- `ROLLUP` — aggregated from a relation, nobody writes
- `HYBRID` — either side can write (e.g., avg cost can be auto-computed from Transactions but user can override)

---

## Table of Contents

1. [Prices](#1-prices) (source of truth for USD values)
2. [Wallets](#2-wallets) (input database for the n8n scanner)
3. [Holdings](#3-holdings) (current token positions)
4. [Transactions](#4-transactions) (full activity log)
5. [DeFi Positions](#5-defi-positions)
6. [NFT Holdings](#6-nft-holdings)
7. [Watchlist](#7-watchlist)
8. [Research Notes](#8-research-notes)
9. [Airdrops Tracker](#9-airdrops-tracker)
10. [Balance Snapshots](#10-balance-snapshots)
11. [Dashboard Page Layout](#dashboard-page-layout)
12. [Settings Page](#settings-page)
13. [n8n Write Matrix](#n8n-write-matrix-cheat-sheet)

---

## 1. Prices

The heartbeat of the template. n8n polls CoinGecko / DexScreener / Jupiter / Birdeye every 5–15 minutes and upserts one row per tracked asset. Every other database relates to this by symbol.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Symbol | title | — | N8N (also MANUAL on first create) |
| Coin Name | rich_text | e.g. "Ethereum", "Jito Staked SOL" | N8N |
| Chain | select | Ethereum, Arbitrum, Base, Optimism, Solana, Bitcoin, Polygon, BNB, Avalanche, Sui, TON, Berachain, Other | N8N |
| Contract Address | rich_text | — | N8N |
| CoinGecko ID | rich_text | e.g. "ethereum", "jito-staked-sol" | MANUAL |
| USD Price | number (format: dollar) | — | N8N |
| 24h Change % | number (format: percent) | — | N8N |
| 7d Change % | number (format: percent) | — | N8N |
| Market Cap | number (format: dollar) | — | N8N |
| Last Updated | date (with time) | — | N8N |
| Price Source | select | CoinGecko, DexScreener, Jupiter, Birdeye, Manual | N8N |
| Holdings (rel) | relation → Holdings | dual | SYSTEM |
| Watchlist (rel) | relation → Watchlist | dual | SYSTEM |
| DeFi Positions (rel) | relation → DeFi Positions | dual | SYSTEM |
| Is Stale | formula | `if(dateBetween(now(), prop("Last Updated"), "minutes") > 30, "⚠️ stale", "✓ fresh")` | FORMULA |

**Key rule:** Never hand-enter a USD price here. If you need to track a token n8n can't auto-fetch, enter a row with `Price Source = Manual` and edit the price yourself — n8n will skip it on upserts if source is Manual.

---

## 2. Wallets

Address book and INPUT to the scanner. This is where you declare which addresses n8n should scan.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Label | title | e.g. "Main ETH", "Cold Storage Ledger", "Farm wallet #3" | MANUAL |
| Address | rich_text | — | MANUAL |
| Chain | select | Ethereum, Arbitrum, Base, Optimism, Solana, Bitcoin, Polygon, BNB, Avalanche, Sui, TON, Berachain, Other | MANUAL |
| Type | select | Hot, Cold, Hardware, Exchange, Multisig, Smart Wallet | MANUAL |
| Purpose | multi_select | Main, Trading, Farming, Airdrops, NFTs, Savings, Degen, Testnet, Burner | MANUAL |
| Scan Enabled | checkbox | — | MANUAL |
| Created Date | date | — | MANUAL |
| Last Scanned | date (with time) | — | N8N |
| Scan Status | select | OK, Error, Rate-limited, Not scanned yet | N8N |
| Scan Error Msg | rich_text | — | N8N |
| Total Value USD | rollup → Holdings.Current Value USD (sum) | — | ROLLUP |
| Token Count | rollup → Holdings (count) | — | ROLLUP |
| Holdings (rel) | relation → Holdings | dual | SYSTEM |
| Transactions (rel) | relation → Transactions | dual | SYSTEM |
| DeFi Positions (rel) | relation → DeFi Positions | dual | SYSTEM |
| NFT Holdings (rel) | relation → NFT Holdings | dual | SYSTEM |
| Notes | rich_text | — | MANUAL |

**Critical:** Only wallets with `Scan Enabled = true` are touched by n8n. The bot reads this database at the start of every run to get the list of addresses + chains.

---

## 3. Holdings

Current token positions, one row per (wallet, token) pair. Largely written by n8n — the user only edits cost basis & notes.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Position | title | Convention: `{SYMBOL} — {Wallet Label}`, e.g. "JITOSOL — Main SOL" | N8N |
| Symbol | rich_text | — | N8N |
| Wallet (rel) | relation → Wallets | dual | N8N |
| Price (rel) | relation → Prices | dual (single-select behavior) | N8N |
| Chain | select | (same list as Prices.Chain) | N8N |
| Amount | number | — | N8N |
| Avg Cost USD (per unit) | number (dollar) | — | HYBRID (default MANUAL; n8n can compute from Transactions if user enables auto-cost) |
| Cost Basis Total | formula | `prop("Amount") * prop("Avg Cost USD (per unit)")` | FORMULA |
| Current Price USD | rollup → Prices.USD Price (show original) | — | ROLLUP |
| Current Value USD | formula | `prop("Amount") * prop("Current Price USD")` | FORMULA |
| P&L USD | formula | `prop("Current Value USD") - prop("Cost Basis Total")` | FORMULA |
| P&L % | formula | `if(prop("Cost Basis Total") == 0, 0, (prop("Current Value USD") - prop("Cost Basis Total")) / prop("Cost Basis Total"))` | FORMULA (format as percent) |
| 24h Change % | rollup → Prices.24h Change % | — | ROLLUP |
| Allocation % | formula | `prop("Current Value USD") / prop("Portfolio Total USD")` | FORMULA (see note below) |
| Portfolio Total USD | rollup → Wallets.Total Value USD (sum, via Wallet relation) OR a single-row "Totals" helper DB | ROLLUP |
| Category | select | L1, L2, Stablecoin, DeFi Blue Chip, LST, LRT, Meme, Gaming, AI, RWA, Privacy, Other | MANUAL |
| Conviction | select | Core, Trade, Farming, Speculation, Dust | MANUAL |
| Last Updated | date (with time) | — | N8N |
| Notes | rich_text | — | MANUAL |

**Note on Allocation %:** Notion formulas can't compute "% of whole DB sum" directly. Two workable patterns:
1. **Totals helper DB** — a single-row database `Portfolio Totals` that n8n writes the grand total to, related from every Holding. Rollup pulls it into each row. Simple and robust.
2. **Wallet-scoped allocation** — use the Wallets rollup; allocation is % within its own wallet. Less useful for DeFi-natives with 8 wallets.

Recommended: pattern 1. Define a hidden `Portfolio Totals` DB with one row, related from every Holdings row, and n8n writes `Grand Total USD` there each sync.

**Duplicate prevention:** n8n uses `(Wallet ID, Symbol, Chain)` as the dedup key. If the row already exists, update Amount + Last Updated + Price relation; do not recreate.

---

## 4. Transactions

Full activity log. User-entered for historical CEX trades, n8n-entered for on-chain txs discovered during wallet scans.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Tx | title | Convention: `{DATE} {TYPE} {AMOUNT} {SYMBOL}`, e.g. "2026-03-14 BUY 2.5 ETH" | HYBRID |
| Date | date (with time) | — | HYBRID |
| Type | select | Buy, Sell, Swap, Transfer In, Transfer Out, Airdrop, Stake, Unstake, Claim Reward, LP Add, LP Remove, Borrow, Repay, Bridge, Gas, NFT Buy, NFT Sell, Other | HYBRID |
| Coin | rich_text | Symbol sent or received (primary leg) | HYBRID |
| Coin Out | rich_text | For swaps: token paid | HYBRID |
| Amount | number | — | HYBRID |
| Amount Out | number | For swaps | HYBRID |
| Price USD (at tx time) | number (dollar) | — | HYBRID |
| USD Value | formula | `prop("Amount") * prop("Price USD (at tx time)")` | FORMULA |
| Fees USD | number (dollar) | Gas + protocol fee converted to USD at tx time | HYBRID |
| Wallet (rel) | relation → Wallets | dual | HYBRID |
| Counterparty | rich_text | CEX name, protocol ("Uniswap V4"), contract label | HYBRID |
| Tx Hash | rich_text | — | HYBRID |
| Explorer Link | formula | `if(prop("Tx Hash") == "", "", "https://etherscan.io/tx/" + prop("Tx Hash"))` | FORMULA (simple version; multi-chain version below) |
| Realized P&L USD | number (dollar) | Only populated for Sell / Swap-out legs | HYBRID (n8n computes FIFO/AVG if auto-cost enabled) |
| Tax Lot Method | select | FIFO, LIFO, HIFO, Average | MANUAL |
| Notes | rich_text | — | MANUAL |
| Ingested By | select | Manual, Moralis, Helius, Mempool, CSV Import | N8N |

**Multi-chain explorer formula:**

```
if(prop("Tx Hash") == "", "",
  if(contains(lower(prop("Counterparty")), "solana"), "https://solscan.io/tx/" + prop("Tx Hash"),
  if(contains(lower(prop("Counterparty")), "arbitrum"), "https://arbiscan.io/tx/" + prop("Tx Hash"),
  if(contains(lower(prop("Counterparty")), "base"), "https://basescan.org/tx/" + prop("Tx Hash"),
  "https://etherscan.io/tx/" + prop("Tx Hash")))))
```

A cleaner approach: add a `Chain` select to Transactions (n8n fills it) and switch on that instead of parsing Counterparty.

**Dedup key for n8n:** `Tx Hash + Wallet + log_index` to handle multi-leg contracts (e.g., Uniswap V3 swap emits multiple Transfer events).

---

## 5. DeFi Positions

One row per active position. Harder to auto-sync than spot holdings because protocol data is heterogeneous — n8n writes the ones it understands (Aave, Compound, Uniswap V3, Pendle, Jito, Kamino, etc.) and user can add others manually.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Position | title | e.g. "Pendle PT-eETH Jun26 — Main ETH" | HYBRID |
| Protocol | select | Aave, Compound, Morpho, Uniswap V3, Uniswap V4, Curve, Pendle, EigenLayer, Symbiotic, Jito, Marinade, Kamino, Drift, Ethena, Lido, Rocket Pool, Gearbox, Fluid, Other | HYBRID |
| Chain | select | (same list as Prices.Chain) | HYBRID |
| Position Type | select | LP, Lending Supply, Lending Borrow, Farming, Staking, Restaking, Vault, PT (fixed yield), YT (yield token), Perp, Option | HYBRID |
| Assets | multi_select | ETH, USDC, USDT, DAI, SOL, BTC, EIGEN, ENA, ezETH, weETH, rsETH, pufETH, wstETH, jitoSOL, PENDLE, AERO, CRV, other | HYBRID |
| Price (rel) | relation → Prices (multi) | dual | N8N |
| Wallet (rel) | relation → Wallets | dual | HYBRID |
| Entry Date | date | — | MANUAL |
| Initial Deposit USD | number (dollar) | — | MANUAL |
| Current Value USD | number (dollar) | — | N8N |
| Rewards Earned USD | number (dollar) | Claimed + unclaimed | N8N |
| Rewards Claimed | checkbox | — | MANUAL |
| APY % | number (percent) | Live/target APY from protocol | N8N |
| Realized Yield USD | formula | `prop("Current Value USD") + prop("Rewards Earned USD") - prop("Initial Deposit USD")` | FORMULA |
| Realized Yield % | formula | `if(prop("Initial Deposit USD") == 0, 0, (prop("Current Value USD") + prop("Rewards Earned USD") - prop("Initial Deposit USD")) / prop("Initial Deposit USD"))` | FORMULA |
| Health Factor | number | For lending positions; NaN/blank otherwise | N8N |
| Liquidation Risk | formula | `if(empty(prop("Health Factor")), "—", if(prop("Health Factor") < 1.2, "🔴 CRITICAL", if(prop("Health Factor") < 1.5, "🟡 watch", "🟢 safe")))` | FORMULA |
| Status | select | Active, Closed, Partially Closed, Underwater, Matured (for Pendle PT) | HYBRID |
| Position URL | url | Direct link to protocol UI (e.g., `https://app.pendle.finance/...`) | MANUAL |
| Last Updated | date (with time) | — | N8N |
| Notes | rich_text | — | MANUAL |

**n8n handling:** For each supported protocol, a sub-workflow queries the protocol's subgraph or RPC and upserts on `(Wallet + Protocol + Position Type + Assets)` key. For unsupported protocols, user maintains manually and n8n skips.

---

## 6. NFT Holdings

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Title | title | e.g. "Pudgy Penguin #4823" | N8N |
| Collection | select | Open list; n8n adds new options as they appear | N8N |
| Token ID | rich_text | — | N8N |
| Chain | select | Ethereum, Solana, Base, Polygon, Arbitrum, Bitcoin (Ordinals), Other | N8N |
| Contract Address | rich_text | — | N8N |
| Wallet (rel) | relation → Wallets | dual | N8N |
| Acquired Date | date | — | HYBRID |
| Cost Basis USD | number (dollar) | — | MANUAL |
| Current Floor USD | number (dollar) | From Magic Eden / OpenSea / Tensor | N8N |
| Last Sale USD | number (dollar) | — | N8N |
| Unrealized P&L USD | formula | `prop("Current Floor USD") - prop("Cost Basis USD")` | FORMULA |
| Unrealized P&L % | formula | `if(prop("Cost Basis USD") == 0, 0, (prop("Current Floor USD") - prop("Cost Basis USD")) / prop("Cost Basis USD"))` | FORMULA |
| Rarity Rank | number | — | N8N |
| Image URL | url | IPFS/Arweave gateway URL | N8N |
| Marketplace Link | url | Direct link to listing page | N8N |
| Status | select | Holding, Listed, Sold, Staked, Locked | MANUAL |
| List Price USD | number (dollar) | Only if Status = Listed | MANUAL |
| Notes | rich_text | — | MANUAL |

**Gallery view** keyed on `Image URL` makes this DB look like a native NFT grid.

---

## 7. Watchlist

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Coin | title | e.g. "Berachain BERA", "Monad MON" | MANUAL |
| Symbol | rich_text | — | MANUAL |
| Chain | select | (same list as Prices.Chain) | MANUAL |
| Thesis | rich_text | 1–3 sentence reason you're watching | MANUAL |
| Target Entry USD | number (dollar) | — | MANUAL |
| Price (rel) | relation → Prices | dual | MANUAL (user links once, n8n keeps price fresh) |
| Current Price USD | rollup → Prices.USD Price | — | ROLLUP |
| 24h Change % | rollup → Prices.24h Change % | — | ROLLUP |
| Distance to Entry % | formula | `if(empty(prop("Target Entry USD")) or prop("Target Entry USD") == 0, 0, (prop("Current Price USD") - prop("Target Entry USD")) / prop("Target Entry USD"))` | FORMULA |
| Entry Signal | formula | `if(empty(prop("Target Entry USD")), "—", if(prop("Current Price USD") <= prop("Target Entry USD"), "🟢 BUY", "⚪ wait"))` | FORMULA |
| Status | select | Researching, Waiting for Entry, Bought, Rejected, Expired | MANUAL |
| Research Note (rel) | relation → Research Notes | dual | MANUAL |
| Added Date | date | — | MANUAL |
| Priority | select | High, Medium, Low | MANUAL |
| Notes | rich_text | — | MANUAL |

---

## 8. Research Notes

One page per coin. The properties are minimal — the real content lives in the page body, which uses a template.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Coin | title | e.g. "Ethena (ENA)" | MANUAL |
| Symbol | rich_text | — | MANUAL |
| Category | multi_select | L1, L2, DeFi, LST, LRT, Stablecoin, RWA, DePIN, AI, Gaming, Meme, Infra, Privacy | MANUAL |
| Decision | select | Buy, Watchlist, Pass, Sold, Revisit | MANUAL |
| Conviction (1–5) | select | 1, 2, 3, 4, 5 | MANUAL |
| Target Price USD | number (dollar) | — | MANUAL |
| Date Researched | date | — | MANUAL |
| Watchlist (rel) | relation → Watchlist | dual | MANUAL |
| Holdings (rel) | relation → Holdings | dual | MANUAL |
| Source Links | url | Primary whitepaper / site | MANUAL |

**Page body template (inserted when user creates a new page in this DB):**

```markdown
## Thesis
_Why does this project matter? What's the bet?_

## Tokenomics
- Supply: 
- Emissions: 
- Unlock schedule: 
- Who holds it:

## Team
- Founders: 
- Track record: 
- Backers:

## Recent News
- 

## On-chain Metrics
- TVL: 
- DAU: 
- Fees / revenue: 
- Holder distribution:

## Risks
- 

## Valuation
- FDV: 
- Comparable projects: 
- Target price: 
- Bear case: 

## Decision
[ ] Buy
[ ] Add to watchlist
[ ] Pass
```

---

## 9. Airdrops Tracker

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Protocol | title | e.g. "LayerZero", "zkSync", "Monad", "Berachain", "Hyperliquid" | MANUAL |
| Chain | select | (same list as Prices.Chain) + "Multi-chain" | MANUAL |
| Status | select | Researching, Farming, Snapshot Taken, Claimable, Claimed, Expired, Dud | MANUAL |
| Eligibility Criteria | rich_text | Bullet list of known requirements | MANUAL |
| Tasks Done | multi_select | Bridged, Swapped, Provided LP, Staked, Voted, Minted NFT, Deployed Contract, Referred, Used N times, Volume threshold | MANUAL |
| Qualifying Wallets (rel) | relation → Wallets | dual, multi | MANUAL |
| Wallet Count | rollup → Qualifying Wallets (count) | — | ROLLUP |
| Estimated Value USD | number (dollar) | User's guess or published estimate | MANUAL |
| Actual Received USD | number (dollar) | — | MANUAL |
| Snapshot Date | date | — | MANUAL |
| Claim Start | date | — | MANUAL |
| Claim Deadline | date | — | MANUAL |
| Days to Deadline | formula | `if(empty(prop("Claim Deadline")), "—", format(dateBetween(prop("Claim Deadline"), now(), "days")) + " days")` | FORMULA |
| Deadline Alert | formula | `if(empty(prop("Claim Deadline")), "", if(dateBetween(prop("Claim Deadline"), now(), "days") < 7 and prop("Status") == "Claimable", "🚨 URGENT", ""))` | FORMULA |
| Claim Tx Hash | rich_text | — | MANUAL |
| Claim URL | url | — | MANUAL |
| Priority | select | High, Medium, Low, Speculative | MANUAL |
| Notes | rich_text | — | MANUAL |

---

## 10. Balance Snapshots

Historical daily snapshots, written once per day by a scheduled n8n run. This is how you get charts in Notion — you don't, Notion can't chart time series well, but you can embed a chart-as-image generated by n8n (see Dashboard section) or use a third-party charting block.

| Property | Type | Options / Formula | Source |
|---|---|---|---|
| Date | title | ISO format `YYYY-MM-DD` so it sorts lexically | N8N |
| Timestamp | date (with time) | — | N8N |
| Total USD | number (dollar) | Sum across all wallets + DeFi positions + NFTs | N8N |
| Spot USD | number (dollar) | Holdings only | N8N |
| DeFi USD | number (dollar) | DeFi Positions only | N8N |
| NFT USD | number (dollar) | NFT Holdings only | N8N |
| Stable USD | number (dollar) | USDC/USDT/DAI/etc. only | N8N |
| Ethereum USD | number (dollar) | By-chain breakdown | N8N |
| Solana USD | number (dollar) | — | N8N |
| Arbitrum USD | number (dollar) | — | N8N |
| Base USD | number (dollar) | — | N8N |
| Other Chains USD | number (dollar) | — | N8N |
| Realized P&L USD | number (dollar) | Sum of Transactions.Realized P&L USD to-date | N8N |
| Unrealized P&L USD | number (dollar) | Sum of Holdings.P&L USD at snapshot time | N8N |
| Day-over-day USD | number (dollar) | n8n computes by reading previous snapshot | N8N |
| Day-over-day % | number (percent) | — | N8N |
| Notes | rich_text | Auto-tag big moves ("BTC +7%", "claimed JTO") | N8N |

**Why not use formulas for day-over-day?** Because Notion can't cleanly query "the previous row in this DB." Much easier to compute in n8n and write the number.

---

## Dashboard Page Layout

The dashboard is the first page the user sees. It's structured top-to-bottom as a single scroll:

### Header Block (top of page)

A 4-column callout row — these are synced blocks pulling from the `Portfolio Totals` helper DB or from the latest `Balance Snapshots` row:

```
┌────────────────┬────────────────┬────────────────┬────────────────┐
│  TOTAL VALUE   │   24H CHANGE   │  UNREALIZED    │   REALIZED     │
│   $284,312     │  +$6,412 (+2.3%)│   +$47,208    │   +$12,844     │
└────────────────┴────────────────┴────────────────┴────────────────┘
```

Implementation: a single-row database or 4 synced blocks that n8n updates by writing rich_text into callout children. Simpler alternative: embed 4 linked views of `Balance Snapshots` filtered to "most recent" and display specific properties.

### "Needs Attention" Strip

Conditional callouts, only visible when relevant (via filtered linked DB views):

- `Holdings.Is Stale` where `Price.Last Updated > 30 min ago` → "Prices are stale, check n8n"
- `DeFi Positions.Liquidation Risk = CRITICAL` → red callout
- `Airdrops.Deadline Alert != ""` → orange callout with claim-soon list
- `Wallets.Scan Status = Error` → "Wallet scanner errors"

### Section 1 — Holdings

**View 1 (default):** Table view of Holdings, sorted by `Current Value USD` desc, filtered to `Amount > 0`. Columns: Symbol, Amount, Current Price, Current Value, 24h %, P&L %, Allocation %, Category.

**View 2:** Board view grouped by `Category` (L1 / L2 / Stablecoin / LST / LRT / etc.) — lets user see diversification at a glance.

**View 3:** Board view grouped by `Chain` — lets DeFi-natives see chain exposure.

**View 4:** Board view grouped by `Wallet` — lets user verify scanner results per wallet.

### Section 2 — DeFi Positions

**View 1:** Table sorted by `Current Value USD` desc, filtered to `Status = Active`. Columns: Protocol, Position Type, Assets, Current Value, APY %, Realized Yield %, Health Factor, Liquidation Risk.

**View 2:** Gallery grouped by `Protocol` with protocol logo emoji in the card. Great for "show me my Pendle positions."

**View 3:** Table filtered to `Position Type = Lending Borrow` showing only positions with a Health Factor — this is the liquidation-risk dashboard.

### Section 3 — Recent Transactions

Linked view of Transactions, sorted by Date desc, limited to last 30 days or top 50 rows. Columns: Date, Type, Coin, Amount, USD Value, Wallet, Counterparty. Default filter: `Type != "Gas"` (toggle off to hide noise).

Secondary view: filtered to `Type = "Claim Reward"` — shows the yield-harvest log.

### Section 4 — Wallets

Table view of Wallets sorted by `Total Value USD` desc. Quick status column showing `Scan Status` and `Last Scanned`.

### Section 5 — NFT Holdings

Gallery view keyed on `Image URL`, sorted by `Current Floor USD` desc. Filter: `Status = Holding`.

### Section 6 — Watchlist + Airdrops (2-column layout)

Left column: Watchlist table, filtered `Status != "Rejected"`, sorted by `Entry Signal` (puts 🟢 BUY rows first).

Right column: Airdrops table, filtered `Status in (Farming, Claimable)`, sorted by `Claim Deadline` asc.

### Section 7 — Charts

Notion has no native charting, so:

1. **Quick wins (no external service):** The 3rd-party Notion chart block from nochart.io / notion-charts.com embeds using a direct DB link to Balance Snapshots.
2. **Best option:** n8n generates a PNG line chart (with QuickChart.io or matplotlib) on every daily snapshot run and uploads it to S3/Cloudflare R2; writes the URL into a `Chart URL` field in the latest snapshot. Dashboard embeds the image block pointing at that URL.
3. **Alternative:** Use `linked_view` of Balance Snapshots as a table — not pretty but functional.

Suggested charts:
- Total portfolio value over time (line)
- By-chain breakdown (stacked area)
- Realized + Unrealized P&L (line)
- Spot vs DeFi vs NFT allocation (area)

---

## Settings Page

A single Notion page containing one `Settings` database with exactly one row (or use synced blocks / plain page properties). n8n reads this before every run.

### Settings DB schema

| Property | Type | Options / Default | Source |
|---|---|---|---|
| Name | title | "Portfolio Config" | MANUAL |
| n8n Webhook URL | url | — | MANUAL |
| n8n Webhook Secret | rich_text | Shared secret sent as header | MANUAL |
| Moralis API Key | rich_text | — | MANUAL |
| Helius API Key | rich_text | — | MANUAL |
| CoinGecko API Key | rich_text | Optional (pro tier) | MANUAL |
| Birdeye API Key | rich_text | Optional | MANUAL |
| Refresh Frequency | select | 5 min, 15 min, 30 min, 1 hour, 4 hours, Manual only | MANUAL |
| Snapshot Frequency | select | Daily, Weekly, Never | MANUAL |
| Chains Enabled | multi_select | Ethereum, Arbitrum, Base, Optimism, Solana, Polygon, BNB, Avalanche, Bitcoin, Sui, TON, Berachain | MANUAL |
| Dust Threshold USD | number | 1 (default) — positions below this get `Conviction = Dust` and hidden from default views | MANUAL |
| Auto-compute Cost Basis | checkbox | If true, n8n derives `Avg Cost USD` from Transactions using Tax Lot Method | MANUAL |
| Default Tax Lot Method | select | FIFO, LIFO, HIFO, Average | MANUAL |
| Hide Scam Tokens | checkbox | Filters airdropped spam from Holdings view | MANUAL |
| Scam Token Denylist | rich_text | Comma-separated contract addresses | MANUAL |
| Last Full Sync | date (with time) | Read-only, n8n writes | N8N |
| Last Snapshot | date (with time) | Read-only | N8N |
| Sync Status | select | OK, Running, Error, Never run | N8N |
| Sync Error Message | rich_text | — | N8N |

### Settings Page Body

Above the database, include:

1. **Quickstart callout** — 5-step setup: (1) deploy n8n workflow, (2) paste webhook URL, (3) paste API keys, (4) add wallets to Wallets DB with `Scan Enabled = true`, (5) click "Run Now" button.
2. **"Run Now" button** — a Notion button block that triggers a webhook to the n8n URL. Notion's webhook buttons are native as of late 2024.
3. **Troubleshooting section** — collapsible toggles for "Scanner errors", "Stale prices", "Missing tokens", "Cost basis wrong", each explaining likely causes and fixes.
4. **Privacy note** — remind user that API keys are stored in plaintext in Notion; recommend using API keys with read-only / restricted scopes where possible, and rotating them if the Notion workspace is ever shared.

---

## n8n Write Matrix (cheat sheet)

Quick reference for n8n workflow authors — every field the bot is allowed to write, grouped by database.

### Prices
`Symbol, Coin Name, Chain, Contract Address, USD Price, 24h Change %, 7d Change %, Market Cap, Last Updated, Price Source`

### Wallets
`Last Scanned, Scan Status, Scan Error Msg` — **never touch Label, Address, Chain, Type, Purpose, Scan Enabled, Notes**

### Holdings
`Position (title), Symbol, Chain, Amount, Current Price USD (only if not using rollup), Last Updated, Wallet (rel), Price (rel)` — **never touch Avg Cost USD unless Auto-compute Cost Basis = true; never touch Category, Conviction, Notes**

### Transactions
`Tx (title), Date, Type, Coin, Coin Out, Amount, Amount Out, Price USD, Fees USD, Wallet (rel), Counterparty, Tx Hash, Realized P&L USD (if Auto-compute), Ingested By` — **never touch Tax Lot Method, Notes**

### DeFi Positions
`Position (title), Protocol, Chain, Position Type, Assets, Price (rel), Wallet (rel), Current Value USD, Rewards Earned USD, APY %, Health Factor, Last Updated` — **never touch Entry Date, Initial Deposit USD, Rewards Claimed, Status, Position URL, Notes**

### NFT Holdings
`Title, Collection, Token ID, Chain, Contract Address, Wallet (rel), Current Floor USD, Last Sale USD, Rarity Rank, Image URL, Marketplace Link` — **never touch Cost Basis USD, Acquired Date, Status, List Price USD, Notes**

### Watchlist
n8n writes nothing directly — it only maintains the linked Prices row, which flows through via rollup.

### Research Notes
n8n writes nothing. Fully manual.

### Airdrops Tracker
n8n writes nothing (too heterogeneous). Fully manual. Optional future: a sub-workflow that checks known eligibility checker endpoints (e.g., LayerZero, zkSync) and updates `Status`.

### Balance Snapshots
All fields except `Notes` (which n8n may optionally auto-generate). This DB is create-only — never update past snapshots.

### Settings
`Last Full Sync, Last Snapshot, Sync Status, Sync Error Message`

---

## Relations Map

Visual summary of how the databases interconnect:

```
                    ┌─────────┐
                    │ Prices  │◄──────┐
                    └────┬────┘       │
                         │            │
         ┌───────────────┼────────────┼──────────────┐
         │               │            │              │
         ▼               ▼            │              ▼
   ┌──────────┐   ┌──────────────┐   │        ┌──────────┐
   │ Holdings │   │ DeFi Positions│   │        │Watchlist │
   └─────┬────┘   └───────┬──────┘   │        └────┬─────┘
         │                │          │             │
         │                │          │             ▼
         ▼                ▼          │       ┌──────────────┐
   ┌──────────┐                      │       │ Research     │
   │ Wallets  │◄─────────────────────┤       │ Notes        │
   └────┬─────┘                      │       └──────────────┘
        │                            │
        ├────────► Transactions ─────┘
        │
        ├────────► NFT Holdings
        │
        └────────► Airdrops (qualifying wallets)

   ┌──────────────────┐
   │ Balance Snapshots│  (no relations — standalone time series)
   └──────────────────┘

   ┌──────────┐
   │ Settings │  (no relations — config only)
   └──────────┘
```

**Dual relations** (both sides visible): Holdings↔Wallets, Holdings↔Prices, DeFi Positions↔Wallets, DeFi Positions↔Prices, NFT Holdings↔Wallets, Transactions↔Wallets, Watchlist↔Prices, Watchlist↔Research Notes, Airdrops↔Wallets.

---

## Example Data Sanity Check

To validate the schema, here's what a realistic DeFi-native user's portfolio should look like populated:

**Wallets (6 rows):**
- "Main ETH" — 0xabc... Ethereum, Hardware, [Main, Savings]
- "Arb Farm" — 0xdef... Arbitrum, Hot, [Farming, Airdrops]
- "Base Degen" — 0x123... Base, Hot, [Degen, Airdrops]
- "Main SOL" — Abc... Solana, Hardware, [Main]
- "SOL Farm" — Xyz... Solana, Hot, [Farming, Airdrops]
- "Berachain Testnet" — 0x... Berachain, Hot, [Airdrops, Testnet]

**Holdings (sample rows):**
- ETH — Main ETH, 12.4 ETH, Category=L1, Conviction=Core
- weETH — Main ETH, 8.2 weETH, Category=LRT, Conviction=Core
- USDC — Arb Farm, 18,400 USDC, Category=Stablecoin
- ARB — Arb Farm, 4,200 ARB, Category=L2, Conviction=Trade
- JITOSOL — Main SOL, 94 JITOSOL, Category=LST, Conviction=Core
- BONK — Main SOL, 12M BONK, Category=Meme, Conviction=Speculation

**DeFi Positions (sample rows):**
- Pendle PT-eETH Jun26 — Main ETH, 6 weETH, $18,400, APY 24%
- Aave V3 USDC Supply — Arb Farm, 10,000 USDC, APY 4.2%
- Aave V3 ETH Borrow — Arb Farm, 2.1 ETH, Health Factor 1.84
- Jito Restaking — Main SOL, 40 JITOSOL
- Uniswap V4 ETH/USDC 0.05% — Main ETH, $8,200

**Airdrops (sample rows):**
- Berachain — Farming, 2 wallets qualifying, est $2k, Claim Deadline TBD
- Monad — Farming, 3 wallets, est $3k
- Hyperliquid Season 2 — Farming, 1 wallet, est $5k

This spread exercises every major schema feature: multi-chain, LSTs, LRTs, lending, LP, restaking, memes, airdrop farming, cold+hot wallet split.

---

## Out of Scope for v1

Deliberately excluded to keep v1 shippable:

- Tax report generation (export to Koinly/CoinTracker instead)
- Perp PnL tracking (Hyperliquid, dYdX, Drift live positions)
- Options positions (Lyra, Aevo)
- Yield aggregator ROI attribution across auto-compounding vaults
- Cross-chain transfer deduplication (bridge txs appear twice; user tags manually)
- Multi-user/shared portfolios
- Mobile-optimized views (Notion mobile handles this reasonably well out of the box)

These are documented here so the user knows what not to expect and can be added in v2+.

---

**End of spec.** Next deliverables (not in this doc):
1. The Notion template itself (duplicate-able public page)
2. The n8n workflow JSON that implements the write matrix above
3. A setup guide linking the two together
