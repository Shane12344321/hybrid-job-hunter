# Hybrid Job Hunter 🎯

A hybrid job hunting automation script that combines the best of both worlds:
1. **API-driven precision** for standard ATS boards (Ashby, Lever, Greenhouse, Oracle HCM, Workday, Amazon, Microsoft) - *Fast, reliable, zero false positives.*
2. **JS-rendering web scraper** using Playwright for custom careers pages (like Google, Jane Street, Citadel) - *Like changedetection.io, handles dynamic content and Javascript.*

### Supported ATS types

| `ats:` | Reach | Notes |
|--------|-------|-------|
| `ashby` | Ashby job boards | `slug` |
| `lever` | Lever postings | `slug` |
| `greenhouse` | Greenhouse boards | `slug` |
| `oracle_hcm` | Oracle HCM Cloud (e.g. JP Morgan) | `host`, `site_number`, `keyword` |
| `workday` | Workday CXS boards (NVIDIA, Citi, BlackRock, Adobe, Salesforce, Sprinklr, Fractal, …) | `tenant`, `wd_host`, `site`, optional `search`/`include_multi_location` |
| `amazon` | amazon.jobs search | optional `query`, `location`, `categories` (server-side `category[]` filter) |
| `microsoft` | Microsoft careers search API | optional `query`, `location` — *adapter shipped but no config entry: the endpoint currently serves a mismatched `*.azureedge.net` cert and gates search behind a bearer token, so it can't be polled unauthenticated yet* |

Every hunter raises on failure (network error, non-200, bad JSON), so a dead
source is told apart from one with no matches and gets a ⚠️ alert after 3
consecutive failures. Each source stays within a **≤ 4 request/run** budget
with a 15s timeout.

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
   - Enterprise boards (Workday / Amazon / Microsoft) are configured under the
     `ats_companies` list too. One example per new adapter type:

     ```yaml
     # Workday: discover tenant/wd_host/site from the company's
     # *.myworkdayjobs.com URL. Location is filtered client-side on the
     # posting's locationsText; include_multi_location turns on the "N Locations"
     # inclusion rule (multi-location intern postings aren't silently dropped).
     - name: NVIDIA
       ats: workday
       tenant: nvidia
       wd_host: wd5
       site: NVIDIAExternalCareerSite
       include_multi_location: true  # optional; default false
       # search: intern          # optional server-side searchText (default "intern")

     # Amazon: one server-side filtered request against amazon.jobs.
     # `categories` uses amazon.jobs' category[] param — strongly recommended,
     # otherwise Amazon India floods the digest with finance/ops interns.
     - name: Amazon
       ats: amazon
       query: intern             # optional (default "intern")
       location: India           # optional (default "India")
       categories:               # optional server-side category filter
       - software-development
       - machine-learning-science
       - data-science

     # Microsoft: adapter exists but is currently unconfigurable (see the
     # supported-ATS table) — the search API needs a bearer token.
     - name: Microsoft
       ats: microsoft
       query: intern
       location: India
     ```

   > **Finding a Workday `site`:** it's the path segment after the tenant in the
   > careers URL — e.g. `sprinklr.wd1.myworkdayjobs.com/careers` → `site: careers`,
   > `citi.wd5.myworkdayjobs.com/.../2` → `site: "2"` (quote numeric sites so YAML
   > keeps them strings). Verify with a live CXS POST before adding.

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
