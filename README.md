# Hybrid Job Hunter 🎯

A hybrid job hunting automation script that combines the best of both worlds:
1. **API-driven precision** for standard ATS boards (Ashby, Lever, Greenhouse) - *Fast, reliable, zero false positives.*
2. **JS-rendering web scraper** using Playwright for custom careers pages (like Google, Jane Street, Citadel) - *Like changedetection.io, handles dynamic content and Javascript.*

This script is built to run entirely on **GitHub Actions for free**, with zero infrastructure needed.

## Features

- Config-driven setup via `config.yaml`
- Monitors multiple ATS APIs automatically
- Uses headless Chromium via Playwright to fetch, render, and hash custom job boards
- CSS selector support to narrow down the monitored scope on custom pages
- Smart Keyword Filtering (alerts only when keywords like "intern" appear)
- Telegram Notifications
- Self-updating `state.json` pushed back to the repo

## Setup Instructions

1. **Telegram Bot Setup (2 min):**
   - Message `@BotFather` on Telegram → `/newbot` → copy the token.
   - Start a chat with your new bot.
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your `chat_id`.

2. **GitHub Repository:**
   - Clone or upload these files to a new GitHub repository (private is recommended).

3. **Configure Secrets:**
   - Go to Repo Settings → Secrets and variables → Actions → New repository secret.
   - Add `TELEGRAM_TOKEN` with your bot token.
   - Add `TELEGRAM_CHAT_ID` with your numeric chat ID.

4. **Customize Config:**
   - Edit `config.yaml` to include your desired keywords, locations, ATS companies, and custom pages.

5. **Run!**
   - The GitHub Action is scheduled to run every 6 hours automatically.
   - Go to the Actions tab in GitHub and click "Run workflow" to test it manually.

## Local Testing

You can run this locally without GitHub Actions:

```bash
pip install -r requirements.txt
playwright install chromium
export TELEGRAM_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python hybrid_hunter.py
```
