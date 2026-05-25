"""FastAPI portfolio tracker webapp — multi-tenant with Web3 sign-in."""
import asyncio
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

import db
import pnl
import history
import portfolio_history
import auth
import pricing
import treasury_watcher
import wallet_payments
import sol_history as sol_history_mod
from applog import log
from rate_limit import rate_limit
from scanner import scan, detect_chain

BASE_DIR = Path(__file__).resolve().parent

# ---------- Global user caps ----------
# Prevents a single account from flooding the wallets/tx tables and burning
# shared resources (Moralis quota, DB disk, treasury polling). Tune upward
# as usage grows; these are "it's a personal portfolio tracker, not a CEX"
# sanity bounds.
MAX_WALLETS_PER_USER = 50
MAX_TX_AMOUNT = 1e15          # absurdly large but still fits float64
MAX_NOTE_LEN = 500
MAX_LABEL_LEN = 64
MAX_SYMBOL_LEN = 32
# Lifetime transaction cap per user. Past this we refuse new inserts —
# protects the DB from a runaway importer and keeps client-side rendering
# responsive until we ship server-side pagination.
MAX_TRANSACTIONS_PER_USER = 100_000
# Max import runs a single wallet can do in a rolling 30-day window.
# Prevents someone from grinding the delta-pricing down by running import
# back-to-back and triggering dozens of Moralis paginations.
MAX_IMPORTS_PER_WALLET_30D = 10

app = FastAPI(title="Crypto Portfolio Tracker")

# Session cookie: signed with a secret so users can't forge user_id values.
# Persisted across restarts via a file — regenerated if missing.
_secret_path = BASE_DIR / ".session_secret"
if _secret_path.exists():
    _session_secret = _secret_path.read_text().strip()
else:
    _session_secret = secrets.token_urlsafe(48)
    _secret_path.write_text(_session_secret)
    _secret_path.chmod(0o600)

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="portfolio_session",
    max_age=60 * 60 * 24 * 30,  # 30-day sessions
    same_site="lax",
    https_only=False,  # flip to True behind HTTPS
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
from fastapi.responses import FileResponse

jinja_env = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)

# Cache-busting version for /static assets. We bump this on every app start
# (using the latest mtime across the static dir) so a redeploy always
# invalidates whatever the browser has cached without relying on the user
# to hard-refresh. Recomputed lazily per render() so touching a file during
# development picks up immediately.
_STATIC_DIR = BASE_DIR / "static"

def _static_version() -> str:
    try:
        latest = 0.0
        for p in _STATIC_DIR.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
        return f"{int(latest)}"
    except Exception:
        return "0"


def render(name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("static_version", _static_version())
    return HTMLResponse(jinja_env.get_template(name).render(**ctx))


db.init_db()


@app.on_event("startup")
def _start_treasury_watcher():
    """Treasury watcher is DISABLED.

    We now require users to top up via the in-app wallet-pay flow which
    calls /api/billing/pay/verify synchronously. The background watcher
    was burning ~115K Moralis CU/day (6 chains × /history every 90s at
    ~20 CU per call) which blew through the free-plan daily cap in hours.
    Re-enable by uncommenting the call below if we ever go back to
    supporting unsolicited deposits as a fallback path.
    """
    # treasury_watcher.start_background_worker()
    log.info("startup: treasury watcher disabled (direct top-up only)")


# ---------- Background price backfill ---------------------------------------
#
# Keeps `price_cache` fresh with daily closes for the curated top-coins list
# WITHOUT requiring any user action. Runs once ~10s after startup (lets the
# webapp bind the port first and answer /health) and then every 24h until
# the app shuts down. Cancelled cleanly on shutdown so the process exits
# promptly.
#
# Also exposed as `_kick_top_coins_backfill()` — a fire-and-forget helper that
# `api_link_wallet` calls so a brand-new wallet's dashboard doesn't render
# against a cold cache. The helper debounces: if a backfill is already in
# flight it's a no-op (we don't want a burst of wallet links to fan out into
# N simultaneous Binance fetches).

_BACKFILL_INTERVAL_SEC = 24 * 3600   # refresh top coins once a day
_BACKFILL_STARTUP_DELAY = 10         # let the server come up before fetching
_BACKFILL_LOCK = asyncio.Lock()      # serialize on-demand kicks
_BACKFILL_TASK: asyncio.Task | None = None


async def _run_top_coins_backfill(reason: str) -> None:
    """Run the sync Binance top-coins backfill on a worker thread.

    `backfill_top_coins()` does ~60 HTTPS calls to Binance + sqlite writes;
    running it on the event loop would block `/api/health` and everything
    else for the duration. `run_in_executor(None, ...)` pushes it onto the
    default thread pool so request handling stays responsive.
    """
    try:
        from binance_prices import backfill_top_coins
    except Exception as e:
        log.warning("backfill.top_coins unavailable: %s", e)
        return
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, backfill_top_coins)
        log.info(
            "backfill.top_coins done reason=%s symbols=%d rows=%d",
            reason, len(result), sum(result.values()),
        )
    except asyncio.CancelledError:
        # Clean shutdown path — log and re-raise so the task exits.
        log.info("backfill.top_coins cancelled reason=%s", reason)
        raise
    except Exception as e:
        log.warning("backfill.top_coins failed reason=%s err=%s", reason, e)


async def _top_coins_backfill_loop() -> None:
    """Long-running: wait a bit, run once, sleep a day, run again.

    The loop is cancelled from the shutdown handler. Sleeping in the loop
    (not the handler) means cancellation actually kills an in-progress
    sleep immediately instead of having to wait out the interval.
    """
    try:
        await asyncio.sleep(_BACKFILL_STARTUP_DELAY)
        await _run_top_coins_backfill(reason="startup")
        while True:
            await asyncio.sleep(_BACKFILL_INTERVAL_SEC)
            await _run_top_coins_backfill(reason="daily")
    except asyncio.CancelledError:
        pass


def _kick_top_coins_backfill(reason: str) -> None:
    """Fire-and-forget one-off backfill. Used by wallet-link.

    Schedules the backfill as a fresh task on the running loop so callers
    don't have to await anything. Uses the async lock to debounce — if
    another kick just fired we skip to avoid piling up duplicate fetches.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (shouldn't happen inside a FastAPI request) —
        # silently skip; the daily loop will catch us up.
        return

    async def _guarded() -> None:
        # Non-blocking lock check: if another run is in flight, bail.
        if _BACKFILL_LOCK.locked():
            return
        async with _BACKFILL_LOCK:
            await _run_top_coins_backfill(reason=reason)

    loop.create_task(_guarded())


@app.on_event("startup")
async def _start_price_backfill_loop():
    """Launch the daily top-coins backfill worker as a background task."""
    global _BACKFILL_TASK
    _BACKFILL_TASK = asyncio.create_task(_top_coins_backfill_loop())
    log.info("startup: top-coins backfill scheduled (every %ds)", _BACKFILL_INTERVAL_SEC)


@app.on_event("shutdown")
async def _stop_price_backfill_loop():
    """Cancel the backfill task so the process shuts down promptly."""
    global _BACKFILL_TASK
    if _BACKFILL_TASK and not _BACKFILL_TASK.done():
        _BACKFILL_TASK.cancel()
        try:
            await _BACKFILL_TASK
        except (asyncio.CancelledError, Exception):
            pass
        log.info("shutdown: top-coins backfill cancelled")


# ---------- Pages ----------
# Frontend is served by Vercel (portfolio-tracker-drab-chi.vercel.app).
# This backend only serves API endpoints. No SPA pages served from VPS.
# The only server-rendered page is /fund (admin-only fund management).


# ---------- Auth API ----------

class NonceIn(BaseModel):
    address: str = Field(..., min_length=10, max_length=128)
    chain_type: str = Field(..., max_length=16)  # 'evm' | 'solana'


class SiweVerifyIn(BaseModel):
    message: str = Field(..., max_length=4096)
    signature: str = Field(..., max_length=512)


class SiwsVerifyIn(BaseModel):
    address: str = Field(..., min_length=10, max_length=128)
    message: str = Field(..., max_length=4096)
    signature: str = Field(..., max_length=512)  # base58-encoded
    nonce: str = Field(..., max_length=64)


@app.post("/api/auth/nonce")
def api_auth_nonce(body: NonceIn):
    """Issue a single-use nonce for the given wallet address + chain type.

    The client embeds this nonce in the SIWE/SIWS message it asks the wallet
    to sign, then posts message + signature to /api/auth/verify.
    """
    if body.chain_type not in {"evm", "solana"}:
        raise HTTPException(400, "chain_type must be 'evm' or 'solana'")
    if not body.address:
        raise HTTPException(400, "address is required")
    nonce = auth.issue_nonce(body.address, body.chain_type)
    return {"nonce": nonce}


@app.post("/api/auth/verify/evm")
def api_auth_verify_evm(body: SiweVerifyIn, request: Request):
    """Verify a SIWE signature and start a session (EVM wallets)."""
    address = auth.verify_siwe(body.message, body.signature, request=request)
    user_id = auth.login_or_create_user(address, "evm")
    request.session["user_id"] = user_id
    return {"ok": True, "user_id": user_id, "address": address}


@app.post("/api/auth/verify/solana")
def api_auth_verify_solana(body: SiwsVerifyIn, request: Request):
    """Verify a Solana signature and start a session."""
    address = auth.verify_siws(body.address, body.message, body.signature, body.nonce)
    user_id = auth.login_or_create_user(address, "solana")
    request.session["user_id"] = user_id
    return {"ok": True, "user_id": user_id, "address": address}


@app.post("/api/auth/logout")
def api_auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    """Return the current session's user profile, or 401 if not signed in."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    user = db.get_user(int(user_id))
    if not user:
        request.session.clear()
        raise HTTPException(401, "User no longer exists")
    wallets = db.list_wallets(int(user_id))
    return {"user": user, "wallet_count": len(wallets)}


# ---------- Link additional wallet ----------

class LinkWalletIn(BaseModel):
    address: str = Field(..., min_length=10, max_length=128)
    label: str = Field(..., min_length=1, max_length=MAX_LABEL_LEN)
    message: str = Field(..., max_length=4096)
    signature: str = Field(..., max_length=512)
    chain_type: str = Field(..., max_length=16)  # 'evm' | 'solana'
    # Solana needs the nonce explicitly (not embedded in a parseable structure)
    nonce: str | None = Field(default=None, max_length=64)


class WatchWalletIn(BaseModel):
    address: str = Field(..., min_length=32, max_length=128)
    label: str = Field("", max_length=MAX_LABEL_LEN)
    chain_type: str = Field(..., max_length=16)  # 'evm' | 'solana'


@app.post("/api/wallets/watch")
def api_watch_wallet(body: WatchWalletIn,
                     user_id: int = Depends(auth.require_user)):
    """Add a wallet as read-only (no signature required).

    Useful for hardware wallets (e.g. Ledger) where signMessage is unreliable.
    The wallet is marked verified=0 so the UI can show an 'Unverified' badge.
    """
    rate_limit("watch_wallet", user_id, limit=10, window_seconds=3600)
    if body.chain_type not in {"evm", "solana"}:
        raise HTTPException(400, "chain_type must be 'evm' or 'solana'")
    address = body.address.strip()
    if body.chain_type == "evm":
        if not address.startswith("0x") or len(address) != 42:
            raise HTTPException(400, "Invalid EVM address (expected 0x + 40 hex chars)")
    elif body.chain_type == "solana":
        if not (32 <= len(address) <= 44):
            raise HTTPException(400, "Invalid Solana address")
    if len(db.list_wallets(user_id)) >= MAX_WALLETS_PER_USER:
        raise HTTPException(
            400,
            f"Wallet limit reached ({MAX_WALLETS_PER_USER}). "
            f"Remove an existing wallet before adding a new one."
        )
    wid = db.add_wallet(user_id, address, body.label or "Wallet", body.chain_type, verified=0)
    _kick_top_coins_backfill(reason=f"wallet_watch:{wid}")
    return {"ok": True, "id": wid}


@app.post("/api/wallets/link")
def api_link_wallet(body: LinkWalletIn, request: Request,
                    user_id: int = Depends(auth.require_user)):
    """Link an additional wallet to the current user after proving ownership.

    Same signature-based proof as sign-in: the wallet signs a nonce we issued,
    we verify the signature, and on success attach the address to the profile.
    """
    # Cap wallets-per-user so a compromised / malicious account can't flood
    # the wallets table and burn our Moralis quota on junk scans.
    if len(db.list_wallets(user_id)) >= MAX_WALLETS_PER_USER:
        raise HTTPException(
            400,
            f"Wallet limit reached ({MAX_WALLETS_PER_USER}). "
            f"Remove an existing wallet before linking a new one."
        )
    if body.chain_type == "evm":
        verified_addr = auth.verify_siwe(body.message, body.signature, request=request)
    elif body.chain_type == "solana":
        if not body.nonce:
            raise HTTPException(400, "nonce is required for Solana links")
        verified_addr = auth.verify_siws(
            body.address, body.message, body.signature, body.nonce
        )
    else:
        raise HTTPException(400, "chain_type must be 'evm' or 'solana'")

    # The address the wallet proved ownership of must match what was posted
    if verified_addr.lower() != body.address.lower():
        raise HTTPException(400, "Signed address does not match posted address")

    wid = auth.link_wallet_to_user(
        user_id, body.address, body.chain_type, body.label or "Wallet"
    )
    # Warm the top-coins price cache so the dashboard trend chart has data
    # the moment the user opens it with this new wallet. Fire-and-forget —
    # the helper debounces if another backfill is already running so rapid
    # wallet links don't fan out into duplicate Binance fetches.
    _kick_top_coins_backfill(reason=f"wallet_link:{wid}")
    return {"ok": True, "id": wid}


