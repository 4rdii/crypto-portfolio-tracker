# Setup Guide

Step-by-step instructions for getting the Crypto Portfolio Tracker running on your own server.

---

## Prerequisites

- Python 3.12+ **or** Docker
- A server or VPS with a public IP (for production)
- [Moralis](https://moralis.io/) API key (free tier is fine for personal use)
- [Helius](https://helius.dev/) API key (optional, required only for Solana wallets)

---

## 1. Clone the repository

```bash
git clone https://github.com/4rdii/crypto-portfolio-tracker.git
cd crypto-portfolio-tracker
```

---

## 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in the values:

```env
# Required
MORALIS_API_KEY=your_moralis_key_here
SESSION_SECRET=replace_with_random_32_byte_hex   # openssl rand -hex 32

# Optional — for Solana wallet history
HELIUS_API_KEY=your_helius_key_here

# Optional — for the credit top-up flow
TREASURY_ADDRESS=0xYourEVMTreasuryAddress

# Optional — restrict sign-in to specific domains (comma-separated)
# Leave blank to allow any domain (fine for local dev)
SIWE_EXPECTED_DOMAINS=yourdomain.com

# Optional — for the internal admin fund page
# SHA-256 of your chosen password: echo -n "password" | sha256sum
FUND_PASSWORD_HASH=

# Optional — better Rango swap routes
RANGO_API_KEY=
```

### Getting API keys

**Moralis** (EVM chains — Ethereum, Arbitrum, Base, Polygon, BSC, etc.)
1. Sign up at [moralis.io](https://moralis.io/)
2. Go to **Settings → API Keys**
3. Copy your Web3 API Key
4. Free tier: 40,000 compute units/day — sufficient for a personal portfolio

**Helius** (Solana)
1. Sign up at [helius.dev](https://helius.dev/)
2. Copy the API Key from your dashboard
3. Free tier: 1,000,000 credits/month

---

## 3a. Run with Docker (recommended)

```bash
docker compose up -d
```

The app serves on port **8787**. Check health:

```bash
curl http://localhost:8787/api/health
```

Logs:

```bash
docker compose logs -f
```

To stop:

```bash
docker compose down
```

### Persistent storage

The Docker setup mounts two paths from the host so data survives container rebuilds:

| Container path | Purpose |
|----------------|---------|
| `/app/webapp/portfolio.db` | SQLite database |
| `/app/webapp/logs/` | Rotating log files |

These are created automatically on first run.

---

## 3b. Run manually (no Docker)

```bash
cd webapp
pip install -r requirements.txt
python3 app.py
```

The app serves on `0.0.0.0:8787`.

For production you'll want a process manager:

```bash
# systemd service or:
nohup python3 app.py > logs/webapp.log 2>&1 &
```

---

## 4. Reverse proxy (production)

For HTTPS, put Traefik or nginx in front.

**nginx example:**

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

Set `SIWE_EXPECTED_DOMAINS=yourdomain.com` in `.env` when running behind a domain — this ties wallet sign-in messages to your specific domain, preventing replay attacks.

---

## 5. First sign-in

1. Open `https://yourdomain.com` in a browser with MetaMask or Phantom installed
2. Click **Sign in with Wallet**
3. Sign the message in your wallet — no password, no email
4. Add wallets via the dashboard and click **Import** to pull transaction history

---

## 6. Credit top-up

Importing wallet history consumes Moralis compute units. The app tracks usage via an internal credit ledger.

To top up:
1. Go to **Billing** in the dashboard
2. Connect your wallet and click **Pay with Wallet**
3. Sign a transaction sending USDT, USDC, or DAI to the treasury address
4. Credits are applied automatically within ~30 seconds

Credits never expire.

---

## 7. Logs and troubleshooting

```bash
# Live log (manual run)
tail -f webapp/logs/webapp.log

# Docker
docker compose logs -f crypto-tracker

# Manual DB inspection
sqlite3 webapp/portfolio.db ".tables"
sqlite3 webapp/portfolio.db "SELECT * FROM user_credits LIMIT 10;"
```

Common issues:

| Symptom | Fix |
|---------|-----|
| `MORALIS_API_KEY not set` | Add the key to `.env` and restart |
| Sign-in fails with domain error | Set `SIWE_EXPECTED_DOMAINS` to match the domain in the browser |
| History import returns 0 transactions | Check Moralis quota; verify the wallet address is correct |
| Credits not appearing after top-up | Check `TREASURY_ADDRESS` is set; treasury watcher logs in `logs/webapp.log` |

---

## 8. Running tests

```bash
cd webapp
pip install pytest
pytest tests/ -v
```

---

## Architecture overview

For a full technical deep-dive see `docs/`:

- `ARCHITECTURE.md` — system overview, data flows, threading model
- `BILLING.md` — credit ledger math, top-up flow, treasury watcher
- `SECURITY.md` — threat model, SIWE hardening, XSS mitigations
