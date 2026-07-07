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
   - The GitHub Action hunts ATS boards **hourly** (fast, no browser) and does a **full run including Playwright custom pages every 4 hours**. The Chromium browser is cached between runs to stay well within the free Actions minutes on private repos.
   - Go to the Actions tab in GitHub and click "Run workflow" to test it manually (manual runs are always full runs).

## Local Testing

You can run this locally without GitHub Actions:

```bash
pip install -r requirements.txt
playwright install chromium
export TELEGRAM_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python hybrid_hunter.py
```

Useful flags:

- `python hybrid_hunter.py --test` — dry run: shows matches per source, no state changes, no notifications.
- `python hybrid_hunter.py --seed` — baseline mode: marks all currently open matching jobs and page hashes as "seen" without notifying. Run this once after adding new companies to `config.yaml` to avoid an alert flood on the first live run.
- `python hybrid_hunter.py --ats-only` / `--pages-only` — hunt only ATS boards (no browser needed) or only Playwright custom pages. The scheduled workflow uses `--ats-only` for the hourly runs.
- `python hybrid_hunter.py --heartbeat` — send a read-only status report (sources, finds in last 24h/7d, failing sources). Does not hunt.

Delivery guarantees: matches are only marked as seen after the Telegram message is delivered (or queued). If Telegram is down or rejects a message, the digest is stored in `state.json` under `_pending` and retried on the next run (up to 5 attempts).

Failure visibility: a source that fails 3 runs in a row (API error, wrong slug, bot-blocked or empty page) triggers a one-time ⚠️ Telegram warning, and a ✅ notice when it recovers. Custom pages returning empty or suspiciously short content (<200 chars — likely CAPTCHA/block) count as failures, not content changes. The daily heartbeat reports real stats: new roles in the last 24h/7d, jobs tracked, pending alerts, and any failing sources. On GitHub Actions, each run writes a per-source result table to the job summary.
