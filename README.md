# PolyBot - Polymarket Trading Bot

Automated trading bot for Polymarket prediction markets. Runs on GitHub Actions (free) with a GitHub Pages dashboard.

## Strategies

- **Weather Arbitrage** - Compares Open-Meteo weather forecasts against Polymarket temperature markets. Buys underpriced outcomes when forecast confidence exceeds market price.
- **Cross-Platform Arbitrage** - Detects YES+NO < $1.00 pricing inefficiencies on Polymarket, and cross-platform price discrepancies with Kalshi.

## Setup

### 1. Create GitHub Repository

Create a new **private** repository on GitHub, then push this code:

```bash
git remote add origin https://github.com/YOUR_USERNAME/polybot.git
git push -u origin main
```

### 2. Add Secrets

Go to **Settings > Secrets and variables > Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `PRIVATE_KEY` | Yes | Polygon wallet private key (0x...) |
| `DRY_RUN` | No | `true` (default) or `false` for live |
| `INITIAL_CAPITAL` | No | Starting capital, default `100` |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | No | Your Telegram chat ID |
| `KALSHI_API_KEY` | No | For cross-platform arbitrage |
| `KALSHI_API_SECRET` | No | For cross-platform arbitrage |

### 3. Enable GitHub Actions

Go to **Actions** tab and enable workflows. The bot runs every 10 minutes automatically.

You can also trigger a manual run: **Actions > PolyBot Trading > Run workflow**.

### 4. Enable Dashboard

Go to **Settings > Pages**:
- Source: **Deploy from a branch**
- Branch: **gh-pages** / **(root)**

Dashboard will be available at: `https://YOUR_USERNAME.github.io/polybot/`

## Architecture

```
GitHub Actions (cron every 10min)
    ├── Restore SQLite DB from artifact
    ├── Run single scan cycle (weather + arb strategies)
    ├── Export dashboard JSON
    ├── Save DB artifact (90-day retention)
    └── Deploy dashboard to GitHub Pages
```

## Risk Management

- Quarter-Kelly position sizing
- Max 5% per trade, 40% total exposure
- 10% daily loss limit auto-halts trading
- Starts in DRY_RUN mode (paper trading)

## Going Live

1. Fund a Polygon wallet with $100 USDC
2. Set `PRIVATE_KEY` secret to your wallet's private key
3. Set `DRY_RUN` secret to `false`
4. Monitor via dashboard and Telegram alerts
