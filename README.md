# Hybrid Job Hunter 🎯

A hybrid job hunting automation script that combines the best of both worlds:
1. **API/structured-source precision** for standard ATS boards and official careers feeds (Ashby, Lever, Greenhouse, SmartRecruiters, Workable, Oracle HCM, Workday, Eightfold, Amazon, Google, Intuit, Goldman Higher, D. E. Shaw) - *Fast, reliable, job-level deduplication.*
2. **JS-rendering web scraper** using Playwright for custom careers pages and structured JS-only boards - *Handles dynamic content and JavaScript.*

### Supported ATS types

| `ats:` | Reach | Notes |
|--------|-------|-------|
| `ashby` | Ashby job boards | `slug` |
| `lever` | Lever postings | `slug` |
| `greenhouse` | Greenhouse boards | `slug` |
| `smartrecruiters` | SmartRecruiters public Posting API | `company_id`, optional `country`, `query` or `queries` (up to four shared requests) |
| `workable` | Workable public careers API | `account` |
| `oracle_hcm` | Oracle HCM Cloud (J.P. Morgan, Uber) | `host`, `site_number`, `keyword` or `queries`, optional `location`/`location_id`/`max_pages` (1–4 shared requests) |
| `workday` | Workday CXS boards (NVIDIA, Citi, BlackRock, Adobe, Salesforce, Sprinklr, Fractal, …) | `tenant`, `wd_host`, `site`, optional `search`/`include_multi_location`/`max_pages` (1–12; Workday caps page size at 20, so wide `search` terms need more pages) |
| `amazon` | amazon.jobs search | optional `query`, `categories` (server-side `category[]` filter), `country_code` (default `IND`; `loc_query` is ignored by amazon.jobs so this is the real location filter) |
| `atlassian` | Atlassian public careers listings feed | optional `location`, `categories` |
| `eightfold` | Public Eightfold/PCSX boards (Microsoft, Qualcomm) | `base_url`, `domain`, optional `query`, `location`, `seniority` |
| `microsoft` | Compatibility alias for Microsoft's Eightfold endpoint | optional `query`, `location` |
| `google` | Google Careers server-rendered results | optional `query`, `location` |
| `intuit` | Intuit TalentBrew search | optional `query`, `location` |
| `goldman_higher` | Goldman Sachs Higher campus GraphQL search | optional `search`, `location` |
| `deshaw` | D. E. Shaw's official public internships page | worldwide by design; optional `keywords` |

