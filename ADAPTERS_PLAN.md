# Enterprise ATS Adapters — Plan (Phase 5)

**Date:** 2026-07-07 · **Author:** Fable (plan/review) + Opus 4.8 (implementation)

## 1. Problem

The crawler covers Greenhouse/Ashby/Lever/Oracle-HCM. Verified today: none of
FAANG or major finance (Google, Meta, Amazon, Apple, Microsoft, Nvidia, Citi,
BlackRock, American Express, Wells Fargo, Goldman Sachs, Morgan Stanley, Visa,
Mastercard, PayPal, Capital One) are reachable on those — every slug 404s
(JP Morgan is the exception, already tracked via oracle_hcm). These employers
run Workday or proprietary career APIs. They are also among the heaviest
intern recruiters in India (Bangalore/Hyderabad), i.e. exactly Shane's target.

## 2. Goals / Non-goals

**Goals (in priority order):**
1. `workday` adapter → unlocks NVIDIA, Citi, BlackRock, American Express +
   large-enterprise India tail (Adobe, Qualcomm, Salesforce, Sprinklr, Fractal).
2. `amazon` adapter (amazon.jobs JSON search endpoint).
3. `microsoft` adapter (gcsservices.careers.microsoft.com search API).
4. Verified config entries for every tenant added; seeded before deploy.

**Stretch (attempt; drop with a one-line justification if flaky):**
5. `apple` adapter (jobs.apple.com — CSRF-gated POST; historically brittle).
6. `eightfold` adapter (PayPal, Netflix).

