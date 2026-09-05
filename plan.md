# Company Coverage Expansion Plan

The repository started this expansion at 84 sources. As of 2026-08-08 it
tracks 370 verified sources: 364 structured ATS companies and 6 custom pages.
The 300-source milestone is complete, and structured coverage is 98.4%.

The long-term objective is to reach 500 verified sources, then 1,000, without
weakening failure detection, pagination limits, or delivery-before-dedup
guarantees. Expansion is currently paused by request; maintenance and repair of
the existing catalog take priority, and no new companies should be admitted
until that direction changes.

## 1. Establish a candidate pipeline

Create a candidate ledger separate from `config.yaml`. Unverified companies
must never be added directly to production configuration.

Track:

- Company name and careers URL
- India and remote relevance
- Internship or new-grad evidence
- Detected ATS family
- Status: `candidate`, `probed`, `verified`, `seeded`, or `active`
- Failure reason and last verification date

Build candidates from startup portfolios, campus-employer lists, major Indian
engineering employers, multinational India offices, and remote-friendly
companies.

## 2. Automate bulk discovery

Extend `probe.py` and `add_source.py` with a batch workflow that:

- Accepts CSV or YAML company lists
- Discovers careers URLs and ATS fingerprints
- Probes sources with bounded concurrency
- Produces a review report
- Generates candidate configuration without modifying production
- Rejects ambiguous, empty, truncated, or unverifiable sources

Only reviewed entries should advance through the existing `--test` and
`--seed` process.

## 3. Expand in measured waves

| Milestone | Main approach | Engineering prerequisite |
|---|---|---|
| 84 to 150 | Greenhouse, Ashby, and Lever | Batch probing |
| 150 to 300 | Workday, Eightfold, Oracle, and existing enterprise adapters | Better URL and tenant discovery |
| 300 to 500 | Add the most common unsupported ATS families | Adapter contract tests |
| 500 to 1,000 | Long-tail ATS platforms and selected browser sources | Scheduling and state sharding |

Fingerprint the candidate ledger and rank unsupported ATS platforms by the
number of blocked companies. Likely candidates include SmartRecruiters,
Workable, SuccessFactors, iCIMS, Jobvite, Taleo, ADP, Phenom, Avature,
Personio, and BambooHR, but implementation order should follow measured
coverage rather than assumptions.

## 4. Preserve a strict admission gate

Every source must pass all of the following:

1. The endpoint or rendered page resolves reliably.
2. Jobs have stable IDs, titles, locations, and URLs.
3. Zero results are explicitly distinguishable from parser failure.
4. Pagination finishes within the four-request budget.
5. Malformed responses and non-200 statuses raise errors.
6. Offline adapter tests pass.
7. A live `--test --company` run succeeds.
8. `--seed` completes before activation.
9. The first three scheduled runs remain healthy.

This follows the repository's add-source decision tree and prevents increased
coverage from introducing silent failures.

## 5. Use browser sources selectively

Custom Playwright pages should remain the last resort because they are slower
and more fragile.

Before exceeding roughly 25 to 50 custom pages:

- Reuse one Chromium process across sources
- Add reusable templates for common job-card layouts
- Enforce per-source navigation and parsing timeouts
- Record explicit zero-result markers
- Run custom pages in deterministic cohorts
- Keep landing-page hash monitors separate from job-level sources

At least 85 to 90 percent of active sources should use structured ATS
adapters.

## 6. Scale execution safely

At several hundred sources, a single sequential hourly run will become
unreliable. Add:

- `--shard-index` and `--shard-count`
- Deterministic source-to-shard assignment
- Separate state files per shard or source
- High-priority hourly and long-tail four-to-six-hour cohorts
- Independent pending-delivery queues
- Workflow locks that prevent concurrent state writers

The delivery-before-dedup invariant must remain local to each shard.

## 7. Operate a maintained catalog

Track:

- Active and verified source count
- Candidate-to-active conversion rate
- Success rate by ATS family
- Consecutive failures and recoveries
- Runtime and request count per source
- Sources returning suspicious zeroes
- Pending Telegram deliveries
- Sources not revalidated recently

