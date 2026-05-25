# Crypto Portfolio Tracker — API Specs

Research date: 2026-04-10. All endpoints verified against current (2025/2026) docs.
Target: n8n HTTP Request nodes for a multi-chain wallet balance tracker (EVM + Solana + BTC).

---

## 1. Moralis Web3 API — EVM token balances with prices

**Docs:** https://docs.moralis.com/web3-data-api/evm/reference/wallet-api/get-wallet-token-balances-price

### Endpoint

```
GET https://deep-index.moralis.io/api/v2.2/wallets/{address}/tokens
```

This is the current unified "Wallet API" endpoint that returns ERC-20 balances **with USD prices and USD values in a single call**. It also includes the native coin (ETH/MATIC/BNB/etc) as one of the token objects when `exclude_native` is not set.

**Multi-chain in one call:** No. There is no single-request multi-chain variant. Call this endpoint once per chain (loop chains in n8n using a Split-in-Batches or Items node).

### Auth

Header:
```
X-API-Key: <YOUR_MORALIS_API_KEY>
```

### Query parameters

| Param | Type | Notes |
|---|---|---|
| `chain` | string | `eth`, `polygon`, `bsc`, `avalanche`, `arbitrum`, `optimism`, `base`, `linea`, `fantom`, etc. (40+ chains). Default: `eth`. |
| `exclude_spam` | bool | Recommended `true` to drop spam tokens. |
| `exclude_unverified_contracts` | bool | Recommended `true`. |
| `exclude_native` | bool | Default `false` — keep it false so the native coin is returned in the same response. |
| `token_addresses` | string[] | Optional filter (max 10). |
| `limit` | int | Page size. |
| `cursor` | string | Pagination cursor. |

### Example request

```
GET https://deep-index.moralis.io/api/v2.2/wallets/0xcB1C1FdE09f811B294172696404e88E658659905/tokens?chain=eth&exclude_spam=true&exclude_unverified_contracts=true
X-API-Key: <KEY>
```

### Example response (shape)

```json
{
  "cursor": null,
  "page": 0,
  "page_size": 100,
  "result": [
    {
      "token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
      "symbol": "USDT",
      "name": "Tether USD",
      "logo": "https://...",
      "decimals": 6,
      "balance": "1500000000",
      "balance_formatted": "1500.0",
      "possible_spam": false,
      "verified_contract": true,
      "usd_price": 1.0003,
      "usd_price_24hr_percent_change": 0.01,
      "usd_value": 1500.45,
      "portfolio_percentage": 42.31,
      "native_token": false,
      "total_supply": "..."
    },
    {
      "token_address": "0x0000000000000000000000000000000000000000",
      "symbol": "ETH",
      "name": "Ether",
      "decimals": 18,
      "balance": "250000000000000000",
      "balance_formatted": "0.25",
      "usd_price": 3200.5,
      "usd_value": 800.13,
      "native_token": true
    }
  ]
}
```

Key fields to read in n8n: `result[*].symbol`, `balance_formatted`, `usd_price`, `usd_value`, `token_address`, `native_token`, `possible_spam`.

### Free tier

- **40,000 Compute Units (CU) per day** on the free plan.
- This endpoint costs **100 CU per call**. So ~400 calls/day free — plenty for a personal tracker across ~10 EVM chains running every few minutes.

---

## 2. Helius Solana API — SPL tokens + native SOL

**Docs:** https://www.helius.dev/docs/api-reference/das/getassetsbyowner

### Endpoint

```
POST https://mainnet.helius-rpc.com/?api-key=<YOUR_HELIUS_API_KEY>
```

Standard Solana JSON-RPC endpoint. Helius extends the DAS API so `getAssetsByOwner` can return **fungible SPL tokens and native SOL** in addition to NFTs.

### Auth

API key is a **query parameter** on the RPC URL: `?api-key=<KEY>`. No header required. Content-Type is `application/json`.

### Request body

```json
{
  "jsonrpc": "2.0",
  "id": "portfolio-tracker",
  "method": "getAssetsByOwner",
  "params": {
    "ownerAddress": "86xCnPeV69n6t3DnyGvkKobf9FdN2H9oiVDdaMpo2MMY",
    "page": 1,
    "limit": 1000,
    "displayOptions": {
      "showFungible": true,
      "showNativeBalance": true,
      "showZeroBalance": false
    }
  }
}
```