# ---------- Wallet API (user-scoped) ----------

@app.get("/api/wallets")
def api_list_wallets(user_id: int = Depends(auth.require_user)):
    from scanner import EVM_CHAINS, EVM_CHAINS_EXTENDED
    wallets = db.list_wallets(user_id)
    for w in wallets:
        raw = w.get("enabled_chains") or ""
        if raw and (w.get("chain") or "evm") == "evm":
            w["enabled_chains"] = [c for c in raw.split(",") if c in EVM_CHAINS_EXTENDED]
        elif (w.get("chain") or "evm") == "evm":
            w["enabled_chains"] = list(EVM_CHAINS)  # default: core 6
        else:
            w["enabled_chains"] = []
    return wallets


@app.delete("/api/wallets/{wallet_id}")
def api_delete_wallet(wallet_id: int, user_id: int = Depends(auth.require_user)):
    db.delete_wallet(user_id, wallet_id)
    return {"ok": True}


@app.get("/api/chains")
def api_list_chains():
    """Return all supported EVM chains for chain selection UI."""
    from scanner import EVM_CHAINS_EXTENDED, ALL_EVM_CHAIN_IDS
    CHAIN_DISPLAY = {
        "eth": "Ethereum", "polygon": "Polygon", "bsc": "BNB Chain",
        "arbitrum": "Arbitrum", "optimism": "Optimism", "base": "Base",
        "avalanche": "Avalanche", "fantom": "Fantom", "cronos": "Cronos",
        "gnosis": "Gnosis", "linea": "Linea", "moonbeam": "Moonbeam",
        "moonriver": "Moonriver", "pulsechain": "PulseChain", "ronin": "Ronin",
        "lisk": "Lisk", "flow": "Flow", "chiliz": "Chiliz",
        "hyperevm": "HyperEVM",
    }
    return [
        {
            "slug": c,
            "name": CHAIN_DISPLAY.get(c, c.title()),
            "chain_id": ALL_EVM_CHAIN_IDS.get(c, 0),
            "core": c in ["eth", "polygon", "bsc", "arbitrum", "optimism", "base"],
        }
        for c in EVM_CHAINS_EXTENDED
    ]


@app.put("/api/wallets/{wallet_id}/chains")
def api_update_wallet_chains(
    wallet_id: int,
    body: dict,
    user_id: int = Depends(auth.require_user),
):
    """Update enabled EVM chains for a wallet. Body: {"chains": ["eth","base",...]}"""
    from scanner import EVM_CHAINS_EXTENDED
    w = db.get_wallet(user_id, wallet_id)
    if not w:
        raise HTTPException(404, "Wallet not found")
    if (w.get("chain") or "evm") != "evm":
        raise HTTPException(400, "Chain selection only applies to EVM wallets")
    raw = body.get("chains", [])
    if not isinstance(raw, list):
        raise HTTPException(400, "chains must be a list")
    # Filter to valid chains only
    chains = [c for c in raw if c in EVM_CHAINS_EXTENDED]
    if not chains:
        raise HTTPException(400, "At least one valid EVM chain required")
    db.update_wallet_chains(user_id, wallet_id, chains)
    return {"ok": True, "enabled_chains": chains}


@app.post("/api/wallets/{wallet_id}/scan")
def api_scan_wallet(wallet_id: int, user_id: int = Depends(auth.require_user)):
    rate_limit("scan_wallet", user_id, limit=20, window_seconds=60)
    w = db.get_wallet(user_id, wallet_id)
    if not w:
        raise HTTPException(404, "Wallet not found")
    try:
        rows = scan(w["address"], w["label"])
    except Exception as e:
        log.error("scan.failed user=%s wallet=%s err=%s", user_id, wallet_id, e)
        raise HTTPException(500, "Scan failed — please try again later")
    db.replace_wallet_holdings(user_id, wallet_id, rows)
    db.mark_wallet_scanned(user_id, wallet_id)
    return {"ok": True, "positions": len(rows),
            "total_usd": sum(r.get("usd_value", 0) for r in rows)}


def _parse_chains_param(chains: str | None) -> list[str] | None:
    """Parse a comma-separated `?chains=eth,base` query param into a clean list.

    Returns None when the param is absent (caller treats as "all chains").
    Filters out any slug that isn't in EVM_CHAINS so a tampered client can't
    trigger fetches against unsupported chains.
    """
    if not chains:
        return None
    from scanner import EVM_CHAINS_EXTENDED
    out = []
    for chunk in chains.split(","):
        c = chunk.strip().lower()
        if c and c in EVM_CHAINS_EXTENDED:
            out.append(c)
    return out or None


@app.get("/api/wallets/{wallet_id}/estimate-import")
def api_estimate_import(
    wallet_id: int,
    chains: str | None = None,
    user_id: int = Depends(auth.require_user),
):
    """Quote the cost of importing a wallet's history BEFORE running it.

    Shown in the UI so the user knows exactly what they'll pay. Uses the
    Moralis /stats endpoint (single call per chain) — doesn't actually
    fetch any history so it's cheap to call.

    Optional `chains` query param (comma-separated slugs) restricts the
    quote to a subset of EVM chains — users can estimate "just Base" or
    "eth + arbitrum" etc.
    """
    rate_limit("estimate", user_id, limit=30, window_seconds=60)
    w = db.get_wallet(user_id, wallet_id)
    if not w:
        raise HTTPException(404, "Wallet not found")
    if w["chain"] not in ("evm", "solana"):
        raise HTTPException(400, f"History import not supported for chain '{w['chain']}'")

    # Solana uses Helius (not Moralis), so it's always free — return a
    # zero-cost quote without hitting any external API.
    if w["chain"] == "solana":
        prior_txs = db.list_transactions(user_id, limit=10000, wallet_ids=[wallet_id])
        already = len(prior_txs)
        return {
            "total_tx": already,          # we don't know the on-chain count without fetching
            "already_imported": already,
            "is_delta": already > 0,
            "new_delta": 0,               # placeholder — actual count known only after fetch
            "free_tx_applied": 0,
            "chargeable_tx": 0,
            "cost_usd": 0.0,
            "free_allowance": pricing.FREE_TX_ALLOWANCE,
            "free_allowance_remaining": 0,
            "min_charge": 0.0,
        }

    chain_list = _parse_chains_param(chains)
    # How many txs already imported for this wallet (drives delta pricing).
    prior_txs = db.list_transactions(user_id, limit=10000, wallet_ids=[wallet_id])
    # Profile-wide free tx allowance. The first FREE_TX_ALLOWANCE txs the
    # user ever imports (across every wallet) are free; anything past
    # that is billed. We derive "remaining" from the current total tx
    # count, which means deleting txs replenishes the pool — fine for
    # the "small user grace" semantic.
    # Free-tier users (admin grant) always get unlimited free allowance.
    total_user_txs = db.count_user_transactions(user_id)
    if db.is_free_tier(user_id):
        remaining_free = 999_999_999
    else:
        remaining_free = max(0, pricing.FREE_TX_ALLOWANCE - total_user_txs)
    return pricing.quote_import(
        address=w["address"],
        chains=chain_list,
        already_imported_tx_count=len(prior_txs),
        free_allowance_remaining=remaining_free,
    )


@app.get("/api/wallets/estimate-import-all")
def api_estimate_import_all(user_id: int = Depends(auth.require_user)):
    """Aggregate quote across every EVM wallet the user owns.

    Powers the "Batch import history" button — one payment can cover a
    full refresh of every linked wallet. We sum the per-wallet costs
    rather than short-circuiting on the min_charge so the displayed
    total matches what we'd actually charge if the user ran each wallet
    individually.
    """
    rate_limit("estimate", user_id, limit=10, window_seconds=60)
    wallets = [w for w in db.list_wallets(user_id) if w["chain"] == "evm"]
    per_wallet: list[dict] = []
    total_tx = 0
    total_cost = 0.0
    # Profile-wide free tx allowance, shared across every wallet in this
    # batch. We decrement `remaining_free` as each wallet's quote consumes
    # from the pool so the aggregate cost matches what we'd actually
    # charge when imports run sequentially.
    total_user_txs = db.count_user_transactions(user_id)
    if db.is_free_tier(user_id):
        remaining_free = 999_999_999
    else:
        remaining_free = max(0, pricing.FREE_TX_ALLOWANCE - total_user_txs)
    free_before = remaining_free
    for w in wallets:
        prior_txs = db.list_transactions(
            user_id, limit=10000, wallet_ids=[w["id"]]
        )
        q = pricing.quote_import(
            address=w["address"],
            chains=None,
            already_imported_tx_count=len(prior_txs),
            free_allowance_remaining=remaining_free,
        )
        remaining_free = int(q.get("free_allowance_remaining_after", remaining_free))
        per_wallet.append({
            "wallet_id": w["id"],
            "label": w["label"],
            "address": w["address"],
            "total_tx": q["total_tx"],
            "chargeable_tx": q["chargeable_tx"],
            "cost_usd": q["cost_usd"],
            "is_delta": q["is_delta"],
            "free_tx_applied": q.get("free_tx_applied", 0),
        })
        total_tx += int(q.get("total_tx", 0))
        total_cost += float(q.get("cost_usd", 0))
    return {
        "wallets": per_wallet,
        "wallet_count": len(per_wallet),
        "total_tx": total_tx,
        "cost_usd": round(total_cost, 4),
        "price_per_tx": pricing.PRICE_PER_TX,
        "min_charge": pricing.MIN_CHARGE,
        "free_allowance": pricing.FREE_TX_ALLOWANCE,
        "free_allowance_remaining_before": free_before,
        "free_allowance_remaining_after": remaining_free,
    }


class PayDetails(BaseModel):
    """On-chain payment proof submitted with an import request.

    The client has already signed and broadcast an ERC-20 transfer (or
    native send) to the treasury address for the quoted cost. We verify
    the tx hash against Moralis, confirm it matches the expected token,
    amount, recipient, and a linked sender wallet — and only then run
    the import. The payment is logged to the credit_transactions ledger
    with kind='import_payment' for audit, but does NOT touch the
    user_credits balance (there is no prepaid balance model anymore).
    """
    chain: str = Field(..., min_length=1, max_length=16)
    tx_hash: str = Field(..., min_length=66, max_length=66)
    expected_token: str = Field(..., min_length=1, max_length=16)
    expected_usd: float = Field(..., ge=0, le=100000)


class ImportHistoryIn(BaseModel):
    """Body for /api/wallets/{id}/import-history.

    `max_cost_usd` caps the cost we're allowed to run — defense against
    TOCTOU drift between the quote the user saw and the quote at the
    moment of execution (their wallet could have added txs between
    clicks). If the real cost exceeds this cap we 409.

    `pay` is REQUIRED when the current quote has cost_usd > 0 — it
    carries the on-chain tx hash that funds this specific import. When
    cost_usd == 0 (first import under the free allowance or a delta
    import with no new txs) the field can be omitted.
    """
    max_cost_usd: float = Field(..., ge=0, description="Max $ user authorized")
    chains: list[str] | None = Field(default=None, description="Restrict to these EVM chains")
    fetch_prices: bool = True
    pay: PayDetails | None = None


def _active_chains_from_quote(quote: dict) -> list[str]:
    """Pick out chains the wallet actually has txs on, per the quote.

    `quote` is a dict returned by `pricing.quote_import` — it carries a
    `chains` array of `{chain, tx_count}` records. We filter to the
    chains where `tx_count > 0` so `_run_import_for_wallet` only calls
    Moralis /history for chains that can possibly produce rows.
    Returning an empty list is fine — the caller should interpret it
    as "skip the import entirely" (nothing to fetch).
    """
    return [
        c["chain"]
        for c in (quote.get("chains") or [])
        if int(c.get("tx_count") or 0) > 0
    ]


