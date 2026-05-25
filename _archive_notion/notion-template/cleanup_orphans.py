#!/usr/bin/env python3
"""
Archive Holdings / DeFi Positions / NFT Holdings / Transactions rows whose
Wallet relation is empty (orphaned because the Wallet row was deleted).

Notion relations do NOT cascade-delete — removing a Wallet leaves its
child rows pointing at nothing. This script walks each wallet-backed DB,
finds rows with an empty Wallet relation, and archives them.

Usage:
    python3 cleanup_orphans.py           # dry-run, shows what would be archived
    python3 cleanup_orphans.py --apply   # actually archive
"""
import argparse
import json
import os
import sys
import time
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

# DBs that have a "Wallet" relation and should purge orphans
WALLET_BACKED_DBS = [
    ("Holdings",       "Wallet"),
    ("DeFi Positions", "Wallet"),
    ("NFT Holdings",   "Wallet"),
    ("Transactions",   "Wallet"),
]


def query_all(db_id: str) -> list[dict]:
    pages = []
    cursor = None
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
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def archive_page(page_id: str) -> None:
    r = requests.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=HEADERS, json={"archived": True}, timeout=30,
    )
    r.raise_for_status()


def page_title(page: dict) -> str:
    for _, prop in page.get("properties", {}).items():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            if parts:
                return "".join(p.get("plain_text", "") for p in parts)
    return "(untitled)"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually archive rows (default: dry-run)")
    args = p.parse_args()

    total_orphans = 0
    for db_name, wallet_prop in WALLET_BACKED_DBS:
        db_id = DB_IDS.get(db_name)
        if not db_id:
            print(f"[SKIP] {db_name} — not in db_ids.json")
            continue
        print(f"\n[{db_name}]")
        pages = query_all(db_id)
        orphans = []
        for page in pages:
            rel = page.get("properties", {}).get(wallet_prop, {})
            if rel.get("type") != "relation":
                continue
            if len(rel.get("relation", [])) == 0:
                orphans.append(page)
        total_orphans += len(orphans)
        if not orphans:
            print(f"  ✓ no orphans ({len(pages)} rows total)")
            continue
        print(f"  Found {len(orphans)} orphan row(s) out of {len(pages)}:")
        for op in orphans:
            print(f"    - {page_title(op)}  ({op['id']})")
            if args.apply:
                archive_page(op["id"])
                time.sleep(0.15)
        if args.apply:
            print(f"  → archived {len(orphans)}")

    print()
    if args.apply:
        print(f"Done. Archived {total_orphans} orphan row(s).")
    else:
        print(f"Dry-run. {total_orphans} orphan row(s) would be archived.")
        print("Re-run with --apply to actually remove them.")


if __name__ == "__main__":
    main()
