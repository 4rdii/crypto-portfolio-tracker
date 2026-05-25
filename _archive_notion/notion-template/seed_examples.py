#!/usr/bin/env python3
"""
Seed example rows into the crypto-portfolio Notion databases so a fresh
duplicate of the template doesn't look empty on first open.

Reads DB IDs from db_ids.json and writes a small curated dataset:
- Prices: BTC, ETH, SOL, USDC, PENDLE, JITOSOL (6 rows)
- Wallets: 3 example wallets (EVM hot, EVM cold, Solana farming)
- Holdings: 5 rows linked to wallets+prices
- Transactions: 4 rows (Buy ETH, Swap USDC→SOL, Stake SOL, Airdrop)
- DeFi Positions: 2 rows (Pendle PT, Jito staking)
- Watchlist: 3 rows
- Airdrops Tracker: 2 rows
- Settings: 1 row with sane defaults

Usage:
    python3 seed_examples.py
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
ENV_PATH = PROJECT_ROOT / ".env"
DB_IDS_PATH = HERE / "db_ids.json"

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
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

DB_IDS = json.loads(DB_IDS_PATH.read_text())


# ---- small builders for Notion property values ------------------------------
def title(text: str):
    return {"title": [{"type": "text", "text": {"content": text}}]}


def rt(text: str):
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def num(x):
    return {"number": x}


def sel(name: str):
    return {"select": {"name": name}}


def msel(names: list[str]):
    return {"multi_select": [{"name": n} for n in names]}


def date(dt: str):
    return {"date": {"start": dt}}


def check(v: bool):
    return {"checkbox": v}


def url(u: str):
    return {"url": u}


def rel(ids: list[str]):
    return {"relation": [{"id": i} for i in ids]}


def create_page(db_id: str, props: dict) -> str:
    body = {"parent": {"database_id": db_id}, "properties": props}
    r = requests.post(
        f"{NOTION_API}/pages", headers=HEADERS, json=body, timeout=30
    )
    if r.status_code >= 300:
        print(f"  [ERR] {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    return r.json()["id"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---- seed ---------------------------------------------------------------------
def seed():
    print("Seeding Prices...")
    price_ids = {}
    prices = [
        ("BTC",   "Bitcoin",  "Bitcoin",  "native", "bitcoin",        83_250.12, 0.021,  0.045, 1_650_000_000_000, "mempool+cg"),
        ("ETH",   "Ethereum", "Ethereum", "native", "ethereum",        3_412.50, 0.015, -0.008,   410_000_000_000, "Moralis"),
        ("SOL",   "Solana",   "Solana",   "native", "solana",             91.24, 0.034,  0.12,     43_000_000_000, "Helius"),
        ("USDC",  "USD Coin", "Ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "usd-coin", 1.00, 0.0, 0.0, 36_000_000_000, "Moralis"),
        ("PENDLE","Pendle",   "Ethereum", "0x808507121b80c02388fad14726482e061b8da827", "pendle",   4.82, 0.08, 0.22, 800_000_000, "CoinGecko"),
        ("JITOSOL","Jito Staked SOL", "Solana", "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "jito-staked-sol", 110.40, 0.034, 0.13, 2_400_000_000, "Helius"),
    ]
    for sym, name, chain, contract, cg_id, price, ch24, ch7, mc, src in prices:
        pid = create_page(DB_IDS["Prices"], {
            "Symbol":        title(sym),
            "Coin Name":     rt(name),
            "Chain":         sel(chain),
            "Contract Address": rt(contract),
            "CoinGecko ID":  rt(cg_id),
            "USD Price":     num(price),
            "24h Change %":  num(ch24),
            "7d Change %":   num(ch7),
            "Market Cap":    num(mc),
            "Last Updated":  date(now()),
            "Price Source":  sel(src),
        })
        price_ids[sym] = pid
        print(f"  ✓ {sym}")

    print("\nSeeding Wallets...")
    wallet_ids = {}
    wallets = [
        ("Main ETH Hot",       "0xcB1C1FdE09f811B294172696404e88E658659905", "Ethereum", "Hot",     ["Main", "Trading"],  days_ago(420)),
        ("ETH Cold Storage",   "0x0000000000000000000000000000000000000dEaD", "Ethereum", "Hardware",["Savings"],          days_ago(365)),
        ("SOL Farming",        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1","Solana",   "Hot",     ["Farming", "Airdrops"], days_ago(120)),
    ]
    for label, addr, chain, wtype, purpose, created in wallets:
        wid = create_page(DB_IDS["Wallets"], {
            "Label":        title(label),
            "Address":      rt(addr),
            "Chain":        sel(chain),
            "Type":         sel(wtype),
            "Purpose":      msel(purpose),
            "Scan Enabled": check(True),
            "Created Date": date(created),
            "Last Scanned": date(now()),
            "Scan Status":  sel("OK"),
        })
        wallet_ids[label] = wid
        print(f"  ✓ {label}")

    print("\nSeeding Holdings...")
    holdings = [
        ("ETH on Main Hot",  "ETH",  "Ethereum", "native",  0.524, 2_850.00, 3_412.50, "L1",               "Core",   "Main ETH Hot",     "ETH"),
        ("USDC on Main Hot", "USDC", "Ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 1250.00, 1.00, 1.00, "Stablecoin", "Core", "Main ETH Hot", "USDC"),
        ("BTC Cold",         "BTC",  "Bitcoin",  "native",  0.0812, 71_000.00, 83_250.12, "L1",            "Core",   "ETH Cold Storage", "BTC"),
        ("SOL Farming",      "SOL",  "Solana",   "native",  34.55,   92.00,    91.24,    "L1",             "Farming","SOL Farming",     "SOL"),
        ("PENDLE bet",       "PENDLE","Ethereum","0x808507121b80c02388fad14726482e061b8da827", 420, 3.10, 4.82, "DeFi Blue Chip", "Trade", "Main ETH Hot", "PENDLE"),
    ]
    for pos, sym, chain, contract, amount, avg_cost, curr_price, category, conviction, wallet_label, price_sym in holdings:
        create_page(DB_IDS["Holdings"], {
            "Position":          title(pos),
            "Symbol":            rt(sym),
            "Chain":             sel(chain),
            "Contract":          rt(contract),
            "Amount":            num(amount),
            "Avg Cost USD":      num(avg_cost),
            "Current Price USD": num(curr_price),
            "Current Value USD": num(round(amount * curr_price, 2)),
            "24h Change %":      num(0.02),
            "Category":          sel(category),
            "Conviction":        sel(conviction),
            "Last Updated":      date(now()),
            "Wallet":            rel([wallet_ids[wallet_label]]),
            "Price":             rel([price_ids[price_sym]]),
        })
        print(f"  ✓ {pos}")

    print("\nSeeding Transactions...")
    txs = [
        ("Buy 0.5 ETH @ 2850", days_ago(180), "Buy",  "ETH",  None,   0.5,   None, 2850.00, 12.50, "Ethereum", "Uniswap", "0xabc...111", "Main ETH Hot"),
        ("Swap 500 USDC→SOL", days_ago(90),  "Swap", "USDC", "SOL",  500.0, 5.4,  1.00,    0.40, "Solana",   "Jupiter", "5xy...222",   "SOL Farming"),
        ("Stake 20 SOL → jitoSOL", days_ago(85), "Stake","SOL", "JITOSOL", 20.0, 16.5, 92.00, 0.01, "Solana", "Jito", "5zz...333", "SOL Farming"),
        ("JTO airdrop",       days_ago(60),  "Airdrop","JTO", None,   180.0, None, 3.20,    0.00, "Solana",   "Jito",    "5ww...444",   "SOL Farming"),
    ]
    for name, dt, ttype, coin, coin_out, amt, amt_out, price, fees, chain, cpty, txhash, wlabel in txs:
        props = {
            "Tx":          title(name),
            "Date":        date(dt),
            "Type":        sel(ttype),
            "Coin":        rt(coin),
            "Amount":      num(amt),
            "Price USD":   num(price),
            "Fees USD":    num(fees),
            "Chain":       sel(chain),
            "Counterparty":rt(cpty),
            "Tx Hash":     rt(txhash),
            "Ingested By": sel("Manual"),
            "Wallet":      rel([wallet_ids[wlabel]]),
        }
        if coin_out:
            props["Coin Out"] = rt(coin_out)
        if amt_out is not None:
            props["Amount Out"] = num(amt_out)
        create_page(DB_IDS["Transactions"], props)
        print(f"  ✓ {name}")

    print("\nSeeding DeFi Positions...")
    defis = [
        ("Pendle PT-weETH", "Pendle", "Ethereum", "PT (fixed yield)", ["weETH"],
         days_ago(45), 1500.00, 1548.00, 0, False, 0.185, "Active",
         "https://app.pendle.finance", "Main ETH Hot", "PENDLE"),
        ("jitoSOL staking", "Jito",   "Solana",   "Restaking",        ["jitoSOL"],
         days_ago(85), 1840.00, 1910.50, 0, False, 0.082, "Active",
         "https://jito.network", "SOL Farming", "JITOSOL"),
    ]
    for pos, proto, chain, ptype, assets, entry, init, curr, rewards, claimed, apy, status, purl, wlabel, psym in defis:
        create_page(DB_IDS["DeFi Positions"], {
            "Position":            title(pos),
            "Protocol":            sel(proto),
            "Chain":               sel(chain),
            "Position Type":       sel(ptype),
            "Assets":              msel(assets),
            "Entry Date":          date(entry),
            "Initial Deposit USD": num(init),
            "Current Value USD":   num(curr),
            "Rewards Earned USD":  num(rewards),
            "Rewards Claimed":     check(claimed),
            "APY %":               num(apy),
            "Status":              sel(status),
            "Position URL":        url(purl),
            "Last Updated":        date(now()),
            "Wallet":              rel([wallet_ids[wlabel]]),
            "Price":               rel([price_ids[psym]]),
        })
        print(f"  ✓ {pos}")

    print("\nSeeding Watchlist...")
    watch = [
        ("Ethena",  "ENA",    "Ethereum", "Restaking thesis + USDe growth", 0.42, 0.58, 0.09, "Waiting for Entry", "High"),
        ("Berachain","BERA",  "Berachain","PoL mechanism, post-launch reset",3.50, 5.10, -0.03,"Researching",       "Medium"),
        ("Sui",     "SUI",    "Sui",      "MoveVM+parallel execution",       2.10, 2.35, 0.04, "Waiting for Entry", "Low"),
    ]
    for coin, sym, chain, thesis, target, curr, ch24, status, prio in watch:
        create_page(DB_IDS["Watchlist"], {
            "Coin":              title(coin),
            "Symbol":            rt(sym),
            "Chain":             sel(chain),
            "Thesis":            rt(thesis),
            "Target Entry USD":  num(target),
            "Current Price USD": num(curr),
            "24h Change %":      num(ch24),
            "Status":            sel(status),
            "Added Date":        date(days_ago(7)),
            "Priority":          sel(prio),
        })
        print(f"  ✓ {coin}")

    print("\nSeeding Airdrops Tracker...")
    airdrops = [
        ("LayerZero",    "Multi-chain", "Claimed", "Bridge + swap via LZ endpoints, use 5+ chains",
         ["Bridged", "Swapped", "Volume threshold"], 1200.00, 960.00,
         days_ago(120), days_ago(90), days_ago(60), ["Main ETH Hot", "SOL Farming"], "High"),
        ("Berachain",    "Berachain",   "Farming", "Testnet activity + mainnet LP",
         ["Deployed Contract", "Provided LP"], 800.00, 0,
         None, None, None, ["Main ETH Hot"], "Medium"),
    ]
    for proto, chain, status, crit, tasks, est, actual, snap, cstart, cdead, wlabels, prio in airdrops:
        props = {
            "Protocol":             title(proto),
            "Chain":                sel(chain),
            "Status":               sel(status),
            "Eligibility Criteria": rt(crit),
            "Tasks Done":           msel(tasks),
            "Estimated Value USD":  num(est),
            "Actual Received USD":  num(actual),
            "Priority":             sel(prio),
            "Qualifying Wallets":   rel([wallet_ids[w] for w in wlabels]),
        }
        if snap:   props["Snapshot Date"]  = date(snap)
        if cstart: props["Claim Start"]    = date(cstart)
        if cdead:  props["Claim Deadline"] = date(cdead)
        create_page(DB_IDS["Airdrops Tracker"], props)
        print(f"  ✓ {proto}")

    print("\nSeeding Settings (1 row)...")
    create_page(DB_IDS["Settings"], {
        "Name":                     title("Default"),
        "Refresh Frequency":        sel("15 min"),
        "Snapshot Frequency":       sel("Daily"),
        "Chains Enabled":           msel(["Ethereum","Arbitrum","Base","Optimism","Polygon","BNB","Solana","Bitcoin"]),
        "Dust Threshold USD":       num(1.0),
        "Auto-compute Cost Basis":  check(True),
        "Default Tax Lot Method":   sel("FIFO"),
        "Hide Scam Tokens":         check(True),
        "Scam Token Denylist":      rt("airdrop.example.com, reward-claim.xyz"),
        "Last Full Sync":           date(now()),
        "Sync Status":              sel("Never run"),
    })
    print("  ✓ Default settings row")

    print("\nDone seeding.")


if __name__ == "__main__":
    seed()