def _verify_import_payment_or_raise(
    user_id: int, cost: float, pay: PayDetails | None, notes: str,
) -> dict:
    """Verify a pay-per-import payment and log it. Raises on failure.

    Returns a dict describing the logged payment (or an empty dict for a
    free import). Shared by the single-wallet and batch import routes so
    the idempotency + verification logic stays in one place.
    """
    if cost <= 0 or db.is_free_tier(user_id):
        return {}
    if pay is None:
        raise HTTPException(
            402,
            f"Payment required: this import costs ${cost:.2f}. "
            f"Submit a signed payment tx in the `pay` field."
        )
    result = wallet_payments.verify_payment_for_import(
        user_id=user_id,
        chain=pay.chain,
        tx_hash=pay.tx_hash,
        expected_token=pay.expected_token,
        expected_usd=max(float(pay.expected_usd), cost),
    )
    status = result.get("status")
    if status == "pending":
        # 425 Too Early: client should retry in a few seconds. We avoid
        # 2xx codes here because the fetch wrapper on the SPA side only
        # throws on >=400 — we need this to reach the error branch so
        # the retry loop kicks in.
        raise HTTPException(425, result.get("reason") or "Payment pending confirmation")
    if status == "already_used":
        raise HTTPException(409, result.get("reason") or "Payment already used")
    if status != "ok":
        raise HTTPException(402, result.get("reason") or "Payment rejected")

    # Record the payment to the ledger. Idempotent on (chain, tx_hash).
    recorded = db.record_import_payment(
        user_id=user_id,
        cost_usd=cost,
        chain=pay.chain.lower(),
        tx_hash=pay.tx_hash.lower(),
        from_address=result.get("matched_from") or "",
        token_symbol=result.get("matched_symbol") or pay.expected_token,
        token_amount=float(result.get("matched_amount") or 0),
        notes=notes,
    )
    if not recorded:
        # Race with a concurrent request that just spent this tx — reject
        # the second attempt so we never double-run an import.
        raise HTTPException(409, "This payment has already been used for an import.")
    return {
        "tx_hash": pay.tx_hash.lower(),
        "chain": pay.chain.lower(),
        "paid_usd": float(result.get("matched_usd") or cost),
        "token": result.get("matched_symbol") or pay.expected_token,
    }


def _run_import_for_wallet(user_id: int, w: dict, chain_list, fetch_prices: bool) -> dict:
    """Execute the history import for a single wallet and persist new txs.

    Factored out of the route handler so the single-wallet and batch
    endpoints share the exact same insert/dedupe pipeline.

    `chain_list` should already be narrowed to chains where the wallet
    actually has activity — the quote endpoint produces a per-chain tx
    count and the routes pass `[c for c in quote.chains if c.tx_count>0]`.
    Running /history on every EVM chain even when the wallet has zero
    txs on 5 of them burns ~125 CU of Moralis budget per wallet for
    nothing.

    Solana wallets use sol_history.fetch_sol_history() instead of the EVM
    Moralis path. The emitted row shape is identical so the dedupe + insert
    pipeline below is shared by both chains.
    """
    wallet_chain = (w.get("chain") or "evm").lower()
    own = {ww["address"] for ww in db.list_wallets(user_id)}
    log.info("import.start user=%s wallet=%s chain=%s",
             user_id, w["id"], wallet_chain)

    # ---------- Fetch rows depending on chain type ----------
    if wallet_chain == "solana":
        # Solana path: Helius Enhanced Transactions API via sol_history.
        # `chain_list` is ignored for Solana — there is only one chain. The
        # fetch_prices flag is also ignored because sol_history always resolves
        # prices inline (the price waterfall is cheaper per-call than EVM's
        # block-level Moralis lookups, so there's no reason to skip it).
        rows = sol_history_mod.fetch_sol_history(w["address"])
        # sol_history rows carry notes already; add a chain tag for consistency
        for r in rows:
            r.setdefault("chain", "solana")
            r.setdefault("tx_hash", "")
    else:
        # EVM path: Moralis history walker (unchanged)
        rows = history.import_wallet_history(
            w["address"], own,
            fetch_prices=fetch_prices,
            chains=chain_list,
        )

    # ---------- Dedupe against already-imported transactions ----------
    # The composite key (ts truncated to seconds, symbol, rounded amount,
    # tx_type) catches exact re-imports. It won't catch legitimate back-to-back
    # identical trades (e.g. two 0.1 ETH buys at the same second) but those
    # are rare and the user can delete the duplicate manually. A stricter dedupe
    # would require storing tx signatures, which needs a schema migration.
    existing = db.list_transactions(user_id, limit=10000)
    seen = {(t["ts"][:19], t["symbol"], round(float(t["amount"]), 8), t["tx_type"])
            for t in existing}
    inserted = 0
    for r in rows:
        key = (r["ts"][:19], r["symbol"], round(r["amount"], 8), r["tx_type"])
        if key in seen:
            continue
        # Build notes string — EVM rows carry chain/tx_hash; Solana rows
        # already have notes but we unify the format here.
        tx_hash = r.get("tx_hash") or ""
        chain_tag = r.get("chain") or wallet_chain
        notes = r.get("notes") or f"auto-imported · {chain_tag} · {tx_hash[:10]}"
        db.add_transaction(
            user_id=user_id,
            ts=r["ts"],
            tx_type=r["tx_type"],
            symbol=r["symbol"],
            amount=r["amount"],
            price_usd=r.get("price_usd", 0),
            wallet_id=w["id"],
            notes=notes,
        )
        inserted += 1
        seen.add(key)
    log.info("import.done user=%s wallet=%s found=%d inserted=%d",
             user_id, w["id"], len(rows), inserted)
    return {"found": len(rows), "inserted": inserted}


@app.post("/api/wallets/{wallet_id}/import-history")
def api_import_history(
    wallet_id: int,
    body: ImportHistoryIn,
    user_id: int = Depends(auth.require_user),
):
    # History imports are the most expensive op — throttle aggressively.
    rate_limit("import_history", user_id, limit=10, window_seconds=3600)

    w = db.get_wallet(user_id, wallet_id)
    if not w:
        raise HTTPException(404, "Wallet not found")
    # Supported chain types for history import. Bitcoin is still out of scope
    # (no history walker implemented). Solana is handled by sol_history.py via
    # the shared _run_import_for_wallet dispatcher below.
    if w["chain"] not in ("evm", "solana"):
        raise HTTPException(400, f"History import not supported for chain '{w['chain']}' yet (Bitcoin coming)")

    # Per-wallet 30-day import cap — prevents import-loop abuse that would
    # grind delta pricing near zero while burning Moralis pagination calls.
    recent_imports = db.count_recent_wallet_imports(user_id, wallet_id, days=30)
    if recent_imports >= MAX_IMPORTS_PER_WALLET_30D:
        raise HTTPException(
            429,
            f"Import limit reached: {MAX_IMPORTS_PER_WALLET_30D} imports per "
            f"wallet per 30 days. Try again later."
        )

    # Lifetime transaction-count cap — refuse imports that would push the
    # user past MAX_TRANSACTIONS_PER_USER.
    if db.count_user_transactions(user_id) >= MAX_TRANSACTIONS_PER_USER:
        raise HTTPException(
            429,
            f"Transaction limit reached ({MAX_TRANSACTIONS_PER_USER:,}). "
            f"Delete old transactions or contact support to raise the cap."
        )

    # Normalize chain list (defense against tampered slugs)
    chain_list = _parse_chains_param(",".join(body.chains)) if body.chains else None

    # Compute the live quote so we charge against the current tx count,
    # not whatever the user's stale UI snapshot said. Profile-wide free
    # allowance is derived from the user's current tx total — deleting
    # txs replenishes the pool which is fine for the "small user grace"
    # semantic.
    prior_txs = db.list_transactions(user_id, limit=10000, wallet_ids=[wallet_id])
    total_user_txs = db.count_user_transactions(user_id)
    if db.is_free_tier(user_id):
        remaining_free = 999_999_999
    else:
        remaining_free = max(0, pricing.FREE_TX_ALLOWANCE - total_user_txs)
    quote = pricing.quote_import(
        address=w["address"],
        chains=chain_list,
        already_imported_tx_count=len(prior_txs),
        free_allowance_remaining=remaining_free,
    )
    cost = float(quote["cost_usd"])

    # TOCTOU guard: cost drift beyond what the user authorized → 409.
    if cost > float(body.max_cost_usd) + 0.01:
        raise HTTPException(
            409,
            f"Cost has increased since your quote. Quoted max: "
            f"${body.max_cost_usd:.2f}, current cost: ${cost:.2f}. "
            f"Please re-check the estimate and try again."
        )

    # Verify + log the payment (or skip cleanly if cost == 0).
    payment_info = _verify_import_payment_or_raise(
        user_id, cost, body.pay,
        notes=f"Import history: {w['label']} ({quote['total_tx']} txs)",
    )

    # Fast-path: delta re-import with no new txs. The quote already
    # verified that Moralis's total count matches what we've got on
    # file, so running /history would burn 25+ CU per chain and find
    # zero new rows. Skip the whole call.
    if quote.get("is_delta") and int(quote.get("chargeable_tx") or 0) == 0:
        log.info("import.skip_nothing_new user=%s wallet=%s total_tx=%d",
                 user_id, wallet_id, quote.get("total_tx", 0))
        db.record_wallet_import(user_id, wallet_id, cost, 0)
        return {
            "ok": True,
            "found": 0,
            "inserted": 0,
            "charged_usd": cost,
            "payment": payment_info or None,
            "note": "No new transactions since last import.",
        }

    # Narrow the import to chains where the wallet actually has txs.
    # `chain_list` from the body takes priority if the user restricted
    # the import; otherwise we use the active set from the live quote.
    import_chains = chain_list or _active_chains_from_quote(quote)

    # Run the import. NOTE: we no longer refund on failure — the payment
    # is on-chain and settled. The user's tx_hash is already burnt against
    # the idempotency index, so a retry needs a fresh payment. If the
    # failure was clearly ours (500 from Moralis etc) support can credit
    # the user manually via an admin tool.
    try:
        result = _run_import_for_wallet(
            user_id, w, import_chains, body.fetch_prices,
        )
    except Exception as e:
        log.error("import.failed user=%s wallet=%s err=%s", user_id, wallet_id, e)
        raise HTTPException(500, "Import failed — please try again later")

    db.record_wallet_import(user_id, wallet_id, cost, result["found"])
    return {
        "ok": True,
        "found": result["found"],
        "inserted": result["inserted"],
        "charged_usd": cost,
        "payment": payment_info or None,
    }


class BatchImportIn(BaseModel):
    """Body for /api/wallets/import-history-all.

    Single payment covers a full refresh across every EVM wallet the user
    has linked. `max_cost_usd` caps the aggregated cost. `pay` is required
    when the combined cost is > 0.
    """
    max_cost_usd: float = Field(..., ge=0)
    fetch_prices: bool = True
    pay: PayDetails | None = None


@app.post("/api/wallets/import-history-all")
def api_import_history_all(
    body: BatchImportIn,
    user_id: int = Depends(auth.require_user),
):
    """Batch-import history for every EVM wallet under one payment.

    Flow:
      1. Recompute the aggregate quote (live, not trust the client)
      2. TOCTOU-check against body.max_cost_usd
      3. Verify + log the payment ONCE, covering every wallet
      4. Run imports sequentially; collect per-wallet results
      5. Return partial success if any wallet fails — the user's payment
         has already been burnt against the ledger, so we surface the
         errors rather than refund, and let support handle refunds on
         our-fault failures.
    """
    rate_limit("import_history", user_id, limit=5, window_seconds=3600)

    wallets = [w for w in db.list_wallets(user_id) if w["chain"] == "evm"]
    if not wallets:
        raise HTTPException(400, "No EVM wallets linked.")
    if db.count_user_transactions(user_id) >= MAX_TRANSACTIONS_PER_USER:
        raise HTTPException(
            429,
            f"Transaction limit reached ({MAX_TRANSACTIONS_PER_USER:,})."
        )

    # Re-compute the aggregate quote server-side so a stale client can't
    # under-pay by sending yesterday's cost. The profile-wide free tx
    # allowance is a shared pool — decrement it as we walk wallets so
    # the second wallet only gets whatever the first didn't consume.
    per_wallet_quotes: list[tuple[dict, dict]] = []
    total_cost = 0.0
    total_tx = 0
    total_user_txs = db.count_user_transactions(user_id)
    remaining_free = max(0, pricing.FREE_TX_ALLOWANCE - total_user_txs)
    for w in wallets:
        prior_txs = db.list_transactions(
            user_id, limit=10000, wallet_ids=[w["id"]]
        )
        q = pricing.quote_import(
            address=w["address"],
            chains=None,
            already_imported_tx_count=len(prior_txs),
            free_allowance_remaining=remaining_free,
        )
        remaining_free = int(q.get("free_allowance_remaining_after", remaining_free))
        per_wallet_quotes.append((w, q))
        total_cost += float(q["cost_usd"])
        total_tx += int(q.get("total_tx", 0))

    total_cost = round(total_cost, 4)
    if total_cost > float(body.max_cost_usd) + 0.01:
        raise HTTPException(
            409,
            f"Cost has increased since your quote. Quoted max: "
            f"${body.max_cost_usd:.2f}, current cost: ${total_cost:.2f}. "
            f"Please re-check the estimate and try again."
        )

    # Verify + log the single payment for the combined cost.
    payment_info = _verify_import_payment_or_raise(
        user_id, total_cost, body.pay,
        notes=f"Batch import history: {len(wallets)} wallets ({total_tx} txs)",
    )

    # Honour per-wallet rate limit caps — skip wallets that are already
    # at the 30-day import ceiling rather than reject the whole batch.
    results: list[dict] = []
    any_failed = False
    for w, q in per_wallet_quotes:
        if db.count_recent_wallet_imports(user_id, w["id"], days=30) \
                >= MAX_IMPORTS_PER_WALLET_30D:
            results.append({
                "wallet_id": w["id"], "label": w["label"],
                "skipped": True, "reason": "30-day import cap reached",
            })
            continue
        # Fast-path: delta re-import with no new txs — skip the whole
        # /history call (would burn 25+ CU per chain for zero rows).
        if q.get("is_delta") and int(q.get("chargeable_tx") or 0) == 0:
            db.record_wallet_import(user_id, w["id"], float(q["cost_usd"]), 0)
            results.append({
                "wallet_id": w["id"], "label": w["label"],
                "found": 0, "inserted": 0,
                "cost_usd": float(q["cost_usd"]),
                "note": "no new txs",
            })
            continue
        # Only fetch from chains with actual activity.
        import_chains = _active_chains_from_quote(q)
        try:
            r = _run_import_for_wallet(user_id, w, import_chains, body.fetch_prices)
            db.record_wallet_import(user_id, w["id"], float(q["cost_usd"]), r["found"])
            results.append({
                "wallet_id": w["id"], "label": w["label"],
                "found": r["found"], "inserted": r["inserted"],
                "cost_usd": float(q["cost_usd"]),
            })
        except Exception as e:
            any_failed = True
            log.error("import_all.failed user=%s wallet=%s err=%s",
                      user_id, w["id"], e)
            results.append({
                "wallet_id": w["id"], "label": w["label"],
                "error": "Scan failed",
            })
    return {
        "ok": not any_failed,
        "wallet_count": len(wallets),
        "total_cost_usd": total_cost,
        "results": results,
        "payment": payment_info or None,
    }


