#!/usr/bin/env python3
"""
Build a BI-dashboard-style Notion page.

Layout (top to bottom):
  1. Hero: big title + last-updated subtitle
  2. KPI row (4 columns): Total Value, 24h P&L, Unrealized P&L, Wallet Count
  3. Charts row (2 columns): Portfolio trend (line) | Chain allocation (doughnut)
  4. Category row (2 columns): Holdings (link to DB) | DeFi Positions (link)
  5. Airdrops row (3 columns): Farming | Claimable | Snapshot
  6. Watchlist + Research row (2 columns)
  7. Recent activity: Transactions link, full width
  8. Drill-downs: toggle blocks for each DB with link inside
  9. Setup / Settings footer

Charts are rendered via QuickChart.io — static demo data baked in for the
template so screenshots look good. For the live version, the sync daemon can
rewrite these image URLs each cycle with real data.
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
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
    raise ValueError(raw)


# ---- rich-text builders ------------------------------------------------------
def rt(text: str, bold=False, code=False, color="default"):
    return {
        "type": "text",
        "text": {"content": text},
        "annotations": {
            "bold": bold, "italic": False, "strikethrough": False,
            "underline": False, "code": code, "color": color,
        },
    }


# ---- block builders ----------------------------------------------------------
def h1(text: str):
    return {"object": "block", "type": "heading_1",
            "heading_1": {"rich_text": [rt(text)]}}


def h2(text: str):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [rt(text)]}}


def h3(text: str):
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [rt(text)]}}


def para(text: str, color="default"):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [rt(text, color=color)]}}


def para_mix(segments: list):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": segments}}


def callout_rich(segments: list, emoji: str = "💡", color: str = "gray_background"):
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": segments,
            "icon": {"type": "emoji", "emoji": emoji},
            "color": color,
        },
    }


def callout(text: str, emoji: str = "💡", color: str = "gray_background"):
    return callout_rich([rt(text)], emoji, color)


def kpi_card(label: str, value: str, trend: str, emoji: str, color: str, trend_color: str = "default"):
    """A KPI card = colored callout with a label line (small) + value line (big) + trend line."""
    return callout_rich(
        [
            rt(label + "\n", color="gray"),
            rt(value, bold=True),
            rt("\n" + trend, color=trend_color),
        ],
        emoji=emoji, color=color,
    )


def divider():
    return {"object": "block", "type": "divider", "divider": {}}


def bullet(text: str):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [rt(text)]}}


def todo(text: str, checked: bool = False):
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [rt(text)], "checked": checked}}


def toggle(text: str, children: list):
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": [rt(text, bold=True)], "children": children}}


def link_to_db(db_id: str):
    return {"object": "block", "type": "link_to_page",
            "link_to_page": {"type": "database_id", "database_id": db_id}}


def columns(*column_blocks):
    """Build a column_list containing N columns, each wrapping a list of blocks."""
    cols = []
    for col_blocks in column_blocks:
        cols.append({
            "object": "block", "type": "column",
            "column": {"children": col_blocks},
        })
    return {"object": "block", "type": "column_list",
            "column_list": {"children": cols}}


def image(url: str, caption: str = ""):
    blk = {"object": "block", "type": "image",
           "image": {"type": "external", "external": {"url": url}}}
    if caption:
        blk["image"]["caption"] = [rt(caption)]
    return blk


def quote(text: str):
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": [rt(text)]}}


# ---- QuickChart URL builders -------------------------------------------------
QC_BASE = "https://quickchart.io/chart"


def qc_url(config: dict, width: int = 600, height: int = 300, bg: str = "white") -> str:
    c = urllib.parse.quote(json.dumps(config, separators=(",", ":")))
    return f"{QC_BASE}?w={width}&h={height}&bkg={bg}&c={c}"


def chart_portfolio_trend() -> str:
    # synthetic 30-day portfolio value trend matching the snapshot seed data
    labels = [f"D-{30 - i}" for i in range(31)]
    base, growth = 38000, 9200
    import math
    values = []
    for i in range(31):
        progress = i / 30
        v = base + growth * progress + math.sin((30 - i) * 0.7) * 900
        values.append(round(v))
    cfg = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "Portfolio Value (USD)",
                "data": values,
                "fill": True,
                "borderColor": "#4F46E5",
                "backgroundColor": "rgba(79, 70, 229, 0.12)",
                "tension": 0.35,
                "pointRadius": 0,
                "borderWidth": 3,
            }],
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "title": {"display": True, "text": "Portfolio Value — Last 30 Days", "font": {"size": 16}},
            },
            "scales": {
                "y": {"ticks": {"callback": "$value"}, "beginAtZero": False},
                "x": {"ticks": {"maxTicksLimit": 6}},
            },
        },
    }
    return qc_url(cfg, width=700, height=320)


def chart_chain_allocation() -> str:
    cfg = {
        "type": "doughnut",
        "data": {
            "labels": ["Ethereum", "Bitcoin", "Solana", "Base", "Arbitrum", "Optimism"],
            "datasets": [{
                "data": [22100, 22400, 10500, 4500, 1190, 894],
                "backgroundColor": [
                    "#627EEA",  # ETH
                    "#F7931A",  # BTC
                    "#9945FF",  # SOL
                    "#0052FF",  # Base
                    "#28A0F0",  # ARB
                    "#FF0420",  # OP
                ],
                "borderWidth": 2,
                "borderColor": "#ffffff",
            }],
        },
        "options": {
            "plugins": {
                "legend": {"position": "right", "labels": {"font": {"size": 12}}},
                "title": {"display": True, "text": "Allocation by Chain", "font": {"size": 16}},
            },
            "cutout": "60%",
        },
    }
    return qc_url(cfg, width=620, height=320)


def chart_pnl_by_position() -> str:
    cfg = {
        "type": "bar",
        "data": {
            "labels": ["BTC", "ETH", "PENDLE", "WIF", "SOL", "BRETT", "LDO", "ENA", "ARB", "OP"],
            "datasets": [{
                "label": "Unrealized P&L (USD)",
                "data": [5880, 2370, 895, 81, 540, 408, 115, -616, -397, -111],
                "backgroundColor": [
                    "#10B981" if x >= 0 else "#EF4444"
                    for x in [5880, 2370, 895, 81, 540, 408, 115, -616, -397, -111]
                ],
                "borderWidth": 0,
            }],
        },
        "options": {
            "plugins": {
                "legend": {"display": False},
                "title": {"display": True, "text": "Unrealized P&L by Position", "font": {"size": 16}},
            },
            "scales": {
                "y": {"ticks": {"callback": "$value"}},
            },
        },
    }
    return qc_url(cfg, width=700, height=320)


def chart_category_split() -> str:
    cfg = {
        "type": "polarArea",
        "data": {
            "labels": ["L1", "L2", "Stablecoin", "DeFi Blue Chip", "LST", "Meme"],
            "datasets": [{
                "data": [44600, 1600, 6620, 6200, 2510, 4080],
                "backgroundColor": [
                    "rgba(79, 70, 229, 0.7)",
                    "rgba(16, 185, 129, 0.7)",
                    "rgba(245, 158, 11, 0.7)",
                    "rgba(168, 85, 247, 0.7)",
                    "rgba(236, 72, 153, 0.7)",
                    "rgba(239, 68, 68, 0.7)",
                ],
            }],
        },
        "options": {
            "plugins": {
                "legend": {"position": "right"},
                "title": {"display": True, "text": "Category Breakdown", "font": {"size": 16}},
            },
        },
    }
    return qc_url(cfg, width=620, height=320)


# ---- page composition --------------------------------------------------------
def build_blocks() -> list:
    blocks = []

    # === 1. HERO ===
    blocks.append(h1("Portfolio Command Center"))
    blocks.append(callout_rich(
        [
            rt("📊 ", bold=True),
            rt("Live multi-chain portfolio tracker. ", bold=True),
            rt("Tracks spot positions, DeFi yield, NFTs, airdrop farms, and research in one place. Auto-syncs from on-chain data every 15 minutes.", color="gray"),
        ],
        emoji="⚡", color="blue_background",
    ))

    blocks.append(divider())

    # === 2. KPI ROW (4 columns) ===
    blocks.append(h2("📈 At a Glance"))

    kpi_total = kpi_card(
        "TOTAL PORTFOLIO",
        "$68,957",
        "↗ +$1,247 (1.84%) today",
        emoji="💰", color="green_background", trend_color="green",
    )
    kpi_24h = kpi_card(
        "UNREALIZED P&L",
        "+$9,158",
        "↗ +15.3% all-time",
        emoji="📊", color="blue_background", trend_color="blue",
    )
    kpi_defi = kpi_card(
        "DEFI EARNING",
        "$12,993",
        "18.8% of portfolio, blended 11.5% APY",
        emoji="🌾", color="purple_background", trend_color="purple",
    )
    kpi_wallets = kpi_card(
        "ACTIVE WALLETS",
        "5",
        "last scan: a few seconds ago",
        emoji="👛", color="gray_background", trend_color="gray",
    )

    blocks.append(columns(
        [kpi_total],
        [kpi_24h],
        [kpi_defi],
        [kpi_wallets],
    ))

    blocks.append(divider())

    # === 3. CHARTS ROW (2 columns) ===
    blocks.append(h2("📉 Trends & Allocation"))

    blocks.append(columns(
        [image(chart_portfolio_trend(), "Portfolio value, last 30 days")],
        [image(chart_chain_allocation(),  "Allocation by chain")],
    ))

    blocks.append(columns(
        [image(chart_pnl_by_position(), "Top gainers and losers")],
        [image(chart_category_split(),  "Category breakdown")],
    ))

    blocks.append(divider())

    # === 4. PRIMARY SECTIONS (Holdings + DeFi) ===
    blocks.append(h2("💼 Positions"))

    blocks.append(columns(
        [
            h3("📊 Holdings"),
            para("18 positions across 6 chains — click to open the full database."),
            link_to_db(DB_IDS["Holdings"]),
        ],
        [
            h3("🌾 DeFi Positions"),
            para("6 active yield positions. Pendle PT, Jito restake, Aave lending, EigenLayer."),
            link_to_db(DB_IDS["DeFi Positions"]),
        ],
    ))

    blocks.append(divider())

    # === 5. AIRDROPS + WATCHLIST (3 columns) ===
    blocks.append(h2("🎯 Opportunity"))

    blocks.append(columns(
        [
            h3("🪂 Airdrop Farms"),
            callout_rich(
                [rt("5 active\n", color="gray"), rt("$2,300", bold=True),
                 rt(" estimated value", color="gray")],
                emoji="🌾", color="yellow_background",
            ),
            link_to_db(DB_IDS["Airdrops Tracker"]),
        ],
        [
            h3("👀 Watchlist"),
            callout_rich(
                [rt("6 coins tracked\n", color="gray"), rt("2 buy signals 🟢", bold=True)],
                emoji="🎯", color="green_background",
            ),
            link_to_db(DB_IDS["Watchlist"]),
        ],
        [
            h3("🧠 Research"),
            callout_rich(
                [rt("4 deep-dives\n", color="gray"), rt("1 high conviction", bold=True)],
                emoji="📚", color="purple_background",
            ),
            link_to_db(DB_IDS["Research Notes"]),
        ],
    ))

    blocks.append(divider())

    # === 6. WALLETS + TRANSACTIONS ===
    blocks.append(h2("🔁 Activity"))

    blocks.append(columns(
        [
            h3("👛 Wallets"),
            para("Main hot, cold storage, farming, degen, BTC vault."),
            link_to_db(DB_IDS["Wallets"]),
        ],
        [
            h3("📝 Recent Transactions"),
            para("12 logged trades over the last 6 months."),
            link_to_db(DB_IDS["Transactions"]),
        ],
    ))

    blocks.append(divider())

    # === 7. DRILL-DOWN TOGGLES ===
    blocks.append(h2("🔍 More Databases"))

    blocks.append(toggle("💲 Prices feed", [
        para("Live price feed — one row per token. Holdings and Watchlist pull from here via relations."),
        link_to_db(DB_IDS["Prices"]),
    ]))
    blocks.append(toggle("🖼️ NFT Holdings", [
        para("Floor price tracking with cost basis and unrealized P&L formulas."),
        link_to_db(DB_IDS["NFT Holdings"]),
    ]))
    blocks.append(toggle("📈 Balance Snapshots", [
        para("Daily portfolio value history — the data source for the trend chart above."),
        link_to_db(DB_IDS["Balance Snapshots"]),
    ]))
    blocks.append(toggle("⚙️ Settings", [
        para("Config: refresh frequency, dust threshold, chains enabled, tax lot method."),
        link_to_db(DB_IDS["Settings"]),
    ]))

    blocks.append(divider())

    # === 8. QUICK START + SETUP ===
    blocks.append(h2("🚀 Quick Start"))

    blocks.append(callout_rich(
        [
            rt("First time here? ", bold=True),
            rt("Follow the setup guide to connect your free API keys and start live-syncing. Takes ~10 minutes.", color="gray"),
        ],
        emoji="📖", color="yellow_background",
    ))

    blocks.append(todo("Read the setup guide (PDF in your download folder)", False))
    blocks.append(todo("Get free Moralis + Helius API keys", False))
    blocks.append(todo("Create a Notion integration and share this page with it", False))
    blocks.append(todo("Run `python3 get_db_ids.py` once to discover your DB IDs", False))
    blocks.append(todo("Start `sync_daemon.py` — new wallets auto-populate Holdings", False))
    blocks.append(todo("Populate Watchlist with coins you're tracking", False))
    blocks.append(todo("Log manual trades in Transactions for accurate cost basis", False))

    blocks.append(divider())

    # === 9. FOOTER ===
    blocks.append(callout_rich(
        [
            rt("Questions, bugs, feature requests? ", color="gray"),
            rt("Reply to your Gumroad receipt — answered within 24h.", bold=True),
        ],
        emoji="💬", color="gray_background",
    ))

    return blocks


def append_blocks(page_id: str, blocks: list) -> None:
    for i in range(0, len(blocks), 100):
        chunk = blocks[i:i + 100]
        r = requests.patch(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=HEADERS, json={"children": chunk}, timeout=30,
        )
        if r.status_code >= 300:
            print(f"[ERR] append: {r.text[:800]}", file=sys.stderr)
            sys.exit(1)


def create_dashboard_page(parent_id: str) -> str:
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "properties": {"title": [{"type": "text", "text": {"content": "📊 Dashboard"}}]},
        "icon": {"type": "emoji", "emoji": "📊"},
        "cover": {
            "type": "external",
            "external": {
                "url": "https://www.notion.so/images/page-cover/gradients_8.png"
            }
        },
    }
    r = requests.post(f"{NOTION_API}/pages", headers=HEADERS, json=body, timeout=30)
    if r.status_code >= 300:
        print(f"[ERR] create page: {r.text[:500]}", file=sys.stderr)
        sys.exit(1)
    return r.json()["id"]


def archive_existing_dashboards(parent_id: str) -> None:
    """Archive any existing subpages titled '📊 Dashboard' so we don't stack them."""
    r = requests.get(
        f"{NOTION_API}/blocks/{parent_id}/children",
        headers=HEADERS, params={"page_size": 100}, timeout=30,
    )
    if r.status_code >= 300:
        return
    for b in r.json().get("results", []):
        if b.get("type") == "child_page":
            title = b.get("child_page", {}).get("title", "")
            if "Dashboard" in title:
                requests.patch(
                    f"{NOTION_API}/pages/{b['id']}",
                    headers=HEADERS, json={"archived": True}, timeout=30,
                )
                print(f"  archived old: {title} ({b['id']})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("parent", help="Parent Notion page URL or ID")
    args = p.parse_args()

    parent_id = extract_page_id(args.parent)
    print(f"Parent page ID: {parent_id}")

    print("Archiving old dashboards...")
    archive_existing_dashboards(parent_id)

    print("Creating new BI dashboard...")
    dash_id = create_dashboard_page(parent_id)
    print(f"  ✓ dashboard: {dash_id}")

    print("Building blocks...")
    blocks = build_blocks()
    print(f"  {len(blocks)} top-level blocks")

    print("Appending...")
    append_blocks(dash_id, blocks)
    print("  ✓ done")

    page_slug = dash_id.replace("-", "")
    print(f"\n✓ BI dashboard live: https://www.notion.so/{page_slug}")


if __name__ == "__main__":
    main()
