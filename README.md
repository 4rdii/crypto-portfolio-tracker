# Crypto Portfolio Tracker

A self-hosted, multi-tenant crypto portfolio tracker with EVM and Solana support.

Connect wallets via Web3 sign-in (no passwords), import on-chain transaction history, track cost basis and P&L, and top up credits to sync data through Moralis.

![FastAPI](https://img.shields.io/badge/FastAPI-0.135-009688?logo=fastapi) ![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python) ![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite)

---

## Features

- **Wallet sign-in** — EIP-4361 (SIWE) for EVM wallets, SIWS for Solana. No passwords, no email required.
- **Multi-wallet / multi-chain** — track any number of EVM wallets (Ethereum, Arbitrum, Base, Polygon, BSC, Avalanche, Optimism…) and Solana wallets under one account.
- **Transaction history import** — pulls on-chain history via Moralis v2.2 and Helius (Solana).
- **Cost basis & P&L** — FIFO cost basis calculation, per-holding and total portfolio P&L.
- **Token prices** — historical prices at time of transaction via Moralis; current prices via Binance/CoinGecko fallback.
- **Credit ledger** — pay-per-scan billing; top up credits by sending ERC-20 stablecoins (USDT, USDC, DAI) to a shared treasury address.
- **Portfolio rebalancer** — define target allocations, compute required swaps, get quotes via Rango Exchange (Solana swaps).
- **PDF tax reports** — export transaction history as a styled PDF.
- **Self-hosted** — SQLite database, no external dependencies beyond the API keys listed below.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI + Uvicorn |
| Database | SQLite (WAL mode) |
| Templates | Jinja2 + vanilla JS |
| Auth | SIWE (EIP-4361) + SIWS |
| On-chain data | Moralis v2.2, Helius |
| Swap routing | Rango Exchange |
| PDF generation | WeasyPrint |
| Deployment | Docker + Traefik (or bare uvicorn) |

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/4rdii/crypto-portfolio-tracker.git
cd crypto-portfolio-tracker
cp .env.example .env   # then fill in your keys
```

### 2. Run with Docker (recommended)

```bash
docker compose up -d
```

The app will be available at `http://localhost:8787`.

### 3. Run manually

```bash
cd webapp
pip install -r requirements.txt
python3 app.py          # serves on 0.0.0.0:8787
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Description |
|----------|----------|-------------|
| `MORALIS_API_KEY` | Yes | Moralis Web3 API key (free tier works for personal use) |
| `HELIUS_API_KEY` | No | Helius API key — needed for Solana history import |
| `SESSION_SECRET` | Yes | Random string for cookie signing (generate with `openssl rand -hex 32`) |
| `TREASURY_ADDRESS` | No | EVM address where users send top-up payments |
| `FUND_PASSWORD_HASH` | No | SHA-256 hash of admin fund password (for internal fund page) |
| `SIWE_EXPECTED_DOMAINS` | No | Comma-separated list of allowed domains for SIWE sign-in |
| `RANGO_API_KEY` | No | Rango Exchange API key — improves swap route quality |

### Getting API keys

- **Moralis** — [moralis.io](https://moralis.io/) → free tier, 40k compute units/day
- **Helius** — [helius.dev](https://helius.dev/) → free tier, 1M credits/month

---

## Project Layout

```
crypto-portfolio-tracker/
├── Dockerfile
├── webapp/                  ← main application
│   ├── app.py               ← FastAPI entry point (all routes)
│   ├── db.py                ← SQLite schema + credit ledger
│   ├── auth.py              ← SIWE / SIWS verification + sessions
│   ├── scanner.py           ← Moralis wrapper + chain detection
│   ├── history.py           ← wallet transaction history import
│   ├── pricing.py           ← token price fetching
│   ├── pnl.py               ← cost basis + P&L calculation
│   ├── treasury_watcher.py  ← background thread for deposit detection
│   ├── wallet_payments.py   ← in-app wallet-signed top-ups
│   ├── rango_client.py      ← Rango swap routing client
│   ├── rate_limit.py        ← per-user sliding window rate limiter
│   ├── requirements.txt
│   ├── static/              ← app.js, wallet-auth.js, CSS
│   ├── templates/           ← Jinja2 HTML templates
│   ├── tests/               ← pytest unit tests
│   └── docs/                ← architecture, security, billing docs
├── scanner.py               ← legacy CLI scanner (imported by webapp)
└── docs/
    └── SETUP_GUIDE.md       ← detailed setup and configuration guide
```

---

## Authentication Flow

Sign-in uses [EIP-4361 (SIWE)](https://eips.ethereum.org/EIPS/eip-4361) for EVM wallets and the Solana equivalent (SIWS) for Solana wallets:

1. Frontend requests a nonce from `/api/auth/nonce`
2. User signs a structured message in their wallet (MetaMask, Phantom, etc.)
3. Backend verifies the signature, burns the nonce, creates a session
4. Session cookie is `HttpOnly`, `SameSite=Lax`

No email, no password, no OAuth.

---

## Credit System

Importing wallet history and running price lookups costs Moralis compute units. The app implements a simple credit ledger:

- Each user has a credit balance
- Scans deduct credits atomically (no race conditions — see `db.py`)
- Users top up by sending ERC-20 stablecoins (USDT, USDC, DAI) to the treasury address
- The treasury watcher thread detects deposits and credits the matching user

The top-up flow uses wallet signing to link the payment to the user's account without needing any off-chain coordination.

---

## Running Tests

```bash
cd webapp
pip install pytest
pytest tests/
```

---

## Deployment

The repo includes a `Dockerfile` designed to run behind a Traefik reverse proxy (HTTPS termination, automatic Let's Encrypt). For a bare VPS:

```bash
docker build -t crypto-tracker .
docker run -d \
  --env-file .env \
  -v $(pwd)/webapp/portfolio.db:/app/webapp/portfolio.db \
  -v $(pwd)/webapp/logs:/app/webapp/logs \
  -p 8787:8787 \
  crypto-tracker
```

For production, put Traefik (or nginx) in front and set `SIWE_EXPECTED_DOMAINS` to your real domain.

---

## License

MIT
