"""Offline tests for candidate discovery and reviewed batch onboarding."""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import yaml

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

import add_source
import catalog_report
import probe
import testing_support

setUpModule = testing_support.block_network
tearDownModule = testing_support.restore_network


def _response(status, payload):
    """Minimal stand-in for a requests.Response."""
    return mock.Mock(status_code=status, json=mock.Mock(return_value=payload))


class TestCandidateLedger(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def path(self, name):
        return os.path.join(self.directory.name, name)

    def test_yaml_and_csv_ledgers_load(self):
        yaml_path = self.path("candidates.yaml")
        with open(yaml_path, "w") as stream:
            yaml.safe_dump({"version": 1, "candidates": [{
                "name": "Example", "careers_url": "https://jobs.example.test",
                "status": "candidate",
            }]}, stream)
        self.assertEqual(probe.load_candidates(yaml_path)[0]["name"], "Example")

        csv_path = self.path("candidates.csv")
        with open(csv_path, "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["name", "careers_url", "status"])
            writer.writeheader()
            writer.writerow({"name": "Other", "careers_url": "", "status": "candidate"})
        self.assertEqual(probe.load_candidates(csv_path)[0]["name"], "Other")

    def test_duplicate_and_invalid_status_are_rejected(self):
        path = self.path("bad.yaml")
        with open(path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "Example", "status": "candidate"},
                {"name": "example", "status": "unknown"},
            ]}, stream)
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            probe.load_candidates(path)

    def test_batch_report_is_ordered_and_counts_fingerprints(self):
        path = self.path("candidates.yaml")
        with open(path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "First"}, {"name": "Second"},
            ]}, stream)

        def fake_probe(candidate, use_derived_domains=True):
            return {
                **candidate, "status": "probed", "probe_status": "verified_endpoint",
                "suggested_entry": {
                    "name": candidate["name"], "ats": "greenhouse", "slug": "example",
                },
            }

        with mock.patch.object(probe, "probe_candidate", side_effect=fake_probe):
            report = probe.batch_probe(path, workers=2)
        self.assertEqual([row["name"] for row in report["candidates"]], ["First", "Second"])
        self.assertEqual(report["summary"]["ats_fingerprints"], {"greenhouse": 2})

    def test_transport_error_is_not_reported_as_not_found(self):
        candidate = {"name": "Example", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([], ["greenhouse/example: connection failed"])), \
                mock.patch.object(probe, "probe_derived_domains",
                                  return_value=(None, None, None, None, [], [])):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "failed")
        self.assertIn("unreliable", result["reason"])

    def test_domain_candidates_are_derived_from_the_name(self):
        self.assertEqual(
            probe.domain_candidates("BrowserStack"),
            ["browserstack.com", "browserstack.ai", "browserstack.io", "browserstack.co"])
        # multi-word names also try the leading word, and suffixes are trimmed
        self.assertIn("huggingface.co", probe.domain_candidates("Hugging Face"))
        self.assertIn("acme.com", probe.domain_candidates("Acme Labs"))
        self.assertEqual(probe.domain_candidates("   "), [])

    def test_slug_miss_falls_back_to_the_derived_domain(self):
        # Slug probing only reaches Greenhouse/Ashby/Lever, so a Workday or
        # Workable company is invisible to it however good the slug guess is.
        entry = {"name": "Example", "ats": "workable", "account": "example"}
        with mock.patch.object(probe, "probe_name_detailed", return_value=([], [])), \
                mock.patch.object(
                    probe, "probe_derived_domains",
                    return_value=(entry, 7, "example.com",
                                  "https://apply.workable.com/example", [], [])):
            result = probe.probe_candidate({"name": "Example", "status": "candidate"})
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertEqual(result["suggested_entry"], entry)
        self.assertEqual(result["company_domain"], "example.com")
        self.assertEqual(result["live_postings"], 7)

    def test_unresolvable_derived_domains_stay_not_found(self):
        # A guessed domain that does not resolve is an expected miss, not an
        # unreliable probe — it must never be upgraded to "failed", which would
        # put a healthy candidate into the failure-alert path.
        with mock.patch.object(probe, "probe_name_detailed", return_value=([], [])), \
                mock.patch.object(
                    probe, "probe_derived_domains",
                    return_value=(None, None, None, None,
                                  ["groq.ai: NXDOMAIN", "groq.io: NXDOMAIN"], [])):
            result = probe.probe_candidate({"name": "Groq", "status": "candidate"})
        self.assertEqual(result["probe_status"], "not_found")
        self.assertIn("derived domains", result["reason"])
        self.assertTrue(any("NXDOMAIN" in w for w in result["warnings"]))

    def test_derived_domain_retains_unsupported_ats_for_roadmap(self):
        blocked = [{
            "ats": "icims", "company_domain": "example.com",
            "careers_url": "https://example.icims.com/jobs/search",
            "reason": "URL not recognized as a supported ATS",
        }]
        with mock.patch.object(probe, "probe_name_detailed", return_value=([], [])), \
                mock.patch.object(
                    probe, "probe_derived_domains",
                    return_value=(None, None, None, None, [], blocked)):
            result = probe.probe_candidate({"name": "Example", "status": "candidate"})
        self.assertEqual(result["probe_status"], "unsupported")
        self.assertEqual(result["unsupported_ats"], "icims")
        self.assertEqual(result["company_domain"], "example.com")

    def test_lossy_single_word_slug_requires_identity_review(self):
        candidate = {"name": "Pine Labs", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{"ats": "greenhouse", "slug": "pine", "jobs": 10}], [])), \
                mock.patch.object(
                    probe, "check_board_identity",
                    return_value=(False, "Jobs at Pine Services")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "needs_review")
        self.assertIn("lossy slug", result["warnings"][0])

    def test_exact_slug_identity_mismatch_requires_review(self):
        candidate = {"name": "TCS", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{"ats": "greenhouse", "slug": "tcs", "jobs": 94}], [])), \
                mock.patch.object(
                    probe, "check_board_identity",
                    return_value=(False, "Jobs at Thornbury Community Services")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "needs_review")
        self.assertIn("Thornbury", result["warnings"][0])

    def test_board_identity_does_not_treat_the_slug_url_as_evidence(self):
        response = mock.Mock(
            status_code=200,
            url="https://job-boards.greenhouse.io/tcs",
            text="<title>Jobs at Thornbury Community Services</title>",
        )
        with mock.patch.object(probe.requests, "get", return_value=response):
            matched, evidence = probe.check_board_identity(
                "TCS", "greenhouse", "tcs")
        self.assertFalse(matched)
        self.assertIn("Thornbury", evidence)

    def test_board_identity_accepts_a_matching_title_or_official_redirect(self):
        title_response = mock.Mock(
            status_code=200,
            url="https://jobs.ashbyhq.com/miro",
            text="<title>Miro Jobs</title>",
        )
        redirect_response = mock.Mock(
            status_code=200,
            url="https://www.highradius.com/about/careers-list/",
            text="<title>Grow With Us</title>",
        )
        with mock.patch.object(
                probe.requests, "get", side_effect=[title_response, redirect_response]):
            self.assertTrue(probe.check_board_identity(
                "Miro", "ashby", "miro")[0])
            self.assertTrue(probe.check_board_identity(
                "HighRadius", "greenhouse", "highradius")[0])

    def test_board_identity_accepts_structured_company_name(self):
        page_response = mock.Mock(
            status_code=200,
            url="https://job-boards.greenhouse.io/figureai",
            text="<title>Jobs</title>",
        )
        payload_response = mock.Mock(status_code=200)
        payload_response.json.return_value = {
            "jobs": [{"company_name": "Figure", "content": ""}],
        }
        with mock.patch.object(
                probe.requests, "get",
                side_effect=[page_response, payload_response]):
            matched, evidence = probe.check_board_identity(
                "Figure AI", "greenhouse", "figureai")
        self.assertTrue(matched)
        self.assertIn("Figure", evidence)

    def test_board_identity_accepts_strong_job_introduction(self):
        page_response = mock.Mock(
            status_code=200,
            url="https://jobs.ashbyhq.com/posthog",
            text="<title>Jobs</title>",
        )
        payload_response = mock.Mock(status_code=200)
        payload_response.json.return_value = {
            "jobs": [{"descriptionHtml": "<h2>About PostHog</h2>"}],
        }
        with mock.patch.object(
                probe.requests, "get",
                side_effect=[page_response, payload_response]):
            matched, evidence = probe.check_board_identity(
                "PostHog", "ashby", "posthog")
        self.assertTrue(matched)
        self.assertIn("posthog", evidence.casefold())

    def test_board_identity_uses_payload_when_landing_page_is_blocked(self):
        page_response = mock.Mock(status_code=403)
        payload_response = mock.Mock(status_code=200)
        payload_response.json.return_value = {
            "jobs": [{"company_name": "Rubrik", "content": ""}],
        }
        with mock.patch.object(
                probe.requests, "get",
                side_effect=[page_response, payload_response]):
            matched, evidence = probe.check_board_identity(
                "Rubrik", "greenhouse", "rubrik")
        self.assertTrue(matched)
        self.assertIn("Rubrik", evidence)

    def test_board_identity_rejects_incidental_payload_mention(self):
        page_response = mock.Mock(
            status_code=200,
            url="https://jobs.ashbyhq.com/alloy",
            text="<title>Jobs</title>",
        )
        payload_response = mock.Mock(status_code=200)
        payload_response.json.return_value = {
            "jobs": [{
                "descriptionPlain": "Experience with alloy materials is useful.",
            }],
        }
        with mock.patch.object(
                probe.requests, "get",
                side_effect=[page_response, payload_response]):
            matched, evidence = probe.check_board_identity(
                "Alloy", "ashby", "alloy")
        self.assertFalse(matched)
        self.assertIn("does not identify", evidence)

    def test_board_identity_rejects_longer_single_word_collision(self):
        page_response = mock.Mock(
            status_code=200,
            url="https://jobs.lever.co/neon",
            text="<title>Neon Pagamentos</title>",
        )
        payload_response = mock.Mock(status_code=200)
        payload_response.json.return_value = [{
            "descriptionPlain": "About Neon Pagamentos",
        }]
        with mock.patch.object(
                probe.requests, "get",
                side_effect=[page_response, payload_response]):
            matched, evidence = probe.check_board_identity(
                "Neon", "lever", "neon")
        self.assertFalse(matched)
        self.assertIn("Neon Pagamentos", evidence)

    def test_short_generic_suffix_free_alias_is_not_identity_evidence(self):
        self.assertEqual(probe._identity_aliases("Pine Labs"), ["pine labs"])
        self.assertEqual(
            probe._identity_aliases("Figure AI"), ["figure ai", "figure"])

    def test_config_identity_audit_checks_only_slug_adapters(self):
        config_path = self.path("config.yaml")
        with open(config_path, "w") as stream:
            yaml.safe_dump({
                "ats_companies": [
                    {"name": "Good", "ats": "ashby", "slug": "good"},
                    {"name": "Wrong", "ats": "greenhouse", "slug": "wrong"},
                    {
                        "name": "IMC Trading", "ats": "greenhouse", "slug": "imc",
                        "identity_aliases": ["IMC"],
                    },
                    {
                        "name": "Workday", "ats": "workday", "tenant": "wd",
                        "wd_host": "wd1", "site": "External",
                    },
                ],
            }, stream)

        def identity(name, ats, slug):
            if name in {"Good", "IMC"}:
                return True, f"Jobs at {name}"
            return False, "Other Jobs"

        with mock.patch.object(probe, "check_board_identity", side_effect=identity):
            results = probe.audit_config_identities(config_path, workers=2)
        self.assertEqual(
            [result["name"] for result in results],
            ["Good", "Wrong", "IMC Trading"])
        self.assertEqual(
            [result["status"] for result in results],
            ["verified", "mismatch", "verified"])
        self.assertIn("configured identity alias", results[2]["evidence"])

    def test_config_identity_audit_keeps_probe_exceptions_visible(self):
        config_path = self.path("config.yaml")
        with open(config_path, "w") as stream:
            yaml.safe_dump({
                "ats_companies": [{
                    "name": "Broken", "ats": "lever", "slug": "broken",
                }],
            }, stream)
        with mock.patch.object(
                probe, "check_board_identity", side_effect=RuntimeError("boom")):
            result = probe.audit_config_identities(config_path)[0]
        self.assertEqual(result["status"], "unverifiable")
        self.assertIn("RuntimeError: boom", result["evidence"])

    def test_lossy_slug_with_payload_identity_can_be_verified(self):
        candidate = {"name": "Lyra Health", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{
                    "ats": "lever", "slug": "lyra", "jobs": 3,
                }], [])), \
                mock.patch.object(
                    probe, "check_board_identity",
                    return_value=(True, "job payload introduction: lyra health")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")

    def test_exact_slug_with_matching_identity_is_verified(self):
        candidate = {"name": "Example", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{
                    "ats": "greenhouse", "slug": "example", "jobs": 3,
                }], [])), \
                mock.patch.object(
                    probe, "check_board_identity",
                    return_value=(True, "Jobs at Example")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertEqual(result["identity_evidence"], "Jobs at Example")

    def test_json_report_round_trip(self):
        path = self.path("report.json")
        report = {
            "version": 1, "generated_at": "now", "source": "input.yaml",
            "summary": {
                "total": 0, "probe_status": {}, "ats_fingerprints": {},
                "blocked_ats_fingerprints": {},
            },
            "candidates": [],
        }
        probe.write_report(report, path)
        with open(path) as stream:
            self.assertEqual(json.load(stream), report)

    def test_report_merge_updates_probe_lifecycle_without_overwriting_active(self):
        ledger = self.path("ledger.yaml")
        report_path = self.path("report.yaml")
        with open(ledger, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "Future", "status": "candidate"},
                {
                    "name": "Active", "status": "active",
                    "company_domain": "active.example",
                },
            ]}, stream)
        with open(report_path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {
                    "name": "Future", "probe_status": "not_found",
                    "last_probed_at": "2026-01-01T00:00:00Z", "reason": "none",
                    "company_domain": "future.example",
                },
                {
                    "name": "Active", "probe_status": "verified_endpoint",
                    "last_probed_at": "2026-01-01T00:00:00Z",
                    "suggested_entry": {"ats": "ashby", "slug": "active"},
                },
            ]}, stream)
        self.assertEqual(probe.merge_probe_report(ledger, report_path), 2)
        rows = probe.load_candidates(ledger)
        self.assertEqual(rows[0]["status"], "probed")
        self.assertEqual(rows[0]["company_domain"], "future.example")
        self.assertEqual(rows[1]["status"], "active")
        self.assertEqual(rows[1]["company_domain"], "active.example")

    def test_report_merge_can_append_new_candidates_when_explicit(self):
        ledger = self.path("ledger.yaml")
        report_path = self.path("report.yaml")
        with open(ledger, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "Existing", "status": "candidate"},
            ]}, stream)
        with open(report_path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {
                    "name": "Existing", "status": "probed",
                    "probe_status": "not_found", "reason": "none",
                },
                {
                    "name": "New Company", "status": "probed",
                    "probe_status": "verified_endpoint", "live_postings": 2,
                    "suggested_entry": {"ats": "ashby", "slug": "newcompany"},
                },
            ]}, stream)
        self.assertEqual(
            probe.merge_probe_report(ledger, report_path, append_new=True), 2)
        rows = probe.load_candidates(ledger)
        self.assertEqual([row["name"] for row in rows], ["Existing", "New Company"])
        self.assertEqual(rows[1]["probe_status"], "verified_endpoint")

    def test_report_merge_does_not_append_new_candidates_by_default(self):
        ledger = self.path("ledger.yaml")
        report_path = self.path("report.yaml")
        with open(ledger, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "Existing", "status": "candidate"},
            ]}, stream)
        with open(report_path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "New Company", "status": "probed",
                 "probe_status": "not_found"},
            ]}, stream)
        self.assertEqual(probe.merge_probe_report(ledger, report_path), 0)
        self.assertEqual(
            [row["name"] for row in probe.load_candidates(ledger)], ["Existing"])

    def test_slug_only_batch_skips_derived_domain_fallback(self):
        path = self.path("candidates.yaml")
        with open(path, "w") as stream:
            yaml.safe_dump({"candidates": [{"name": "Example"}]}, stream)
        with mock.patch.object(
                probe, "probe_name_detailed", return_value=([], [])), \
                mock.patch.object(probe, "probe_derived_domains") as domains:
            report = probe.batch_probe(
                path, workers=1, use_derived_domains=False)
        self.assertEqual(
            report["candidates"][0]["probe_status"], "not_found")
        domains.assert_not_called()

    def test_known_unsupported_ats_is_fingerprinted_for_roadmap(self):
        candidate = {
            "name": "Example", "status": "candidate",
            "careers_url": "https://example.icims.com/jobs/search",
        }
        with mock.patch.object(
                probe, "probe_url", return_value=(None, "not supported")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "unsupported")
        self.assertEqual(result["unsupported_ats"], "icims")

    def test_smartrecruiters_url_is_recognized_as_supported(self):
        with mock.patch.object(probe, "check_smartrecruiters", return_value=12):
            entry, count = probe.probe_url(
                "https://careers.smartrecruiters.com/Example/india", name="Example")
        self.assertEqual(count, 12)
        self.assertEqual(entry["ats"], "smartrecruiters")
        self.assertEqual(entry["company_id"], "Example")

    def test_check_smartrecruiters_parses_a_200_response(self):
        # The URL test above mocks check_smartrecruiters, so it stayed green
        # while the real function had no success path at all and returned None
        # for every 200 — breaking `add_source.py --ats smartrecruiters` and
        # every SmartRecruiters URL probe. Exercise the real body.
        payload = {"totalFound": 12, "content": [{"id": "abc"}]}
        with mock.patch.object(probe.requests, "get",
                               return_value=_response(200, payload)):
            self.assertEqual(probe.check_smartrecruiters("Example"), 12)

    def test_check_smartrecruiters_rejects_malformed_and_non_200(self):
        with mock.patch.object(probe.requests, "get",
                               return_value=_response(404, {})):
            self.assertIsNone(probe.check_smartrecruiters("Example"))
        for bad in ({"content": []}, {"totalFound": 3}, {"totalFound": "x", "content": []}):
            with mock.patch.object(probe.requests, "get",
                                   return_value=_response(200, bad)):
                self.assertIsNone(probe.check_smartrecruiters("Example"))

    def test_check_workable_is_unaffected_by_the_shared_parse_block(self):
        payload = {"name": "Example", "jobs": [{"id": 1}, {"id": 2}]}
        with mock.patch.object(probe.requests, "get",
                               return_value=_response(200, payload)):
            self.assertEqual(probe.check_workable("example"), 2)

    def test_workable_url_is_recognized_as_supported(self):
        with mock.patch.object(probe, "check_workable", return_value=7):
            entry, count = probe.probe_url(
                "https://apply.workable.com/example/jobs", name="Example")
        self.assertEqual(count, 7)
        self.assertEqual(entry["ats"], "workable")
        self.assertEqual(entry["account"], "example")

    def test_workday_probe_uses_narrow_search_and_computed_page_budget(self):
        with mock.patch.object(probe, "check_workday", return_value=101) as check:
            entry, count = probe.probe_url(
                "https://example.wd1.myworkdayjobs.com/External", name="Example")
        self.assertEqual(count, 101)
        self.assertEqual(entry["search"], "internship")
        self.assertEqual(entry["max_pages"], 6)
        check.assert_called_once_with(
            "example", "wd1", "External", search="internship")

    def test_workday_probe_rejects_an_unreadable_result_set(self):
        with mock.patch.object(probe, "check_workday", return_value=241):
            entry, reason = probe.probe_url(
                "https://example.wd1.myworkdayjobs.com/External", name="Example")
        self.assertIsNone(entry)
        self.assertIn("12-request ceiling", reason)

    def test_company_domain_discovers_and_verifies_linked_ats(self):
        candidate = {
            "name": "Example", "status": "candidate",
            "company_domain": "example.test",
        }
        with mock.patch.object(
                probe, "discover_careers_urls",
                return_value=(["https://jobs.ashbyhq.com/example"], [])), \
                mock.patch.object(
                    probe, "probe_url",
                    return_value=({
                        "name": "Example", "ats": "ashby", "slug": "example",
                    }, 4)), \
                mock.patch.object(
                    probe, "check_entry_identity", return_value=(True, "Example Jobs")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertEqual(
            result["discovered_careers_url"],
            "https://jobs.ashbyhq.com/example")

    def test_company_domain_rejects_an_unrelated_supported_link(self):
        candidate = {
            "name": "ClickHouse", "status": "candidate",
            "company_domain": "clickhouse.com",
        }
        with mock.patch.object(
                probe, "discover_careers_urls",
                return_value=(["https://jobs.ashbyhq.com/langfuse"], [])), \
                mock.patch.object(
                    probe, "probe_url",
                    return_value=({
                        "name": "ClickHouse", "ats": "ashby", "slug": "langfuse",
                    }, 7)), \
                mock.patch.object(
                    probe, "check_entry_identity",
                    return_value=(False, "Langfuse Jobs")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "needs_review")
        self.assertNotIn("suggested_entry", result)
        self.assertIn("Langfuse", result["warnings"][0])

    def test_equivalent_discovered_urls_are_one_verified_endpoint(self):
        candidate = {
            "name": "Example", "status": "candidate",
            "company_domain": "example.test",
        }
        entry = {
            "name": "Example", "ats": "workday", "tenant": "example",
            "instance": "wd1", "site": "External",
        }
        with mock.patch.object(
                probe, "discover_careers_urls",
                return_value=([
                    "https://example.wd1.myworkdayjobs.com/External",
                    "https://example.wd1.myworkdayjobs.com/en-US/External",
                ], [])), \
                mock.patch.object(
                    probe, "probe_url", side_effect=[
                        (dict(entry), 10), (dict(entry), 10),
                    ]), \
                mock.patch.object(
                    probe, "check_entry_identity", return_value=(True, None)):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertNotIn("alternatives", result)

    def test_reprobe_removes_stale_evidence_fields(self):
        candidate = {
            "name": "Example", "status": "candidate",
            "reason": "old failure", "unsupported_ats": "icims",
            "alternatives": [{"careers_url": "https://old.example.test"}],
        }
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{"ats": "ashby", "slug": "example", "jobs": 2}], [])), \
                mock.patch.object(
                    probe, "check_board_identity",
                    return_value=(True, "Example Jobs")):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertNotIn("reason", result)
        self.assertNotIn("unsupported_ats", result)
        self.assertNotIn("alternatives", result)


