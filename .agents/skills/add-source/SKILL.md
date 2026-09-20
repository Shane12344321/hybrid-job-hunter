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
python3 add_source.py "Company Name" --preview --review-out tmp/company-review.yaml
python3 add_source.py "Company Name" --url <careers-URL> --preview --review-out tmp/company-review.yaml
python3 add_source.py "Company Name" --from-job-url <job-URL> --preview --review-out tmp/company-review.yaml
```

Create the report's parent directory if needed. Preview discovers a board,
checks identity and duplicates, and shows accepted, rejected, and uncertain
role examples without editing config or runtime state. Summarize this evidence
to the user. A clear request to "track" the named company authorizes adding
and baselining that source; do not ask for the same approval again. A request
only to inspect a company does not authorize application.

When the preview identifies the intended company unambiguously and has no
unresolved scope/duplicate warnings, apply its reviewed row:

```bash
python3 add_source.py --batch tmp/company-review.yaml --approve "Company Name" --apply
```

Application rechecks live evidence and verifies/baselines only the selected
source. A saved preview is not proof that a board still belongs to the company.
If identity, a distinct board scope, or a missing company name remains
ambiguous, retain the draft and ask for the missing choice. Do not choose the
largest board, invent a name from a URL slug, or bypass identity checks using
explicit adapter flags. If the source is already tracked, report its existing
entry instead of adding it twice.

`python3 probe.py "Name or URL"` is also available for read-only discovery.

## Step 2 — Probe failed? Identify what the page actually is

Retain the preview's non-actionable draft, evidence, and next action. Inspect
the URL to distinguish these cases:

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
4. Report verification, the new-source baseline, and any remaining draft.
   Remind the user to commit `config.yaml` and `state.json`; do not commit or
   push unless requested.

## Constraints

- One company can have several entries (ATS board + fallback/supplement
  monitors); name collisions across `ats_companies`/`custom_pages` are bugs.
- Custom pages run only on the 4-hourly full runs, not hourly.
- Never put tokens in config.yaml; never hand-edit state.json.
- Selector guesses need visual verification; never promote them automatically.
- Role relevance is independent of career stage/location. In enforcement mode
  uncertain roles go to "Needs review" and explicit unrelated roles are omitted.
  In shadow mode, show the proposed verdicts without claiming alerts are filtered.