`showFungible: true` is required to get SPL tokens. `showNativeBalance: true` adds a `nativeBalance` object alongside `items` so you can skip a separate `getBalance` RPC call.

### Example response (shape — only relevant fields)

```json
{
  "jsonrpc": "2.0",
  "id": "portfolio-tracker",
  "result": {
    "total": 12,
    "limit": 1000,
    "page": 1,
    "nativeBalance": {
      "lamports": 2500000000,
      "price_per_sol": 142.37,
      "total_price": 355.93
    },
    "items": [
      {
        "interface": "FungibleToken",
        "id": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
        "content": { "metadata": { "symbol": "JitoSOL", "name": "Jito Staked SOL" } },
        "token_info": {
          "symbol": "JitoSOL",
          "balance": 35688813508,
          "supply": 5949594702758293,
          "decimals": 9,
          "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
          "associated_token_address": "H7iLu4DPFpzEx1AGN8BCN7Qg966YFndt781p6ukhgki9",
          "price_info": {
            "price_per_token": 56.47943,
            "total_price": 2015.68,
            "currency": "USDC"
          }
        }
      }
    ]
  }
}
```

Key fields:
- Native SOL: `result.nativeBalance.lamports` (divide by 1e9), `result.nativeBalance.total_price` (USD).
- SPL tokens: iterate `result.items[*]` where `interface == "FungibleToken"`, read `token_info.symbol`, `token_info.balance` / `10^decimals`, and `token_info.price_info.total_price` (USD).

### USD prices

**Yes, Helius returns USD prices natively** via `token_info.price_info` for tokens that have Jupiter/Birdeye pricing data, and for SOL via `nativeBalance.price_per_sol`. You do NOT strictly need Birdeye. Use Birdeye only as a fallback for long-tail tokens where `price_info` is missing/null.

### Free tier

Helius free plan: 1 million credits/month, 10 RPS. One `getAssetsByOwner` call ≈ a few credits — ample for a personal tracker.

---

## 3. Birdeye Public API — multi-token price fallback (Solana)

**Docs:** https://docs.birdeye.so/reference/get-defi-multi_price

### Endpoint

```
GET https://public-api.birdeye.so/defi/multi_price?list_address=<mint1>,<mint2>,...
```

Also accepts POST with the same params in the body (use POST if the comma-list gets long — max 100 tokens per request).

### Headers

```
X-API-KEY: <YOUR_BIRDEYE_API_KEY>
x-chain: solana
accept: application/json
```

`x-chain` defaults to `solana` but set it explicitly. Supports solana, ethereum, arbitrum, avalanche, bsc, optimism, polygon, base, zksync, sui, etc.

### Query parameters

| Param | Type | Notes |
|---|---|---|
| `list_address` | string | Comma-separated token mint addresses. **Max 100.** Required. |
| `include_liquidity` | bool | Optional. |
| `check_liquidity` | number | Optional liquidity threshold. |
| `ui_amount_mode` | string | `raw` \| `scaled` \| `both`. Default `raw`. |

### Example request

```
GET https://public-api.birdeye.so/defi/multi_price?list_address=So11111111111111111111111111111111111111112,DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263&include_liquidity=true
X-API-KEY: <KEY>
x-chain: solana
```

### Example response

```json
{
  "success": true,
  "data": {
    "So11111111111111111111111111111111111111112": {
      "value": 142.37,
      "updateUnixTime": 1726673192,
      "updateHumanTime": "2024-09-18T15:26:32",
      "priceChange24h": -5.83,
      "liquidity": 7026264396
    },
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": {
      "value": 0.0000142,
      "updateUnixTime": 1726673192,
      "priceChange24h": 2.11
    }
  }
}
```

Read prices as `data[<mint_address>].value` (USD).

### Free tier

- Free "Standard" package requires a Birdeye account and API key (sign up at bds.birdeye.so).
- Rate limits are **per-account across all endpoints**, not per-endpoint. Exact free-tier RPS is not publicly stated on the pricing page but is sufficient for periodic polling. HTTP 401 = missing/invalid key, HTTP 429 = rate limited.

---

## 4. mempool.space — Bitcoin address balance

**Docs:** https://mempool.space/docs/api/rest

### Endpoint

```
GET https://mempool.space/api/address/{address}
```

No auth. Public.

### Example request

```
GET https://mempool.space/api/address/bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq
```

### Example response