**Non-goals:** Google & Meta (bot-protected custom sites — Playwright
hash-watch remains the only option; do not attempt). No workflow changes
(hourly `--ats-only` already covers new adapters — they're plain HTTP).
No git commits (review happens first).

## 3. Architecture

- Extend `ATSHunter` in `hybrid_hunter.py` with `hunt_workday`,
  `hunt_amazon`, `hunt_microsoft` (+ stretch), matching the existing idiom:
  - **raise on any failure** (network, non-200, malformed JSON) — Phase 2
    failure-tracking and ⚠️ alerts then work for free;
  - return `[{id, title, location, url}]`;
  - accept `keywords=None` override; filter via `self.matches_criteria`;
  - use `API_HEADERS` (+ per-adapter extras like `Content-Type`).
- Extend the dispatch in `main()` with the new `ats:` types.
- **Request budget: ≤ 4 HTTP requests per source per run**, `timeout=15`.
  Prefer server-side filtering (searchText / loc_query / lc=India) so 1–2
  requests suffice; paginate at most to that cap and stop.
- Stable job IDs (dedup keys): Workday → trailing requisition id from
  `externalPath` (e.g. `JR1988855`), fall back to full path; Amazon →
  `id` / `id_icims`; Microsoft → `jobId`.

## 4. Adapter specs

### 4.1 workday (Workday CXS)
- `POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
  body `{"appliedFacets": {…}, "limit": 20, "offset": N, "searchText": "intern"}`,
  headers: `Content-Type: application/json`, `Accept: application/json`.
- Response: `total`, `jobPostings[]` with `title`, `externalPath`,
  `locationsText`, `postedOn`, `bulletFields`.
- Job URL: `https://{tenant}.{wd_host}.myworkdayjobs.com/en-US/{site}{externalPath}`.
- Config: `tenant`, `wd_host` (wd1/wd3/wd5/…), `site`, optional `search`
  (default `intern`), optional `india_only: true`.
- **India facet:** empirically test `appliedFacets: {"Location_Country":
  ["c4f78be1a8f14da0ab49ce1162348a5e"]}` (Workday's shared India GUID) on the
  NVIDIA tenant. If it filters correctly → use for `india_only` tenants.
  If not → rely on searchText + client-side `matches_criteria` on
  `locationsText`. NOTE: multi-location postings show `locationsText: "N
  Locations"` — when the text is that pattern AND the location filter would
  reject it, include the job anyway iff the title matches (better one extra
  alert than a silent miss); document this choice in a comment.

### 4.2 amazon (amazon.jobs)
- `GET https://www.amazon.jobs/en/search.json?base_query={query}&loc_query={location}&result_limit=100&offset=0`
- Response: `jobs[]` with `title`, `id_icims`/`id`, `job_path`,
  `normalized_location`/`location`. URL = `https://www.amazon.jobs{job_path}`.
- Config: optional `query` (default `intern`), `location` (default `India`).
- Send a browser-like User-Agent; if the endpoint returns 403 in CI-like
  conditions, note it and keep the adapter (failure tracking will surface it).

### 4.3 microsoft (Microsoft careers)
- `GET https://gcsservices.careers.microsoft.com/search/api/v1/search?q={query}&lc={location}&l=en_us&pg={page}&pgSz=20`
- Response under `operationResult.result`: `jobs[]` (`jobId`, `title`,
  `properties.locations[]`), `totalJobs`.
- URL = `https://jobs.careers.microsoft.com/global/en/job/{jobId}`.
- Config: optional `query` (default `intern`), `location` (default `India`).

### 4.4 apple (STRETCH)
- `jobs.apple.com` search API requires a CSRF token from an initial GET, then
  `POST /api/role/search`. Attempt; if token dance is unreliable, drop.

### 4.5 eightfold (STRETCH)
- `GET https://{host}/api/apply/v2/jobs?query={q}&location={loc}&num=50`
  (PayPal: `paypal.eightfold.ai`). Attempt; drop if unstable.

## 5. Config roster (verify EVERY entry live before adding)

New section `# -- Enterprise (Workday / custom APIs) --`:

| Company | ats | Discovery hint |
|---|---|---|
| NVIDIA | workday | nvidia.wd5 / NVIDIAExternalCareerSite |
| Citi | workday | citi.wd5, site likely `2` |
| BlackRock | workday | blackrock.wd1 / BlackRock_Professional |
| American Express | workday | aexp.wd1 (verify site name) |
| Adobe | workday | adobe.wd5 / external_experienced (check univ site too) |
| Qualcomm | workday | qualcomm.wd5 / External |
| Salesforce | workday | salesforce tenant (verify host) |
| Sprinklr | workday | sprinklr.wd1 (India-heavy) |
| Fractal Analytics | workday | fractal.wd1 (India AI) |
| Amazon | amazon | query=intern, location=India |
| Microsoft | microsoft | query=intern, location=India |
| Apple | apple | stretch |
| PayPal | eightfold | stretch |

Discovery: WebSearch/WebFetch the company careers URL → the
`*.myworkdayjobs.com` redirect reveals tenant/host/site. Verify with a live
curl POST returning `jobPostings` before adding to config. Drop any tenant
that can't be verified (list it in the final report instead).

## 6. Testing & verification

1. `test_adapters.py` at repo root (unittest + `unittest.mock`, offline):
   - happy path per adapter (realistic response fixtures → parsed matches);
   - keyword/location/exclude filtering applied;
   - failure paths raise (non-200, malformed JSON);
   - Workday pagination stops at the request cap;
   - "N Locations" inclusion rule.
2. Regression: `python3 -m py_compile hybrid_hunter.py`; run the three
   existing suites in the session scratchpad (paths in the task brief) —
   all must stay green.
3. Live: `python3 hybrid_hunter.py --test --ats-only` → every new source ✅
   (0 matches is fine; ❌ means fix or drop the entry).
4. YAML validity of config.yaml.

## 7. Rollout (after Fable review passes)

1. `python3 hybrid_hunter.py --seed --ats-only` (baseline new sources;
   capture the currently-open matches list first for the user).
2. Commit + push (Fable does this, not the coder).
3. Trigger one manual Actions run; confirm per-source ✅ in the step summary.

## 8. Acceptance criteria

- [ ] `workday`, `amazon`, `microsoft` adapters merged, matching house style
- [ ] ≥ 6 verified Workday tenants + Amazon + Microsoft in config.yaml
- [ ] Every configured source shows ✅ in a live `--test --ats-only`
- [ ] `test_adapters.py` passes; all existing suites pass; YAML parses
- [ ] README: adapter table + config examples updated; stale "not scrapable"
      claims corrected
- [ ] Request budget & timeouts respected; failures raise
- [ ] No git operations performed by the coder

## 9. Risks & fallbacks

- **Workday API drift / per-tenant quirks** → verify per tenant; drop
  non-conforming tenants rather than special-casing.
- **amazon.jobs bot protection from GitHub runner IPs** → acceptable: Phase 2
  failure alerts will surface it within 3 runs; document as known-risk.
- **Huge boards (Amazon ~50k jobs)** → server-side query params are
  mandatory, never fetch unfiltered listings.
- **ID instability** → dedup keys must come from requisition/job IDs, never
  from array positions or titles.