class TestReviewedBatch(unittest.TestCase):
    def test_unapproved_candidate_never_builds_command(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed",
            "suggested_entry": {"name": "Example", "ats": "ashby", "slug": "example"},
        })
        self.assertIsNone(command)
        self.assertEqual(reason, "not approved")

    def test_approved_candidate_is_reverified_by_single_source_cli(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed", "approved": True,
            "suggested_entry": {"name": "Example", "ats": "ashby", "slug": "example"},
        })
        self.assertIsNone(reason)
        self.assertIn("--ats", command)
        self.assertIn("ashby", command)
        self.assertIn("--slug", command)
        self.assertIn("--comment", command)

    def test_batch_empty_board_override_is_explicitly_forwarded(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed", "approved": True,
            "suggested_entry": {"name": "Example", "ats": "ashby", "slug": "example"},
        }, allow_empty=True)
        self.assertIsNone(reason)
        self.assertIn("--allow-empty", command)

    def test_url_candidate_uses_url_probe_again(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed", "approved": True,
            "careers_url": "https://example.test/jobs",
            "suggested_entry": {"name": "Example", "ats": "eightfold"},
        }, no_seed=True)
        self.assertIsNone(reason)
        self.assertIn("--url", command)
        self.assertIn("--no-seed", command)

    def test_workday_batch_preserves_safe_search_and_page_budget(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed", "approved": True,
            "careers_url": "https://example.wd1.myworkdayjobs.com/External",
            "suggested_entry": {
                "name": "Example", "ats": "workday", "tenant": "example",
                "wd_host": "wd1", "site": "External",
                "search": "internship", "max_pages": 7,
            },
        })
        self.assertIsNone(reason)
        self.assertIn("--search", command)
        self.assertIn("internship", command)
        self.assertIn("--max-pages", command)
        self.assertIn("7", command)

    def test_unknown_invocation_approval_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "report.yaml")
            with open(path, "w") as stream:
                yaml.safe_dump({"candidates": [{
                    "name": "Known", "status": "probed",
                    "suggested_entry": {
                        "name": "Known", "ats": "ashby", "slug": "known",
                    },
                }]}, stream)
            with self.assertRaisesRegex(SystemExit, "not found"):
                add_source.run_batch(path, approved_names=["Unknown"])

    def test_reviewed_batch_skips_an_already_tracked_source(self):
        with tempfile.TemporaryDirectory() as directory:
            report = os.path.join(directory, "report.yaml")
            config = os.path.join(directory, "config.yaml")
            with open(report, "w") as stream:
                yaml.safe_dump({"candidates": [{
                    "name": "Tracked", "status": "probed",
                    "suggested_entry": {
                        "name": "Tracked", "ats": "ashby", "slug": "tracked",
                    },
                }]}, stream)
            with open(config, "w") as stream:
                yaml.safe_dump({"ats_companies": [{
                    "name": "Tracked", "ats": "ashby", "slug": "tracked",
                }]}, stream)
            with mock.patch.object(add_source, "CONFIG_FILE", config), \
                    mock.patch.object(add_source, "batch_command") as build:
                add_source.run_batch(report, approved_names=["Tracked"])
            build.assert_not_called()

    def test_sync_active_updates_only_configured_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = os.path.join(directory, "candidates.yaml")
            config = os.path.join(directory, "config.yaml")
            with open(ledger, "w") as stream:
                yaml.safe_dump({"candidates": [
                    {"name": "Tracked", "status": "probed"},
                    {"name": "Future", "status": "candidate"},
                ]}, stream)
            with open(config, "w") as stream:
                yaml.safe_dump({
                    "ats_companies": [{
                        "name": "Tracked", "ats": "ashby", "slug": "tracked",
                    }],
                    "custom_pages": [],
                }, stream)
            with mock.patch.object(add_source, "CONFIG_FILE", config):
                add_source.sync_active_candidates(ledger)
            rows = probe.load_candidates(ledger)
            self.assertEqual(rows[0]["status"], "active")
            self.assertEqual(rows[0]["active_source"]["ats"], "ashby")
            self.assertEqual(rows[1]["status"], "candidate")

    def test_config_insert_preserves_comments_and_adds_verification_note(self):
        original = "ats_companies:\n# existing\n- {name: Old, ats: amazon}\n\ncustom_pages: []\n"
        updated = add_source.insert_entry(
            original, {"name": "New", "ats": "ashby", "slug": "new"},
            comment="Verified safely.")
        self.assertIn("# existing", updated)
        self.assertIn("# Verified safely.", updated)
        self.assertLess(updated.index("# Verified safely."), updated.index("custom_pages:"))

    def test_seed_failure_rolls_back_config_and_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            state_path = os.path.join(directory, "state.json")
            original_config = (
                "keywords: [intern]\nlocations: [india]\n"
                "ats_companies: []\ncustom_pages: []\n")
            original_state = b'{"Existing": {"jobs": ["1"], "hash": ""}}\n'
            with open(config_path, "w") as stream:
                stream.write(original_config)
            with open(state_path, "wb") as stream:
                stream.write(original_state)
            successful_test = mock.Mock(returncode=0, stdout="✅ Example", stderr="")
            failed_seed = mock.Mock(returncode=1, stdout="❌ seed failed", stderr="")
            with mock.patch.object(add_source, "CONFIG_FILE", config_path), \
                    mock.patch.object(add_source, "STATE_FILE", state_path), \
                    mock.patch.object(
                        add_source, "resolve_entry",
                        return_value=({"name": "Example", "ats": "ashby", "slug": "example"}, 1)), \
                    mock.patch.object(
                        add_source, "run_hunter",
                        side_effect=[(True, successful_test), (False, failed_seed)]), \
                    mock.patch.object(
                        add_source.sys, "argv", ["add_source.py", "Example"]):
                with self.assertRaisesRegex(SystemExit, "rolled"):
                    add_source.main()
            with open(config_path) as stream:
                self.assertEqual(stream.read(), original_config)
            with open(state_path, "rb") as stream:
                self.assertEqual(stream.read(), original_state)


