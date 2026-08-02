# AGENTS.md

Guidance for coding agents working in this repo. Human-facing docs live in
[README.md](README.md); this file covers what an agent needs to make safe,
verifiable changes.

## What this is

A job-hunting crawler for internships/new-grad roles (India-focused). One
script, `hybrid_hunter.py` (~1,300 lines), hunts two kinds of sources defined
in `config.yaml`:

1. **ATS adapters** (`ats_companies`) — direct HTTP against structured
   APIs/HTML: Ashby, Greenhouse, Lever, Workday, Amazon, Eightfold
   (Microsoft/Qualcomm), Oracle HCM, Google, Intuit, Goldman Higher, D. E.
   Shaw. Fast, job-level dedup.
2. **Custom pages** (`custom_pages`) — Playwright-rendered pages, either
   parsed into individual jobs (`job_selector`) or hashed for change
   detection (program monitors / landing pages).

New matches are delivered as a Telegram digest; per-source state (seen job
IDs, page hashes, failure counts, undelivered digests) persists in
`state.json`, which GitHub Actions commits back to the repo.

## Layout

| Path | Role |
|---|---|
| `hybrid_hunter.py` | Everything: `StateManager`, `ATSHunter` (all adapters), `CustomWebHunter` (Playwright), digest delivery, `main()` |
| `config.yaml` | Keywords, exclude-keywords, locations, Telegram env refs, `ats_companies`, `custom_pages` |
| `state.json` | Runtime state — **machine-managed, never hand-edit** (see below) |
| `test_adapters.py`, `test_priority_sources.py`, `test_ats.py`, `test_reliability.py` | Offline unittest suites, all network mocked |
| `diagnose.py` | Shows what Playwright actually renders on a custom page (`python3 diagnose.py "Name" [--screenshots]`) |
| `probe.py` | Read-only ATS detection: slug probing (Greenhouse/Ashby/Lever) + URL recognition (Workday/Eightfold/amazon.jobs) |
| `add_source.py` | One-command add: probe → append to config.yaml → verify `--test` → baseline `--seed`, with rollback on failure |
| `.claude/skills/add-source/` | Decision tree for sources the probe can't handle (JS-only boards, program monitors) |
| `append_ats.py`, `append_custom.py`, `ats_results.txt` | Legacy one-off helpers, superseded by `probe.py`/`add_source.py` (note: they rewrite config.yaml via yaml.dump, destroying comments — don't reuse) |
| `.github/workflows/hunt.yml` | Hourly `--ats-only` + 4-hourly full run; commits `state.json` |
| `.github/workflows/heartbeat.yml` | Daily `--heartbeat` status report (read-only, no browser) |
| `ADAPTERS_PLAN.md` | Design doc for the Phase 5 enterprise adapters |

## Running things

```bash
pip install -r requirements.txt
playwright install chromium          # only needed for custom pages

# Offline tests (no network, no state side effects — they chdir to a tmpdir):
python3 test_adapters.py
python3 test_priority_sources.py
python3 test_ats.py
python3 test_reliability.py

# Live dry run of one source — the standard way to verify a config change:
python3 hybrid_hunter.py --test --company "Exact Source Name"

# Preflight lint (missing required adapter fields, duplicate names):
python3 hybrid_hunter.py --validate

# Other flags: --seed (baseline new sources without alerting), --ats-only,
# --pages-only, --heartbeat. --company is repeatable and matches aliases;
# fallback/supplement monitors ride along with their parent automatically.
```

`--test` prints matches and never touches `state.json` or Telegram — prefer it
for all local verification. A real run (no flags) requires `TELEGRAM_TOKEN`
and `TELEGRAM_CHAT_ID` env vars and mutates `state.json`; don't do that
locally unless explicitly asked.

## Core invariants — keep these true

- **Fail loudly, never return a silent zero.** Every adapter raises on
  network errors, non-200s, bad JSON/HTML, missing result markers, or
  truncated pagination. "Source broken" and "source has no matches" must stay
  distinguishable: 3 consecutive failures trigger a ⚠️ Telegram alert (and ✅
  on recovery). A custom page with no matched cards is a *failure* unless a
  configured `zero_result_text` marker is present on the page.
- **Delivery before dedup.** Jobs are marked seen / hashes stored only after
  the Telegram digest is delivered or queued in `state.json[_pending]`
  (retried up to 5 attempts). Never reorder this — an alert must never be
  silently lost.
- **Pagination budget.** Structured sources cap at ≤ 4 requests/run
  (~15s timeout). An incomplete read raises rather than returning a partial
  page. `max_pages:` raises the cap per source where the ATS forces it —
  Workday hard-caps `limit` at 20 server-side, so a wide `searchText` needs
  more pages to be read in full (ceiling 12; every other adapter stays at 4).
- **Workday `search:` must be narrow.** The default `intern` fuzzy-matches
  `internal`/`international`, inflating totals past any budget (NVIDIA 901,
  Citi 2000 — and 2000 is Workday's response cap, so that set is unreadable at
  any `max_pages`). Prefer `search: internship`, which Workday still stems onto
  `Intern` titles. Before narrowing a source, read its full `intern` set once
  and diff the India-relevant matches, then record the date in a comment —
  a narrower term must never drop a role.
- **Trust Workday's `total` from the first page only.** Some tenants report
  `total=0` on every `offset>0` request; letting a later page overwrite it made
  a partial read look complete, which is the silent miss this design forbids.
- **Title filtering** is word-boundary and case-insensitive, against
  `keywords` minus `exclude_keywords`. Per-company `keywords:` overrides the
  global list.

## Editing `config.yaml`

- **Adding a company? Use `python3 add_source.py "Name" [--url URL]` first.**
  It probes, verifies, appends (comment-preserving textual insert), and seeds
  in one shot, rolling back on failure. Only fall back to hand-editing for
  adapters it doesn't cover (oracle_hcm, google, intuit, goldman_higher,
  deshaw) or custom pages — the `/add-source` skill has the decision tree.
