"""Rango Exchange API client for multi-chain swap routing."""
import os
import requests
from pathlib import Path
from typing import Dict, List, Optional
from applog import log

RANGO_API_BASE = "https://api.rango.exchange"

# Load RANGO_API_KEY from .env (same discovery logic as scanner.py)
def _load_rango_key() -> str | None:
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if line.startswith("RANGO_API_KEY="):
                    return line.split("=", 1)[1].strip() or None
    return os.environ.get("RANGO_API_KEY")

RANGO_API_KEY = _load_rango_key()

# Token tables keyed by chain (uppercase) then symbol (uppercase).
# Rango format: "CHAIN.SYMBOL" (native) or "CHAIN.SYMBOL--address" (token)
_TOKENS: Dict[str, Dict[str, Dict]] = {
    "SOLANA": {
        "SOL":  {"address": None,                                              "decimals": 9},
        "USDC": {"address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  "decimals": 6},
        "USDT": {"address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  "decimals": 6},
        "BTC":  {"address": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  "decimals": 8},
        "WBTC": {"address": "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh",  "decimals": 8},
        "ETH":  {"address": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  "decimals": 8},
        "WETH": {"address": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  "decimals": 8},
        "BONK": {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  "decimals": 5},
        "JUP":  {"address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",   "decimals": 6},
        "RAY":  {"address": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  "decimals": 6},
        "ORCA": {"address": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",   "decimals": 6},
        "MNGO": {"address": "MangoCzJ36AjZyKwVj3VnYU4GTonjfVEnJmvvWaxLac",   "decimals": 6},
    },
    "ETH": {
        "ETH":  {"address": None,                                           "decimals": 18},
        "USDC": {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  "decimals": 6},
        "USDT": {"address": "0xdac17f958d2ee523a2206206994597c13d831ec7",  "decimals": 6},
        "DAI":  {"address": "0x6b175474e89094c44da98b954eedeac495271d0f",  "decimals": 18},
        "WBTC": {"address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  "decimals": 8},
        "BTC":  {"address": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  "decimals": 8},  # WBTC
        "WETH": {"address": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  "decimals": 18},
        "PAXG": {"address": "0x45804880de22913dafe09f4980848ece6ecbaf78",  "decimals": 18},
        "XAUT": {"address": "0x68749665ff8d2d112fa859aa293f07a622782f38",  "decimals": 6},
        "LINK": {"address": "0x514910771af9ca656af840dff83e8264ecf986ca",  "decimals": 18},
        "UNI":  {"address": "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984",  "decimals": 18},
    },
    "BSC": {
        "BNB":  {"address": None,                                           "decimals": 18},
        "USDC": {"address": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  "decimals": 18},
        "USDT": {"address": "0x55d398326f99059ff775485246999027b3197955",  "decimals": 18},
        "BTC":  {"address": "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",  "decimals": 18},  # BTCB
        "ETH":  {"address": "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  "decimals": 18},
        "BUSD": {"address": "0xe9e7cea3dedca5984780bafc599bd69add087d56",  "decimals": 18},
    },
    "MATIC": {
        "MATIC": {"address": None,                                           "decimals": 18},
        "USDC":  {"address": "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  "decimals": 6},
        "USDT":  {"address": "0xc2132d05d31c914a87c6611c10748aeb04b58e8f",  "decimals": 6},
        "WBTC":  {"address": "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",  "decimals": 8},
        "WETH":  {"address": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",  "decimals": 18},
        "BTC":   {"address": "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6",  "decimals": 8},
        "ETH":   {"address": "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619",  "decimals": 18},
    },
}

# EVM stablecoins with 6 decimals (fallback for unlisted chains)
_EVM_6DEC = {"USDC", "USDT", "BUSD", "DAI", "FRAX", "TUSD", "USDP"}


def _token_info(symbol: str, chain: str = "SOLANA",
                override_address: Optional[str] = None) -> Dict:
    """Return {address, decimals} for a token on a given chain.

    If override_address is provided (from user holdings), it takes precedence
    for the address but decimals still come from the table (fallback 18 for EVM).
    """
    chain_up  = chain.upper()
    sym_up    = symbol.upper()
    chain_map = _TOKENS.get(chain_up, {})
    info      = chain_map.get(sym_up) or chain_map.get(sym_up.lstrip("W"), {})

    if not info:
        # Unknown token — use override address if available, guess decimals
        evm_chains = {"ETH", "BSC", "MATIC", "AVAX", "ARB", "OP", "FTM"}
        if chain_up in evm_chains:
            dec = 6 if sym_up in _EVM_6DEC else 18
        else:
            dec = 6  # SPL default
        info = {"address": None, "decimals": dec}

    if override_address:
        return {"address": override_address, "decimals": info["decimals"]}
    return dict(info)


def _rango_token_str(chain: str, symbol: str, address: Optional[str]) -> str:
    """Build Rango token identifier: 'CHAIN.SYM' or 'CHAIN.SYM--address'."""
    if address:
        return f"{chain}.{symbol.upper()}--{address}"
    return f"{chain}.{symbol.upper()}"


def _to_base_units(amount: float, decimals: int) -> str:
    """Convert human-readable float to integer base-unit string (e.g. 10.5 USDC → '10500000')."""
    return str(int(round(amount * (10 ** decimals))))


def _from_base_units(raw: str, decimals: int) -> float:
    """Convert integer base-unit string back to human-readable float."""
    try:
        return int(raw) / (10 ** decimals)
    except (ValueError, TypeError):
        return 0.0


class RangoError(Exception):
    """Rango API error."""
    pass


def get_best_route(from_token: str, to_token: str, amount: float,
                   from_chain: str = "SOLANA", to_chain: str = "SOLANA",
                   from_contract: Optional[str] = None,
                   slippage: float = 1.0) -> Dict:
    """
    Get best swap route from Rango via GET /basic/quote.

    Args:
        from_token:    Token symbol (e.g. "USDC", "SOL", "XAUT")
        to_token:      Token symbol
        amount:        Human-readable amount of the from-token
        from_chain:    Source blockchain — must be uppercase (e.g. "ETH", "SOLANA")
        to_chain:      Destination blockchain
        from_contract: Optional contract address override (from user holdings)
        slippage:      Slippage tolerance in percent (default 1.0)

    Returns:
        Raw Rango response dict with keys: requestId, resultType, route, error, errorCode
    """
    try:
        from_info = _token_info(from_token, from_chain, override_address=from_contract)
        to_info   = _token_info(to_token,   to_chain)

        params: Dict = {
            "from":     _rango_token_str(from_chain, from_token, from_info["address"]),
            "to":       _rango_token_str(to_chain,   to_token,   to_info["address"]),
            "amount":   _to_base_units(amount, from_info["decimals"]),
            "slippage": str(slippage),
        }
        if RANGO_API_KEY:
            params["apiKey"] = RANGO_API_KEY

        response = requests.get(f"{RANGO_API_BASE}/basic/quote", params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get("error"):
            raise RangoError(f"Rango API error: {data['error']}")

        return data

    except requests.exceptions.RequestException as e:
        log.error(f"Rango API request failed: {e}")
        raise RangoError(f"Failed to fetch route: {str(e)}")


def build_swap_transaction(from_token: str, to_token: str, amount: float,
                           from_address: str, to_address: str,
                           from_chain: str = "SOLANA", to_chain: str = "SOLANA",
                           from_contract: Optional[str] = None,
                           slippage: float = 1.0) -> Dict:
    """
    Get final quote AND build the unsigned swap transaction via GET /basic/swap.

    Args:
        from_token:    Token symbol to swap from
        to_token:      Token symbol to swap to
        amount:        Human-readable amount of the from-token
        from_address:  User's source wallet address
        to_address:    User's destination wallet address
        from_chain:    Source blockchain
        to_chain:      Destination blockchain
        slippage:      Slippage tolerance in percent

    Returns:
        Raw Rango response dict containing the unsigned transaction payload
    """
    try:
        from_info = _token_info(from_token, from_chain, override_address=from_contract)
        to_info   = _token_info(to_token,   to_chain)

        params: Dict = {
            "from":            _rango_token_str(from_chain, from_token, from_info["address"]),
            "to":              _rango_token_str(to_chain,   to_token,   to_info["address"]),
            "amount":          _to_base_units(amount, from_info["decimals"]),
            "slippage":        str(slippage),
            "fromAddress":     from_address,
            "toAddress":       to_address,
            "disableEstimate": "true",
        }
        if RANGO_API_KEY:
            params["apiKey"] = RANGO_API_KEY

        response = requests.get(f"{RANGO_API_BASE}/basic/swap", params=params, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get("error"):
            raise RangoError(f"Rango API error: {data['error']}")

        return data

    except requests.exceptions.RequestException as e:
        log.error(f"Rango transaction build failed: {e}")
        raise RangoError(f"Failed to build transaction: {str(e)}")


def quote_swap(from_token: str, to_token: str, amount: float,
               from_chain: str = "SOLANA", to_chain: str = "SOLANA",
               from_contract: Optional[str] = None,
               slippage: float = 1.0) -> Dict:
    """
    Quick quote for a swap.

    Args:
        from_token:  Source token symbol
        to_token:    Destination token symbol
        amount:      Human-readable amount of the from-token
        from_chain:  Source blockchain (default "SOLANA")
        to_chain:    Destination blockchain (default "SOLANA")
        slippage:    Slippage tolerance in percent

    Returns:
        Dict with:
        - output_amount:  human-readable estimated output (float)
        - price_impact:   price impact percent (float)
        - fee_usd:        estimated fee in USD (float)
        - route_id:       requestId string for use in build_swap_transaction
    """
    try:
        data = get_best_route(from_token, to_token, amount,
                              from_chain=from_chain, to_chain=to_chain,
                              from_contract=from_contract,
                              slippage=slippage)

        if data.get("resultType") != "OK" or not data.get("route"):
            raise RangoError(f"No route found (resultType={data.get('resultType')})")

        route    = data["route"]
        to_info  = _token_info(to_token)
        out_dec  = route.get("to", {}).get("decimals") or to_info["decimals"]

        return {
            "output_amount": _from_base_units(route.get("outputAmount", "0"), out_dec),
            "price_impact":  float(route.get("priceImpactUsdPercent") or 0),
            "fee_usd":       float(route.get("feeUsd") or 0),
            "route_id":      data.get("requestId"),
        }

    except RangoError:
        raise
    except Exception as e:
        log.error(f"Rango quote failed: {e}")
        raise RangoError(f"Failed to get quote: {str(e)}")


def get_tokens_list(blockchain: str = "SOLANA") -> List[Dict]:
    """
    Get list of supported tokens on a blockchain from Rango meta endpoint.

    Returns:
        List of dicts with token metadata (symbol, address, decimals)
    """
    try:
        params: Dict = {"blockchain": blockchain}
        if RANGO_API_KEY:
            params["apiKey"] = RANGO_API_KEY

        response = requests.get(f"{RANGO_API_BASE}/basic/meta", params=params, timeout=15)
        response.raise_for_status()

        data = response.json()
        return data.get("tokens", [])

    except Exception as e:
        log.error(f"Failed to fetch Rango tokens list: {e}")
        return []


# Curated list of xStocks (tokenized stocks) available on Solana
# NOTE: contract addresses below are placeholders — replace with real mainnet addresses
#       before enabling xStock swaps.
XSTOCKS_LIST = {
    "SPYx": {
        "name": "S&P 500 ETF",
        "contract": "SPYxtkpJLGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "ETF"
    },
    "QQQx": {
        "name": "Nasdaq-100 ETF",
        "contract": "QQQxkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "ETF"
    },
    "NVDAx": {
        "name": "NVIDIA Corporation",
        "contract": "NVDAxtkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Tech"
    },
    "MSFTx": {
        "name": "Microsoft Corporation",
        "contract": "MSFTxtkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Tech"
    },
    "GOOGLx": {
        "name": "Alphabet Inc",
        "contract": "GOOGLxkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Tech"
    },
    "AMZNx": {
        "name": "Amazon.com Inc",
        "contract": "AMZNxtkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Tech"
    },
    "TSLAx": {
        "name": "Tesla Inc",
        "contract": "TSLAxtkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Tech"
    },
    "CPBx": {
        "name": "Campbell Soup Company",
        "contract": "CPBxktkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Consumer"
    },
    "CRWDx": {
        "name": "CrowdStrike Holdings",
        "contract": "CRWDxtkJPGFDxdBU5jx3dSPJCWzrNSzPJqEYHkiXrn1",
        "category": "Cybersecurity"
    }
}


def is_valid_token(symbol: str) -> bool:
    """Check if token is valid (either crypto or xStock)."""
    if symbol in XSTOCKS_LIST:
        return True
    common_tokens = ["SOL", "USDC", "USDT", "BTC", "WBTC", "ETH", "WETH",
                     "BONK", "JUP", "RAY", "ORCA", "MNGO", "SRM"]
    return symbol.upper() in common_tokens


def get_xstocks_list() -> Dict:
    """Return the curated list of available xStocks."""
    return XSTOCKS_LIST
