"""SPL token allowlist and spam-filter for the Solana history importer.

Why a whitelist rather than a blacklist?
-----------------------------------------
Solana token creation is essentially free — anyone can deploy a new SPL mint
in a single transaction for ~0.003 SOL (< $0.50 at most price points). Spam
airdrop campaigns routinely create thousands of distinct mint addresses daily,
many of them with names and symbols that clone well-known projects exactly.
A blacklist can never keep up: you need at least one victim wallet to receive
a scam token before you can add it, and the attacker regenerates fresh mint
addresses faster than any team can ban them.

An allowlist inverts the burden. The set of SPL tokens that generate real
taxable events for 99% of retail wallets is small and stable. It includes:
  - Native SOL (wrapped for DeFi purposes)
  - The two dominant stablecoins (USDC, USDT)
  - The major liquid-staking derivatives (mSOL, jitoSOL, bSOL)
  - The top Solana-native DEX and governance tokens (JUP, RAY, ORCA, PYTH, JTO)
  - A curated set of high-market-cap memes (BONK, WIF, POPCAT, WEN, PENGU)
  - Cross-chain tokens that frequently appear in Solana wallets (W/Wormhole)
  - Infrastructure tokens with real on-chain utility (HNT, SHDW, MNDE, SAMO)

Every entry in LEGIT_MINTS was verified against Solscan, Solana Explorer, and
at least one exchange listing page (Coinbase/Binance/Phantom) before inclusion.
We intentionally err on the side of OMISSION — a missing legitimate token means
the user files a manual transaction; a spurious spam token means polluted cost
basis records with phantom gains. The first error is recoverable; the second
requires a full re-import after manual cleanup.

The is_spam_token() function provides a secondary pass for tokens NOT in the
allowlist: if the transaction type is "SWAP" or "TRANSFER" (the user actively
moved/traded it), we preserve the row and let the user decide. If the type is
anything else (AIRDROP, UNKNOWN, CONTRACT_INTERACTION …) with an unlisted mint,
we drop it silently — that is the signature of a dust/spam airdrop and keeping
it would inject a $0-cost-basis "buy" that inflates future reported gains.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Verified mint addresses for legitimate SPL tokens
# ---------------------------------------------------------------------------
#
# Schema: {mint_address: {"symbol": str, "decimals": int}}
#
# Source verification citations (checked April 2026 — these mints are
# controlled by immutable freeze-disabled programs and cannot be reassigned):
#   - Solana Explorer (explorer.solana.com)
#   - Solscan (solscan.io)
#   - Phantom wallet token registry
#   - Official project documentation where noted
#
# Native SOL uses the canonical "Wrapped SOL" mint address because Helius
# Enhanced Transactions API represents native SOL movements via the WSOL
# mint in tokenTransfers. The history importer normalizes WSOL → SOL at
# emit time so the tax engine sees "SOL" not "WSOL".

LEGIT_MINTS: dict[str, dict] = {
    # ---- Native / Wrapped SOL ----
    # So11111111111111111111111111111111111111112 is the canonical Wrapped SOL mint.
    # It appears in tokenTransfers for native SOL swaps on Jupiter and other DEXes.
    "So11111111111111111111111111111111111111112": {
        "symbol": "SOL",
        "decimals": 9,
    },

    # ---- Stablecoins ----
    # USDC: Circle's USD Coin on Solana. The dominant stable; every major DEX
    # pair uses it. Verified: solscan.io/token/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
        "symbol": "USDC",
        "decimals": 6,
    },
    # USDT: Tether on Solana. Second-most used stable for trading pairs.
    # Verified: solscan.io/token/Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": {
        "symbol": "USDT",
        "decimals": 6,
    },

    # ---- Liquid Staking Tokens (LSTs) ----
    # mSOL: Marinade Finance liquid-staked SOL. Largest historical LST by TVL.
    # Verified: solscan.io/token/mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So
    # Docs: docs.marinade.finance/marinade-protocol/protocol-overview/marinade-liquid/what-is-msol
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": {
        "symbol": "mSOL",
        "decimals": 9,
    },
    # jitoSOL: Jito liquid-staked SOL with MEV reward sharing.
    # Verified: solscan.io/token/J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn
    # Docs: jito.network/docs/jitosol
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": {
        "symbol": "jitoSOL",
        "decimals": 9,
    },
    # bSOL: BlazeStake liquid-staked SOL. Non-custodial, Solana-Foundation
    # supported stake pool; $300M+ TVL across Solana DeFi.
    # Verified: solscan.io/token/bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": {
        "symbol": "bSOL",
        "decimals": 9,
    },

    # ---- DEX / Protocol Tokens ----
    # JUP: Jupiter Exchange governance token. Jupiter is the leading DEX
    # aggregator on Solana by volume. Launched Jan 2024 with a massive airdrop.
    # Verified: solscan.io/token/JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": {
        "symbol": "JUP",
        "decimals": 6,
    },
    # RAY: Raydium AMM governance and fee-sharing token. One of the oldest and
    # most liquid DEXes on Solana.
    # Verified: solscan.io/token/4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {
        "symbol": "RAY",
        "decimals": 6,
    },
    # ORCA: Orca DEX governance token. Concentrated liquidity AMM. Known for
    # clean UX and wide DeFi integrations.
    # Verified: solscan.io/token/orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE
    "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE": {
        "symbol": "ORCA",
        "decimals": 6,
    },
    # PYTH: Pyth Network oracle governance token. The dominant price-feed oracle
    # on Solana; also deployed cross-chain via Wormhole.
    # Verified: solscan.io/token/HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": {
        "symbol": "PYTH",
        "decimals": 6,
    },
    # JTO: Jito governance token (separate from jitoSOL). Governs the Jito
    # protocol's fee distribution and parameter changes.
    # Verified: solscan.io/token/jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": {
        "symbol": "JTO",
        "decimals": 9,
    },
    # JLP: Jupiter Perpetuals Liquidity Provider token. Represents a share of
    # Jupiter's perp trading pool. Acts like an index of blue-chip assets.
    # Verified: solscan.io/token/27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4
    "27G8MtK7VtTcCHkpASjSDdkWWYfoqT6ggEuKidVJidD4": {
        "symbol": "JLP",
        "decimals": 6,
    },

    # ---- Meme Tokens ----
    # BONK: First major Solana meme coin with broad exchange listings. Launched
    # Dec 2022 as a community airdrop to NFT holders and contributors.
    # Verified: solscan.io/token/DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": {
        "symbol": "BONK",
        "decimals": 5,
    },
    # WIF: dogwifhat meme coin. Reached top-20 market cap in 2024.
    # Verified: solscan.io/token/EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm
    # Confirmed by Coinbase exchange listing.
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": {
        "symbol": "WIF",
        "decimals": 6,
    },
    # POPCAT: Popcat meme coin. Confirmed by Coinbase Assets official tweet at
    # @CoinbaseAssets: "contract address for Popcat (SOL) (POPCAT) is 7GCi..."
    # Verified: solscan.io/token/7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr": {
        "symbol": "POPCAT",
        "decimals": 9,
    },
    # WEN: Wen meme coin, created by Jupiter team, launched Jan 2024 as a
    # community token before the JUP airdrop. 1 trillion supply.
    # Verified: solscan.io/token/WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk
    "WENWENvqqNya429ubCdR81ZmD69brwQaaBYY6p3LCpk": {
        "symbol": "WEN",
        "decimals": 5,
    },
    # PENGU: Pudgy Penguins official Solana token. Cross-chain between ETH and SOL.
    # Verified: solscan.io/token/2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv": {
        "symbol": "PENGU",
        "decimals": 6,
    },
    # SAMO: Samoyed Coin, Solana's first meme coin community token. Old enough
    # that it appears in many wallets with long Solana history.
    # Verified: solscan.io/token/7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU
    "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU": {
        "symbol": "SAMO",
        "decimals": 9,
    },

    # ---- Infrastructure / Utility ----
    # W: Wormhole cross-chain messaging governance token. Native Solana SPL,
    # bridged to other chains via the Wormhole protocol itself.
    # Verified: solscan.io/token/85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ
    # Note: the last characters are ...QAmQ not ...bwi57 — the brief contained
    # a typo; this is the address confirmed by wormhole.com/ecosystem/w-token.
    "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ": {
        "symbol": "W",
        "decimals": 6,
    },
    # HNT: Helium Network Token. Migrated from native Helium chain to Solana
    # in April 2023. The canonical post-migration SPL mint.
    # Verified: solscan.io/token/hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux
    # Docs: docs.helium.com/tokens/hnt-token/
    "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux": {
        "symbol": "HNT",
        "decimals": 8,
    },
    # SHDW: Shadow Drive decentralized storage token. Powers the GenesysGo
    # Shadow ecosystem on Solana.
    # Verified: solscan.io/token/SHDWyBxihqiCj6YekG2GUr7wqKLeLAMK1gHZck9pL6y
    "SHDWyBxihqiCj6YekG2GUr7wqKLeLAMK1gHZck9pL6y": {
        "symbol": "SHDW",
        "decimals": 9,
    },
    # MNDE: Marinade governance token (separate from mSOL). Controls protocol
    # parameters and treasury.
    # Verified: solscan.io/token/MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey
    "MNDEFzGvMt87ueuHvVU9VcTqsAP5b3fTGPsHuuPA5ey": {
        "symbol": "MNDE",
        "decimals": 9,
    },
}

# ---------------------------------------------------------------------------
# Stablecoin subset — price always resolves to $1.00
# ---------------------------------------------------------------------------
#
# These are the SPL stables whose USD peg is robust enough to hard-code.
# Only add a mint here if (a) it's in LEGIT_MINTS and (b) the peg is
# maintained by on-chain collateral or an issuer with regulatory standing
# (Circle, Tether). Algorithmic stables are NOT included — they can depeg.
STABLECOIN_MINTS: set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def is_legit_mint(mint: str) -> bool:
    """Return True if the mint address is in our verified allowlist.

    This is the primary spam filter gate. Any mint not returning True here
    is UNKNOWN — callers must then decide whether the tx context justifies
    inclusion (see is_spam_token).
    """
    return mint in LEGIT_MINTS


def resolve_symbol(mint: str) -> str | None:
    """Return the canonical symbol for a known mint, or None if unknown.

    Unknown mints should trigger the spam-filter check in is_spam_token.
    Never invent a symbol — if the mint isn't in LEGIT_MINTS the token is
    either spam or a token we haven't vetted yet. Both cases need explicit
    handling at the call site.
    """
    entry = LEGIT_MINTS.get(mint)
    return entry["symbol"] if entry else None


def is_spam_token(mint: str, tx_type: str) -> bool:
    """Return True if this (mint, tx_type) row should be silently dropped.

    The logic: a token we can't identify is innocent (and worth keeping) ONLY
    if the user actively did something with it — a SWAP means the user chose
    to trade it; a TRANSFER means they moved it intentionally. Both imply
    voluntary engagement with whatever that token is. Any other type
    (AIRDROP, UNKNOWN, CONTRACT_INTERACTION, NFT_MINT, …) combined with an
    unknown mint is almost certainly dust / scam traffic that landed in the
    wallet without the user's participation.

    Keeping legitimate swap rows for unknown mints is intentional: sometimes a
    new/low-cap token that hasn't been added to the allowlist yet generates a
    real taxable swap event. We'd rather surface it (with $0 price that the
    user can manually fix) than silently drop it and undercount their gains.

    Note: tokens in LEGIT_MINTS are NEVER dropped — this function returns
    False for them regardless of tx_type.
    """
    if mint in LEGIT_MINTS:
        # Known-good mints are always kept; we trust the allowlist.
        return False
    # Unknown mint: keep only if the user deliberately interacted with it
    active_types = {"SWAP", "TRANSFER"}
    return tx_type.upper() not in active_types