@app.post("/api/scan-all")
def api_scan_all(user_id: int = Depends(auth.require_user)):
    rate_limit("scan_all", user_id, limit=6, window_seconds=60)
    results = []
    for w in db.list_wallets(user_id):
        if not w["scan_enabled"]:
            continue
        try:
            rows = scan(w["address"], w["label"])
            db.replace_wallet_holdings(user_id, w["id"], rows)
            db.mark_wallet_scanned(user_id, w["id"])
            results.append({"wallet": w["label"], "positions": len(rows),
                            "total": sum(r.get("usd_value", 0) for r in rows)})
        except Exception as e:
            log.error("scan-all.failed wallet=%s err=%s", w["id"], e)
            results.append({"wallet": w["label"], "error": "Scan failed"})
    total = sum(h["usd_value"] for h in db.list_holdings_raw(user_id))
    db.record_snapshot(user_id, total)
    return {"ok": True, "scanned": results, "total_usd": total}


# ---------- Holdings / Dashboard API ----------

def _parse_wallet_filter(wallets: str | None) -> list[int] | None:
    """Parse a `?wallets=1,2,3` query param into a list of ints.

    Returns None when the param is absent → "no filter, show everything".
    Returns an empty list when the param is present but empty/invalid → used
    by the holdings page when the user has deselected every wallet, which we
    want to render as an honest empty state instead of the unfiltered view.
    """
    if wallets is None:
        return None
    out: list[int] = []
    for chunk in wallets.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError:
            continue
    return out


@app.get("/api/holdings")
def api_holdings(
    user_id: int = Depends(auth.require_user),
    wallets: str | None = None,
):
    wallet_ids = _parse_wallet_filter(wallets)
    by_sym = db.list_holdings_by_symbol(user_id, wallet_ids=wallet_ids)
    txs = db.list_transactions(user_id, limit=5000, wallet_ids=wallet_ids)
    rows = pnl.merge_holdings_with_cost_basis(by_sym, txs)
    summary = pnl.portfolio_summary(rows)
    # Include the full wallet list so the UI can render the filter chips
    # even when the current filter has hidden some of them.
    all_wallets = [
        {"id": w["id"], "label": w["label"], "address": w["address"], "chain": w["chain"]}
        for w in db.list_wallets(user_id)
    ]
    return {
        "holdings": rows,
        "summary": summary,
        "wallets": all_wallets,
        "filter_active": wallet_ids is not None,
    }


@app.get("/api/holdings/raw")
def api_holdings_raw(user_id: int = Depends(auth.require_user)):
    return db.list_holdings_raw(user_id)


# ---------- Billing / Credits API ----------

@app.get("/api/billing/summary")
def api_billing_summary(user_id: int = Depends(auth.require_user)):
    """Pay-per-import payment history + pricing for the Billing page.

    The credit-balance model is disabled — each history import is paid
    directly via an on-chain tx. This endpoint returns the per-import
    payment ledger (kind='import_payment') plus the pricing constants
    so the UI can show "what does the next import cost" without
    hitting the estimate endpoints.
    """
    all_txs = db.list_credit_transactions(user_id, limit=200)
    # Only surface import payments to keep the page focused on the new
    # billing model. Legacy top-up / charge / refund rows are kept in
    # the DB for audit but not shown in the redesigned UI.
    import_txs = [t for t in all_txs if t.get("kind") == "import_payment"]
    # Profile-wide free tx allowance — shared across every wallet. We
    # expose "remaining" so the UI can show a "X of 10 free txs left"
    # hint on the billing page.
    total_user_txs = db.count_user_transactions(user_id)
    remaining_free = max(0, pricing.FREE_TX_ALLOWANCE - total_user_txs)
    return {
        "treasury_address": treasury_watcher.TREASURY_ADDRESS,
        "accepted_tokens": sorted(treasury_watcher.ACCEPTED_SYMBOLS),
        "pricing": {
            "per_tx": pricing.PRICE_PER_TX,
            "min_charge": pricing.MIN_CHARGE,
            "free_allowance": pricing.FREE_TX_ALLOWANCE,
            "free_allowance_remaining": remaining_free,
            "delta_multiplier": pricing.DELTA_MULTIPLIER,
        },
        "transactions": import_txs,
    }


# EIP-155 chain IDs for the EVM chains we accept. Used by the wallet-pay
# frontend to switch networks (wallet_switchEthereumChain) before signing.
# Keep in lockstep with treasury_watcher's chain names.
EVM_CHAIN_IDS: dict[str, int] = {
    "eth": 1,
    "arbitrum": 42161,
    "optimism": 10,
    "base": 8453,
    "polygon": 137,
    "bsc": 56,
}

# ERC-20 decimals for tokens on our allowlist. USDT on BSC is 18, everything
# else is 6 for USD stables and 18 for DAI — hardcoded because Moralis
# doesn't return decimals on the /transaction endpoint shape we use and
# we don't want a per-token RPC lookup on every pay initiation.
_TOKEN_DECIMALS: dict[tuple[str, str], int] = {
    # Stables — 6 decimals almost everywhere except DAI (18) and BSC USDT (18).
    ("eth", "USDC"): 6, ("eth", "USDT"): 6, ("eth", "DAI"): 18,
    ("polygon", "USDC"): 6, ("polygon", "USDC.E"): 6, ("polygon", "USDT"): 6, ("polygon", "DAI"): 18,
    ("arbitrum", "USDC"): 6, ("arbitrum", "USDC.E"): 6, ("arbitrum", "USDT"): 6, ("arbitrum", "DAI"): 18,
    ("optimism", "USDC"): 6, ("optimism", "USDC.E"): 6, ("optimism", "USDT"): 6, ("optimism", "DAI"): 18,
    ("base", "USDC"): 6, ("base", "USDBC"): 6, ("base", "DAI"): 18,
    ("bsc", "USDC"): 18, ("bsc", "USDT"): 18,
    # Wrapped/volatile tokens — all 18 decimals
    ("eth", "WETH"): 18, ("arbitrum", "WETH"): 18, ("optimism", "WETH"): 18, ("base", "WETH"): 18,
    ("polygon", "WMATIC"): 18, ("bsc", "WBNB"): 18,
    ("eth", "WBTC"): 8, ("polygon", "WBTC"): 8, ("arbitrum", "WBTC"): 8, ("optimism", "WBTC"): 8,
    ("base", "CBBTC"): 8, ("eth", "CBBTC"): 8,
}


@app.get("/api/billing/pay/tokens")
def api_billing_pay_tokens(user_id: int = Depends(auth.require_user)):
    """Return per-chain token config for the in-app wallet-pay flow.

    Derived from the treasury_watcher allowlist so the frontend can never
    offer a token we wouldn't accept on verify. Includes stablecoins and
    wrapped/volatile tokens (WETH, WBTC, WBNB, etc.).
    """
    chains_out = []
    all_contracts = treasury_watcher.ACCEPTED_CONTRACTS  # {(chain, contract): symbol}
    for chain_name in ("eth", "arbitrum", "optimism", "base", "polygon", "bsc"):
        chain_id = EVM_CHAIN_IDS.get(chain_name)
        tokens = []
        for (c, contract), sym in all_contracts.items():
            if c != chain_name:
                continue
            decimals = _TOKEN_DECIMALS.get((chain_name, sym))
            if decimals is None:
                continue  # skip anything we don't have decimals for
            is_stable = sym in treasury_watcher.STABLE_SYMBOLS
            tokens.append({
                "symbol": sym,
                "contract": contract,
                "decimals": decimals,
                "stable": is_stable,
            })
        if not tokens:
            continue
        # Sort: stables first, then by symbol name
        chains_out.append({
            "chain": chain_name,
            "chain_id": chain_id,
            "display": {
                "eth": "Ethereum", "arbitrum": "Arbitrum", "optimism": "Optimism",
                "base": "Base", "polygon": "Polygon", "bsc": "BNB Chain",
            }.get(chain_name, chain_name),
            "tokens": sorted(tokens, key=lambda t: (0 if t["stable"] else 1, t["symbol"])),
        })
    return {
        "treasury_address": treasury_watcher.TREASURY_ADDRESS,
        "chains": chains_out,
    }


class PayVerifyIn(BaseModel):
    """Body for /api/billing/pay/verify — in-app wallet top-up verification.

    The client builds and signs an ERC-20 `transfer` (or native send) to the
    treasury address from the user's connected wallet, broadcasts it, then
    posts the resulting tx hash back here so we can look it up on Moralis
    and credit the user. Amount/token fields are what the UI prompted the
    user for — the server still re-derives the real USD value from the
    on-chain transfer and cross-checks against these to reject tampering.
    """
    chain: str = Field(..., min_length=1, max_length=16)
    tx_hash: str = Field(..., min_length=66, max_length=66)
    expected_token: str = Field(..., min_length=1, max_length=16)
    expected_usd: float = Field(..., gt=0, le=100000)


@app.post("/api/billing/pay/verify")
def api_billing_pay_verify(
    body: PayVerifyIn,
    user_id: int = Depends(auth.require_user),
):
    """DEPRECATED: prepaid top-up verification.

    The credit-balance model is disabled. Payments are now submitted
    inline with each import via /api/wallets/{id}/import-history (or the
    batch variant). This route is kept to return a clear 410 so any
    lingering client bookmarks get an informative error instead of a
    silent 404.
    """
    raise HTTPException(
        410,
        "Prepaid top-ups are disabled. Pay per import directly on the "
        "Wallets page — each history import is billed as its own on-chain "
        "transaction."
    )


@app.post("/api/billing/poll-now")
def api_billing_poll_now(user_id: int = Depends(auth.require_user)):
    """DEPRECATED: nudge the treasury watcher.

    The watcher is permanently disabled (direct top-ups were removed
    along with the credit balance). This endpoint now 410s so stale
    clients see a clear error.
    """
    raise HTTPException(410, "Treasury polling is disabled.")


# ---------- Chain auto-detect ----------

@app.get("/api/chains/detect")
def api_detect_chains(address: str, user_id: int = Depends(auth.require_user)):
    """Return the EVM chains where an address has any on-chain activity.

    Used by the "add wallet" flow to pre-select chains for a freshly linked
    wallet. Pure lookup, safe to call without linking the wallet first.
    """
    rate_limit("chain_detect", user_id, limit=20, window_seconds=60)
    from scanner import EVM_CHAINS
    if not detect_chain(address) == "evm":
        raise HTTPException(400, "Auto-detect is EVM-only right now")
    active = pricing.detect_active_chains(address)
    return {
        "address": address,
        "active_chains": active,
        "supported_chains": EVM_CHAINS,
    }