Every hunter raises on failure (network error, non-200, bad JSON/HTML, missing
result markers, or truncated pagination), so a dead source is told apart from
one with no matches and gets a ⚠️ alert after 3 consecutive failures. Structured
sources use a hard **≤ 4 request/run** pagination budget by default and normally
a 15s timeout; incomplete reads fail loudly. A source can raise its own cap with
`max_pages:` where the ATS forces it (see the `workday` row below).

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
   - **Fastest way to add a company:**

     ```bash
     python3 add_source.py "Figma"                          # probes Greenhouse/Ashby/Lever slugs
     python3 add_source.py "NVIDIA" --url https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
     python3 probe.py --batch candidates.yaml --status candidate --output probe-report.yaml
     python3 add_source.py --batch probe-report.yaml --approve "Company" --apply
     ```

     This detects the ATS, appends a verified entry to `config.yaml`, dry-runs it
     (`--test`), and baselines it (`--seed`) so the first live run doesn't flood you.
     It rolls the config back if verification fails. `python3 probe.py "Name or URL"`
     does the detection read-only. `python3 hybrid_hunter.py --validate` lints the
     config (missing adapter fields, duplicate names) without hunting.
     Newly discovered Workday boards use the narrower `search: internship`,
     calculate the pages required for a complete read, and are rejected when
     they cannot fit Workday's 12-request ceiling. Explicit additions can use
     `--search` and `--max-pages`; too-small page budgets are rejected up front.
     Run `python3 probe.py --audit-config-identities` to live-check every
     configured Greenhouse, Ashby, and Lever source for renamed boards or
     cross-company slug collisions; any mismatch or unverifiable identity exits
     nonzero and remains visible for repair.
     Batch probing accepts YAML/CSV candidate ledgers, uses bounded concurrency,
     records transport failures separately from genuine misses, and produces an
     ATS-frequency roadmap without modifying production. Generic ATS landing
     page titles are corroborated with structured company fields or strong
     company-introduction phrases in job payloads; incidental name mentions do
     not count. Batch additions are dry-run-only by default and require an
     explicit approval for every source.
     Approved rows that are already present in `config.yaml` are skipped rather
     than turning an otherwise successful bulk run into a false failure.
     A reviewed wave of structurally verified but currently empty boards can
     use batch `--allow-empty`; the override is forwarded to every explicitly
     approved row and is never the default.
     For large waves, start with `--slug-only` to complete the fast
     Greenhouse/Ashby/Lever phase, then run domain discovery only for selected
     misses; this avoids multiplying every miss into many sequential requests.
     Name-based slug hits also verify the public board title or an official
     redirect before they are marked verified. This catches plausible but wrong
     collisions such as `bcg` and `tcs`; uncertain identity stays review-only.
     Company-domain discovery applies the same check to structured links found
     on careers pages, preventing an unrelated customer or partner ATS link from
     being attributed to the company being probed.
     Use `python3 probe.py --batch candidates.yaml --merge-report probe-report.yaml`
     to retain probe lifecycle evidence. When the report is a new wave, add
     `--append-new`; importing absent rows is opt-in so ordinary re-probes cannot
     grow the ledger unexpectedly. Then run
     `python3 add_source.py --batch candidates.yaml --sync-active` after onboarding.
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

     # Microsoft and Qualcomm share the public Eightfold/PCSX adapter.
     - name: Microsoft
       ats: eightfold
       base_url: https://apply.careers.microsoft.com
       domain: microsoft.com
       query: intern
       location: India

     - name: Qualcomm
       ats: eightfold
       base_url: https://careers.qualcomm.com
       domain: qualcomm.com
       query: intern
       location: India
       seniority: Intern
     ```

   JS-only boards can emit individual jobs instead of a generic page-change
   alert by setting `job_selector` plus an optional `id_regex`,
   `title_selector`, `job_location_selector`, and `zero_result_text`. If the
   selector is missing and no explicit zero marker is present, the source is
   recorded as failed.

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
- `python hybrid_hunter.py --test --company "Microsoft"` — run or seed one named source. `--company` is repeatable and accepts configured aliases; associated fallback/program monitors are included automatically.
- `python hybrid_hunter.py --ats-only` / `--pages-only` — hunt only ATS boards (no browser needed) or only Playwright custom pages. The scheduled workflow uses `--ats-only` for the hourly runs.
- `python hybrid_hunter.py --heartbeat` — send a read-only status report (sources, finds in last 24h/7d, failing sources). Does not hunt.
- `python hybrid_hunter.py --help` — list every supported flag. An unrecognized
  flag aborts the run rather than being ignored, so a typo can't silently turn
  an intended `--test` into a live alerting run.
- `python hybrid_hunter.py --validate` — preflight config lint: unknown `ats:` types, missing required adapter fields, duplicate source names. Exits nonzero on problems, hunts nothing.
- `python hybrid_hunter.py --test --shard-count 8 --shard-index 0` — run one
  deterministic source shard. Related fallback/program pages stay with their
  parent. Live shards use isolated `state.shard-I-of-N.json` files and refuse
  to start until each shard has first been initialized with `--seed`.
- `python catalog_report.py` — read-only catalog/health report covering source
  counts, structured ratio, candidate conversion, current failures, missing or
  stale runtime evidence, suspicious zero streaks, request counts, and p95
  source latency.

Offline reliability checks are available through `python test_ats.py`,
`python test_adapters.py`, `python test_priority_sources.py`, and
`python test_reliability.py`, and `python test_expansion.py`; the hunt workflow
runs all five before crawling.

Delivery guarantees: live and heartbeat runs refuse to start without Telegram
credentials. Matches are only marked as seen after the Telegram message is
delivered or durably queued. If Telegram is down or rejects a message, the
digest remains in `state.json` under `_pending` and is retried on later runs;
it is never discarded because of an attempt limit.

Failure visibility: a source that fails 3 runs in a row (API error, wrong slug, bot-blocked or empty page) triggers a one-time ⚠️ Telegram warning, and a ✅ notice when it recovers. Custom pages returning empty or suspiciously short content (<200 chars — likely CAPTCHA/block) count as failures, not content changes. The daily heartbeat reports real stats: new roles in the last 24h/7d, jobs tracked, pending alerts, and any failing sources. On GitHub Actions, each run writes a per-source result table to the job summary.
