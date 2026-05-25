#!/usr/bin/env python3
"""
Crypto portfolio sync daemon.

Polls the Notion "Wallets" database every POLL_SECONDS. For each wallet
where Scan Enabled=true and Last Scanned is empty or older than
STALE_MINUTES, runs the multi-chain scanner, refreshes that wallet's
Holdings rows and the Prices DB, and updates Last Scanned / Scan Status.

Also archives any global orphans (Holdings/DeFi/Transactions rows whose
Wallet relation is empty) on every cycle.

Run via:
    python3 sync_daemon.py            # foreground
    systemctl start crypto-sync       # via systemd unit (see install)
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# Support both dev layout (scanner.py one dir up) and packaged layout (same dir)
PROJECT_ROOT = HERE.parent if (HERE.parent / "scanner.py").exists() else HERE
if (HERE / ".env").exists():
    ENV_PATH = HERE / ".env"
else:
    ENV_PATH = PROJECT_ROOT / ".env"
DB_IDS_PATH = HERE / "db_ids.json"
LOG_PATH = HERE / "sync_daemon.log"

# Make scanner.py importable from either location
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(HERE))
import scanner  # noqa: E402

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"

POLL_SECONDS = 60          # how often to check Notion for new wallets
STALE_MINUTES = 15         # re-scan wallets whose Last Scanned is older than this


# ---- env / config ------------------------------------------------------------
def load_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()
TOKEN = ENV.get("NOTION_TOKEN") or os.getenv("NOTION_TOKEN", "")
if not TOKEN:
    print("NOTION_TOKEN missing", file=sys.stderr)
    sys.exit(1)

DB_IDS = json.loads(DB_IDS_PATH.read_text())
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---- Notion helpers ----------------------------------------------------------
def notion_query(db_id: str, filter_: dict = None) -> list[dict]:
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        if filter_:
            body["filter"] = filter_
        r = requests.post(
            f"{NOTION_API}/databases/{db_id}/query",
            headers=HEADERS, json=body, timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def notion_create(db_id: str, properties: dict) -> str:
    r = requests.post(
        f"{NOTION_API}/pages",
        headers=HEADERS,
        json={"parent": {"database_id": db_id}, "properties": properties},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"create failed: {r.text[:500]}")
    return r.json()["id"]


def notion_update(page_id: str, properties: dict) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=HEADERS, json={"properties": properties}, timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"update failed: {r.text[:500]}")


def notion_archive(page_id: str) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=HEADERS, json={"archived": True}, timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"archive failed: {r.text[:500]}")


# ---- property value builders -------------------------------------------------
def p_title(text: str):      return {"title": [{"type": "text", "text": {"content": text or ""}}]}
def p_rt(text: str):         return {"rich_text": [{"type": "text", "text": {"content": text or ""}}]}
def p_num(x):                return {"number": float(x) if x is not None else None}
def p_sel(name: str):        return {"select": {"name": name}} if name else {"select": None}
def p_msel(names: list):     return {"multi_select": [{"name": n} for n in names]}
def p_date(iso: str):        return {"date": {"start": iso}}
def p_check(v: bool):        return {"checkbox": bool(v)}
def p_rel(ids: list[str]):   return {"relation": [{"id": i} for i in ids]}


def get_title(page: dict) -> str:
    for _, prop in page.get("properties", {}).items():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            if parts:
                return "".join(p.get("plain_text", "") for p in parts)
    return ""


def get_rt(page: dict, key: str) -> str:
    prop = page.get("properties", {}).get(key, {})
    parts = prop.get("rich_text", [])
    return "".join(p.get("plain_text", "") for p in parts)


def get_sel(page: dict, key: str) -> str:
    prop = page.get("properties", {}).get(key, {})
    s = prop.get("select")
    return s.get("name") if s else ""


def get_check(page: dict, key: str) -> bool:
    return page.get("properties", {}).get(key, {}).get("checkbox", False)


def get_date(page: dict, key: str) -> str:
    d = page.get("properties", {}).get(key, {}).get("date")
    return d.get("start") if d else ""


# ---- chain name mapping ------------------------------------------------------
CHAIN_DISPLAY = {
    "eth": "Ethereum",
    "ethereum": "Ethereum",
    "polygon": "Polygon",
    "bsc": "BNB",
    "arbitrum": "Arbitrum",
    "optimism": "Optimism",
    "base": "Base",
    "solana": "Solana",
    "bitcoin": "Bitcoin",
}


def chain_display(raw: str) -> str:
    return CHAIN_DISPLAY.get((raw or "").lower(), "Other")


# ---- prices upsert -----------------------------------------------------------
_price_cache: dict[tuple, str] = {}  # (chain_display, contract) -> page_id


def price_key(chain: str, contract: str) -> tuple:
    return (chain.lower(), (contract or "native").lower())


def upsert_price(row: dict) -> str:
    """Find-or-create a Prices row for this token. Return its page_id."""
    chain = chain_display(row["chain"])
    contract = row.get("contract") or "native"
    key = price_key(chain, contract)

    if key in _price_cache:
        pid = _price_cache[key]
        notion_update(pid, {
            "USD Price":    p_num(row.get("usd_price") or 0),
            "24h Change %": p_num((row.get("price_24h_change") or 0) / 100),
            "Last Updated": p_date(datetime.now(timezone.utc).isoformat()),
        })
        return pid

    # find existing
    results = notion_query(DB_IDS["Prices"], {
        "and": [
            {"property": "Chain", "select": {"equals": chain}},
            {"property": "Contract Address", "rich_text": {"equals": contract}},
        ]
    })
    if results:
        pid = results[0]["id"]
    else:
        source_map = {
            "moralis": "Moralis", "helius": "Helius",
            "mempool+coingecko": "mempool+cg",
        }
        source = source_map.get(row.get("source", ""), "Manual")
        pid = notion_create(DB_IDS["Prices"], {
            "Symbol":           p_title(row.get("token_symbol") or "?"),
            "Coin Name":        p_rt(row.get("token_name") or ""),
            "Chain":            p_sel(chain),
            "Contract Address": p_rt(contract),
            "USD Price":        p_num(row.get("usd_price") or 0),
            "24h Change %":     p_num((row.get("price_24h_change") or 0) / 100),
            "Last Updated":     p_date(datetime.now(timezone.utc).isoformat()),
            "Price Source":     p_sel(source),
        })

    _price_cache[key] = pid
    # always patch latest price
    notion_update(pid, {
        "USD Price":    p_num(row.get("usd_price") or 0),
        "24h Change %": p_num((row.get("price_24h_change") or 0) / 100),
        "Last Updated": p_date(datetime.now(timezone.utc).isoformat()),
    })
    return pid


# ---- holdings upsert ---------------------------------------------------------
def archive_wallet_holdings(wallet_page_id: str) -> int:
    """Archive all existing Holdings for a given wallet before a fresh scan."""
    rows = notion_query(DB_IDS["Holdings"], {
        "property": "Wallet",
        "relation": {"contains": wallet_page_id},
    })
    for r in rows:
        try:
            notion_archive(r["id"])
        except Exception as e:
            log(f"  [WARN] failed to archive {r['id']}: {e}")
    return len(rows)


def create_holding(row: dict, wallet_page_id: str, price_page_id: str) -> None:
    chain = chain_display(row["chain"])
    contract = row.get("contract") or "native"
    symbol = row.get("token_symbol") or "?"
    amount = float(row.get("balance") or 0)
    price = float(row.get("usd_price") or 0)
    value = float(row.get("usd_value") or amount * price)
    ch24 = float(row.get("price_24h_change") or 0) / 100

    # crude category — daemon leaves this manual for the user to override
    cat = "L1" if (row.get("native") and chain in ("Ethereum", "Solana", "Bitcoin")) else "Other"

    props = {
        "Position":          p_title(f"{symbol} on {chain}"),
        "Symbol":            p_rt(symbol),
        "Chain":             p_sel(chain),
        "Contract":          p_rt(contract),
        "Amount":            p_num(amount),
        "Avg Cost USD":      p_num(0),  # user fills in manually
        "Current Price USD": p_num(price),
        "Current Value USD": p_num(round(value, 2)),
        "24h Change %":      p_num(ch24),
        "Category":          p_sel(cat),
        "Last Updated":      p_date(datetime.now(timezone.utc).isoformat()),
        "Wallet":             p_rel([wallet_page_id]),
        "Price":              p_rel([price_page_id]),
    }
    notion_create(DB_IDS["Holdings"], props)


# ---- orphan cleanup ----------------------------------------------------------
ORPHAN_DBS = [
    ("Holdings",       "Wallet"),
    ("DeFi Positions", "Wallet"),
    ("NFT Holdings",   "Wallet"),
    ("Transactions",   "Wallet"),
]


def archive_global_orphans() -> int:
    total = 0
    for db_name, wallet_prop in ORPHAN_DBS:
        db_id = DB_IDS.get(db_name)
        if not db_id:
            continue
        # filter: Wallet relation is_empty
        try:
            rows = notion_query(db_id, {
                "property": wallet_prop,
                "relation": {"is_empty": True},
            })
        except Exception as e:
            log(f"  [WARN] orphan query {db_name}: {e}")
            continue
        for r in rows:
            try:
                notion_archive(r["id"])
                total += 1
            except Exception as e:
                log(f"  [WARN] archive {db_name} {r['id']}: {e}")
    return total


# ---- main scan flow ----------------------------------------------------------
def wallet_needs_scan(wallet: dict) -> bool:
    if not get_check(wallet, "Scan Enabled"):
        return False
    last = get_date(wallet, "Last Scanned")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return True
    return datetime.now(timezone.utc) - last_dt > timedelta(minutes=STALE_MINUTES)


def scan_wallet(wallet: dict) -> None:
    wid = wallet["id"]
    label = get_title(wallet) or "Wallet"
    address = get_rt(wallet, "Address").strip()
    if not address:
        log(f"  [SKIP] {label}: no address")
        return

    log(f"→ scanning '{label}' ({address[:12]}...)")

    try:
        rows = scanner.scan(address, label=label)
    except Exception as e:
        log(f"  [ERR] scanner failed: {e}")
        notion_update(wid, {
            "Scan Status":   p_sel("Error"),
            "Scan Error Msg": p_rt(str(e)[:500]),
            "Last Scanned":  p_date(datetime.now(timezone.utc).isoformat()),
        })
        return

    # wipe existing holdings for this wallet, then re-create fresh
    archived = archive_wallet_holdings(wid)
    log(f"  archived {archived} stale holdings for this wallet")

    created = 0
    for row in rows:
        try:
            pid = upsert_price(row)
            create_holding(row, wid, pid)
            created += 1
        except Exception as e:
            log(f"  [WARN] row failed ({row.get('token_symbol','?')}): {e}")

    total = sum(r.get("usd_value", 0) for r in rows)
    log(f"  ✓ {label}: {created} holdings created, total ${total:,.2f}")

    notion_update(wid, {
        "Scan Status":    p_sel("OK"),
        "Scan Error Msg": p_rt(""),
        "Last Scanned":   p_date(datetime.now(timezone.utc).isoformat()),
    })


def process_cycle() -> None:
    # 1. global orphan cleanup
    try:
        n = archive_global_orphans()
        if n:
            log(f"archived {n} global orphan row(s)")
    except Exception as e:
        log(f"[WARN] orphan sweep failed: {e}")

    # 2. pick wallets that need scanning
    try:
        wallets = notion_query(DB_IDS["Wallets"], {
            "property": "Scan Enabled", "checkbox": {"equals": True}
        })
    except Exception as e:
        log(f"[ERR] wallet query failed: {e}")
        return

    pending = [w for w in wallets if wallet_needs_scan(w)]
    if not pending:
        return

    log(f"=== cycle: {len(pending)} wallet(s) need scanning ===")
    for w in pending:
        try:
            scan_wallet(w)
        except Exception:
            log("[ERR] scan_wallet crashed:")
            log(traceback.format_exc())


def main():
    log(f"sync daemon starting — poll={POLL_SECONDS}s stale={STALE_MINUTES}m")
    while True:
        try:
            process_cycle()
        except KeyboardInterrupt:
            log("shutdown via SIGINT")
            return
        except Exception:
            log("[ERR] cycle crashed:")
            log(traceback.format_exc())
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