@app.get("/api/dashboard")
def api_dashboard(user_id: int = Depends(auth.require_user)):
    by_sym = db.list_holdings_by_symbol(user_id)
    txs = db.list_transactions(user_id, limit=5000)
    rows = pnl.merge_holdings_with_cost_basis(by_sym, txs)
    summary = pnl.portfolio_summary(rows)

    top = sorted(rows, key=lambda r: -r["current_value"])[:10]
    allocation = [
        {
            "symbol": r["symbol"],
            "value": r["current_value"],
            "protocols": r.get("protocols") or "wallet",
        }
        for r in top
    ]

    # Full token split — one slice per symbol, used by the new Chain/Token
    # toggle on the dashboard. Uses the aggregated-by-symbol rows directly so
    # Aave-deposited positions fold into the same slice as any wallet-held
    # underlying (e.g. 0.2 WBTC in Aave + 0.0005 WBTC in wallet = one "WBTC"
    # slice). Anything under 1% of total is collapsed into an "Other" bucket
    # so the pie chart doesn't turn into confetti.
    total_value = summary.get("total_value") or 1.0
    token_split: list[dict] = []
    other_bucket = 0.0
    for r in sorted(rows, key=lambda r: -r["current_value"]):
        v = r["current_value"]
        if v <= 0:
            continue
        if v / total_value < 0.01:
            other_bucket += v
        else:
            token_split.append({"symbol": r["symbol"], "value": v})
    if other_bucket > 0:
        token_split.append({"symbol": "Other", "value": other_bucket})

    raw = db.list_holdings_raw(user_id)
    chain_totals: dict[str, float] = {}
    for h in raw:
        chain_totals[h["chain"]] = chain_totals.get(h["chain"], 0) + float(h["usd_value"] or 0)
    chain_split = [{"chain": k, "value": v} for k, v in chain_totals.items()]

    # Freshen the price cache for every held symbol via Binance daily
    # klines (throttled to once per 6h per symbol). This is what makes
    # the portfolio chart reflect real market moves between imports —
    # without it the chart carry-forwards the last imported price and
    # flat-lines for days. Throttle + per-symbol HTTP fan-out keeps the
    # cost bounded; typical refresh adds <2s on the first load of the
    # 6-hour window and nothing on subsequent loads.
    try:
        refreshed = portfolio_history.ensure_fresh_price_cache(
            user_id=user_id, lookback_days=365
        )
        if refreshed:
            log.info("dashboard.price_backfill user=%s refreshed=%d symbols",
                     user_id, len(refreshed))
    except Exception as e:
        log.warning("dashboard.price_backfill failed user=%s: %s", user_id, e)

    try:
        daily = portfolio_history.reconstruct_daily_history(
            user_id=user_id, lookback_days=365
        )
    except Exception as e:
        print(f"[dashboard] reconstruct_daily_history failed: {e}")
        daily = []

    if daily:
        snaps = [{"ts": d["date"], "total_usd": d["total_usd"]} for d in daily]
    else:
        snaps = db.list_snapshots(user_id, limit=365)

    return {
        "summary": summary,
        "allocation": allocation,
        "chain_split": chain_split,
        "token_split": token_split,
        "snapshots": snaps,
        "history_source": "reconstructed" if daily else "snapshots",
        "wallet_count": len(db.list_wallets(user_id)),
        "holdings_count": len([r for r in rows if r["balance"] > 0]),
    }


# ---------- Transactions API ----------

class TxIn(BaseModel):
    ts: str = Field(..., min_length=4, max_length=32)
    tx_type: str = Field(..., min_length=1, max_length=16)
    symbol: str = Field(..., min_length=1, max_length=MAX_SYMBOL_LEN)
    amount: float = Field(..., gt=-MAX_TX_AMOUNT, lt=MAX_TX_AMOUNT)
    price_usd: float = Field(default=0, ge=0, lt=MAX_TX_AMOUNT)
    wallet_id: int | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTE_LEN)


@app.get("/api/transactions")
def api_list_transactions(symbol: str | None = None,
                          user_id: int = Depends(auth.require_user)):
    return db.list_transactions(user_id, symbol=symbol)


@app.post("/api/transactions")
def api_add_transaction(t: TxIn, user_id: int = Depends(auth.require_user)):
    rate_limit("add_tx", user_id, limit=120, window_seconds=60)
    if db.count_user_transactions(user_id) >= MAX_TRANSACTIONS_PER_USER:
        raise HTTPException(
            429,
            f"Transaction limit reached ({MAX_TRANSACTIONS_PER_USER:,}). "
            f"Delete some old transactions first."
        )
    ttype = t.tx_type.lower().strip()
    if ttype not in {"buy", "sell", "deposit", "withdraw", "swap"}:
        raise HTTPException(400, "Invalid tx_type")
    try:
        if "T" not in t.ts:
            datetime.strptime(t.ts, "%Y-%m-%d")
            ts = t.ts + "T00:00:00"
        else:
            datetime.fromisoformat(t.ts.replace("Z", "+00:00"))
            ts = t.ts
    except Exception:
        raise HTTPException(400, "Invalid timestamp (use YYYY-MM-DD or ISO format)")

    # Ownership check on wallet_id — user can't attach a transaction to a
    # wallet they don't own (cross-tenant leak).
    if t.wallet_id is not None:
        if not db.get_wallet(user_id, t.wallet_id):
            raise HTTPException(400, "wallet_id does not belong to this user")

    tid = db.add_transaction(
        user_id=user_id,
        ts=ts,
        tx_type=ttype,
        symbol=t.symbol.strip()[:MAX_SYMBOL_LEN],
        amount=t.amount,
        price_usd=t.price_usd,
        wallet_id=t.wallet_id,
        notes=(t.notes or None),
    )
    return {"id": tid}


@app.delete("/api/transactions/{tx_id}")
def api_delete_transaction(tx_id: int, user_id: int = Depends(auth.require_user)):
    db.delete_transaction(user_id, tx_id)
    return {"ok": True}


@app.post("/api/transactions/reprice-all")
def api_reprice_all(user_id: int = Depends(auth.require_user)):
    """Re-resolve every tx's USD price using Binance 5m candles.

    Why this exists: when the price cascade falls through to "current
    market" or "unknown" at import time (rate limit, Binance outage,
    long-tail token that later got listed), the tx sits at a wrong or
    $0 price forever unless the user manually fixes it. This endpoint
    walks every tx, canonicalizes the symbol, groups by (canonical, date),
    and makes ONE Binance 5m klines call per group — then matches each
    tx timestamp to its candle and writes the corrected price back.

    Wrapped tokens automatically inherit the underlying's price via
    canonical_symbol — holding WBTC means each WBTC tx gets repriced
    to the BTC 5m close at its timestamp.

    Rate-limited tightly because a single call bulk-updates the whole
    transaction table; users don't need to spam this.
    """
    rate_limit("reprice_all", user_id, limit=5, window_seconds=600)

    try:
        from binance_prices import (
            binance_intraday_klines,
            pair_for_symbol,
            price_at_timestamp,
            canonical_symbol,
        )
    except Exception as e:
        log.error("binance.price.failed err=%s", e)
        raise HTTPException(503, "Price service temporarily unavailable")

    # Pull every tx the user has. `list_transactions` caps at limit=500
    # by default — bump to the user's full set so reprice is actually "all".
    txs = db.list_transactions(user_id, limit=MAX_TRANSACTIONS_PER_USER)
    if not txs:
        return {"ok": True, "updated": 0, "groups": 0, "skipped": 0}

    from collections import defaultdict

    # Group by (canonical_symbol, YYYY-MM-DD). Non-Binance / long-tail
    # tokens fall out of the group build because pair_for_symbol returns
    # None, and we leave those alone (they'd need Moralis, not free).
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped = 0
    for t in txs:
        sym_raw = (t.get("symbol") or "").strip()
        ts = t.get("ts") or ""
        if not sym_raw or not ts:
            skipped += 1
            continue
        if sym_raw.upper() in history.STABLECOINS:
            # Stables already priced correctly at $1.
            skipped += 1
            continue
        if not pair_for_symbol(sym_raw):
            skipped += 1
            continue
        canon = canonical_symbol(sym_raw)
        date = ts[:10]
        groups[(canon, date)].append(t)

    updated = 0
    for (canon, date), group_txs in groups.items():
        candles = binance_intraday_klines(canon, date, interval="5m")
        if not candles:
            continue
        for t in group_txs:
            ts_ms = history._ts_to_ms(t.get("ts") or "")
            if not ts_ms:
                continue
            price = price_at_timestamp(candles, ts_ms)
            if price <= 0:
                # Fall back to the day's last candle close
                price = candles[-1][2]
            if price <= 0:
                continue
            if db.update_transaction_price(user_id, int(t["id"]), float(price)):
                updated += 1

    log.info(
        "reprice.all user=%s txs=%d groups=%d updated=%d skipped=%d",
        user_id, len(txs), len(groups), updated, skipped,
    )
    return {
        "ok": True,
        "total_txs": len(txs),
        "groups": len(groups),
        "updated": updated,
        "skipped": skipped,
    }


# ---------- Tax reports (paid feature) ----------
#
# User flow:
#   1. GET  /api/tax/status         — has the user unlocked? price? jurisdictions?
#   2. POST /api/tax/unlock         — verify on-chain USDC payment, set unlock
#   3. GET  /api/tax/compare        — side-by-side method comparison (gated)
#   4. GET  /api/tax/report         — full JSON report (gated)
#   5. GET  /api/tax/report/pdf     — rendered PDF download (gated)
#   6. GET  /api/tax/report/csv     — generic CSV (gated)
#   7. GET  /api/tax/report/8949    — IRS Form 8949 CSV (gated)
#
# Reports are computed on demand from the user's transaction history —
# no per-request cost on our side, so the $9.99 one-time unlock is pure
# margin after the first payment. We gate BEHIND the unlock to give the
# paywall real teeth, but `/api/tax/status` is always open so the UI can
# render the "unlock" call-to-action.

TAX_JURISDICTIONS_INFO = [
    # (code, name, supported_methods, tax_year_hint) — tiny summary the
    # frontend uses to populate the picker without importing the full
    # dataclasses.
    ("US", "United States", ["FIFO", "LIFO", "HIFO", "SPEC", "AVG"], "Dec 31"),
    ("GB", "United Kingdom", ["AVG"], "Apr 5"),
    ("SG", "Singapore", ["FIFO", "LIFO", "HIFO", "AVG"], "Dec 31"),
    ("DE", "Germany", ["FIFO"], "Dec 31"),
    ("CA", "Canada", ["AVG"], "Dec 31"),
    ("AU", "Australia", ["FIFO", "LIFO", "HIFO", "SPEC"], "Jun 30"),
    ("JP", "Japan", ["AVG", "FIFO"], "Dec 31"),
    ("IN", "India", ["FIFO"], "Mar 31"),
]


@app.get("/api/tax/status")
def api_tax_status(user_id: int = Depends(auth.require_user)):
    """Is this user unlocked? What would an unlock cost? What's supported?

    Always accessible — the UI uses this to decide whether to show the
    "unlock tax reports" paywall or the "generate report" form.
    """
    from wallet_payments import TAX_UNLOCK_PRICE_USD
    unlock = db.get_tax_unlock(user_id)
    return {
        "unlocked": unlock is not None,
        "unlocked_at": unlock.get("unlocked_at") if unlock else None,
        "price_usd": TAX_UNLOCK_PRICE_USD,
        "jurisdictions": [
            {
                "code": code,
                "name": name,
                "methods": methods,
                "tax_year_end": year_hint,
            }
            for (code, name, methods, year_hint) in TAX_JURISDICTIONS_INFO
        ],
    }


class TaxUnlockIn(BaseModel):
    """Body for /api/tax/unlock — same shape as the import-payment verify
    path. The user builds a token transfer for $9.99 to the treasury,
    signs + broadcasts, then POSTs the hash here."""
    chain: str = Field(..., min_length=1, max_length=16)
    tx_hash: str = Field(..., min_length=66, max_length=66)
    expected_token: str = Field(..., min_length=1, max_length=16)


@app.post("/api/tax/unlock")
def api_tax_unlock(
    body: TaxUnlockIn,
    user_id: int = Depends(auth.require_user),
):
    """Verify on-chain payment, grant the tax-reports unlock for this user.

    On success the unlock is recorded in `tax_unlocks` and the user can
    call every other /api/tax/* endpoint freely. Rate-limited tightly so
    a user can't spam-retry with junk hashes and burn our Moralis quota.
    """
    rate_limit("tax_unlock", user_id, limit=10, window_seconds=600)
    import wallet_payments

    # Early exit if already unlocked — no need to re-verify.
    if db.has_tax_unlock(user_id):
        return {
            "ok": True,
            "already_unlocked": True,
            "message": "Tax reports are already unlocked for your account.",
        }

    result = wallet_payments.verify_payment_for_tax_unlock(
        user_id=user_id,
        chain=body.chain,
        tx_hash=body.tx_hash,
        expected_token=body.expected_token,
    )
    if result["status"] != "ok":
        # Surface the structured status back to the UI so it can render a
        # nice "pending / rejected / already_used" banner.
        return {
            "ok": False,
            "status": result["status"],
            "reason": result["reason"],
        }

    db.grant_tax_unlock(
        user_id=user_id,
        amount_paid_usd=result["matched_usd"],
        tx_hash=body.tx_hash.lower(),
        chain=body.chain.lower(),
        notes=f"Unlocked via {result.get('matched_symbol') or body.expected_token}",
    )
    log.info("tax.unlock_granted user=%s amount=%.2f tx=%s",
             user_id, result["matched_usd"], body.tx_hash[:12])
    return {
        "ok": True,
        "already_unlocked": False,
        "amount_paid_usd": result["matched_usd"],
        "message": "Tax reports unlocked. Every year, jurisdiction, and "
                   "method is now available for your account.",
    }


def _require_tax_unlock(user_id: int) -> None:
    """Guard used by every gated tax endpoint.

    Raises 402 Payment Required if the user hasn't paid for the unlock.
    402 (vs 403) makes it trivial for the frontend to distinguish "you're
    not paying us" from "you're not allowed to see this at all".
    """
    if not db.has_tax_unlock(user_id):
        raise HTTPException(
            402,
            "Tax reports require a one-time unlock. "
            "Call /api/tax/unlock to purchase.",
        )


