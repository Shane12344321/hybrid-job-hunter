# Company Coverage Expansion Plan

The repository started this expansion at 84 sources. As of 2026-07-23 it
tracks 175 verified sources: 170 structured ATS companies and 5 custom pages.
The first 150-source milestone is complete, and structured coverage is 97.1%.

The objective is to reach 500 verified sources, then 1,000, without weakening
failure detection, pagination limits, or delivery-before-dedup guarantees.

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
- [x] First two candidate waves: 205 cataloged targets and 91 activated
- [x] SmartRecruiters and Workable structured adapters with failure-contract
  tests
- [x] Shared Chromium lifecycle and configurable per-source browser timeouts
- [x] Deterministic source sharding with isolated state and unseeded-live-run
  protection
- [x] Per-source health, request, latency, successful-zero, failure, and
  pending-delivery reporting
- [x] GitHub Actions validation, expansion tests, manual shard controls,
  concurrency lock, and catalog summary
- [ ] Reach 300 verified active sources
- [ ] Reach 500 verified active sources
- [ ] Reach 1,000 verified active sources
- [ ] Implement additional ATS adapters in measured blocked-candidate order
