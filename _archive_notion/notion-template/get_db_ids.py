#!/usr/bin/env python3
"""
Discover database IDs inside a duplicated Notion template.

When a buyer duplicates the template into their own workspace, every database
gets a brand-new ID. This script walks the parent page's children, finds the
11 databases by title, and writes db_ids.json so sync_daemon.py knows where
to write.

Usage:
    python3 get_db_ids.py <parent_page_url_or_id>

Requires:
    .env with NOTION_TOKEN
    The parent page must be shared with your Notion integration.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE.parent / ".env" if (HERE.parent / ".env").exists() else HERE / ".env"
DB_IDS_PATH = HERE / "db_ids.json"

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"

EXPECTED_DBS = [
    "Prices",
    "Wallets",
    "Holdings",
    "Transactions",
    "DeFi Positions",
    "NFT Holdings",
    "Watchlist",
    "Research Notes",
    "Airdrops Tracker",
    "Balance Snapshots",
    "Settings",
]


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


def list_children(block_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = requests.get(
            f"{NOTION_API}/blocks/{block_id}/children",
            headers=HEADERS, params=params, timeout=30,
        )
        if r.status_code >= 300:
            print(f"[ERR] list children: {r.text[:500]}", file=sys.stderr)
            sys.exit(1)
        data = r.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def db_title(db_id: str) -> str:
    r = requests.get(f"{NOTION_API}/databases/{db_id}", headers=HEADERS, timeout=30)
    if r.status_code >= 300:
        return ""
    parts = r.json().get("title", [])
    return "".join(p.get("plain_text", "") for p in parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("parent", help="Parent page URL or ID")
    args = p.parse_args()

    if not load_token():
        print("NOTION_TOKEN missing in .env — create a .env file with NOTION_TOKEN=ntn_...")
        sys.exit(1)

    parent_id = extract_page_id(args.parent)
    print(f"Walking children of {parent_id}...")

    children = list_children(parent_id)
    found = {}
    for block in children:
        if block.get("type") == "child_database":
            db_id = block["id"]
            title = block.get("child_database", {}).get("title") or db_title(db_id)
            if title in EXPECTED_DBS:
                found[title] = db_id
                print(f"  ✓ {title:20s} {db_id}")

    missing = [n for n in EXPECTED_DBS if n not in found]
    if missing:
        print(f"\n[WARN] missing databases: {', '.join(missing)}")
        print("Make sure all 11 databases from the template are direct children of the parent page,")
        print("and that the parent page is shared with your integration.")
        sys.exit(1)

    DB_IDS_PATH.write_text(json.dumps(found, indent=2))
    print(f"\n✓ wrote {DB_IDS_PATH}")
    print("Now run: python3 sync_daemon.py")


if __name__ == "__main__":
    main()