def _normalize_tax_args(year: int, jurisdiction: str, method: str) -> tuple[int, str, str]:
    """Shared validation: checks year range, normalizes codes, defaults method."""
    from tax import JURISDICTIONS
    if year < 2010 or year > 2100:
        raise HTTPException(400, f"tax_year out of range: {year}")
    code = (jurisdiction or "US").upper()
    if code not in JURISDICTIONS:
        raise HTTPException(400, f"unknown jurisdiction: {jurisdiction}")
    juris = JURISDICTIONS[code]
    m = (method or juris.default_method).upper()
    if m not in juris.allowed_methods:
        raise HTTPException(
            400,
            f"method {m} is not allowed in {juris.name}. "
            f"Allowed: {', '.join(juris.allowed_methods)}",
        )
    return year, code, m


@app.get("/api/tax/compare")
def api_tax_compare(
    year: int,
    jurisdiction: str = "US",
    user_id: int = Depends(auth.require_user),
):
    """Run every allowed method for the given year + jurisdiction in parallel.

    Returns a sorted list (cheapest taxable gain first) so the UI can
    highlight the winner. This is the KILLER feature — "FIFO costs you
    $X more than HIFO on this year's trades".
    """
    _require_tax_unlock(user_id)
    year, code, _ = _normalize_tax_args(year, jurisdiction, "FIFO")
    from tax import compare_methods
    txs = db.list_all_transactions_for_tax(user_id)
    return {
        "year": year,
        "jurisdiction": code,
        "tx_count": len(txs),
        "results": compare_methods(txs, year, code),
    }


def _load_spec_id_map(user_id: int, method: str) -> dict[int, dict[int, float]] | None:
    """Load user's lot overrides from DB into the shape the engine wants.

    Only relevant for method='SPEC'. Returns None otherwise so the engine
    keeps its fast FIFO/LIFO/HIFO/AVG paths uncontaminated by empty dicts.
    """
    if method.upper() != "SPEC":
        return None
    rows = db.list_tax_overrides(user_id)
    spec_map: dict[int, dict[int, float]] = {}
    for r in rows:
        tx_id = int(r["sell_tx_id"])
        lot_id = int(r["lot_id"])
        amt = float(r["amount"])
        spec_map.setdefault(tx_id, {})[lot_id] = amt
    return spec_map


@app.get("/api/tax/report")
def api_tax_report(
    year: int,
    jurisdiction: str = "US",
    method: str = "",
    user_id: int = Depends(auth.require_user),
):
    """Full tax report as JSON. See render_pdf for the PDF variant."""
    _require_tax_unlock(user_id)
    year, code, m = _normalize_tax_args(year, jurisdiction, method)
    from tax import compute_tax_report
    from dataclasses import asdict
    txs = db.list_all_transactions_for_tax(user_id)
    spec_map = _load_spec_id_map(user_id, m)
    report = compute_tax_report(txs, year, code, m, spec_id_map=spec_map)
    return asdict(report)


def _build_report_or_400(
    user_id: int, year: int, jurisdiction: str, method: str,
):
    """Helper: run the common validation + report computation for downloads."""
    _require_tax_unlock(user_id)
    year, code, m = _normalize_tax_args(year, jurisdiction, method)
    from tax import compute_tax_report
    txs = db.list_all_transactions_for_tax(user_id)
    spec_map = _load_spec_id_map(user_id, m)
    return compute_tax_report(txs, year, code, m, spec_id_map=spec_map)


@app.get("/api/tax/report/pdf")
def api_tax_report_pdf(
    year: int,
    jurisdiction: str = "US",
    method: str = "",
    user_id: int = Depends(auth.require_user),
):
    """Rendered PDF, streamed as application/pdf."""
    from fastapi.responses import Response
    from tax.reports import render_pdf
    report = _build_report_or_400(user_id, year, jurisdiction, method)
    pdf_bytes = render_pdf(report)
    filename = f"tax_report_{report.jurisdiction_code}_{report.tax_year}_{report.method}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/tax/report/csv")
def api_tax_report_csv(
    year: int,
    jurisdiction: str = "US",
    method: str = "",
    user_id: int = Depends(auth.require_user),
):
    """Generic per-disposal CSV for any accountant tool."""
    from fastapi.responses import Response
    from tax.reports import render_csv
    report = _build_report_or_400(user_id, year, jurisdiction, method)
    csv_bytes = render_csv(report)
    filename = f"tax_report_{report.jurisdiction_code}_{report.tax_year}_{report.method}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/tax/report/8949")
def api_tax_report_8949(
    year: int,
    jurisdiction: str = "US",
    method: str = "",
    user_id: int = Depends(auth.require_user),
):
    """IRS Form 8949 CSV format — importable by TurboTax / CoinTracker / Koinly."""
    from fastapi.responses import Response
    from tax.reports import render_form_8949_csv
    report = _build_report_or_400(user_id, year, jurisdiction, method)
    csv_bytes = render_form_8949_csv(report)
    filename = f"form_8949_{report.tax_year}_{report.method}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Spec-ID lot picker endpoints
# ---------------------------------------------------------------------------
# The picker UI needs three things:
#   1. a snapshot of open lots at each sell moment (so the user sees what
#      they COULD pick from),
#   2. a way to read any overrides they've already saved, and
#   3. a way to write new overrides (or clear them).
# All four endpoints are gated behind the tax unlock.

@app.get("/api/tax/lot-snapshots")
def api_tax_lot_snapshots(
    year: int,
    jurisdiction: str = "US",
    user_id: int = Depends(auth.require_user),
):
    """Open-lot snapshots for every sell in the requested tax year.

    The snapshots are computed across the user's FULL history (buys in
    2020 still anchor a 2024 sell) but then filtered down to just the
    sells whose disposed_ts falls inside the requested year so the picker
    UI isn't swamped with old data. Jurisdiction is passed so we can use
    its fiscal-year boundaries.
    """
    _require_tax_unlock(user_id)
    year, code, _ = _normalize_tax_args(year, jurisdiction, "FIFO")
    from tax import build_sell_lot_snapshots, JURISDICTIONS
    from datetime import datetime, timedelta

    juris = JURISDICTIONS[code]
    txs = db.list_all_transactions_for_tax(user_id)
    snaps = build_sell_lot_snapshots(txs)

    # Filter by jurisdiction fiscal year. Mirrors calculator._filter_to_year
    # but works on dict rows (not Disposal instances) so we inline the logic.
    month, day = juris.tax_year_end
    if (month, day) == (12, 31):
        def in_year(iso: str) -> bool:
            return iso[:4] == str(year)
    else:
        end = datetime(year, month, day, 23, 59, 59)
        start_y = year - 1
        start = (
            datetime(start_y, month, day)
            if (month, day) != (2, 29) else datetime(start_y, 3, 1)
        ) + timedelta(days=1)
        start = start.replace(hour=0, minute=0, second=0)
        def in_year(iso: str) -> bool:
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                return False
            return start <= dt <= end

    filtered = [s for s in snaps if in_year(s["disposed_ts"])]
    return {
        "tax_year": year,
        "jurisdiction": code,
        "snapshots": filtered,
    }


@app.get("/api/tax/overrides")
def api_tax_overrides_list(user_id: int = Depends(auth.require_user)):
    """Return the user's saved spec-ID picks, keyed by sell_tx_id.

    Shape: { "12345": { "7": 0.5, "9": 0.25 }, ... }
    The frontend reshapes this into the per-sell accordion state.
    """
    _require_tax_unlock(user_id)
    rows = db.list_tax_overrides(user_id)
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        tx_id = str(r["sell_tx_id"])
        out.setdefault(tx_id, {})[str(r["lot_id"])] = float(r["amount"])
    return {"overrides": out}


class TaxOverrideIn(BaseModel):
    sell_tx_id: int
    symbol: str
    lot_id: int
    amount: float  # units from this lot; 0 deletes


@app.post("/api/tax/overrides")
def api_tax_overrides_set(
    body: TaxOverrideIn,
    user_id: int = Depends(auth.require_user),
):
    """Upsert a single lot selection. amount <= 0 deletes the row.

    We don't validate here that `amount` ≤ the lot's remaining — the
    engine does that at report time (a pick bigger than the lot is
    silently clipped). This keeps the write path snappy and forgiving.
    """
    _require_tax_unlock(user_id)
    rate_limit("tax_override_set", user_id, limit=240, window_seconds=60)
    sym = (body.symbol or "").strip().upper()
    if not sym:
        raise HTTPException(400, "symbol is required")
    if body.sell_tx_id <= 0:
        raise HTTPException(400, "sell_tx_id must be positive")
    if body.lot_id <= 0:
        raise HTTPException(400, "lot_id must be positive")
    db.set_tax_override(
        user_id=user_id,
        sell_tx_id=body.sell_tx_id,
        symbol=sym,
        lot_id=body.lot_id,
        amount=max(0.0, float(body.amount)),
    )
    return {"ok": True}


@app.delete("/api/tax/overrides/{sell_tx_id}")
def api_tax_overrides_clear(
    sell_tx_id: int,
    user_id: int = Depends(auth.require_user),
):
    """Clear every lot override for one sell (the picker's Reset button)."""
    _require_tax_unlock(user_id)
    rate_limit("tax_override_clear", user_id, limit=60, window_seconds=60)
    db.clear_tax_overrides_for_tx(user_id, sell_tx_id)
    return {"ok": True}


# NOTE: no user-facing `/api/prices/backfill-top-coins` endpoint.
# Price fetching runs on the background task installed at startup
# (`_top_coins_backfill_loop`) and is additionally kicked on new wallet
# links via `_kick_top_coins_backfill`. Keeping it server-owned means
# users never wait on a button — the chart is always already warm.


# ---------- Fund Management ----------

import fund as fund_mod

# Fund management is admin-only (user_id=1)
FUND_ADMIN_USER_ID = 1


def _require_fund_admin(request: Request):
    """Raise 401/403 if the caller is not the fund admin."""
    if not request.session.get("fund_admin"):
        raise HTTPException(401, "Not authenticated — sign in at /fund first")


@app.get("/fund", response_class=HTMLResponse)
def page_fund(request: Request):
    if not request.session.get("fund_admin"):
        return render("fund_login.html", page="fund")
    return render("fund.html", page="fund")


class FundLoginIn(BaseModel):
    password: str


@app.post("/api/fund/login")
def fund_login(request: Request, data: FundLoginIn):
    # Simple password auth for fund management page
    import hashlib
    # SHA-256 of the fund admin password
    FUND_PASSWORD_HASH = os.getenv("FUND_PASSWORD_HASH", "")  # set via env var
    if hashlib.sha256(data.password.encode()).hexdigest() == FUND_PASSWORD_HASH:
        request.session["fund_admin"] = True
        return {"ok": True}
    raise HTTPException(401, "Wrong password")


class FundTxIn(BaseModel):
    person: str
    amount: float
    note: str = ""


class FundPersonIn(BaseModel):
    name: str
    shares: float = 0


def _build_fund_status(conn, user_id: int):
    """Shared logic: build fund status dict for a given user."""
    pooled = fund_mod.get_pooled_value(conn, user_id)
    total_shares = fund_mod.get_total_shares(conn, user_id)
    nav = fund_mod.get_nav_per_share(conn, user_id)

    shareholders = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = ? ORDER BY shares DESC",
        (user_id,)
    ).fetchall()

    result = []
    total_aum = 0
    for sh in shareholders:
        pool_val = sh["shares"] * nav if total_shares > 0 else 0
        pool_pct = (sh["shares"] / total_shares * 100) if total_shares > 0 else 0
        personal = fund_mod.get_personal_value(conn, sh["name"], user_id)
        total = pool_val + personal
        total_aum += total
        result.append({
            "name": sh["name"],
            "shares": sh["shares"],
            "pool_pct": pool_pct,
            "pool_value": round(pool_val, 2),
            "personal": round(personal, 2),
            "total": round(total, 2),
        })

    return {
        "pooled_value": round(pooled, 2),
        "total_shares": round(total_shares, 4),
        "nav_per_share": round(nav, 4),
        "total_aum": round(total_aum, 2),
        "shareholders": result,
    }


def _build_fund_history(conn, user_id: int, person: str = ""):
    """Shared logic: build fund transaction history for a given user."""
    if person:
        rows = conn.execute("""
            SELECT ft.*, fs.name FROM fund_transactions ft
            JOIN fund_shareholders fs ON ft.shareholder_id = fs.id
            WHERE fs.user_id = ? AND LOWER(fs.name) = LOWER(?)
            ORDER BY ft.created_at DESC
        """, (user_id, person)).fetchall()
    else:
        rows = conn.execute("""
            SELECT ft.*, fs.name FROM fund_transactions ft
            JOIN fund_shareholders fs ON ft.shareholder_id = fs.id
            WHERE fs.user_id = ?
            ORDER BY ft.created_at DESC
        """, (user_id,)).fetchall()

    return [
        {
            "created_at": r["created_at"],
            "name": r["name"],
            "type": r["type"],
            "usd_amount": round(r["usd_amount"], 2),
            "shares_delta": round(r["shares_delta"], 4),
            "nav_per_share": round(r["nav_per_share"], 4),
            "note": r["note"],
        }
        for r in rows
    ]


