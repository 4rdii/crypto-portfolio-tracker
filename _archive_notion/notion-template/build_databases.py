#!/usr/bin/env python3
"""
Build all 10 crypto-portfolio Notion databases under a given parent page.

Usage:
    python3 build_databases.py <parent_page_id_or_url>

The parent page must already be shared with the "Crypto Asset Tracker" integration
(Notion → ... → Connections → Connect to → Crypto Asset Tracker).

Output: writes /root/crypto-tracker-project/notion-template/db_ids.json with the
created database IDs so the n8n workflow and downstream scripts can reference them.

Design notes:
- Databases are created in dependency order: Prices first (no relations), then
  Wallets, then Holdings / Transactions / DeFi / NFT / Watchlist / Research /
  Airdrops which all relate to Prices and/or Wallets, then Balance Snapshots
  (standalone).
- Relations are added in a second pass after all DBs exist, so both sides can
  reference each other.
- Formulas are added at creation time (Notion API accepts formula on create).
- Rollups are added in a third pass because they require the relation to exist.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DB_IDS_PATH = Path(__file__).resolve().parent / "db_ids.json"

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
TOKEN = ENV.get("NOTION_TOKEN") or os.getenv("NOTION_TOKEN")
if not TOKEN:
    print("NOTION_TOKEN missing", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def extract_page_id(raw: str) -> str:
    """Accept either a raw 32-char ID or a Notion URL."""
    raw = raw.strip()
    # Raw id (with or without dashes)
    m = re.search(r"([0-9a-f]{32})", raw.replace("-", ""))
    if m:
        x = m.group(1)
        return f"{x[0:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:32]}"
    raise ValueError(f"Could not extract a Notion page ID from: {raw}")


def create_database(parent_id: str, title: str, properties: dict, icon: str = None) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": properties,
    }
    if icon:
        body["icon"] = {"type": "emoji", "emoji": icon}
    r = requests.post(f"{NOTION_API}/databases", headers=HEADERS, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"[ERR] creating {title}:", r.text, file=sys.stderr)
        sys.exit(1)
    db_id = r.json()["id"]
    print(f"  ✓ {title:30s} {db_id}")
    return db_id


def patch_database(db_id: str, properties: dict) -> None:
    body = {"properties": properties}
    r = requests.patch(f"{NOTION_API}/databases/{db_id}", headers=HEADERS, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"[ERR] patching {db_id}:", r.text, file=sys.stderr)
        sys.exit(1)


# ---- chain / category option lists (reused across DBs) -----------------------
CHAIN_OPTIONS = [
    {"name": "Ethereum", "color": "blue"},
    {"name": "Arbitrum", "color": "blue"},
    {"name": "Base", "color": "blue"},
    {"name": "Optimism", "color": "red"},
    {"name": "Polygon", "color": "purple"},
    {"name": "BNB", "color": "yellow"},
    {"name": "Avalanche", "color": "red"},
    {"name": "Solana", "color": "purple"},
    {"name": "Bitcoin", "color": "orange"},
    {"name": "Sui", "color": "blue"},
    {"name": "TON", "color": "blue"},
    {"name": "Berachain", "color": "brown"},
    {"name": "Other", "color": "gray"},
]

CATEGORY_OPTIONS = [
    {"name": "L1", "color": "blue"},
    {"name": "L2", "color": "blue"},
    {"name": "Stablecoin", "color": "green"},
    {"name": "DeFi Blue Chip", "color": "purple"},
    {"name": "LST", "color": "pink"},
    {"name": "LRT", "color": "pink"},
    {"name": "Meme", "color": "yellow"},
    {"name": "Gaming", "color": "orange"},
    {"name": "AI", "color": "red"},
    {"name": "RWA", "color": "brown"},
    {"name": "Privacy", "color": "gray"},
    {"name": "Other", "color": "default"},
]


# ---- database schemas --------------------------------------------------------
def schema_prices() -> dict:
    return {
        "Symbol":      {"title": {}},
        "Coin Name":   {"rich_text": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Contract Address": {"rich_text": {}},
        "CoinGecko ID":{"rich_text": {}},
        "USD Price":   {"number": {"format": "dollar"}},
        "24h Change %":{"number": {"format": "percent"}},
        "7d Change %": {"number": {"format": "percent"}},
        "Market Cap":  {"number": {"format": "dollar"}},
        "Last Updated":{"date": {}},
        "Price Source":{"select": {"options": [
            {"name": "Moralis",     "color": "blue"},
            {"name": "Helius",      "color": "purple"},
            {"name": "CoinGecko",   "color": "green"},
            {"name": "mempool+cg",  "color": "orange"},
            {"name": "Manual",      "color": "gray"},
        ]}},
    }


def schema_wallets() -> dict:
    return {
        "Label":       {"title": {}},
        "Address":     {"rich_text": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Type":        {"select": {"options": [
            {"name": "Hot", "color": "red"},
            {"name": "Cold", "color": "blue"},
            {"name": "Hardware", "color": "green"},
            {"name": "Exchange", "color": "yellow"},
            {"name": "Multisig", "color": "purple"},
            {"name": "Smart Wallet", "color": "pink"},
        ]}},
        "Purpose":     {"multi_select": {"options": [
            {"name": "Main"}, {"name": "Trading"}, {"name": "Farming"},
            {"name": "Airdrops"}, {"name": "NFTs"}, {"name": "Savings"},
            {"name": "Degen"}, {"name": "Testnet"}, {"name": "Burner"},
        ]}},
        "Scan Enabled":{"checkbox": {}},
        "Created Date":{"date": {}},
        "Last Scanned":{"date": {}},
        "Scan Status": {"select": {"options": [
            {"name": "OK", "color": "green"},
            {"name": "Error", "color": "red"},
            {"name": "Rate-limited", "color": "yellow"},
            {"name": "Not scanned yet", "color": "gray"},
        ]}},
        "Scan Error Msg": {"rich_text": {}},
        "Notes":       {"rich_text": {}},
    }


def schema_holdings() -> dict:
    return {
        "Position":    {"title": {}},
        "Symbol":      {"rich_text": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Contract":    {"rich_text": {}},
        "Amount":      {"number": {"format": "number"}},
        "Avg Cost USD":{"number": {"format": "dollar"}},
        "Current Price USD": {"number": {"format": "dollar"}},
        "Current Value USD": {"number": {"format": "dollar"}},
        "P&L USD":     {"formula": {"expression": 'prop("Current Value USD") - (prop("Amount") * prop("Avg Cost USD"))'}},
        "P&L %":       {"formula": {"expression": 'if((prop("Amount") * prop("Avg Cost USD")) == 0, 0, (prop("Current Value USD") - (prop("Amount") * prop("Avg Cost USD"))) / (prop("Amount") * prop("Avg Cost USD")))'}},
        "24h Change %":{"number": {"format": "percent"}},
        "Category":    {"select": {"options": CATEGORY_OPTIONS}},
        "Conviction":  {"select": {"options": [
            {"name": "Core", "color": "blue"},
            {"name": "Trade", "color": "yellow"},
            {"name": "Farming", "color": "green"},
            {"name": "Speculation", "color": "red"},
            {"name": "Dust", "color": "gray"},
        ]}},
        "Last Updated":{"date": {}},
        "Notes":       {"rich_text": {}},
    }


def schema_transactions() -> dict:
    return {
        "Tx":          {"title": {}},
        "Date":        {"date": {}},
        "Type":        {"select": {"options": [
            {"name": "Buy", "color": "green"},
            {"name": "Sell", "color": "red"},
            {"name": "Swap", "color": "blue"},
            {"name": "Transfer In", "color": "green"},
            {"name": "Transfer Out", "color": "red"},
            {"name": "Airdrop", "color": "purple"},
            {"name": "Stake", "color": "yellow"},
            {"name": "Unstake", "color": "yellow"},
            {"name": "Claim Reward", "color": "pink"},
            {"name": "LP Add", "color": "orange"},
            {"name": "LP Remove", "color": "orange"},
            {"name": "Borrow", "color": "brown"},
            {"name": "Repay", "color": "brown"},
            {"name": "Bridge", "color": "blue"},
            {"name": "Gas", "color": "gray"},
            {"name": "NFT Buy", "color": "purple"},
            {"name": "NFT Sell", "color": "purple"},
            {"name": "Other", "color": "default"},
        ]}},
        "Coin":        {"rich_text": {}},
        "Coin Out":    {"rich_text": {}},
        "Amount":      {"number": {"format": "number"}},
        "Amount Out":  {"number": {"format": "number"}},
        "Price USD":   {"number": {"format": "dollar"}},
        "USD Value":   {"formula": {"expression": 'prop("Amount") * prop("Price USD")'}},
        "Fees USD":    {"number": {"format": "dollar"}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Counterparty":{"rich_text": {}},
        "Tx Hash":     {"rich_text": {}},
        "Realized P&L USD": {"number": {"format": "dollar"}},
        "Tax Lot Method": {"select": {"options": [
            {"name": "FIFO"}, {"name": "LIFO"}, {"name": "HIFO"}, {"name": "Average"},
        ]}},
        "Ingested By": {"select": {"options": [
            {"name": "Manual"}, {"name": "Moralis"}, {"name": "Helius"},
            {"name": "Mempool"}, {"name": "CSV Import"},
        ]}},
        "Notes":       {"rich_text": {}},
    }


def schema_defi() -> dict:
    return {
        "Position":    {"title": {}},
        "Protocol":    {"select": {"options": [
            {"name": n} for n in [
                "Aave", "Compound", "Morpho", "Uniswap V3", "Uniswap V4",
                "Curve", "Pendle", "EigenLayer", "Symbiotic", "Jito",
                "Marinade", "Kamino", "Drift", "Ethena", "Lido",
                "Rocket Pool", "Gearbox", "Fluid", "Other",
            ]
        ]}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Position Type": {"select": {"options": [
            {"name": n} for n in [
                "LP", "Lending Supply", "Lending Borrow", "Farming",
                "Staking", "Restaking", "Vault", "PT (fixed yield)",
                "YT (yield token)", "Perp", "Option",
            ]
        ]}},
        "Assets":      {"multi_select": {"options": [
            {"name": n} for n in [
                "ETH", "USDC", "USDT", "DAI", "SOL", "BTC",
                "EIGEN", "ENA", "ezETH", "weETH", "rsETH", "pufETH",
                "wstETH", "jitoSOL", "PENDLE", "AERO", "CRV",
            ]
        ]}},
        "Entry Date":  {"date": {}},
        "Initial Deposit USD": {"number": {"format": "dollar"}},
        "Current Value USD":   {"number": {"format": "dollar"}},
        "Rewards Earned USD":  {"number": {"format": "dollar"}},
        "Rewards Claimed":     {"checkbox": {}},
        "APY %":       {"number": {"format": "percent"}},
        "Realized Yield USD": {"formula": {"expression": 'prop("Current Value USD") + prop("Rewards Earned USD") - prop("Initial Deposit USD")'}},
        "Realized Yield %":   {"formula": {"expression": 'if(prop("Initial Deposit USD") == 0, 0, (prop("Current Value USD") + prop("Rewards Earned USD") - prop("Initial Deposit USD")) / prop("Initial Deposit USD"))'}},
        "Health Factor": {"number": {"format": "number"}},
        "Status":      {"select": {"options": [
            {"name": "Active", "color": "green"},
            {"name": "Closed", "color": "gray"},
            {"name": "Partially Closed", "color": "yellow"},
            {"name": "Underwater", "color": "red"},
            {"name": "Matured", "color": "blue"},
        ]}},
        "Position URL":{"url": {}},
        "Last Updated":{"date": {}},
        "Notes":       {"rich_text": {}},
    }


def schema_nft() -> dict:
    return {
        "Title":       {"title": {}},
        "Collection":  {"select": {"options": []}},
        "Token ID":    {"rich_text": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Contract Address": {"rich_text": {}},
        "Acquired Date": {"date": {}},
        "Cost Basis USD": {"number": {"format": "dollar"}},
        "Current Floor USD": {"number": {"format": "dollar"}},
        "Last Sale USD": {"number": {"format": "dollar"}},
        "Unrealized P&L USD": {"formula": {"expression": 'prop("Current Floor USD") - prop("Cost Basis USD")'}},
        "Unrealized P&L %":   {"formula": {"expression": 'if(prop("Cost Basis USD") == 0, 0, (prop("Current Floor USD") - prop("Cost Basis USD")) / prop("Cost Basis USD"))'}},
        "Rarity Rank": {"number": {"format": "number"}},
        "Image URL":   {"url": {}},
        "Marketplace Link": {"url": {}},
        "Status":      {"select": {"options": [
            {"name": "Holding", "color": "green"},
            {"name": "Listed", "color": "yellow"},
            {"name": "Sold", "color": "gray"},
            {"name": "Staked", "color": "blue"},
            {"name": "Locked", "color": "purple"},
        ]}},
        "List Price USD": {"number": {"format": "dollar"}},
        "Notes":       {"rich_text": {}},
    }


def schema_watchlist() -> dict:
    return {
        "Coin":        {"title": {}},
        "Symbol":      {"rich_text": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS}},
        "Thesis":      {"rich_text": {}},
        "Target Entry USD": {"number": {"format": "dollar"}},
        "Current Price USD": {"number": {"format": "dollar"}},
        "24h Change %":{"number": {"format": "percent"}},
        "Distance to Entry %": {"formula": {"expression": 'if(empty(prop("Target Entry USD")) or prop("Target Entry USD") == 0, 0, (prop("Current Price USD") - prop("Target Entry USD")) / prop("Target Entry USD"))'}},
        "Entry Signal": {"formula": {"expression": 'if(empty(prop("Target Entry USD")), "—", if(prop("Current Price USD") <= prop("Target Entry USD"), "🟢 BUY", "⚪ wait"))'}},
        "Status":      {"select": {"options": [
            {"name": "Researching", "color": "yellow"},
            {"name": "Waiting for Entry", "color": "blue"},
            {"name": "Bought", "color": "green"},
            {"name": "Rejected", "color": "red"},
            {"name": "Expired", "color": "gray"},
        ]}},
        "Added Date":  {"date": {}},
        "Priority":    {"select": {"options": [
            {"name": "High", "color": "red"},
            {"name": "Medium", "color": "yellow"},
            {"name": "Low", "color": "gray"},
        ]}},
        "Notes":       {"rich_text": {}},
    }


def schema_research() -> dict:
    return {
        "Coin":        {"title": {}},
        "Symbol":      {"rich_text": {}},
        "Category":    {"multi_select": {"options": [
            {"name": n} for n in [
                "L1", "L2", "DeFi", "LST", "LRT", "Stablecoin",
                "RWA", "DePIN", "AI", "Gaming", "Meme", "Infra", "Privacy",
            ]
        ]}},
        "Decision":    {"select": {"options": [
            {"name": "Buy", "color": "green"},
            {"name": "Watchlist", "color": "yellow"},
            {"name": "Pass", "color": "red"},
            {"name": "Sold", "color": "gray"},
            {"name": "Revisit", "color": "blue"},
        ]}},
        "Conviction (1-5)": {"select": {"options": [
            {"name": "1"}, {"name": "2"}, {"name": "3"}, {"name": "4"}, {"name": "5"},
        ]}},
        "Target Price USD": {"number": {"format": "dollar"}},
        "Date Researched": {"date": {}},
        "Source Links": {"url": {}},
    }


def schema_airdrops() -> dict:
    return {
        "Protocol":    {"title": {}},
        "Chain":       {"select": {"options": CHAIN_OPTIONS + [{"name": "Multi-chain", "color": "default"}]}},
        "Status":      {"select": {"options": [
            {"name": "Researching", "color": "yellow"},
            {"name": "Farming", "color": "blue"},
            {"name": "Snapshot Taken", "color": "purple"},
            {"name": "Claimable", "color": "green"},
            {"name": "Claimed", "color": "gray"},
            {"name": "Expired", "color": "red"},
            {"name": "Dud", "color": "red"},
        ]}},
        "Eligibility Criteria": {"rich_text": {}},
        "Tasks Done":  {"multi_select": {"options": [
            {"name": n} for n in [
                "Bridged", "Swapped", "Provided LP", "Staked", "Voted",
                "Minted NFT", "Deployed Contract", "Referred",
                "Used N times", "Volume threshold",
            ]
        ]}},
        "Estimated Value USD": {"number": {"format": "dollar"}},
        "Actual Received USD": {"number": {"format": "dollar"}},
        "Snapshot Date": {"date": {}},
        "Claim Start": {"date": {}},
        "Claim Deadline": {"date": {}},
        "Claim Tx Hash": {"rich_text": {}},
        "Claim URL":   {"url": {}},
        "Priority":    {"select": {"options": [
            {"name": "High", "color": "red"},
            {"name": "Medium", "color": "yellow"},
            {"name": "Low", "color": "gray"},
            {"name": "Speculative", "color": "purple"},
        ]}},
        "Notes":       {"rich_text": {}},
    }


def schema_snapshots() -> dict:
    return {
        "Date":        {"title": {}},
        "Timestamp":   {"date": {}},
        "Total USD":   {"number": {"format": "dollar"}},
        "Spot USD":    {"number": {"format": "dollar"}},
        "DeFi USD":    {"number": {"format": "dollar"}},
        "NFT USD":     {"number": {"format": "dollar"}},
        "Stable USD":  {"number": {"format": "dollar"}},
        "Ethereum USD":{"number": {"format": "dollar"}},
        "Solana USD":  {"number": {"format": "dollar"}},
        "Arbitrum USD":{"number": {"format": "dollar"}},
        "Base USD":    {"number": {"format": "dollar"}},
        "Other Chains USD": {"number": {"format": "dollar"}},
        "Realized P&L USD": {"number": {"format": "dollar"}},
        "Unrealized P&L USD": {"number": {"format": "dollar"}},
        "Day-over-day USD": {"number": {"format": "dollar"}},
        "Day-over-day %":   {"number": {"format": "percent"}},
        "Notes":       {"rich_text": {}},
    }


def schema_settings() -> dict:
    return {
        "Name":        {"title": {}},
        "n8n Webhook URL": {"url": {}},
        "n8n Webhook Secret": {"rich_text": {}},
        "Moralis API Key":    {"rich_text": {}},
        "Helius API Key":     {"rich_text": {}},
        "CoinGecko API Key":  {"rich_text": {}},
        "Refresh Frequency":  {"select": {"options": [
            {"name": "5 min"}, {"name": "15 min"}, {"name": "30 min"},
            {"name": "1 hour"}, {"name": "4 hours"}, {"name": "Manual only"},
        ]}},
        "Snapshot Frequency": {"select": {"options": [
            {"name": "Daily"}, {"name": "Weekly"}, {"name": "Never"},
        ]}},
        "Chains Enabled":     {"multi_select": {"options": CHAIN_OPTIONS}},
        "Dust Threshold USD": {"number": {"format": "dollar"}},
        "Auto-compute Cost Basis": {"checkbox": {}},
        "Default Tax Lot Method": {"select": {"options": [
            {"name": "FIFO"}, {"name": "LIFO"}, {"name": "HIFO"}, {"name": "Average"},
        ]}},
        "Hide Scam Tokens":   {"checkbox": {}},
        "Scam Token Denylist":{"rich_text": {}},
        "Last Full Sync":     {"date": {}},
        "Last Snapshot":      {"date": {}},
        "Sync Status":        {"select": {"options": [
            {"name": "OK", "color": "green"},
            {"name": "Running", "color": "yellow"},
            {"name": "Error", "color": "red"},
            {"name": "Never run", "color": "gray"},
        ]}},
        "Sync Error Message": {"rich_text": {}},
    }


DATABASES = [
    ("Prices",          "💲", schema_prices),
    ("Wallets",         "👛", schema_wallets),
    ("Holdings",        "📊", schema_holdings),
    ("Transactions",    "📝", schema_transactions),
    ("DeFi Positions",  "🌾", schema_defi),
    ("NFT Holdings",    "🖼️", schema_nft),
    ("Watchlist",       "👀", schema_watchlist),
    ("Research Notes",  "🧠", schema_research),
    ("Airdrops Tracker","🪂", schema_airdrops),
    ("Balance Snapshots","📈", schema_snapshots),
    ("Settings",        "⚙️", schema_settings),
]


def add_relations(db_ids: dict) -> None:
    """Second pass: add relation properties now that all DBs exist."""
    print("\nAdding relations (pass 2)...")

    relation_specs = [
        # (source_db, source_prop, target_db)
        ("Holdings",       "Wallet",   "Wallets"),
        ("Holdings",       "Price",    "Prices"),
        ("Transactions",   "Wallet",   "Wallets"),
        ("DeFi Positions", "Wallet",   "Wallets"),
        ("DeFi Positions", "Price",    "Prices"),
        ("NFT Holdings",   "Wallet",   "Wallets"),
        ("Watchlist",      "Price",    "Prices"),
        ("Watchlist",      "Research", "Research Notes"),
        ("Airdrops Tracker","Qualifying Wallets", "Wallets"),
    ]

    for src, prop, tgt in relation_specs:
        src_id = db_ids[src]
        tgt_id = db_ids[tgt]
        body = {
            prop: {
                "relation": {
                    "database_id": tgt_id,
                    "type": "dual_property",
                    "dual_property": {},
                }
            }
        }
        r = requests.patch(
            f"{NOTION_API}/databases/{src_id}",
            headers=HEADERS,
            json={"properties": body},
            timeout=30,
        )
        if r.status_code >= 300:
            print(f"  [WARN] relation {src}.{prop} → {tgt}: {r.text[:200]}")
        else:
            print(f"  ✓ {src}.{prop} → {tgt}")
        time.sleep(0.2)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("parent", help="Notion page URL or ID (must be shared with the integration)")
    args = p.parse_args()

    parent_id = extract_page_id(args.parent)
    print(f"Parent page ID: {parent_id}\n")

    print("Creating databases (pass 1)...")
    db_ids = {}
    for name, icon, schema_fn in DATABASES:
        db_id = create_database(parent_id, name, schema_fn(), icon=icon)
        db_ids[name] = db_id
        time.sleep(0.2)

    add_relations(db_ids)

    DB_IDS_PATH.write_text(json.dumps(db_ids, indent=2))
    print(f"\nSaved DB IDs to {DB_IDS_PATH}")
    print("\nDone. Next: run the snapshot/seed script to pre-populate sample rows.")


if __name__ == "__main__":
    main()
