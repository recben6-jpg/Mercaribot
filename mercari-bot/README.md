# Mercari Japan Pokemon Bot

Monitors Mercari Japan for cheap Pokémon full art cards and sends Discord alerts.

## Setup

### 1. Discord Webhook
1. Open Discord → your server → channel settings
2. Integrations → Webhooks → New Webhook
3. Copy the webhook URL

### 2. Deploy to Railway
1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo

### 3. Set Environment Variables in Railway
Go to your project → Variables → add these:

| Variable | Example | Description |
|---|---|---|
| `DISCORD_WEBHOOK` | `https://discord.com/api/webhooks/...` | Your webhook URL |
| `PRICE_THRESHOLD` | `500` | Max price in yen to flag |
| `CHECK_INTERVAL` | `300` | Seconds between checks (300 = 5 min) |

### 4. Done
Bot runs 24/7, sends a Discord embed whenever it finds a listing under your threshold.

## Customise search terms
Edit the `SEARCH_TERMS` list in `bot.py`:
```python
SEARCH_TERMS = [
    "フルアート まとめ",   # full art bulk
    "SR まとめ",           # Super Rare bulk
    "ポケモン カード まとめ", # Pokemon card bulk
    "ポケモン レア まとめ", # Pokemon rare bulk
]
```
