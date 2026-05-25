#!/usr/bin/env python3
"""
Build the Crypto Portfolio Tracker dashboard page inside the parent Notion page.

Creates a dashboard subpage with:
- Hero header + welcome callout
- Quick-start checklist
- Section headers with link_to_page blocks for each database
- Settings + support links

Notion API limitation: we cannot create "linked database views" with filters
via the API. The script creates link_to_page blocks; the user then opens the
dashboard in Notion and drags each link to "Turn into → Linked view of
database" (15 seconds per section).

Usage:
    python3 build_dashboard.py <parent_page_id_or_url>
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

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


def extract_page_id(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"([0-9a-f]{32})", raw.replace("-", ""))
    if m:
        x = m.group(1)
        return f"{x[0:8]}-{x[8:12]}-{x[12:16]}-{x[16:20]}-{x[20:32]}"
    raise ValueError(f"Could not extract Notion page ID from: {raw}")


# ---- block builders -----------------------------------------------------------
def rt(text: str, bold=False, code=False, color="default"):
    ann = {"bold": bold, "italic": False, "strikethrough": False,
           "underline": False, "code": code, "color": color}
    return {"type": "text", "text": {"content": text}, "annotations": ann}


def h1(text: str):
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": [rt(text)]}}


def h2(text: str):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(text)]}}


def h3(text: str):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(text)]}}


def para(text: str):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [rt(text)]}}


def para_mixed(segments: list):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": segments}}


def callout(text: str, emoji: str = "💡", color: str = "gray_background"):
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [rt(text)],
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def bullet(text: str):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [rt(text)]}}


def todo(text: str, checked: bool = False):
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [rt(text)], "checked": checked}}


def link_to_db(db_id: str):
    # link_to_page supports database_id type for linking to a database
    return {"object": "block", "type": "link_to_page",
            "link_to_page": {"type": "database_id", "database_id": db_id}}


def quote(text: str):
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": [rt(text)]}}


def code_block(text: str, lang: str = "bash"):
    return {"object": "block", "type": "code",
            "code": {"rich_text": [rt(text)], "language": lang}}


# ---- page composition --------------------------------------------------------
def build_blocks() -> list:
    blocks = []

    # Hero
    blocks.append(h1("🚀 Crypto Portfolio Tracker"))
    blocks.append(callout(
        "Welcome! This is your command center. Everything below links to the "
        "underlying databases — open each one to browse, or convert the links "
        "into inline views by selecting the block and choosing Turn into → "
        "Linked view of database.",
        emoji="👋", color="blue_background",
    ))

    # Quick start
    blocks.append(h2("⚡ Quick Start"))
    blocks.append(todo("Read the Setup Guide (see link below) to connect your API keys", False))
    blocks.append(todo("Add your first wallet in the Wallets database", False))
    blocks.append(todo("Run the sync script / workflow once — holdings will appear", False))
    blocks.append(todo("Populate the Watchlist with coins you're tracking", False))
    blocks.append(todo("Log a few manual Transactions so P&L calculations work", False))

    blocks.append(divider())

    # Sections — each with heading + description + link to DB
    sections = [
        ("📊 Holdings",        "Your live token positions across all chains. Auto-populated by the sync script; P&L calculated from Avg Cost × Amount vs. Current Value.",
         "Holdings"),
        ("👛 Wallets",          "Every wallet you track. Toggle Scan Enabled to start/stop syncing a wallet. The sync daemon picks up new wallets automatically.",
         "Wallets"),
        ("💲 Prices",           "Live price feed — one row per token. The sync script writes here on every cycle; Holdings and Watchlist pull prices via relations.",
         "Prices"),
        ("📝 Transactions",     "Manual trade log. Used for cost basis, realized P&L, and tax reporting. Supports Buy / Sell / Swap / Stake / Airdrop / Bridge and more.",
         "Transactions"),
        ("🌾 DeFi Positions",   "Active LPs, lending, staking, restaking, Pendle PTs, etc. Track initial deposit, current value, and realized yield.",
         "DeFi Positions"),
        ("🖼️ NFT Holdings",     "NFT collection tracking with floor prices and cost basis. Optional — leave empty if you're not into NFTs.",
         "NFT Holdings"),
        ("👀 Watchlist",         "Coins you're tracking for entry. Set a Target Entry price and the Entry Signal formula flips to 🟢 BUY when current price <= target.",
         "Watchlist"),
        ("🧠 Research Notes",   "Deep-dive research on coins before buying. Link from Watchlist entries. Decision: Buy / Watchlist / Pass / Sold / Revisit.",
         "Research Notes"),
        ("🪂 Airdrops Tracker", "Farming status, eligibility criteria, tasks done, and claim deadlines. Relate to the Wallets you're farming from.",
         "Airdrops Tracker"),
        ("📈 Balance Snapshots","Daily portfolio value history. The sync script writes one row per day — use this DB as the data source for charts and day-over-day P&L.",
         "Balance Snapshots"),
        ("⚙️ Settings",         "Config: refresh frequency, dust threshold, chains enabled, tax lot method. The sync script reads this row on every cycle.",
         "Settings"),
    ]

    for heading, description, db_key in sections:
        blocks.append(h2(heading))
        blocks.append(para(description))
        blocks.append(link_to_db(DB_IDS[db_key]))
        blocks.append(divider())

    # Setup + sync
    blocks.append(h2("🔧 Setup & Sync"))
    blocks.append(callout(
        "First time here? Follow the setup guide to get your free Moralis + "
        "Helius API keys and connect them to the sync script. Takes about 10 minutes.",
        emoji="📖", color="yellow_background",
    ))

    blocks.append(h3("Setup steps"))
    blocks.append(bullet("Create a free Moralis account → deep-index.moralis.io → copy API key"))
    blocks.append(bullet("Create a free Helius account → helius.dev → copy API key"))
    blocks.append(bullet("Create a Notion integration → notion.so/my-integrations → copy token"))
    blocks.append(bullet("Share this page with your new integration (… menu → Connections)"))
    blocks.append(bullet("Paste all three keys into the sync script's .env file"))

    blocks.append(h3("Sync options"))
    blocks.append(bullet("Python script (recommended): run sync_daemon.py on any VPS / Raspberry Pi / local machine — polls Notion every 60 seconds"))
    blocks.append(bullet("n8n workflow: import the provided JSON into your own n8n instance and activate"))
    blocks.append(bullet("Manual: update Holdings amounts yourself — formulas still calculate P&L"))

    blocks.append(divider())

    # Tips
    blocks.append(h2("💡 Tips"))
    blocks.append(bullet("Uncheck Scan Enabled on a wallet to pause syncing without deleting it"))
    blocks.append(bullet("The Watchlist Entry Signal column turns 🟢 when current price drops below your Target Entry"))
    blocks.append(bullet("Tag holdings with Conviction (Core / Trade / Farming / Speculation / Dust) to filter quickly"))
    blocks.append(bullet("Use Balance Snapshots + a line chart (Notion 2.0 charts or an embed) to visualize portfolio value over time"))
    blocks.append(bullet("Filter Transactions by date range to check monthly P&L or tax-year activity"))

    blocks.append(divider())

    # Footer
    blocks.append(callout(
        "Questions? Bugs? Feature requests? Check the setup guide or reply to "
        "the Gumroad receipt email — you'll hear back within 24h.",
        emoji="💬", color="gray_background",
    ))

    return blocks


def append_blocks(page_id: str, blocks: list) -> None:
    # Notion API caps at 100 blocks per request; batch if needed
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        r = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=HEADERS, json={"children": chunk}, timeout=30,
        )
        if r.status_code >= 300:
            print(f"[ERR] append: {r.text[:500]}", file=sys.stderr)
            sys.exit(1)


def create_dashboard_page(parent_id: str) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": {"title": [{"type": "text", "text": {"content": "📊 Dashboard"}}]},
        "icon": {"type": "emoji", "emoji": "📊"},
    }
    r = requests.post(f"{NOTION_API}/pages", headers=HEADERS, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"[ERR] create page: {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    return r.json()["id"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("parent", help="Parent Notion page URL or ID")
    args = p.parse_args()

    parent_id = extract_page_id(args.parent)
    print(f"Parent page ID: {parent_id}")

    print("Creating dashboard page...")
    dash_id = create_dashboard_page(parent_id)
    print(f"  ✓ dashboard: {dash_id}")

    print("Building blocks...")
    blocks = build_blocks()
    print(f"  {len(blocks)} blocks")

    print("Appending to page...")
    append_blocks(dash_id, blocks)
    print("  ✓ appended")

    print(f"\nDone. Open Notion and look for '📊 Dashboard' under the parent page.")
    print("Manual polish: select each 'Open Holdings' (etc) link → Turn into → Linked view of database.")


if __name__ == "__main__":
    main()
