# Listing Copy — Crypto Portfolio Tracker (Notion Template)

Draft copy for Gumroad + Notion Template Gallery. Positioning: the missing P&L layer between DeBank/Zapper (net worth tracking) and Koinly/CoinTracker (tax software).

---

## Title (short, 50 char max)

**Crypto P&L Tracker for Notion — Cost Basis, DeFi, Airdrops & Research**

Alt: **The Crypto Tracker DeBank Won't Build**

---

## One-line hook

**DeBank shows what you own. This tracks what you actually made.**
A Notion template with real cost-basis P&L, tax-lot accounting, research notes, and airdrop tracking — across EVM, Solana, and Bitcoin — with an optional sync script that auto-updates from on-chain data.

---

## Long description

### The gap nobody talks about

You probably already use DeBank or Zapper. They're great at one thing: showing you a net worth number. You open the app, see `$47,382`, and close it.

But try answering any of these:

- *"How much have I actually made on my PENDLE position?"*
- *"What was my cost basis on that SOL I bought in three chunks over six months?"*
- *"Is my realized P&L positive this year, or am I just vibing on unrealized gains?"*
- *"What did I think when I bought this? Does the thesis still hold?"*
- *"Which of my 14 airdrop farms are actually close to claiming?"*

DeBank can't answer any of those. Neither can Zapper. That's not a flaw — it's by design. They're balance viewers. They show you a snapshot, not a story.

Koinly and CoinTracker *can* answer some of them, but they cost $99–279/year and are built for tax filing, not daily use. They're also read-only: no notes, no theses, no watchlist, no airdrop tracker, no custom fields.

**This template is the missing middle.** It's what I built for myself after 3 years of DeFi and getting tired of guessing whether I was up or down.

### What makes it different

| Feature | DeBank / Zapper | Koinly / CoinTracker | **This Template** |
|---|---|---|---|
| Current net worth | ✅ | ✅ | ✅ |
| Multi-chain balance view | ✅ | ✅ | ✅ |
| **Cost basis per position** | ❌ | ✅ | ✅ |
| **Realized P&L** | ❌ | ✅ | ✅ |
| **Tax lot method (FIFO/LIFO/HIFO)** | ❌ | ✅ | ✅ (toggle) |
| **Research notes linked to positions** | ❌ | ❌ | ✅ |
| **Watchlist with buy-signal formulas** | ❌ | ❌ | ✅ |
| **Airdrop farming tracker** | ❌ | ❌ | ✅ |
| **Customizable (rename / add fields)** | ❌ | ❌ | ✅ |
| **Works offline / in manual mode** | ❌ | ❌ | ✅ |
| **Mobile-friendly (Notion)** | ✅ | ⚠️ | ✅ |
| **Annual cost** | Free | $99–279/yr | **One-time purchase** |

### What's inside

**11 interconnected databases:**

- 📊 **Holdings** — live positions with **Cost Basis, Current Value, Unrealized P&L, P&L %** formulas
- 👛 **Wallets** — unlimited wallets, per-wallet scan toggle, last-scanned timestamp
- 💲 **Prices** — centralized live price feed, auto-updated by the sync script
- 📝 **Transactions** — tagged trade log with Buy / Sell / Swap / Stake / Airdrop / Bridge / LP types. This is what powers the cost basis math.
- 🌾 **DeFi Positions** — LPs, lending, Pendle PTs, restaking, with **realized yield** calculation
- 🖼️ **NFT Holdings** — floor price tracking and cost basis
- 👀 **Watchlist** — target entry prices with a formula that flips to 🟢 BUY when hit
- 🧠 **Research Notes** — deep-dives with Buy / Pass / Revisit decisions, linked directly to positions
- 🪂 **Airdrops Tracker** — farming status, tasks done, claim deadlines, realized value
- 📈 **Balance Snapshots** — daily portfolio history for charts and DoD P&L
- ⚙️ **Settings** — refresh frequency, dust threshold, chains enabled, tax lot method (FIFO/LIFO/HIFO)

**Plus a BI-style dashboard page** with KPI cards (portfolio total, unrealized P&L, DeFi earning, active wallets), 4 live charts (portfolio trend, chain allocation, P&L per position, category split), and inline links to every database.

### The automation bonus (included free)

This is what sets it apart from every other Notion crypto template.

A bundled Python sync daemon that:

- Polls your Wallets database every 60 seconds
- Scans each wallet across 6 EVM chains + Solana + Bitcoin
- Writes live Holdings with current prices and relations
- **Auto-archives stale Holdings when you remove a wallet** (cascade-delete the Notion API doesn't give you for free)
- Runs as a `systemd` service on any Linux / Mac / Raspberry Pi / free VPS

You can also:
- Run in **manual mode** — update Holdings yourself, the P&L formulas still work
- Import the included **n8n workflow JSON** into your own n8n instance
- Run the scanner one-off via command line

Everything uses **free API tiers** (Moralis + Helius). Zero recurring cost.

### Who this is for

- ✅ DeFi users who want to know their *actual* P&L, not just net worth
- ✅ Anyone frustrated that DeBank/Zapper don't track cost basis
- ✅ Airdrop farmers managing 5–20 farms across multiple chains
- ✅ Researchers who want investment thesis → watchlist → position → realized P&L in one place
- ✅ Notion-native users who already live in their vault
- ❌ Users who only need a net worth number (DeBank is free and does that fine)
- ❌ Users who need full tax filing with generated forms (buy Koinly)
- ❌ Complete crypto beginners (start with something simpler)

### What you get

1. **Notion template** (duplicate link) — 11 databases + BI dashboard, formulas, polished demo data
2. **Setup guide** (PDF) — 10-minute walkthrough from zero to syncing
3. **Automation bonus** (zip) — `sync_daemon.py`, `scanner.py`, `get_db_ids.py`, `cleanup_orphans.py`, n8n workflow, systemd service
4. **Lifetime updates** — new features and chain support as they're added
5. **Email support** — reply to your Gumroad receipt, answered within 24h

---

## Pricing options

Pick one:

| Price | Rationale |
|---|---|
| **$39** | Impulse buy tier. Broader audience, higher volume. |
| **$59** | **Recommended.** Matches top crypto-adjacent templates. Anchors against Koinly ($99+). |
| **$89** | Premium tier. Positions as the "serious DeFi user" product. Requires a strong hero screenshot. |

All three are one-time purchases. No subscription, no annual renewal. Compare to Koinly's $99/yr starter tier.

---

## Tags / keywords (for Gumroad + Notion gallery)

`notion template`, `crypto p&l tracker`, `cost basis tracker`, `crypto portfolio tracker`, `defi p&l`, `airdrop tracker`, `solana portfolio`, `ethereum portfolio`, `multi-chain portfolio`, `debank alternative`, `zapper alternative`, `koinly alternative`, `notion crypto`, `crypto research notes`, `watchlist notion`, `web3 portfolio`

---

## Suggested screenshots (for the listing)

1. **Hero**: BI dashboard with KPI cards showing $68,957 total / +$9,158 unrealized / $12,993 DeFi earning, all 4 charts visible
2. **The comparison table above**, rendered cleanly as an image — it's the sharpest pitch in the whole listing
3. **Holdings DB** sorted by P&L %, showing cost basis + realized + unrealized columns
4. **DeFi Positions** with Pendle + Jito + Ethena rows and realized yield formulas
5. **Watchlist** with 🟢 BUY signals on two rows and ⚪ wait on the rest
6. **Airdrops Tracker** board view grouped by farming status
7. **Research Notes**: one expanded page showing a real thesis with linked Holdings row
8. **Setup flow**: side-by-side of Moralis dashboard + sync daemon running in a terminal
9. **Mobile**: Notion mobile showing Holdings and the KPI cards

---

## FAQ answers (to preempt support questions)

**Q: Isn't this just DeBank inside Notion?**
A: No — and that's the whole point. DeBank shows you current balances and a net worth number. It does not track cost basis, realized P&L, tax lots, research notes, watchlists, or airdrop farms. This template does. Use DeBank as your live monitor; use this to actually know whether you're in profit.

**Q: Why not just use Koinly or CoinTracker?**
A: Use them if you need to file taxes — they're purpose-built for that and generate the forms. This template is for daily use: notes, theses, watchlist, airdrops, and day-to-day P&L tracking. It's one-time $59 instead of $99–279/year, and everything lives in your Notion alongside the rest of your second brain. You can export Transactions as CSV and hand it to Koinly at tax time.

**Q: Does this track X chain?**
A: EVM chains: Ethereum, Arbitrum, Base, Optimism, Polygon, BNB. Solana via Helius. Bitcoin via mempool.space. More can be added — the scanner is open source and modular.

**Q: Do I need to pay for API access?**
A: No. Moralis, Helius, and mempool.space all have free tiers that handle personal portfolios (up to ~15 wallets scanning every 15 min) indefinitely.

**Q: Do I need to share my private key?**
A: Never. The sync script reads **public** on-chain balances using your wallet *address* only. Private keys are never requested or touched.

**Q: Can I use this without running the sync script?**
A: Yes. Manual mode works perfectly — update Holdings amounts yourself and all P&L formulas still calculate. The automation is a bonus for users who want it.

**Q: Does it reconstruct my full trade history automatically?**
A: No — the scanner reads *current balances* but does not rebuild historical transactions. For accurate cost basis, you log trades in the Transactions database (or paste CSV from your exchange). Automated transaction ingestion is on the v2 roadmap.

**Q: How does cost basis actually work?**
A: Every Buy / Sell / Swap row in Transactions contributes to the cost basis of the associated Holding via linked rollups. You pick your tax lot method in Settings (FIFO, LIFO, or HIFO). The Holdings table shows Cost Basis, Current Value, Unrealized P&L, and P&L % automatically.

**Q: Does this work with Notion mobile?**
A: Yes — dashboard and all databases render cleanly on mobile.

**Q: Can I customize it?**
A: It's a Notion template, you own your copy. Add/remove fields, rename databases, change views, change colors. The formulas are visible and editable.

**Q: What if I use a hardware wallet / multisig / smart wallet?**
A: Works fine. The scanner reads balances by address, regardless of wallet type.

**Q: Lifetime updates?**
A: Yes. New chains, new DeFi protocols, new dashboard features — you get all future versions at no extra cost. Re-download from your Gumroad library anytime.