def _do_fund_deposit(conn, user_id: int, person: str, amount: float, note: str = ""):
    """Shared logic: record a deposit for a given user's fund."""
    nav = fund_mod.get_nav_per_share(conn, user_id)
    sh = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (user_id, person)
    ).fetchone()
    if not sh:
        raise HTTPException(404, f"Shareholder '{person}' not found")
    new_shares = amount / nav
    conn.execute("UPDATE fund_shareholders SET shares = shares + ? WHERE id = ?",
                 (new_shares, sh["id"]))
    conn.execute("""INSERT INTO fund_transactions
        (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
        VALUES (?, 'deposit', ?, ?, ?, ?)""",
        (sh["id"], amount, new_shares, nav, note))
    conn.commit()
    return {"message": f"{sh['name']}: +${amount:,.2f} -> +{new_shares:.4f} shares @ ${nav:.2f}/share"}


def _do_fund_withdrawal(conn, user_id: int, person: str, amount: float, note: str = ""):
    """Shared logic: record a withdrawal for a given user's fund."""
    nav = fund_mod.get_nav_per_share(conn, user_id)
    sh = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = ? AND LOWER(name) = LOWER(?)",
        (user_id, person)
    ).fetchone()
    if not sh:
        raise HTTPException(404, f"Shareholder '{person}' not found")
    shares_to_burn = amount / nav
    if shares_to_burn > sh["shares"]:
        raise HTTPException(400, f"{sh['name']} only has {sh['shares']:.4f} shares (${sh['shares'] * nav:,.2f})")
    conn.execute("UPDATE fund_shareholders SET shares = shares - ? WHERE id = ?",
                 (shares_to_burn, sh["id"]))
    conn.execute("""INSERT INTO fund_transactions
        (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
        VALUES (?, 'withdrawal', ?, ?, ?, ?)""",
        (sh["id"], amount, -shares_to_burn, nav, note))
    conn.commit()
    return {"message": f"{sh['name']}: -${amount:,.2f} -> -{shares_to_burn:.4f} shares @ ${nav:.2f}/share"}


def _do_fund_add_person(conn, user_id: int, name: str, shares: float = 0):
    """Shared logic: add a shareholder to a user's fund."""
    try:
        conn.execute("INSERT INTO fund_shareholders (user_id, name, shares) VALUES (?, ?, ?)",
                     (user_id, name, shares))
        conn.commit()
        return {"message": f"Added {name} with {shares} shares"}
    except Exception:
        raise HTTPException(409, f"'{name}' already exists")


@app.get("/api/fund/status")
def fund_status(request: Request):
    _require_fund_admin(request)
    conn = fund_mod.get_db()
    return _build_fund_status(conn, user_id=1)


@app.get("/api/fund/history")
def fund_history(request: Request, person: str = ""):
    _require_fund_admin(request)
    conn = fund_mod.get_db()
    return _build_fund_history(conn, user_id=1, person=person)


@app.post("/api/fund/deposit")
def fund_deposit(request: Request, data: FundTxIn):
    _require_fund_admin(request)
    conn = fund_mod.get_db()
    return _do_fund_deposit(conn, user_id=1, person=data.person, amount=data.amount, note=data.note)


@app.post("/api/fund/withdrawal")
def fund_withdrawal(request: Request, data: FundTxIn):
    _require_fund_admin(request)
    conn = fund_mod.get_db()
    return _do_fund_withdrawal(conn, user_id=1, person=data.person, amount=data.amount, note=data.note)


@app.post("/api/fund/add-person")
def fund_add_person(request: Request, data: FundPersonIn):
    _require_fund_admin(request)
    conn = fund_mod.get_db()
    return _do_fund_add_person(conn, user_id=1, name=data.name, shares=data.shares)


# ---------- Fund Management v2 (multi-tenant, wallet auth) ----------


class FundSetWalletTypeIn(BaseModel):
    wallet_id: int
    ownership_type: str = Field(pattern=r'^(pooled|personal)$')
    owner_name: str | None = None


class FundInitIn(BaseModel):
    wallets: list[dict]  # [{"wallet_id": int, "ownership_type": "pooled"|"personal", "owner_name": str|None}]
    shareholders: list[dict]  # [{"name": str, "shares": float}]


@app.get("/api/fund/v2/status")
def fund_v2_status(user_id: int = Depends(auth.require_user)):
    conn = fund_mod.get_db()
    return _build_fund_status(conn, user_id)


@app.get("/api/fund/v2/history")
def fund_v2_history(person: str = "", user_id: int = Depends(auth.require_user)):
    conn = fund_mod.get_db()
    return _build_fund_history(conn, user_id, person=person)


@app.post("/api/fund/v2/deposit")
def fund_v2_deposit(data: FundTxIn, user_id: int = Depends(auth.require_user)):
    conn = fund_mod.get_db()
    return _do_fund_deposit(conn, user_id, person=data.person, amount=data.amount, note=data.note)


@app.post("/api/fund/v2/withdrawal")
def fund_v2_withdrawal(data: FundTxIn, user_id: int = Depends(auth.require_user)):
    conn = fund_mod.get_db()
    return _do_fund_withdrawal(conn, user_id, person=data.person, amount=data.amount, note=data.note)


@app.post("/api/fund/v2/add-person")
def fund_v2_add_person(data: FundPersonIn, user_id: int = Depends(auth.require_user)):
    conn = fund_mod.get_db()
    return _do_fund_add_person(conn, user_id, name=data.name, shares=data.shares)


@app.post("/api/fund/v2/set-wallet-type")
def fund_v2_set_wallet_type(data: FundSetWalletTypeIn, user_id: int = Depends(auth.require_user)):
    """Mark a wallet as pooled or personal for this user's fund."""
    conn = fund_mod.get_db()
    # Verify the wallet belongs to this user
    w = db.get_wallet(user_id, data.wallet_id)
    if not w:
        raise HTTPException(404, "Wallet not found or not yours")
    conn.execute(
        "INSERT OR REPLACE INTO fund_wallet_ownership (wallet_id, user_id, ownership_type, owner_name) VALUES (?, ?, ?, ?)",
        (data.wallet_id, user_id, data.ownership_type, data.owner_name)
    )
    conn.commit()
    return {"ok": True, "wallet_id": data.wallet_id, "ownership_type": data.ownership_type}


@app.post("/api/fund/v2/init")
def fund_v2_init(data: FundInitIn, user_id: int = Depends(auth.require_user)):
    """Initialize a fund from the user's current wallets.

    Expects:
      wallets: [{wallet_id, ownership_type, owner_name?}, ...]
      shareholders: [{name, shares}, ...]
    """
    conn = fund_mod.get_db()

    # Check if already initialized
    existing = conn.execute(
        "SELECT COUNT(*) as c FROM fund_shareholders WHERE user_id = ?", (user_id,)
    ).fetchone()["c"]
    if existing > 0:
        raise HTTPException(409, "Fund already initialized. Use deposit/withdrawal to adjust.")

    # Classify wallets
    for wspec in data.wallets:
        w = db.get_wallet(user_id, wspec["wallet_id"])
        if not w:
            raise HTTPException(404, f"Wallet {wspec['wallet_id']} not found or not yours")
        conn.execute(
            "INSERT OR REPLACE INTO fund_wallet_ownership (wallet_id, user_id, ownership_type, owner_name) VALUES (?, ?, ?, ?)",
            (wspec["wallet_id"], user_id, wspec["ownership_type"], wspec.get("owner_name"))
        )

    # Get pooled value for this user
    conn.commit()  # commit wallet ownership first so pooled value query works
    pooled_value = fund_mod.get_pooled_value(conn, user_id)

    # Create shareholders
    total_shares = sum(s["shares"] for s in data.shareholders)
    for s in data.shareholders:
        conn.execute(
            "INSERT INTO fund_shareholders (user_id, name, shares) VALUES (?, ?, ?)",
            (user_id, s["name"], s["shares"])
        )
        sh_id = conn.execute(
            "SELECT id FROM fund_shareholders WHERE user_id = ? AND name = ?",
            (user_id, s["name"])
        ).fetchone()["id"]

        if s["shares"] > 0 and total_shares > 0:
            usd_value = pooled_value * (s["shares"] / total_shares)
            nav = pooled_value / total_shares if total_shares > 0 else 1.0
            conn.execute("""
                INSERT INTO fund_transactions
                (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
                VALUES (?, 'init', ?, ?, ?, 'Initial allocation')
            """, (sh_id, usd_value, s["shares"], nav))

    conn.commit()
    return {
        "ok": True,
        "shareholders": len(data.shareholders),
        "wallets_classified": len(data.wallets),
        "pooled_value": round(pooled_value, 2),
    }


@app.get("/api/fund/v2/wallets")
def fund_v2_wallets(user_id: int = Depends(auth.require_user)):
    """List user's wallets with their fund ownership classification."""
    conn = fund_mod.get_db()
    wallets = db.list_wallets(user_id)
    ownership_rows = conn.execute(
        "SELECT wallet_id, ownership_type, owner_name FROM fund_wallet_ownership WHERE user_id = ?",
        (user_id,)
    ).fetchall()
    ownership_map = {r["wallet_id"]: {"type": r["ownership_type"], "owner": r["owner_name"]} for r in ownership_rows}

    result = []
    for w in wallets:
        ow = ownership_map.get(w["id"])
        # Get wallet value from holdings
        val_row = conn.execute(
            "SELECT COALESCE(SUM(usd_value), 0) as total FROM holdings WHERE wallet_id = ?",
            (w["id"],)
        ).fetchone()
        result.append({
            "id": w["id"],
            "label": w["label"],
            "address": w["address"],
            "chain": w.get("chain", ""),
            "value": round(val_row["total"], 2) if val_row else 0,
            "ownership_type": ow["type"] if ow else None,
            "owner_name": ow["owner"] if ow else None,
        })
    return result


# ---------- Portfolio Rebalancing ----------

@app.get("/api/rebalance/strategies")
def get_strategies(user_id: int = Depends(auth.require_user)):
    """Get all available strategies (presets + user custom)."""
    # Get preset strategies
    presets = db.get_preset_strategies()
    # Get user custom strategies
    user_strategies = db.get_user_strategies(user_id)

    # Parse allocations JSON for each strategy
    def parse_strategy(row):
        s = dict(row)
        s["allocations"] = json.loads(s["allocations"]) if isinstance(s["allocations"], str) else s["allocations"]
        return s

    return {
        "presets": [parse_strategy(row) for row in presets],
        "custom": [parse_strategy(row) for row in user_strategies]
    }


class StrategyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    allocations: dict = Field(...)  # {symbol: percentage}


@app.post("/api/rebalance/strategies")
def create_strategy(body: StrategyCreate, user_id: int = Depends(auth.require_user)):
    """Create a new custom rebalancing strategy."""
    rate_limit("rebalance_create", user_id, limit=20, window_seconds=3600)

    # Validate allocations
    allocations_json = json.dumps(body.allocations)

    # Validate sum is ~100%
    total = sum(body.allocations.values())
    if not (99.0 <= total <= 101.0):
        raise HTTPException(400, "Allocations must sum to 100%")

    # Create strategy
    strategy_id = db.create_strategy(user_id, body.name, allocations_json)
    log.info(f"User {user_id} created strategy {strategy_id}: {body.name}")

    return {"success": True, "strategy_id": strategy_id}


@app.put("/api/rebalance/strategies/{strategy_id}")
def update_strategy(strategy_id: int, body: StrategyCreate, user_id: int = Depends(auth.require_user)):
    """Update an existing custom strategy."""

    allocations_json = json.dumps(body.allocations)

    # Validate sum is ~100%
    total = sum(body.allocations.values())
    if not (99.0 <= total <= 101.0):
        raise HTTPException(400, "Allocations must sum to 100%")

    # Update strategy
    success = db.update_strategy(strategy_id, user_id, body.name, allocations_json)
    if not success:
        raise HTTPException(404, "Strategy not found or not owned by user")

    log.info(f"User {user_id} updated strategy {strategy_id}")
    return {"success": True}


