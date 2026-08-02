---
name: add-source
description: Add a company, careers page, or program URL to job tracking. Use when the user says "track X", "add X to tracking", "monitor X's careers page", or gives a company/URL to watch for internships. Covers ATS probing, JS-only boards, and program-monitor pages.
---

# Adding a source to tracking

Goal: end with a **verified, baselined** entry in `config.yaml`. Never add an
entry you haven't seen work — a wrong slug becomes a ❌-failing source that
pages the user after 3 runs.

## Step 1 — Try the automated path first

```bash
python3 add_source.py "Company Name"                  # name-based slug probe
python3 add_source.py "Company Name" --url <careers-URL>   # URL-based (Workday/Eightfold too)
```

This probes Greenhouse/Ashby/Lever (by slug) or Workday/Eightfold/amazon.jobs
(by URL), appends a correct entry before `custom_pages:`, verifies with
`--test --ats-only --company`, and baselines with `--seed`. If it succeeds,
you're done — tell the user to commit `config.yaml` + `state.json`.

`python3 probe.py "Name or URL"` runs the same detection read-only if you
only want to look.

## Step 2 — Probe failed? Identify what the page actually is

Fetch the URL (WebFetch) and classify it:

**(a) A structured board on an ATS we support but probe can't guess** —
check the README "Supported ATS types" table; some adapters (oracle_hcm,
google, intuit, goldman_higher, deshaw) take hand-configured params. Verify
the live endpoint with curl first, add the entry manually, then:

```bash
python3 hybrid_hunter.py --validate
python3 hybrid_hunter.py --test --company "Name"
python3 hybrid_hunter.py --seed --company "Name"
```

**(b) A JS-rendered job board (individual role cards)** — add a
`custom_pages` entry with `job_selector` so it emits deduped jobs, not page
hashes. Find selectors with `python3 diagnose.py "Name" --screenshots`.
Pattern (see the Atlassian entry in config.yaml):

```yaml
- name: Company
  url: https://...
  wait_for_selector: "a[href*='/careers/']"
  job_selector: "a[href*='/careers/']"
  id_regex: "/careers/([^/?#]+)"
  zero_result_text: ["No jobs found"]   # REQUIRED — else empty board = failure
  location_filter: false                # if the URL is already location-filtered
```

**(c) A program/landing page (no role cards — e.g. "applications closed")** —
add a hash-based program monitor. Pattern (see "Anthropic Claude Campus" in
config.yaml):

```yaml
- name: Company Program Name (program monitor)
  supplement_for: Company        # if the company already has an ATS entry
  url: https://...
  keyword_filter: false          # false = alert on ANY substantive change
  location_filter: false
```

Use `keyword_filter: true` only when the page reliably contains the word
"intern"/"summer" while relevant; a reopening announcement may not.

## Step 3 — Always finish the same way

1. `python3 hybrid_hunter.py --test --company "Name"` must show ✅ (custom
   pages need the full run, not `--ats-only`).
2. `python3 hybrid_hunter.py --seed --company "Name"` unless the user wants
   immediate alerts for everything currently open.
3. Annotate the config entry with a short comment (why this pattern/filters —
   match the file's existing habit).
4. Remind the user to commit `config.yaml` and `state.json`.

## Constraints

- One company can have several entries (ATS board + fallback/supplement
  monitors); name collisions across `ats_companies`/`custom_pages` are bugs.
- Custom pages run only on the 4-hourly full runs, not hourly.
- Never put tokens in config.yaml; never hand-edit state.json.