```json
{
  "address": "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",
  "chain_stats": {
    "funded_txo_count": 15,
    "funded_txo_sum": 125000000,
    "spent_txo_count": 10,
    "spent_txo_sum": 50000000,
    "tx_count": 18
  },
  "mempool_stats": {
    "funded_txo_count": 0,
    "funded_txo_sum": 0,
    "spent_txo_count": 0,
    "spent_txo_sum": 0,
    "tx_count": 0
  }
}
```

### Computing balance

All values are in **satoshis**. Current confirmed + unconfirmed balance:

```
balance_sats = (chain_stats.funded_txo_sum - chain_stats.spent_txo_sum)
             + (mempool_stats.funded_txo_sum - mempool_stats.spent_txo_sum)
balance_btc  = balance_sats / 100000000
```

For confirmed-only, drop the mempool_stats term.

### Rate limits

Not publicly documented ("if you have to ask, you'll hit them"). HTTP 429 is returned on breach. For a personal tracker polling every 1–5 minutes you will not come close. If you hit limits, self-host mempool or use the enterprise sponsorship program.

---

## 5. CoinGecko free/demo API — simple multi-token prices

**Docs:** https://docs.coingecko.com/v3.0.1/reference/simple-price

Use this to (a) convert BTC sats → USD, and (b) as a fallback for SOL / ETH / native coin pricing.

### Endpoint

```
GET https://api.coingecko.com/api/v3/simple/price?ids=<id1,id2>&vs_currencies=usd
```

### Auth (Demo plan — free)

Pass your demo key via **either**:

- Header: `x-cg-demo-api-key: <KEY>`
- Query param: `x_cg_demo_api_key=<KEY>`

The endpoint also works without a key at heavily reduced limits, but signup is free and gives you the stable 30/min limit below.

### Query parameters

| Param | Notes |
|---|---|
| `ids` | Comma-separated CoinGecko coin IDs (e.g. `bitcoin,ethereum,solana`). |
| `vs_currencies` | Comma-separated fiat/crypto codes (e.g. `usd`, `usd,eur`). |
| `include_market_cap` | bool |
| `include_24hr_vol` | bool |
| `include_24hr_change` | bool |
| `include_last_updated_at` | bool |

### Example request

```
GET https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true
x-cg-demo-api-key: <KEY>
```

### Example response

```json
{
  "bitcoin":  { "usd": 67187.34, "usd_24h_change": 3.64 },
  "ethereum": { "usd": 3200.50,  "usd_24h_change": 1.22 },
  "solana":   { "usd": 142.37,   "usd_24h_change": -2.10 }
}
```

Read as `response.bitcoin.usd`, etc.

### Free (Demo) tier rules

- **30 calls / minute**
- **10,000 calls / month**
- Public API cache/update frequency ~60 seconds (don't poll faster than that).
- No credit card required.

---

## Quick reference — n8n node setup matrix

| API | Method | URL template | Auth location |
|---|---|---|---|
| Moralis EVM tokens | GET | `https://deep-index.moralis.io/api/v2.2/wallets/{{address}}/tokens?chain={{chain}}&exclude_spam=true` | Header `X-API-Key` |
| Helius Solana DAS | POST | `https://mainnet.helius-rpc.com/?api-key={{key}}` | Query `api-key` |
| Birdeye multi-price | GET | `https://public-api.birdeye.so/defi/multi_price?list_address={{mints}}` | Header `X-API-KEY` + `x-chain` |
| mempool.space BTC | GET | `https://mempool.space/api/address/{{btc_address}}` | none |
| CoinGecko simple/price | GET | `https://api.coingecko.com/api/v3/simple/price?ids={{ids}}&vs_currencies=usd` | Header `x-cg-demo-api-key` |

## Recommended data flow

1. Loop EVM chains → Moralis `/wallets/{addr}/tokens` per chain → flatten `result[]` into unified rows `{chain, symbol, balance, usd_value}`.
2. Helius `getAssetsByOwner` once for the Solana wallet → read `nativeBalance` (SOL) and `items[].token_info` (SPL). Use `price_info.total_price` where present.
3. For Solana tokens where Helius `price_info` is null → collect mints → single Birdeye `/defi/multi_price` call → merge.
4. mempool.space `/api/address/{btc}` → compute sats → one CoinGecko `/simple/price?ids=bitcoin` call → USD.
5. Merge all rows, sum `usd_value` for total portfolio.
