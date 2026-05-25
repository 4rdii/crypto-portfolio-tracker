#!/usr/bin/env python3
"""
Standalone multi-chain wallet balance scanner.
Mirrors the n8n workflow logic so we can verify the full pipeline end-to-end.

Usage:
    python3 scanner.py <address> [--label LABEL]
    python3 scanner.py 0xcB1C1FdE09f811B294172696404e88E658659905 --label "Test ETH"
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

def _find_env():
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.exists():
            return candidate
    return here / ".env"

ENV_PATH = _find_env()

def load_env():
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
# Legacy single-key var kept for back-compat. All Moralis calls now go through
# the moralis_keys rotator, which also honors MORALIS_API_KEYS (comma list).
MORALIS_API_KEY = ENV.get("MORALIS_API_KEY", os.getenv("MORALIS_API_KEY", ""))
HELIUS_API_KEY = ENV.get("HELIUS_API_KEY", os.getenv("HELIUS_API_KEY", ""))
BIRDEYE_API_KEY = ENV.get("BIRDEYE_API_KEY", os.getenv("BIRDEYE_API_KEY", ""))

from moralis_keys import get_moralis_key, mark_key_failed


def _moralis_get(url: str, params: dict | None = None, timeout: int = 30,
                 max_retries: int = 3):
    """GET wrapper that rotates the Moralis API key on rate-limit / auth errors.

    Retries up to `max_retries` times, marking each failing key for cooldown
    so the rotator stops handing it out. Returns the final Response object
    regardless — caller can still raise_for_status() on a persistent failure.
    """
    last = None
    for attempt in range(max_retries):
        key = get_moralis_key()
        try:
            r = requests.get(
                url,
                params=params,
                headers={"X-API-Key": key, "accept": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException:
            mark_key_failed(key)
            last = None
            continue
        last = r
        # 429 = rate limit, 401 = key invalid/revoked, 402 = quota blown.
        # Cool the key down and try the next one.
        if r.status_code in (401, 402, 429):
            mark_key_failed(key)
            continue
        return r
    return last

# Core chains — always scanned for balances and history.
EVM_CHAINS = ["eth", "polygon", "bsc", "arbitrum", "optimism", "base"]

# Extended chains supported by Moralis. Users can enable these per-wallet
# via the chain toggles in the UI. The scanner/history modules accept any
# slug from this list.
EVM_CHAINS_EXTENDED = [
    "eth", "polygon", "bsc", "arbitrum", "optimism", "base",
    "avalanche", "fantom", "cronos", "gnosis", "linea",
    "moonbeam", "moonriver", "pulsechain", "ronin", "lisk",
    "flow", "chiliz", "hyperevm",
]

# Chain ID map for all supported chains (used by wallet-pay + frontend).
ALL_EVM_CHAIN_IDS = {
    "eth": 1, "polygon": 137, "bsc": 56, "arbitrum": 42161,
    "optimism": 10, "base": 8453, "avalanche": 43114, "fantom": 250,
    "cronos": 25, "gnosis": 100, "linea": 59144, "moonbeam": 1284,
    "moonriver": 1285, "pulsechain": 369, "ronin": 2020, "lisk": 1135,
    "flow": 747, "chiliz": 88888, "hyperevm": 999,
}
DUST_USD = 1.0

# Aave aTokens that Moralis labels "Aave <network> <SYMBOL>" — e.g. "Aave
# Ethereum WBTC" (v3), "Aave Polygon USDC" (v3), "Aave v3 USDC" (generic), or
# the v2 "Aave interest bearing <SYM>". They are ERC-20s so Moralis returns the
# balance just fine, but it does NOT price them (returns usd_price=0), which
# means the normal "price <= 0 → drop" filter would silently eat a user's
# entire Aave deposit. We special-case them below:
#
#   1. Recognise the token by name prefix "Aave " (covers v1/v2/v3 on every
#      chain Aave is deployed on — unambiguous because "Aave" is a trademarked
#      protocol name, not a word anyone else uses in a token name).
#   2. Parse the underlying symbol from the last whitespace-separated chunk
#      ("Aave Ethereum WBTC" → "WBTC", "Aave v3 USDC" → "USDC").
#   3. Re-price by looking up the underlying symbol — first in the same-chain
#      results we already have (no extra API call), then falling back to a
#      Moralis metadata+price lookup if the user doesn't also hold the plain
#      asset alongside their deposit.
#   4. aToken balances are 1:1 rebasing claims on the underlying (the balance
#      grows as interest accrues — Aave rebases the token itself), so
#      `balance × underlying_price` is the correct USD value.
#
# Debt tokens (variable/stable borrow receipts) have "Debt" somewhere in the
# name — we skip those for now since we have no UI to show negative positions.
_AAVE_NAME_PREFIX = "aave "
_AAVE_DEBT_MARKERS = ("debt", "variable debt", "stable debt")


def _is_aave_atoken(name: str, symbol: str) -> bool:
    if not name:
        return False
    low = name.lower()
    if not low.startswith(_AAVE_NAME_PREFIX):
        return False
    if any(m in low for m in _AAVE_DEBT_MARKERS):
        return False
    return True


def _aave_underlying_symbol(name: str, symbol: str) -> str | None:
    """Extract the underlying asset symbol from an Aave aToken name.

    Moralis uses "Aave <network> <SYMBOL>" (v3) or "Aave interest bearing
    <SYMBOL>" (v2). In both cases the last whitespace-separated chunk is the
    underlying symbol. As a safety net, if the name parse fails, try stripping
    the "a<NetworkPrefix>" part from the symbol itself (aEthWBTC → WBTC).
    """
    if name:
        parts = name.strip().split()
        if parts:
            candidate = parts[-1].upper()
            # Guard against pathological "Aave USD Coin" (unlikely but safer).
            if candidate.isalnum() and 2 <= len(candidate) <= 10:
                return candidate
    if symbol and symbol.startswith("a") and len(symbol) > 3:
        # aEthWBTC → WBTC  (strip leading 'a' then any CamelCase network prefix)
        rest = symbol[1:]
        for prefix in ("Eth", "Pol", "Arb", "Opt", "Bas", "Bnb", "Avax", "Fan"):
            if rest.startswith(prefix):
                return rest[len(prefix):].upper()
        return rest.upper()
    return None


def _moralis_lookup_price_by_symbol(chain: str, symbol: str) -> tuple[float, float]:
    """Resolve (price, 24h_change) for a symbol on a chain via Moralis metadata.

    Falls back when we can't reuse a price from the same scan (e.g. the user
    has aEthWBTC but zero plain WBTC sitting in their wallet). Makes at most
    two Moralis calls: one to look up the canonical contract address for the
    symbol, then one to price that contract. Returns (0, 0) on any failure —
    the caller should treat that as "give up on this position".
    """
    try:
        mr = _moralis_get(
            "https://deep-index.moralis.io/api/v2.2/erc20/metadata/symbols",
            params={"chain": chain, "symbols": symbol},
            timeout=20,
        )
        if mr is None:
            return 0.0, 0.0
        mr.raise_for_status()
        meta = mr.json()
        if not meta or not isinstance(meta, list):
            return 0.0, 0.0
        # Moralis returns multiple candidates (anyone can deploy a token with
        # the same symbol). Prefer a verified, non-spam entry.
        best = None
        for m in meta:
            if m.get("possible_spam"):
                continue
            if m.get("verified_contract") is False:
                continue
            best = m
            break
        best = best or meta[0]
        contract = best.get("address")
        if not contract:
            return 0.0, 0.0
        pr = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/erc20/{contract}/price",
            params={"chain": chain},
            timeout=20,
        )
        if pr is None:
            return 0.0, 0.0
        pr.raise_for_status()
        pd = pr.json()
        return (
            float(pd.get("usdPrice") or 0),
            float(pd.get("24hrPercentChange") or 0),
        )
    except Exception as e:
        print(f"  [WARN] Moralis symbol-lookup {chain}/{symbol} failed: {e}", file=sys.stderr)
        return 0.0, 0.0


def detect_chain(address: str) -> str:
    if re.match(r"^0x[a-fA-F0-9]{40}$", address):
        return "evm"
    if re.match(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$", address):
        return "bitcoin"
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", address):
        return "solana"
    return "unknown"


def scan_evm(address: str, label: str) -> list[dict]:
    """Loop EVM chains and fetch ERC-20 balances with prices from Moralis.

    Two-pass per chain:
      Pass 1 — classify every token Moralis returns into plain holdings (priced
      by Moralis, passes spam/dust filters) vs. Aave aTokens (un-priced wrapper
      receipts for deposits). Plain holdings are finalised immediately.
      Pass 2 — for each aToken, resolve the underlying symbol's price. We first
      check the plain holdings we just collected on the same chain (cheap: no
      extra API call if the user also holds the unwrapped asset). If that
      misses, fall back to a two-call Moralis metadata+price lookup.

    Positions are tagged with ``protocol`` so the UI can render an "Aave"
    badge. Aave balances are aggregated with the underlying symbol (e.g. a
    0.2 aEthWBTC deposit reports as WBTC on-chain in the rollup views), which
    keeps the "Top holdings" breakdown semantically correct and lets realized-
    P&L bookkeeping treat deposits/withdrawals as internal transfers later on.
    """
    rows: list[dict] = []
    for chain in EVM_CHAINS:
        try:
            r = _moralis_get(
                f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/tokens",
                params={"chain": chain},
                timeout=30,
            )
            if r is None:
                raise RuntimeError("no Moralis response (all keys exhausted)")
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  [WARN] Moralis {chain} failed: {e}", file=sys.stderr)
            continue

        chain_rows: list[dict] = []          # plain holdings, already priced
        aave_candidates: list[dict] = []     # raw moralis entries needing re-pricing

        # ---- Pass 1: classify ----
        for t in data.get("result", []):
            name = t.get("name") or ""
            symbol = t.get("symbol") or ""
            balance = float(t.get("balance_formatted") or 0)
            if balance <= 0:
                continue

            if _is_aave_atoken(name, symbol):
                # Queue it for pass 2 — even if verified_contract was false or
                # possible_spam was true (which aTokens sometimes get flagged
                # as on newer Moralis endpoints, because the aToken factory is
                # not always in Moralis' verified list per chain).
                aave_candidates.append(t)
                continue

            if t.get("possible_spam"):
                continue
            if t.get("verified_contract") is False:
                continue
            price = float(t.get("usd_price") or 0)
            value = float(t.get("usd_value") or balance * price)
            if price <= 0:
                continue
            if value < DUST_USD:
                continue
            chain_rows.append({
                "wallet_label": label,
                "wallet_address": address,
                "chain": chain,
                "token_symbol": symbol,
                "token_name": name,
                "contract": t.get("token_address") or "native",
                "balance": balance,
                "decimals": t.get("decimals"),
                "usd_price": price,
                "usd_value": value,
                "price_24h_change": float(t.get("usd_price_24hr_percent_change") or 0),
                "native": bool(t.get("native_token")),
                "source": "moralis",
                "protocol": None,
            })

        # ---- Pass 2: re-price aTokens ----
        # Build a {symbol -> (price, 24h)} map from the plain holdings on this
        # chain so we can skip the metadata lookup when the user also holds
        # the underlying asset directly.
        chain_prices: dict[str, tuple[float, float]] = {}
        for r_ in chain_rows:
            sym = (r_["token_symbol"] or "").upper()
            if sym and sym not in chain_prices:
                chain_prices[sym] = (r_["usd_price"], r_["price_24h_change"])

        for t in aave_candidates:
            name = t.get("name") or ""
            symbol = t.get("symbol") or ""
            balance = float(t.get("balance_formatted") or 0)
            underlying = _aave_underlying_symbol(name, symbol)
            if not underlying:
                print(
                    f"  [WARN] Could not parse Aave underlying from "
                    f"name={name!r} symbol={symbol!r}",
                    file=sys.stderr,
                )
                continue
            price, change = chain_prices.get(underlying, (0.0, 0.0))
            if price <= 0:
                # Fallback: ask Moralis for the canonical contract + price.
                price, change = _moralis_lookup_price_by_symbol(chain, underlying)
                if price > 0:
                    chain_prices[underlying] = (price, change)
            if price <= 0:
                print(
                    f"  [WARN] Could not price Aave underlying "
                    f"{underlying} on {chain} — dropping",
                    file=sys.stderr,
                )
                continue
            value = balance * price
            if value < DUST_USD:
                continue
            chain_rows.append({
                "wallet_label": label,
                "wallet_address": address,
                "chain": chain,
                # Report as the UNDERLYING symbol so the position aggregates
                # with any wallet-held underlying in "Top holdings" / P&L.
                "token_symbol": underlying,
                "token_name": name,
                "contract": t.get("token_address") or "native",
                "balance": balance,
                "decimals": t.get("decimals"),
                "usd_price": price,
                "usd_value": value,
                "price_24h_change": change,
                "native": False,
                "source": "moralis+aave",
                "protocol": "aave",
            })

        rows.extend(chain_rows)
    return rows


def scan_solana(address: str, label: str) -> list[dict]:
    """Fetch SOL + SPL tokens via Helius getAssetsByOwner."""
    try:
        r = requests.post(
            f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}",
            json={
                "jsonrpc": "2.0",
                "id": "portfolio-tracker",
                "method": "getAssetsByOwner",
                "params": {
                    "ownerAddress": address,
                    "page": 1,
                    "limit": 1000,
                    "displayOptions": {
                        "showFungible": True,
                        "showNativeBalance": True,
                        "showZeroBalance": False,
                    },
                },
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [ERR] Helius call failed: {e}", file=sys.stderr)
        return []

    result = data.get("result", {}) or {}
    rows = []

    nb = result.get("nativeBalance") or {}
    lamports = nb.get("lamports", 0)
    if lamports:
        sol_balance = lamports / 1e9
        sol_price = float(nb.get("price_per_sol") or 0)
        sol_value = float(nb.get("total_price") or sol_balance * sol_price)
        if sol_value >= 0.01:
            rows.append({
                "wallet_label": label,
                "wallet_address": address,
                "chain": "solana",
                "token_symbol": "SOL",
                "token_name": "Solana",
                "contract": "native",
                "balance": sol_balance,
                "decimals": 9,
                "usd_price": sol_price,
                "usd_value": sol_value,
                "native": True,
                "source": "helius",
            })

    # Collect items first, then batch-fetch missing prices via Birdeye.
    pending = []
    for t in result.get("items", []):
        if t.get("interface") not in ("FungibleToken", "FungibleAsset"):
            continue
        info = t.get("token_info") or {}
        meta = (t.get("content") or {}).get("metadata") or {}
        decimals = info.get("decimals") or 0
        balance_raw = info.get("balance") or 0
        balance = float(balance_raw) / (10 ** decimals) if decimals else float(balance_raw)
        if balance <= 0:
            continue
        pi = info.get("price_info") or {}
        price = float(pi.get("price_per_token") or 0)
        value = float(pi.get("total_price") or balance * price)
        pending.append({
            "wallet_label": label,
            "wallet_address": address,
            "chain": "solana",
            "token_symbol": info.get("symbol") or meta.get("symbol") or "?",
            "token_name": meta.get("name") or info.get("symbol") or "?",
            "contract": t.get("id"),
            "balance": balance,
            "decimals": decimals,
            "usd_price": price,
            "usd_value": value,
            "native": False,
            "source": "helius",
        })

    # Birdeye fallback for tokens Helius has no price for (e.g. new xStock mints).
    if BIRDEYE_API_KEY:
        no_price = [r for r in pending if r["usd_price"] == 0 and r["contract"]]
        for row in no_price:
            try:
                bp = requests.get(
                    "https://public-api.birdeye.so/defi/price",
                    params={"address": row["contract"]},
                    headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"},
                    timeout=8,
                )
                if bp.ok:
                    bird_price = float((bp.json().get("data") or {}).get("value") or 0)
                    if bird_price > 0:
                        row["usd_price"] = bird_price
                        row["usd_value"] = bird_price * row["balance"]
            except Exception:
                pass

    for row in pending:
        # Apply dust filter only when we have price data.
        if row["usd_price"] > 0 and row["usd_value"] < DUST_USD:
            continue
        rows.append(row)
    return rows


def scan_bitcoin(address: str, label: str) -> list[dict]:
    """Fetch BTC balance from mempool.space + price from CoinGecko."""
    try:
        r = requests.get(f"https://mempool.space/api/address/{address}", timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [ERR] mempool.space call failed: {e}", file=sys.stderr)
        return []

    cs = data.get("chain_stats", {})
    ms = data.get("mempool_stats", {})
    sats = (cs.get("funded_txo_sum", 0) - cs.get("spent_txo_sum", 0)) + (
        ms.get("funded_txo_sum", 0) - ms.get("spent_txo_sum", 0)
    )
    btc = sats / 1e8
    if btc <= 0:
        return []

    try:
        pr = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=15,
        )
        pr.raise_for_status()
        pdata = pr.json().get("bitcoin", {})
    except Exception as e:
        print(f"  [WARN] CoinGecko price fetch failed: {e}", file=sys.stderr)
        pdata = {}

    price = float(pdata.get("usd") or 0)
    change = float(pdata.get("usd_24h_change") or 0)
    value = btc * price
    return [{
        "wallet_label": label,
        "wallet_address": address,
        "chain": "bitcoin",
        "token_symbol": "BTC",
        "token_name": "Bitcoin",
        "contract": "native",
        "balance": btc,
        "decimals": 8,
        "balance_sats": sats,
        "usd_price": price,
        "usd_value": value,
        "price_24h_change": change,
        "native": True,
        "source": "mempool+coingecko",
    }]


def scan(address: str, label: str = "Wallet") -> list[dict]:
    chain = detect_chain(address)
    print(f"\n[scanner] Address: {address}")
    print(f"[scanner] Detected chain: {chain}")
    print(f"[scanner] Label: {label}\n")

    if chain == "evm":
        return scan_evm(address, label)
    if chain == "solana":
        return scan_solana(address, label)
    if chain == "bitcoin":
        return scan_bitcoin(address, label)
    print(f"[scanner] Unknown chain for address: {address}", file=sys.stderr)
    return []


def print_results(rows: list[dict]):
    if not rows:
        print("No holdings found (or all below dust threshold).")
        return
    total = sum(r["usd_value"] for r in rows)
    print(f"Found {len(rows)} positions  |  Total: ${total:,.2f}\n")
    print(f"{'CHAIN':10s} {'SYMBOL':12s} {'BALANCE':>18s}  {'USD VALUE':>14s}  {'PRICE':>12s}")
    print("-" * 74)
    for r in sorted(rows, key=lambda x: -x["usd_value"]):
        print(
            f"{r['chain']:10s} {r['token_symbol']:12s} "
            f"{r['balance']:>18.6f}  ${r['usd_value']:>12,.2f}  "
            f"${r['usd_price']:>10.4f}"
        )
    print()


def main():
    p = argparse.ArgumentParser(description="Multi-chain wallet balance scanner")
    p.add_argument("address", help="Wallet address (EVM / Solana / Bitcoin)")
    p.add_argument("--label", default="Wallet", help="Wallet label")
    p.add_argument("--json", action="store_true", help="Output JSON instead of table")
    args = p.parse_args()

    rows = scan(args.address, args.label)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print_results(rows)


if __name__ == "__main__":
    main()