- **Verify before adding by hand**: slugs/tenants 404 easily. Confirm the live
  endpoint (WebFetch/curl the ATS API, or `diagnose.py` for custom pages),
  run `python3 hybrid_hunter.py --validate`, then
  `python3 hybrid_hunter.py --test --company "New Name"` and confirm it parses.
- After adding a source, note that the first live run will alert on every
  currently-open match; run `--seed` (or tell the user to) if that flood is
  unwanted.
- Custom-page entries: `keyword_filter`/`location_filter` default to
  false/true respectively; program monitors that should alert on *any* change
  set both false. `fallback_for:`/`supplement_for:` tie a page to a parent
  ATS source for `--company` selection and reporting.
- Comments in `config.yaml` carry real operational knowledge (why a slug is
  quoted, why a filter is off). Keep the habit: annotate non-obvious entries.
- Never put real tokens in `config.yaml` — `${TELEGRAM_TOKEN}` /
  `${TELEGRAM_CHAT_ID}` are resolved from the environment.

## `state.json`

Keys are source names → seen job IDs / page hash; `_pending` holds
undelivered digests, `_failures` tracks consecutive-failure counts. It churns
constantly from CI (`chore: update job hunter state` commits). Don't hand-edit
it, don't "clean it up", and leave it out of feature commits unless the change
is deliberate (e.g. a rename migration). Expect `git pull --rebase` noise from
the Actions bot when pushing.

## Adding an ATS adapter

Follow the existing pattern in `hybrid_hunter.py` (see `hunt_workday`,
`hunt_eightfold`): raise on anything unexpected, respect the pagination
budget, return `{"id", "title", "location", "url"}` dicts, and wire the
`ats:` key into the dispatch in `main()`. Add offline mocked tests in the
style of `test_adapters.py` (patch `hh.requests`, cover: parses matches,
raises on failure, filtering, pagination cap). Update the README's ATS table.
`ADAPTERS_PLAN.md` shows the level of up-front verification expected —
endpoints were curl-verified before implementation.

## Style & conventions

- Python 3.11, stdlib + `requests`/`bs4`/`yaml`/`playwright` only. Pinned
  versions in `requirements.txt`.
- Single-file architecture is deliberate (easy to vendor into Actions); don't
  split into a package without being asked.
- Tests are plain `unittest`, run directly as scripts (no pytest, no runner
  config).
- Commit style: `feat:`/`fix:`/`chore:` prefixes, imperative subject.