Run weekly revalidation. Broken sources should be visibly flagged for repair
or migration and must never be silently disabled.

## First implementation slice

1. Define the candidate ledger format.
2. Add `probe.py --batch`.
3. Produce an ATS fingerprint summary report.
4. Onboard the first batch of 50 to 100 companies using existing adapters.
5. Generate the adapter roadmap from unsupported-candidate counts.

This delivers immediate coverage growth while identifying the next adapter
that will unlock the largest verified batch.

## Implementation status

- [x] Candidate ledger with lifecycle, relevance, category, evidence, and
  active-source metadata
- [x] Bounded YAML/CSV batch probing with persistent review reports
- [x] ATS fingerprint and blocked-ATS roadmap summaries
- [x] Explicit approval-gated batch onboarding through probe, test, and seed
- [x] Transactional rollback for failed tests and failed seeds
- [x] Five candidate waves plus enterprise and identity-review follow-up: 711
  cataloged targets and 336 activated in the candidate ledger
- [x] SmartRecruiters and Workable structured adapters with failure-contract
  tests
- [x] Shared Chromium lifecycle and configurable per-source browser timeouts
- [x] Deterministic source sharding with isolated state and unseeded-live-run
  protection
- [x] Per-source health, request, latency, successful-zero, failure, and
  pending-delivery reporting
- [x] GitHub Actions validation, expansion tests, manual shard controls,
  concurrency lock, and catalog summary
- [x] Workday discovery defaults to narrow `search: internship`, computes the
  complete page budget, and rejects boards above the 12-request ceiling
- [x] Enterprise follow-up added Applied Materials, Lam Research, KLA, Palo
  Alto Networks, and Western Digital through verified test-and-seed admission
- [x] Third wave added 17 more verified sources; one unreadable Workday board
  was rolled back instead of accepting a partial result set
- [x] Slug identity checks reject exact-looking collisions (the `bcg` and `tcs`
  slugs were unrelated companies) and large waves support a fast `--slug-only`
  first stage
- [x] Fourth wave plus its official-domain follow-ups added 75 verified
  sources through the full test-and-seed gate, raising active
  coverage from 196 to 271; nine structurally verified empty boards were
  admitted with an explicit override so future openings are still detected
- [x] Probe reports can append an explicitly requested new wave to the ledger;
  ordinary evidence merges still cannot grow it unexpectedly
- [x] Company-domain discovery rechecks the identity of every discovered slug
  board; an unrelated Langfuse link on ClickHouse's site exposed and now
  regression-tests this cross-company attribution failure
- [x] Batch onboarding can explicitly forward `--allow-empty` for reviewed
  boards; empty-source admission remains opt-in rather than a silent default
- [x] Fifth wave and its official-domain/entity follow-ups added 74 verified
  sources, raised active coverage from 271 to 345, and kept Samsung Electronics and
  NTT Global Data Centers separate from misleading parent/research labels
- [x] Payload-backed identity review resolved 27 existing candidates, then a
  full live audit checked all 311 configured slug sources and exposed three
  production collisions: xAI pointed to SpaceXAI, LinkedIn to LI Test Company,
  and Remote to General Assembly Remote Jobs. The invalid entries were removed;
  IMC Trading was retained only after its official job ID corroborated the
  shorter `IMC` board identity. LinkedIn was migrated to its official India
  engineering-internship page rather than dropped, leaving the frozen catalog
  at 370 sources.
- [x] Add a repeatable `probe.py --audit-config-identities` maintenance check;
  all 308 remaining slug sources currently verify, all structured adapters pass
  a full dry run (one transient OpenAI timeout passed on retry), and all six
  custom pages pass Playwright verification
- [x] Full-catalog output review added measured foreign-location vetoes for
  `San Francisco` and `Warsaw`, removing the only two unintended remote matches
  (Cribl and UiPath) without changing any other live match
- [x] Reach 300 verified active sources
- [ ] Reach 500 verified active sources (paused by request)
- [ ] Reach 1,000 verified active sources (paused by request)
- [ ] Implement additional ATS adapters in measured blocked-candidate order
