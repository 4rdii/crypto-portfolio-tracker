"""Web3 wallet authentication.

Two sign-in flows, both following the "sign a random nonce" pattern:

  1. EVM  — EIP-4361 "Sign-In with Ethereum" (SIWE). Uses the `siwe` library to
     parse and verify the signed message. Covers 600+ EVM wallets via either an
     injected EIP-1193 provider (MetaMask/Rabby/Coinbase/Brave/etc.) or
     WalletConnect v2 for mobile wallets.
  2. Solana — "Sign-In with Solana" (SIWS). Same pattern but signatures are
     ed25519, verified via pynacl. Covers Phantom / Solflare / Backpack.

Bitcoin and other chains aren't currently covered — no universal web-signing
standard exists for BTC, and most users don't want to copy/paste signatures.
They can still be added read-only after a user is authenticated.

Session model: on successful verification we set Starlette session["user_id"].
The `require_user` dependency pulls it back out on every protected route.
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime

from fastapi import HTTPException, Request

import db
from applog import log

# Expected domain(s) the SIWE message may name. Defaults to whatever the
# incoming request advertises (Host header), which is fine for single-origin
# deployments. Operators behind a CDN / reverse proxy can pin an explicit
# allowlist via SIWE_EXPECTED_DOMAINS="app.example.com,example.com" to reject
# phishing sites that trick a wallet into signing a message naming their own
# origin but replaying the signature here.
def _allowed_siwe_domains() -> set[str]:
    raw = os.getenv("SIWE_EXPECTED_DOMAINS", "").strip()
    if not raw:
        try:
            # Read from the .env file the rest of the app uses
            from scanner import ENV
            raw = ENV.get("SIWE_EXPECTED_DOMAINS", "").strip()
        except Exception:
            raw = ""
    if not raw:
        return set()
    return {d.strip().lower() for d in raw.split(",") if d.strip()}

# EIP-4361 requires nonces to match [a-zA-Z0-9]{8,} — no punctuation, no dashes.
# `secrets.token_urlsafe` returns base64url which includes '-' and '_' and
# therefore breaks the siwe-py parser. Use an explicit alphanumeric alphabet.
_NONCE_ALPHABET = string.ascii_letters + string.digits


# ---------- Nonce issuance ----------

def issue_nonce(address: str, chain_type: str) -> str:
    """Generate a single-use random nonce bound to (address, chain_type).

    Stored server-side for 10 minutes and consumed on successful verification —
    prevents replay of a previously-signed message.
    """
    nonce = "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(24))
    db.store_nonce(nonce, address, chain_type)
    return nonce


# ---------- EVM / SIWE ----------

def verify_siwe(message: str, signature: str, request: Request | None = None) -> str:
    """Verify an EIP-4361 SIWE message + signature. Returns the lowercase address.

    Uses the `siwe` library for full spec compliance (domain/uri/version/chain
    ID/nonce/expiration checks). Raises HTTPException on any failure.

    Domain-binding: the SIWE message declares a `domain` field naming the
    origin that asked for the signature. A phishing site can make the wallet
    sign a message naming their own domain, then replay that signature
    against us — if we don't check the declared domain, we accept it. We now
    verify the message domain matches either SIWE_EXPECTED_DOMAINS (env
    allowlist) or the current request's Host header. The nonce is also
    passed to siwe_msg.verify() so the signed nonce matches what the wallet
    actually put in the message (defense-in-depth — we also consume it from
    the DB).

    Note: siwe-py is strict about a handful of things that normal JS/TS clients
    get wrong — alphanumeric-only nonces, no milliseconds in Issued At, etc.
    We log the full repr of any parse error so we can debug the actual error
    instead of the empty-string ValueErrors the parser sometimes throws.
    """
    try:
        from siwe import SiweMessage
    except ImportError:
        raise HTTPException(500, "siwe package not installed on server")

    try:
        siwe_msg = SiweMessage.from_message(message)
    except Exception as e:
        err = repr(e) if not str(e) else str(e)
        print(f"[auth] SIWE parse failed: {err}")
        print(f"[auth] offending message:\n{message}")
        raise HTTPException(400, "Invalid wallet signature — please try again")

    address = (siwe_msg.address or "").lower()
    nonce = siwe_msg.nonce or ""
    msg_domain = (siwe_msg.domain or "").lower()

    # Domain check — happens BEFORE nonce consumption so a phishing attempt
    # doesn't burn a nonce on the way out.
    allowed = _allowed_siwe_domains()
    if not allowed and request is not None:
        # Derive the expected domain from the incoming request host. Strips
        # the port since SIWE domains typically don't include it.
        host = (request.headers.get("host") or request.url.hostname or "").lower()
        host = host.split(":")[0]
        if host:
            allowed = {host}
    if allowed:
        # Normalise the message domain too (siwe-py keeps port; strip it)
        msg_host = msg_domain.split(":")[0]
        if msg_host not in allowed:
            log.warning("auth.siwe_domain_mismatch addr=%s msg_domain=%s allowed=%s",
                        address[:10], msg_domain, sorted(allowed))
            raise HTTPException(400, "SIWE message domain is not allowed")

    # Confirm the server actually issued this nonce for this address and it
    # hasn't been used yet.
    if not db.consume_nonce(nonce, address):
        raise HTTPException(400, "Invalid or expired nonce")

    # Cryptographic signature verification. Pass the nonce explicitly so
    # siwe-py double-checks the signed nonce matches what we expect (it
    # already checks the message-embedded nonce; this pins what we verified).
    try:
        siwe_msg.verify(signature, nonce=nonce)
    except Exception as e:
        err = repr(e) if not str(e) else str(e)
        log.warning("auth.siwe_verify_failed addr=%s err=%s", address[:10], err[:120])
        raise HTTPException(401, f"Signature verification failed: {err or type(e).__name__}")

    log.info("auth.siwe_verified addr=%s domain=%s", address[:10], msg_domain)
    return address


# ---------- Solana / SIWS ----------

def verify_siws(address: str, message: str, signature_b58: str, nonce: str) -> str:
    """Verify a Solana sign-in signature (ed25519).

    `address` is the base58 public key. `signature_b58` is the base58-encoded
    signature. `message` is the plaintext the wallet signed (must embed the
    same nonce we issued). Returns the verified address on success.
    """
    try:
        import base58
        import nacl.signing
        import nacl.exceptions
    except ImportError:
        raise HTTPException(500, "base58 / pynacl not installed on server")

    # Make sure the nonce actually appears in the message — otherwise a valid
    # signature over a different message could be replayed.
    if nonce not in message:
        raise HTTPException(400, "Message is missing the issued nonce")

    if not db.consume_nonce(nonce, address):
        raise HTTPException(400, "Invalid or expired nonce")

    try:
        pubkey_bytes = base58.b58decode(address)
        sig_bytes = base58.b58decode(signature_b58)
        verify_key = nacl.signing.VerifyKey(pubkey_bytes)
        verify_key.verify(message.encode("utf-8"), sig_bytes)
    except nacl.exceptions.BadSignatureError:
        raise HTTPException(401, "Signature verification failed")
    except Exception as e:
        raise HTTPException(400, f"Could not verify signature: {e}")

    return address


# ---------- User resolution ----------

def login_or_create_user(address: str, chain_type: str) -> int:
    """Return the user_id for this address, creating a profile on first sign-in.

    The first wallet someone signs in with becomes their `primary_address` —
    that's the identity of the account. Subsequent wallet links attach to the
    same user_id via the wallets table.

    On first sign-in the signing wallet is ALSO auto-linked as a verified
    wallet so the user doesn't have to sign a second challenge just to add
    their own login wallet (which they already proved ownership of by
    signing the nonce). Additional wallets still require a link-signature.
    """
    address_norm = address.lower() if chain_type == "evm" else address
    existing = db.get_user_by_address(address_norm)
    if existing:
        db.touch_user_login(existing["id"])
        # Back-fill: if an older account exists but the primary wallet was
        # never linked (pre-fix users), link it now on next login so the
        # fix applies retroactively without requiring a re-signature.
        _ensure_primary_wallet_linked(existing["id"], address_norm, chain_type)
        return existing["id"]
    user_id = db.create_user(address_norm, chain_type)
    _ensure_primary_wallet_linked(user_id, address_norm, chain_type)
    return user_id


def _ensure_primary_wallet_linked(user_id: int, address: str, chain_type: str):
    """Add the login wallet to the user's wallet list if it isn't already.

    Verified=1 because the login signature we just accepted IS proof of
    ownership — there's no stronger link we could ask for. Label defaults to
    "Main" for the primary wallet; users can rename it later.
    """
    # Already linked? nothing to do — keep the existing label/settings.
    for w in db.list_wallets(user_id):
        if w["address"].lower() == address.lower():
            return
    # chain_type maps directly: 'evm' | 'solana' | 'bitcoin' are valid chain
    # values everywhere else in the app.
    db.add_wallet(user_id, address, "Main", chain_type, verified=1)


def link_wallet_to_user(user_id: int, address: str, chain_type: str, label: str):
    """After a user signs a link-wallet challenge, attach the wallet to their profile.

    Idempotent: if the wallet is already linked, returns its id.
    """
    # Detect chain from address if caller didn't specify explicitly
    from scanner import detect_chain
    chain = detect_chain(address)
    if chain == "unknown":
        raise HTTPException(400, "Could not detect chain for address")

    # Already linked? return existing
    for w in db.list_wallets(user_id):
        if w["address"].lower() == address.lower():
            return w["id"]

    return db.add_wallet(user_id, address, label, chain, verified=1)


# ---------- FastAPI dependency ----------

def require_user(request: Request) -> int:
    """Dependency: extract the current user_id from the session cookie.

    Returns the user_id integer. Raises 401 if no valid session. Use in a
    FastAPI route like:

        @app.get("/api/...")
        def route(user_id: int = Depends(require_user)):
            ...
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    return int(user_id)


def current_user_optional(request: Request) -> int | None:
    """Same as `require_user` but returns None instead of raising when not signed in."""
    user_id = request.session.get("user_id")
    return int(user_id) if user_id else None
