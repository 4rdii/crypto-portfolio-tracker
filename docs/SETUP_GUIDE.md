# 🚀 Crypto Portfolio Tracker — Setup Guide

Welcome! This guide walks you through everything you need to get your portfolio syncing automatically. Total time: **~10 minutes** for the Notion side, **~5 minutes** for the sync script.

> You only need to do this **once**. After setup, new wallets you add to Notion will sync automatically.

---

## What you'll set up

1. ✅ Duplicate the Notion template into your workspace
2. 🔑 Get three free API keys (Moralis, Helius, Notion integration)
3. 📦 Run the sync script on any computer or VPS

---

## Step 1 — Duplicate the template

1. Open the Notion template link from your Gumroad receipt
2. Click **Duplicate** in the top-right corner
3. Choose your workspace — the template (11 databases + dashboard) will be copied in
4. Rename the top page to anything you like (e.g., "My Portfolio")

---

## Step 2 — Get your free API keys

You need three keys. All are free. Skip Helius if you don't hold any Solana tokens; skip Moralis if you only hold Bitcoin + Solana.

### 2a. Moralis (EVM chains — Ethereum, Arbitrum, Base, etc.)

1. Go to [deep-index.moralis.io](https://deep-index.moralis.io/) and sign up (free)
2. Create a new project
3. Navigate to **Settings → API Keys**
4. Copy your **Web3 API Key**
5. Free tier: 40,000 compute units/day — enough for a personal portfolio of ~5 wallets scanned every 15 minutes

### 2b. Helius (Solana)

1. Go to [helius.dev](https://helius.dev/) and sign up (free)
2. Create a new project
3. Copy your **API Key** from the dashboard
4. Free tier: 1,000,000 credits/month — plenty for personal use

### 2c. Notion Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **+ New integration**
3. Name it anything (e.g., "Portfolio Sync")
4. Associated workspace: pick the one with your duplicated template
5. Capabilities: check **Read content**, **Update content**, **Insert content**
6. Click **Submit**
7. Copy the **Internal Integration Secret** (starts with `ntn_` or `secret_`)

### 2d. Connect the integration to your template

1. Open your duplicated Notion template (the top-level page)
2. Click the **…** menu in the top-right
3. Scroll down to **Connections** → **Connect to** → pick your new integration
4. A popup confirms — hit **Confirm**
5. This grants the integration access to the top page and all its subpages/databases

---

## Step 3 — Run the sync script

You have three options. Pick whichever fits your comfort level.

### Option A — Run locally (easiest, free, no VPS)

**Requirements:** a computer that's on when you want portfolio updates (desktop, laptop, Mac Mini, old Raspberry Pi).

1. Install Python 3.10+ if you don't already have it
2. Unzip the `automation-bonus.zip` from your Gumroad receipt — you'll see `scanner.py`, `sync_daemon.py`, and `db_ids.json`
3. Create a file named `.env` in the same folder, with these three lines:
   ```
   MORALIS_API_KEY=your_moralis_key_here
   HELIUS_API_KEY=your_helius_key_here
   NOTION_TOKEN=your_notion_integration_secret_here
   ```
4. You also need `db_ids.json` — **this is unique to your workspace**. Run `build_databases.py` once to generate it:
   ```bash
   pip install requests
   python3 build_databases.py https://www.notion.so/your-template-page-url
   ```
   Wait — actually you don't need this if you're using the duplicated template. Skip to 4b.

4b. Run `get_db_ids.py` (included) — it walks your workspace and writes the correct `db_ids.json`:
   ```bash
   python3 get_db_ids.py https://www.notion.so/your-template-page-url
   ```

5. Now start the sync daemon:
   ```bash
   python3 sync_daemon.py
   ```
   You should see `sync daemon starting — poll=60s stale=15m` in the terminal.

6. Go add a wallet in the Notion **Wallets** database: fill in Label, Address, Chain, and tick **Scan Enabled**. Within 60 seconds, the **Holdings** database will populate.

### Option B — Run on a free VPS (always-on)

**Best for:** 24/7 sync without leaving a laptop running.

Free options: [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) (AArch64, 4GB RAM, free forever), [Fly.io](https://fly.io/) (3 small VMs free), [Google Cloud Free Tier](https://cloud.google.com/free) (e2-micro, free forever).

1. Spin up a small VM (any Linux distro)
2. SSH in, install Python 3 + git
3. Copy the automation-bonus files to the VM
4. Follow Option A steps 3–5
5. To run in the background:
   ```bash
   # quick and dirty:
   nohup python3 sync_daemon.py > sync.log 2>&1 &

   # or as a systemd service (recommended):
   sudo cp crypto-sync.service /etc/systemd/system/
   sudo systemctl enable --now crypto-sync
   sudo systemctl status crypto-sync
   ```
   The `crypto-sync.service` file is included in the automation bonus.

### Option C — Run as an n8n workflow

**Best for:** users already running n8n for other automations.

1. Import `wallet-scanner.json` (included in the automation bonus) into your n8n instance
2. Open the workflow → click each HTTP node → paste your Moralis / Helius / Notion credentials
3. Activate the workflow
4. Same behavior as the Python daemon: polls your Wallets DB every 5 minutes

---

## Step 4 — Verify it's working

1. Open your Notion **Wallets** database
2. Add a row with any wallet address you want to test. Example test wallets:
   - EVM: `0xcB1C1FdE09f811B294172696404e88E658659905`
   - Solana: `5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1`
   - Bitcoin: `bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97`
3. Tick **Scan Enabled**
4. Watch the **Holdings** database — fresh rows should appear within ~60 seconds
5. The wallet's **Last Scanned** and **Scan Status** fields update automatically

---

## Common issues

| Problem | Fix |
|---|---|
| "NOTION_TOKEN missing" | You forgot the `.env` file or have a typo. Check the file is in the same folder as `sync_daemon.py`. |
| "Could not find object" from Notion | You didn't share the page with your integration. Go back to Step 2d. |
| "Request is unauthorized" from Moralis | API key is wrong or your account is over the free tier. Double-check the key. |
| Holdings DB is empty | Scan Enabled is unticked, OR the address is wrong, OR the script isn't running. Check the sync log. |
| Sync is slow | Free tier Moralis limits scans to ~6 EVM chains. A 5-wallet portfolio takes ~15–30 seconds per full cycle. |
| I removed a wallet but Holdings still show it | The daemon archives orphans on the next cycle (within 60s). If not, run `python3 cleanup_orphans.py --apply`. |

---

## How the formulas work

All P&L math is done by Notion formula fields — you never need to calculate manually.

- **Holdings → P&L USD** = `Current Value USD − (Amount × Avg Cost USD)`
- **Holdings → P&L %** = P&L USD ÷ cost basis
- **DeFi Positions → Realized Yield USD** = `Current Value + Rewards Earned − Initial Deposit`
- **Watchlist → Entry Signal** = 🟢 BUY when Current Price ≤ Target Entry, otherwise ⚪ wait
- **Watchlist → Distance to Entry %** = percentage gap between current and target

To get accurate P&L, make sure you fill in **Avg Cost USD** on each Holdings row. The sync script doesn't know your cost basis — it just writes the current amount and price. You set the cost basis either:
- Manually when you buy (quickest)
- Automatically by logging every Buy/Sell in the Transactions database with the `Tax Lot Method` of your choice

---

## Privacy & security

- **Wallet addresses are read-only.** The sync script only queries public blockchain balance APIs — it can never move or sign anything.
- **API keys stay on your machine** — nothing goes to our server. We have zero visibility into your holdings.
- **Notion integration is scoped to the template.** Unshare the page at any time to revoke access.
- **The sync script is open source** — you can read every line in `sync_daemon.py` before running it.

---

## Getting help

- Read this guide again carefully — 90% of issues are typos in the `.env` file or a missed Notion integration share
- Check the sync log (`sync_daemon.log` in the script folder)
- Reply to your Gumroad receipt email — you'll get an answer within 24 hours
- Feature requests and bug reports welcome

Happy tracking! 📊
