#!/usr/bin/env python3
"""
Wipe existing demo/test data and reseed the template with a polished
portfolio that looks impressive in screenshots.

Actions:
1. Archive every row in Wallets, Holdings, Prices, Transactions,
   DeFi Positions, NFT Holdings, Watchlist, Research Notes,
   Airdrops Tracker, Balance Snapshots, Settings.
2. Reseed:
   - Prices (15 tokens)
   - Wallets (5 diverse)
   - Holdings (18 positions, ~$47k total)
   - Transactions (12 realistic trades)
   - DeFi Positions (6 active positions)
   - NFT Holdings (3)
   - Watchlist (6 coins with signals)
   - Research Notes (4)
   - Airdrops Tracker (5)
   - Balance Snapshots (30 daily rows for a nice chart)
   - Settings (1 default row)
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import random

import requests

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env"
DB_IDS = json.loads((HERE / "db_ids.json").read_text())

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"


def load_token() -> str:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env.get("NOTION_TOKEN") or os.getenv("NOTION_TOKEN", "")


HEADERS = {
    "Authorization": f"Bearer {load_token()}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# ---- property builders -------------------------------------------------------
def title(t):  return {"title": [{"type": "text", "text": {"content": t}}]}
def rt(t):     return {"rich_text": [{"type": "text", "text": {"content": t}}]}
def num(x):    return {"number": x}
def sel(n):    return {"select": {"name": n}}
def msel(ns):  return {"multi_select": [{"name": n} for n in ns]}
def date(d):   return {"date": {"start": d}}
def check(v):  return {"checkbox": v}
def url(u):    return {"url": u}
def rel(ids):  return {"relation": [{"id": i} for i in ids]}


# ---- Notion helpers ----------------------------------------------------------
def query_all(db_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=HEADERS, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def create_page(db_id: str, props: dict) -> str:
    body = {"parent": {"database_id": db_id}, "properties": props}
    r = requests.post(f"{NOTION_API}/pages", headers=HEADERS, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"  [ERR] {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    return r.json()["id"]


def archive_page(page_id: str) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=HEADERS, json={"archived": True}, timeout=30,
    )
    if r.status_code >= 300:
        print(f"  [WARN] archive {page_id}: {r.text[:200]}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def date_only(n_days_ago: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=n_days_ago)
    return d.strftime("%Y-%m-%d")


# ---- WIPE --------------------------------------------------------------------
WIPE_DBS = [
    "Holdings", "Transactions", "DeFi Positions", "NFT Holdings",
    "Watchlist", "Research Notes", "Airdrops Tracker",
    "Balance Snapshots", "Settings", "Wallets", "Prices",
]


def wipe():
    print("Wiping existing data...")
    for name in WIPE_DBS:
        db_id = DB_IDS.get(name)
        if not db_id:
            continue
        rows = query_all(db_id)
        for row in rows:
            archive_page(row["id"])
            time.sleep(0.05)
        print(f"  ✓ wiped {name} ({len(rows)} rows)")


# ---- SEED --------------------------------------------------------------------
def seed_prices() -> dict[str, str]:
    print("\nSeeding Prices (15)...")
    rows = [
        # (symbol, name, chain, contract, cg_id, price, ch24, ch7, mcap, src)
        ("BTC",    "Bitcoin",              "Bitcoin",  "native", "bitcoin",           83_250.12,  0.021,  0.045, 1_650_000_000_000, "mempool+cg"),
        ("ETH",    "Ethereum",             "Ethereum", "native", "ethereum",           3_412.50,  0.015, -0.008,   410_000_000_000, "Moralis"),
        ("SOL",    "Solana",               "Solana",   "native", "solana",                91.24,  0.034,  0.121,    43_000_000_000, "Helius"),
        ("USDC",   "USD Coin",             "Ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "usd-coin", 1.00, 0.0001, -0.0002, 36_000_000_000, "Moralis"),
        ("USDT",   "Tether",               "Ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7", "tether",   1.00, 0.0,     0.0,    120_000_000_000, "Moralis"),
        ("ARB",    "Arbitrum",             "Arbitrum", "0x912CE59144191C1204E64559FE8253a0e49E6548", "arbitrum", 0.84, -0.023,  0.047,   3_800_000_000, "Moralis"),
        ("OP",     "Optimism",             "Optimism", "0x4200000000000000000000000000000000000042", "optimism", 2.18, 0.012,  -0.034,  2_900_000_000, "Moralis"),
        ("PENDLE", "Pendle",               "Ethereum", "0x808507121b80c02388fad14726482e061b8da827", "pendle",   4.82, 0.087,  0.224,     800_000_000, "CoinGecko"),
        ("ENA",    "Ethena",               "Ethereum", "0x57e114B691Db790C35207b2e685D4A43181e6061", "ethena-usde", 0.71, -0.045, -0.089,  2_000_000_000, "Moralis"),
        ("LDO",    "Lido DAO",             "Ethereum", "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", "lido-dao", 1.92, 0.034, 0.061,     1_700_000_000, "Moralis"),
        ("JITOSOL","Jito Staked SOL",      "Solana",   "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn", "jito-staked-sol", 110.40, 0.034, 0.132, 2_400_000_000, "Helius"),
        ("JUP",    "Jupiter",              "Solana",   "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  "jupiter-exchange-solana", 0.68, 0.021, -0.018, 920_000_000, "Helius"),
        ("WIF",    "dogwifhat",            "Solana",   "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",  "dogwifhat", 2.14, 0.089, 0.034, 2_100_000_000, "Helius"),
        ("AERO",   "Aerodrome",            "Base",     "0x940181a94A35A4569E4529A3CDfB74e38FD98631",    "aerodrome-finance", 1.08, 0.012, 0.078,    900_000_000, "Moralis"),
        ("BRETT",  "Brett",                "Base",     "0x532f27101965dd16442E59d40670FaF5eBB142E4",    "based-brett", 0.089, 0.124, -0.032,    880_000_000, "Moralis"),
    ]
    ids = {}
    for sym, name, chain, contract, cg, price, c24, c7, mcap, src in rows:
        pid = create_page(DB_IDS["Prices"], {
            "Symbol":           title(sym),
            "Coin Name":        rt(name),
            "Chain":            sel(chain),
            "Contract Address": rt(contract),
            "CoinGecko ID":     rt(cg),
            "USD Price":        num(price),
            "24h Change %":     num(c24),
            "7d Change %":      num(c7),
            "Market Cap":       num(mcap),
            "Last Updated":     date(now()),
            "Price Source":     sel(src),
        })
        ids[sym] = pid
        print(f"  ✓ {sym}")
    return ids


def seed_wallets() -> dict[str, str]:
    print("\nSeeding Wallets (5)...")
    rows = [
        # (label, address, chain, type, purpose, created_days, status)
        ("Main ETH Hot",       "0xcB1C1FdE09f811B294172696404e88E658659905", "Ethereum", "Hot",      ["Main", "Trading"],           520, "OK"),
        ("Degen Base",          "0x5a0b54D5dc17e0AadC383d2db43B0a0D3E029c4c", "Base",     "Hot",      ["Degen", "Farming"],           95, "OK"),
        ("Cold Storage",        "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb6", "Ethereum", "Hardware", ["Savings"],                   720, "OK"),
        ("SOL Farming",         "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1","Solana",   "Hot",      ["Farming", "Airdrops"],       180, "OK"),
        ("BTC Vault",           "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97", "Bitcoin", "Cold", ["Savings"], 900, "OK"),
    ]
    ids = {}
    for label, addr, chain, wtype, purpose, created, status in rows:
        wid = create_page(DB_IDS["Wallets"], {
            "Label":         title(label),
            "Address":       rt(addr),
            "Chain":         sel(chain),
            "Type":          sel(wtype),
            "Purpose":       msel(purpose),
            "Scan Enabled":  check(False),  # disabled for static demo
            "Created Date":  date(days_ago(created)),
            "Last Scanned":  date(days_ago(0)),
            "Scan Status":   sel(status),
        })
        ids[label] = wid
        print(f"  ✓ {label}")
    return ids


def seed_holdings(w: dict, p: dict):
    print("\nSeeding Holdings (18)...")
    # (pos, sym, chain, contract, amount, avg_cost, curr_price, cat, conv, wallet, price)
    rows = [
        ("ETH",         "ETH",   "Ethereum", "native",                                        4.21,   2_850.00, 3_412.50, "L1",              "Core",        "Main ETH Hot",  "ETH"),
        ("BTC",         "BTC",   "Bitcoin",  "native",                                        0.182,  62_400.00, 83_250.12, "L1",              "Core",        "BTC Vault",     "BTC"),
        ("SOL",         "SOL",   "Solana",   "native",                                       58.40,      82.00,    91.24, "L1",              "Core",        "SOL Farming",   "SOL"),
        ("USDC",        "USDC",  "Ethereum", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 4_200.00,       1.00,     1.00, "Stablecoin",      "Core",        "Main ETH Hot",  "USDC"),
        ("USDC Base",   "USDC",  "Base",     "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 1_800.00,       1.00,     1.00, "Stablecoin",      "Farming",     "Degen Base",    "USDC"),
        ("ARB",         "ARB",   "Arbitrum", "0x912CE59144191C1204E64559FE8253a0e49E6548", 1_420.00,       1.12,     0.84, "L2",              "Trade",       "Main ETH Hot",  "ARB"),
        ("OP",          "OP",    "Optimism", "0x4200000000000000000000000000000000000042",   410.00,       2.45,     2.18, "L2",              "Trade",       "Main ETH Hot",  "OP"),
        ("PENDLE",      "PENDLE","Ethereum", "0x808507121b80c02388fad14726482e061b8da827",   520.00,       3.10,     4.82, "DeFi Blue Chip",  "Core",        "Main ETH Hot",  "PENDLE"),
        ("ENA",         "ENA",   "Ethereum", "0x57e114B691Db790C35207b2e685D4A43181e6061", 2_800.00,       0.93,     0.71, "DeFi Blue Chip",  "Trade",       "Main ETH Hot",  "ENA"),
        ("LDO",         "LDO",   "Ethereum", "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32",   480.00,       1.68,     1.92, "LST",             "Core",        "Main ETH Hot",  "LDO"),
        ("Cold BTC",    "BTC",   "Bitcoin",  "native",                                        0.094,  45_000.00, 83_250.12, "L1",              "Core",        "Cold Storage",  "BTC"),
        ("jitoSOL",     "JITOSOL","Solana",  "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",   12.50,      94.00,   110.40, "LST",             "Core",        "SOL Farming",   "JITOSOL"),
        ("JUP",         "JUP",   "Solana",   "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", 1_200.00,       0.72,     0.68, "DeFi Blue Chip",  "Trade",       "SOL Farming",   "JUP"),
        ("WIF",         "WIF",   "Solana",   "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",   280.00,       1.85,     2.14, "Meme",            "Speculation", "SOL Farming",   "WIF"),
        ("AERO",        "AERO",  "Base",     "0x940181a94A35A4569E4529A3CDfB74e38FD98631",    850.00,       0.88,     1.08, "DeFi Blue Chip",  "Farming",     "Degen Base",    "AERO"),
        ("BRETT",       "BRETT", "Base",     "0x532f27101965dd16442E59d40670FaF5eBB142E4", 24_000.00,       0.072,    0.089, "Meme",            "Speculation", "Degen Base",    "BRETT"),
        ("Cold ETH",    "ETH",   "Ethereum", "native",                                        1.85,   1_980.00, 3_412.50, "L1",              "Core",        "Cold Storage",  "ETH"),
        ("USDT",        "USDT",  "Ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7",   620.00,       1.00,     1.00, "Stablecoin",      "Core",        "Cold Storage",  "USDT"),
    ]
    total = 0
    for pos, sym, chain, contract, amount, avg, curr, cat, conv, wlabel, psym in rows:
        value = round(amount * curr, 2)
        total += value
        create_page(DB_IDS["Holdings"], {
            "Position":          title(pos),
            "Symbol":            rt(sym),
            "Chain":             sel(chain),
            "Contract":          rt(contract),
            "Amount":            num(amount),
            "Avg Cost USD":      num(avg),
            "Current Price USD": num(curr),
            "Current Value USD": num(value),
            "24h Change %":      num(random.uniform(-0.03, 0.08)),
            "Category":          sel(cat),
            "Conviction":        sel(conv),
            "Last Updated":      date(now()),
            "Wallet":             rel([w[wlabel]]),
            "Price":              rel([p[psym]]),
        })
        print(f"  ✓ {pos:12s} ${value:>11,.2f}")
    print(f"  → total portfolio ${total:,.2f}")


def seed_transactions(w: dict):
    print("\nSeeding Transactions (12)...")
    txs = [
        ("Buy 2 ETH",                    60, "Buy",  "ETH",  None,   2.00,    None,   2_750.00, 18.40, "Ethereum", "Coinbase", "0xa1...", "Main ETH Hot"),
        ("Buy 0.1 BTC",                  180, "Buy", "BTC",  None,   0.10,    None, 63_200.00, 12.00, "Bitcoin",  "Kraken",   "tx:b2...","BTC Vault"),
        ("Swap 1000 USDC → SOL",         120, "Swap","USDC", "SOL", 1_000.00, 12.20,  1.00,      0.80, "Solana",   "Jupiter", "jup:c3...","SOL Farming"),
        ("Stake 10 SOL → jitoSOL",       110, "Stake","SOL", "JITOSOL", 10.0,  9.8,  88.00,      0.01, "Solana",   "Jito",    "jito:d4...","SOL Farming"),
        ("Buy 500 PENDLE",                90, "Buy",  "PENDLE", None, 500.0,  None,   3.10,      4.50, "Ethereum", "Uniswap", "uni:e5...","Main ETH Hot"),
        ("LP Add: AERO/USDC",             75, "LP Add","AERO", "USDC", 400.0, 432.0,  1.02,      2.10, "Base",     "Aerodrome","aero:f6...","Degen Base"),
        ("Sell 1 ETH @ 3600",             40, "Sell", "ETH",  None,   1.00,   None,  3_600.00,  6.40, "Ethereum", "Uniswap", "uni:g7...","Main ETH Hot"),
        ("Swap 500 ARB → USDC",           30, "Swap", "ARB",  "USDC", 500.0, 420.0,   0.84,      1.10, "Arbitrum", "1inch",   "1inch:h8..","Main ETH Hot"),
        ("Bridge USDC → Base",            28, "Bridge","USDC",None, 1_500.0,  None,   1.00,      3.20, "Base",     "Across",  "across:i9.","Degen Base"),
        ("JTO airdrop",                   70, "Airdrop","JTO",None, 280.0,    None,   3.20,      0.00, "Solana",   "Jito",    "jito:j1...","SOL Farming"),
        ("Claim PENDLE rewards",          14, "Claim Reward","PENDLE",None, 42.0, None, 4.65,    2.10, "Ethereum", "Pendle",  "pndl:k2...","Main ETH Hot"),
        ("Buy 24000 BRETT (degen)",       45, "Buy",  "BRETT", None, 24_000.0,None,    0.072,    1.80, "Base",     "Aerodrome","aero:l3...","Degen Base"),
    ]
    for name, dago, ttype, coin, coin_out, amt, amt_out, price, fees, chain, cpty, txhash, wlabel in txs:
        props = {
            "Tx":          title(name),
            "Date":        date(days_ago(dago)),
            "Type":        sel(ttype),
            "Coin":        rt(coin),
            "Amount":      num(amt),
            "Price USD":   num(price),
            "Fees USD":    num(fees),
            "Chain":       sel(chain),
            "Counterparty":rt(cpty),
            "Tx Hash":     rt(txhash),
            "Ingested By": sel("Manual"),
            "Wallet":      rel([w[wlabel]]),
        }
        if coin_out: props["Coin Out"] = rt(coin_out)
        if amt_out is not None: props["Amount Out"] = num(amt_out)
        create_page(DB_IDS["Transactions"], props)
        print(f"  ✓ {name}")


def seed_defi(w: dict, p: dict):
    print("\nSeeding DeFi Positions (6)...")
    rows = [
        ("Pendle PT-weETH",  "Pendle", "Ethereum", "PT (fixed yield)", ["weETH"],
         45,  3_500.00, 3_712.00,  0,  False, 0.192, "Active", "https://app.pendle.finance", "Main ETH Hot", "PENDLE"),
        ("jitoSOL restake",  "Jito",   "Solana",   "Restaking",         ["jitoSOL"],
         140, 1_380.00, 1_544.00, 18,  False, 0.082, "Active", "https://jito.network",      "SOL Farming",  "JITOSOL"),
        ("Aerodrome vAMM",   "Other",  "Base",     "LP",                ["AERO", "USDC"],
         75,  1_260.00, 1_402.00, 42,  False, 0.184, "Active", "https://aerodrome.finance", "Degen Base",   "AERO"),
        ("Aave USDC supply", "Aave",   "Ethereum", "Lending Supply",    ["USDC"],
         200, 3_000.00, 3_189.00,  0,  True,  0.047, "Active", "https://app.aave.com",       "Main ETH Hot", "USDC"),
        ("EigenLayer restake","EigenLayer","Ethereum","Restaking",      ["wstETH"],
         180, 2_100.00, 2_245.00,  0,  False, 0.041, "Active", "https://app.eigenlayer.xyz","Main ETH Hot", "ETH"),
        ("Kamino SOL vault", "Kamino", "Solana",   "Vault",             ["SOL", "USDC"],
         55,    820.00,   901.00, 12,  False, 0.165, "Active", "https://kamino.finance",    "SOL Farming",  "SOL"),
    ]
    for pos, proto, chain, ptype, assets, d_ago, init, curr, rewards, claimed, apy, status, purl, wlabel, psym in rows:
        create_page(DB_IDS["DeFi Positions"], {
            "Position":            title(pos),
            "Protocol":            sel(proto),
            "Chain":               sel(chain),
            "Position Type":       sel(ptype),
            "Assets":              msel(assets),
            "Entry Date":          date(days_ago(d_ago)),
            "Initial Deposit USD": num(init),
            "Current Value USD":   num(curr),
            "Rewards Earned USD":  num(rewards),
            "Rewards Claimed":     check(claimed),
            "APY %":               num(apy),
            "Status":              sel(status),
            "Position URL":        url(purl),
            "Last Updated":        date(now()),
            "Wallet":              rel([w[wlabel]]),
            "Price":               rel([p[psym]]),
        })
        print(f"  ✓ {pos}")


def seed_nfts(w: dict):
    print("\nSeeding NFT Holdings (3)...")
    rows = [
        ("Pudgy Penguin #4821", "Pudgy Penguins", "4821",  "Ethereum", "0xBd3531dA5CF5857e7CfAA92426877b022e612cf8", 420, 8_400, 24_500, 26_100, 4821, "https://opensea.io", "Holding", "Main ETH Hot"),
        ("Mad Lads #1247",      "Mad Lads",       "1247",  "Solana",   "J1S9H3QjnRtBbbuD4HjPV6RpRhwuk4zKbxsnCHuTgh9w", 365,  180, 260, 285, 1247, "https://magiceden.io", "Holding", "SOL Farming"),
        ("Azuki #3301",         "Azuki",          "3301",  "Ethereum", "0xED5AF388653567Af2F388E6224dC7C4b3241C544", 600, 12_500, 4_800, 5_100, 3301, "https://opensea.io", "Holding", "Cold Storage"),
    ]
    for title_, coll, tid, chain, contract, d_ago, cost, floor, last, rank, ourl, status, wlabel in rows:
        create_page(DB_IDS["NFT Holdings"], {
            "Title":             title(title_),
            "Token ID":          rt(tid),
            "Chain":             sel(chain),
            "Contract Address":  rt(contract),
            "Acquired Date":     date(days_ago(d_ago)),
            "Cost Basis USD":    num(cost),
            "Current Floor USD": num(floor),
            "Last Sale USD":     num(last),
            "Rarity Rank":       num(rank),
            "Marketplace Link":  url(ourl),
            "Status":            sel(status),
            "Wallet":            rel([w[wlabel]]),
        })
        print(f"  ✓ {title_}")


def seed_watchlist(p: dict):
    print("\nSeeding Watchlist (6)...")
    rows = [
        ("Sui",          "SUI",   "Sui",       "MoveVM + parallel execution, post-FTX reset complete",   2.10, 2.35,  0.04,  "Waiting for Entry", "Medium"),
        ("Ethena",       "ENA",   "Ethereum",  "USDe scaling + restaking thesis, reward season",         0.55, 0.71, -0.045, "Bought",            "High"),
        ("Berachain",    "BERA",  "Berachain", "PoL mechanism, post-launch volatility opportunity",      3.50, 5.10, -0.034, "Researching",       "Medium"),
        ("Hyperliquid",  "HYPE",  "Other",     "Perp DEX dominance, revenue-share model",               18.00, 16.20, 0.076, "Waiting for Entry", "High"),
        ("Jito",         "JTO",   "Solana",    "Solana MEV leader + restaking, undervalued",             2.50, 3.20, 0.089, "Watchlist",         "Medium"),
        ("Kaito",        "KAITO", "Other",     "AI-powered crypto sentiment platform, early",            1.40, 1.25, 0.124, "Researching",       "Low"),
    ]
    for coin, sym, chain, thesis, target, curr, c24, status, prio in rows:
        create_page(DB_IDS["Watchlist"], {
            "Coin":              title(coin),
            "Symbol":            rt(sym),
            "Chain":             sel(chain),
            "Thesis":            rt(thesis),
            "Target Entry USD":  num(target),
            "Current Price USD": num(curr),
            "24h Change %":      num(c24),
            "Status":            sel(status),
            "Added Date":        date(days_ago(14)),
            "Priority":          sel(prio),
        })
        print(f"  ✓ {coin}")


def seed_research():
    print("\nSeeding Research Notes (4)...")
    rows = [
        ("Ethena",      "ENA",   ["DeFi", "Stablecoin"], "Buy",       "4",   0.55, 3),
        ("Hyperliquid", "HYPE",  ["DeFi", "Infra"],      "Watchlist", "5",  18.00, 5),
        ("Sui",         "SUI",   ["L1"],                 "Watchlist", "3",   2.10, 10),
        ("Pump.fun",    "PUMP",  ["Meme", "Infra"],      "Pass",      "2",   0.00, 8),
    ]
    for coin, sym, cats, decision, conv, target, d_ago in rows:
        create_page(DB_IDS["Research Notes"], {
            "Coin":              title(coin),
            "Symbol":            rt(sym),
            "Category":          msel(cats),
            "Decision":          sel(decision),
            "Conviction (1-5)":  sel(conv),
            "Target Price USD":  num(target),
            "Date Researched":   date(days_ago(d_ago)),
        })
        print(f"  ✓ {coin}")


def seed_airdrops(w: dict):
    print("\nSeeding Airdrops Tracker (5)...")
    rows = [
        ("LayerZero",    "Multi-chain", "Claimed",       "Bridge 5+ chains, sustained volume",
         ["Bridged", "Swapped", "Volume threshold"], 1_200.00,  960.00,
         150, 120, 90, ["Main ETH Hot", "SOL Farming"], "High"),
        ("ZKsync",       "Ethereum",    "Claimed",       "Transactions on zkSync Era, bridged ETH",
         ["Bridged", "Swapped", "Used N times"], 2_400.00, 1_850.00,
         80, 65, 45, ["Main ETH Hot"], "High"),
        ("Berachain",    "Berachain",   "Farming",       "Testnet activity + mainnet LP on BEX",
         ["Deployed Contract", "Provided LP", "Staked"], 1_500.00, 0,
         None, None, None, ["Main ETH Hot"], "High"),
        ("Monad",        "Other",       "Farming",       "Testnet activity, awaiting mainnet",
         ["Used N times"], 800.00, 0,
         None, None, None, ["Main ETH Hot"], "Medium"),
        ("Eigenpie",     "Ethereum",    "Snapshot Taken","LRT restaking points, snapshot taken",
         ["Staked", "Provided LP"], 420.00, 0,
         14, None, None, ["Main ETH Hot"], "Medium"),
    ]
    for proto, chain, status, crit, tasks, est, actual, snap, cstart, cdead, wlabels, prio in rows:
        props = {
            "Protocol":             title(proto),
            "Chain":                sel(chain),
            "Status":               sel(status),
            "Eligibility Criteria": rt(crit),
            "Tasks Done":           msel(tasks),
            "Estimated Value USD":  num(est),
            "Actual Received USD":  num(actual),
            "Priority":             sel(prio),
            "Qualifying Wallets":   rel([w[wl] for wl in wlabels]),
        }
        if snap is not None:   props["Snapshot Date"]  = date(days_ago(snap))
        if cstart is not None: props["Claim Start"]    = date(days_ago(cstart))
        if cdead is not None:  props["Claim Deadline"] = date(days_ago(cdead))
        create_page(DB_IDS["Airdrops Tracker"], props)
        print(f"  ✓ {proto}")


def seed_snapshots():
    print("\nSeeding Balance Snapshots (30 days)...")
    # simulate a portfolio growing from ~$38k to ~$47k over 30 days, with some volatility
    base = 38_000
    growth = 9_200
    for i in range(30, -1, -1):
        # smooth growth + sinusoidal noise
        progress = (30 - i) / 30
        total = base + growth * progress + math.sin(i * 0.7) * 900 + random.uniform(-350, 350)
        total = round(total, 2)
        spot = round(total * 0.72, 2)
        defi = round(total * 0.18, 2)
        nft = round(total * 0.07, 2)
        stable = round(total * 0.14, 2)
        eth_usd = round(total * 0.32, 2)
        sol_usd = round(total * 0.18, 2)
        arb_usd = round(total * 0.08, 2)
        base_usd = round(total * 0.10, 2)
        other = round(total * 0.32, 2)
        d = date_only(i)
        create_page(DB_IDS["Balance Snapshots"], {
            "Date":               title(d),
            "Timestamp":          date(days_ago(i)),
            "Total USD":          num(total),
            "Spot USD":           num(spot),
            "DeFi USD":           num(defi),
            "NFT USD":            num(nft),
            "Stable USD":         num(stable),
            "Ethereum USD":       num(eth_usd),
            "Solana USD":         num(sol_usd),
            "Arbitrum USD":       num(arb_usd),
            "Base USD":           num(base_usd),
            "Other Chains USD":   num(other),
            "Realized P&L USD":   num(round(progress * 2_100, 2)),
            "Unrealized P&L USD": num(round(progress * 7_100, 2)),
        })
    print(f"  ✓ 31 daily snapshots (${base:,.0f} → ~${base + growth:,.0f})")


def seed_settings():
    print("\nSeeding Settings (1)...")
    create_page(DB_IDS["Settings"], {
        "Name":                   title("Default"),
        "Refresh Frequency":      sel("15 min"),
        "Snapshot Frequency":     sel("Daily"),
        "Chains Enabled":         msel(["Ethereum","Arbitrum","Base","Optimism","Polygon","BNB","Solana","Bitcoin"]),
        "Dust Threshold USD":     num(1.0),
        "Auto-compute Cost Basis":check(True),
        "Default Tax Lot Method": sel("FIFO"),
        "Hide Scam Tokens":       check(True),
        "Last Full Sync":         date(now()),
        "Sync Status":            sel("OK"),
    })
    print("  ✓ Default")


def main():
    random.seed(42)  # reproducible
    wipe()
    p = seed_prices()
    w = seed_wallets()
    seed_holdings(w, p)
    seed_transactions(w)
    seed_defi(w, p)
    seed_nfts(w)
    seed_watchlist(p)
    seed_research()
    seed_airdrops(w)
    seed_snapshots()
    seed_settings()
    print("\n✓ reseed complete")


if __name__ == "__main__":
    main()