@app.delete("/api/rebalance/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, user_id: int = Depends(auth.require_user)):
    """Delete a custom strategy."""

    success = db.delete_strategy(strategy_id, user_id)
    if not success:
        raise HTTPException(404, "Strategy not found or not owned by user")

    log.info(f"User {user_id} deleted strategy {strategy_id}")
    return {"success": True}


class RebalanceCalculate(BaseModel):
    target_allocations: dict[str, float] = Field(..., description="symbol → target percentage (must sum to ~100)")
    total_value_usd: float = Field(..., ge=0)


@app.post("/api/rebalance/calculate")
def calculate_rebalance(body: RebalanceCalculate, user_id: int = Depends(auth.require_user)):
    """Calculate required swaps to reach target allocation."""
    rate_limit("rebalance_calculate", user_id, limit=60, window_seconds=60)

    # Get current holdings
    holdings_rows = db.list_holdings_by_symbol(user_id)
    if not holdings_rows:
        raise HTTPException(400, "No holdings found")

    # Pull raw contract address + price per symbol directly from DB
    # (list_holdings_by_symbol aggregates and loses per-row contract info)
    with db.get_conn() as _c:
        _raw = _c.execute(
            """SELECT symbol, chain, contract, balance, usd_price, usd_value
               FROM holdings WHERE user_id=?""",
            [user_id]
        ).fetchall()
    # Build per-symbol info keyed by UPPER symbol (conservative: take the row with highest value)
    holding_meta: dict[str, dict] = {}
    for r in _raw:
        sym_up = (r["symbol"] or "").upper()
        val = float(r["usd_value"] or 0)
        if sym_up not in holding_meta or val > holding_meta[sym_up]["usd_value"]:
            holding_meta[sym_up] = {
                "symbol":    r["symbol"],                           # original case
                "chain":     (r["chain"] or "SOLANA").upper(),      # ← uppercase!
                "contract":  r["contract"] or None,
                "balance":   float(r["balance"] or 0),
                "usd_price": float(r["usd_price"] or 0),
                "usd_value": float(r["usd_value"] or 0),
            }

    # Calculate current allocation
    current = {}
    total_value = 0.0
    for row in holdings_rows:
        symbol = row["symbol"]
        value = float(row["usd_value"] or 0)
        current[symbol] = current.get(symbol, 0) + value
        total_value += value

    target = body.target_allocations
    effective_total = total_value if total_value > 0 else body.total_value_usd

    sells: list[dict] = []
    buys:  list[dict] = []

    for symbol, target_pct in target.items():
        # Match target symbol to holding (case-insensitive)
        meta = holding_meta.get(symbol.upper(), {})
        current_value = current.get(meta.get("symbol", symbol), 0) or current.get(symbol, 0)
        target_value  = (target_pct / 100) * effective_total
        gap = target_value - current_value

        if abs(gap) <= 1.0:
            continue

        if gap < 0:
            sells.append({
                "symbol":   meta.get("symbol", symbol),
                "chain":    meta.get("chain", "SOLANA"),
                "contract": meta.get("contract"),
                "usd_price": meta.get("usd_price", 0),
                "amount_usd": round(abs(gap), 2),
            })
        else:
            buys.append({"symbol": symbol, "amount_usd": round(gap, 2)})

    # Also sell assets not in target at all
    for sym_raw, current_value in current.items():
        sym_up = sym_raw.upper()
        if sym_up not in {k.upper() for k in target} and current_value > 1.0:
            meta = holding_meta.get(sym_up, {})
            sells.append({
                "symbol":    meta.get("symbol", sym_raw),
                "chain":     meta.get("chain", "SOLANA"),
                "contract":  meta.get("contract"),
                "usd_price": meta.get("usd_price", 0),
                "amount_usd": round(current_value, 2),
            })

    swaps_needed = (
        [{"action": "sell", "symbol": s["symbol"], "chain": s["chain"], "amount_usd": s["amount_usd"]} for s in sells] +
        [{"action": "buy",  "symbol": b["symbol"], "amount_usd": b["amount_usd"]} for b in buys]
    )

    # Generate swap_pairs — each pair uses the actual token amount (not USD)
    # so rango_client can convert it to base units correctly.
    swap_pairs: list[dict] = []
    sell_pool = [dict(s) for s in sells]

    for buy in buys:
        buy_sym       = buy["symbol"]
        buy_remaining = buy["amount_usd"]
        to_meta       = holding_meta.get(buy_sym.upper(), {})

        for sell in sell_pool:
            if buy_remaining <= 0:
                break
            if sell["amount_usd"] <= 0:
                continue

            alloc_usd   = min(buy_remaining, sell["amount_usd"])
            from_price  = sell["usd_price"]
            # Convert USD slice → actual token amount for the from-token
            from_amount = (alloc_usd / from_price) if from_price > 0 else 0

            from_chain = sell["chain"]
            to_chain   = to_meta.get("chain") or from_chain  # default same network

            swap_pairs.append({
                "from_symbol":   sell["symbol"],
                "from_chain":    from_chain,
                "from_contract": sell["contract"],
                "to_symbol":     buy_sym,
                "to_chain":      to_chain,
                "from_amount":   round(from_amount, 8),   # actual token amount
                "amount_usd":    round(alloc_usd, 2),
            })
            sell["amount_usd"] -= alloc_usd
            buy_remaining      -= alloc_usd

    return {
        "current_allocation": current,
        "target_allocation":  target,
        "swaps_needed":       swaps_needed,
        "swap_pairs":         swap_pairs,
        "total_value":        total_value,
    }


class _SwapPair(BaseModel):
    from_symbol:   str         = Field(..., min_length=1, max_length=MAX_SYMBOL_LEN)
    from_chain:    str         = Field(default="SOLANA", max_length=32)
    from_contract: str | None  = Field(default=None, max_length=256)
    to_symbol:     str         = Field(..., min_length=1, max_length=MAX_SYMBOL_LEN)
    to_chain:      str         = Field(default="SOLANA", max_length=32)
    amount:        float       = Field(..., ge=0)   # actual token amount (not USD)


class RebalanceQuote(BaseModel):
    swaps: list[_SwapPair] = Field(..., min_length=1, max_length=50)
    slippage: float = Field(default=1.0, ge=0.0, le=50.0)


@app.post("/api/rebalance/quote")
def quote_rebalance(body: RebalanceQuote, user_id: int = Depends(auth.require_user)):
    """Get quotes for a list of swaps using Rango."""
    rate_limit("rebalance_quote", user_id, limit=30, window_seconds=60)

    import rango_client

    quotes = []
    total_cost = 0

    for swap in body.swaps:
        try:
            quote = rango_client.quote_swap(
                swap.from_symbol,
                swap.to_symbol,
                swap.amount,
                from_chain=swap.from_chain,
                to_chain=swap.to_chain,
                from_contract=swap.from_contract,
                slippage=body.slippage,
            )
            quotes.append({
                "from_symbol":   swap.from_symbol,
                "from_chain":    swap.from_chain,
                "from_contract": swap.from_contract,
                "to_symbol":     swap.to_symbol,
                "to_chain":      swap.to_chain,
                "input_amount":  swap.amount,
                "output_amount": quote["output_amount"],
                "price_impact":  quote["price_impact"],
                "fee_usd":       quote["fee_usd"],
                "route_id":      quote["route_id"],
            })
            total_cost += quote["fee_usd"]
        except Exception as e:
            log.error(f"Failed to quote swap {swap.from_symbol}→{swap.to_symbol}: {e}")
            quotes.append({
                "from_symbol": swap.from_symbol,
                "to_symbol": swap.to_symbol,
                "input_amount": swap.amount,
                "error": str(e)
            })

    return {
        "quotes": quotes,
        "total_cost_usd": round(total_cost, 2)
    }


class _QuoteItem(BaseModel):
    from_symbol:   str        = Field(..., min_length=1, max_length=MAX_SYMBOL_LEN)
    from_chain:    str        = Field(default="SOLANA", max_length=32)
    from_contract: str | None = Field(default=None, max_length=256)
    to_symbol:     str        = Field(..., min_length=1, max_length=MAX_SYMBOL_LEN)
    to_chain:      str        = Field(default="SOLANA", max_length=32)
    input_amount:  float      = Field(default=0, ge=0)
    output_amount: float      = Field(default=0, ge=0)
    price_impact:  float      = Field(default=0)
    fee_usd:       float      = Field(default=0, ge=0)
    route_id:      str | None = Field(default=None, max_length=256)
    error:         str | None = Field(default=None, max_length=512)


class RebalanceExecute(BaseModel):
    quotes: list[_QuoteItem] = Field(..., min_length=1, max_length=50)
    strategy_id: int | None = None


@app.post("/api/rebalance/execute")
def execute_rebalance(body: RebalanceExecute, user_id: int = Depends(auth.require_user)):
    """Build transactions for rebalance execution. Returns unsigned tx for wallet signing."""
    rate_limit("rebalance_execute", user_id, limit=10, window_seconds=3600)

    # Get user's wallet address from session
    me = db.get_user(user_id)
    if not me:
        raise HTTPException(404, "User not found")

    user_address = me["primary_address"]

    import rango_client

    transactions = []
    errors = []

    for quote in body.quotes:
        if quote.error:
            errors.append({"from_symbol": quote.from_symbol, "to_symbol": quote.to_symbol, "error": quote.error})
            continue

        try:
            # Build transaction for this swap
            tx_data = rango_client.build_swap_transaction(
                from_token=quote.from_symbol,
                to_token=quote.to_symbol,
                amount=quote.input_amount,
                from_address=user_address,
                to_address=user_address,
                from_chain=quote.from_chain,
                to_chain=quote.to_chain,
                from_contract=quote.from_contract,
            )
            raw_tx  = tx_data.get("tx") or {}
            tx_type = raw_tx.get("type", "")  # "EVM" | "SOLANA" | "TRANSFER" etc.

            # Compute raw from-amount in base units (needed by frontend for allowance check)
            from_info = rango_client._token_info(
                quote.from_symbol, quote.from_chain,
                override_address=quote.from_contract
            )
            from_amount_raw = rango_client._to_base_units(quote.input_amount, from_info["decimals"])

            # Chain ID: Rango returns blockChain as an object {"chainId": "1", ...}
            block_chain = raw_tx.get("blockChain") or {}
            chain_id = block_chain.get("chainId") if isinstance(block_chain, dict) else None

            transactions.append({
                "from_symbol":       quote.from_symbol,
                "from_chain":        quote.from_chain,
                "to_symbol":         quote.to_symbol,
                "to_chain":          quote.to_chain,
                "tx_type":           tx_type,
                # Solana: base64 serialised transaction
                "serialized_tx":     raw_tx.get("serializedMessage"),
                # EVM swap tx
                "evm_tx": {
                    "to":                   raw_tx.get("txTo"),
                    "data":                 raw_tx.get("txData"),
                    "value":                raw_tx.get("value") or "0x0",
                    "gas":                  raw_tx.get("gasLimit"),
                    "maxFeePerGas":         raw_tx.get("maxFeePerGas"),
                    "maxPriorityFeePerGas": raw_tx.get("maxPriorityFeePerGas") or raw_tx.get("priorityGasPrice"),
                    "chainId":              chain_id,
                } if tx_type == "EVM" else None,
                # ERC-20 approval (Rango pre-builds this for us)
                "approve_to":        raw_tx.get("approveTo"),   # token contract address
                "approve_data":      raw_tx.get("approveData"), # encoded approve(spender, amount)
                "spender":           raw_tx.get("txTo"),        # Rango router = spender
                "from_amount_raw":   from_amount_raw,            # for allowance comparison
                "metadata":          tx_data.get("route"),
            })
        except Exception as e:
            log.error(f"Failed to build transaction for {quote.from_symbol}→{quote.to_symbol}: {e}")
            errors.append({
                "from_symbol": quote.from_symbol,
                "to_symbol": quote.to_symbol,
                "error": str(e)
            })

    # Record rebalance attempt
    swaps_json = json.dumps([{
        "from": q.from_symbol,
        "to": q.to_symbol,
        "amount": q.input_amount
    } for q in body.quotes])

    rebalance_id = db.record_rebalance(
        user_id=user_id,
        strategy_id=body.strategy_id,
        swaps=swaps_json,
        status="pending",
        tx_hashes=None,
        error=None if not errors else json.dumps(errors)
    )

    log.info(f"User {user_id} initiated rebalance {rebalance_id} with {len(transactions)} swaps")

    return {
        "rebalance_id": rebalance_id,
        "transactions": transactions,
        "errors": errors,
        "instructions": "Sign and submit these transactions with your wallet. After submission, call /api/rebalance/confirm with tx hashes."
    }


class RebalanceConfirm(BaseModel):
    rebalance_id: int = Field(..., gt=0)
    tx_hashes: list[str] = Field(default_factory=list, max_length=100)
    status: str = Field(..., pattern="^(success|partial|failed)$")


@app.post("/api/rebalance/confirm")
def confirm_rebalance(body: RebalanceConfirm, user_id: int = Depends(auth.require_user)):
    """Update rebalance history with transaction hashes after execution."""

    # Update the rebalance record
    with db.get_conn() as c:
        c.execute(
            """UPDATE rebalance_history
               SET tx_hashes = ?, status = ?
               WHERE id = ? AND user_id = ?""",
            (json.dumps(body.tx_hashes), body.status, body.rebalance_id, user_id)
        )

    log.info(f"User {user_id} confirmed rebalance {body.rebalance_id} with status {body.status}")

    return {"success": True}


@app.get("/api/rebalance/history")
def get_rebalance_history(limit: int = 50, user_id: int = Depends(auth.require_user)):
    """Get rebalance execution history for this user."""

    history = db.get_rebalance_history(user_id, limit=min(limit, 100))

    return {"history": [dict(row) for row in history]}


@app.get("/api/xstocks")
def get_xstocks():
    """Get list of available xStocks (tokenized stocks on Solana)."""
    import rango_client
    return rango_client.get_xstocks_list()


# ---------- Health ----------

@app.get("/api/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
