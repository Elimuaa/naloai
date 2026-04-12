# ⚡ CryptoBot — Automated Crypto Trading Platform

A full-stack SaaS platform for automated Z-Score Retest crypto trading on Robinhood,
with Claude AI trade analysis and daily reports.

## Quick Start

```bash
# Install Python deps
pip install -r requirements.txt

# Build + run (demo mode, no API keys needed)
bash start.sh
```

Open http://localhost:8080

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Optional | Enables real AI trade analysis |
| `JWT_SECRET_KEY` | Recommended | JWT signing key (auto-generated if missing) |
| `JWT_REFRESH_SECRET` | Recommended | Refresh token key (auto-generated if missing) |
| `DEMO_MODE` | Optional | `true` = simulated prices (default). Set `false` for live trading |

## Demo Mode

By default `DEMO_MODE=true` — the bot runs with simulated BTC/ETH/SOL/DOGE prices.
No real orders are placed. The UI shows a yellow **DEMO** badge.

To use real Robinhood Crypto API keys:
1. Sign up / log in
2. Click ⚙️ Settings
3. Enter your Robinhood API Key and Base64 Ed25519 private key
4. Set `DEMO_MODE=false` in your environment

## Architecture

```
main.py             FastAPI app + WebSocket + SPA serving
database.py         SQLAlchemy async models (User, Trade, DailyReport)
auth.py             JWT tokens + Argon2 password hashing
bot_engine.py       Z-Score Retest strategy, per-user asyncio task
mock_robinhood.py   Simulated price feed for demo mode
robinhood.py        Real Robinhood Crypto API (Ed25519 signed)
post_trade_ai_learner.py  Claude AI trade analysis + daily reports
ws_manager.py       WebSocket connection manager
scheduler.py        Daily report cron job (midnight UTC)
routers/            FastAPI route handlers
frontend/           React 18 + TypeScript + Tailwind + Recharts
```

## Replit Deployment

Set these Replit Secrets:
- `ANTHROPIC_API_KEY`
- `JWT_SECRET_KEY`  
- `JWT_REFRESH_SECRET`
- `DEMO_MODE` = `false` (for live trading)