class TestCatalogReport(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.directory.name, "config.yaml")
        self.candidates = os.path.join(self.directory.name, "candidates.yaml")
        self.state = os.path.join(self.directory.name, "state.json")
        with open(self.config, "w") as stream:
            yaml.safe_dump({
                "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
                "telegram": {},
                "ats_companies": [
                    {"name": "Healthy", "ats": "greenhouse", "slug": "healthy"},
                    {"name": "Broken", "ats": "ashby", "slug": "broken"},
                ],
                "custom_pages": [],
            }, stream)
        with open(self.candidates, "w") as stream:
            yaml.safe_dump({"candidates": [
                {"name": "Healthy", "status": "active"},
                {"name": "Future", "status": "candidate"},
            ]}, stream)
        with open(self.state, "w") as stream:
            json.dump({
                "_pending": [{"message": "x", "attempts": 1}],
                "_failures": {"Broken": {"count": 3}},
                "_health": {
                    "Healthy": {
                        "last_run": "2099-01-01T00:00:00Z",
                        "last_success": "2099-01-01T00:00:00Z",
                        "duration_ms": 20, "request_count": 1,
                        "successful_zero_streak": 30,
                    },
                },
            }, stream)

    def tearDown(self):
        self.directory.cleanup()

    def test_report_combines_coverage_candidates_and_health(self):
        report = catalog_report.build_report(
            self.config, self.candidates, [self.state], zero_streak=24)
        self.assertEqual(report["coverage"]["active_sources"], 2)
        self.assertEqual(report["candidates"]["active_conversion_count"], 1)
        self.assertEqual(report["health"]["failing_sources"], ["Broken"])
        self.assertEqual(report["health"]["pending_alerts"], 1)
        self.assertEqual(report["health"]["suspicious_zero_sources"], ["Healthy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
