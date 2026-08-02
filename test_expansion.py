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

        def fake_probe(candidate):
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
                return_value=([], ["greenhouse/example: connection failed"])):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "failed")
        self.assertIn("unreliable", result["reason"])

    def test_lossy_single_word_slug_requires_identity_review(self):
        candidate = {"name": "Pine Labs", "status": "candidate"}
        with mock.patch.object(
                probe, "probe_name_detailed",
                return_value=([{"ats": "greenhouse", "slug": "pine", "jobs": 10}], [])):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "needs_review")
        self.assertIn("lossy slug", result["warnings"][0])

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
                {"name": "Active", "status": "active"},
            ]}, stream)
        with open(report_path, "w") as stream:
            yaml.safe_dump({"candidates": [
                {
                    "name": "Future", "probe_status": "not_found",
                    "last_probed_at": "2026-01-01T00:00:00Z", "reason": "none",
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
        self.assertEqual(rows[1]["status"], "active")

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

    def test_workable_url_is_recognized_as_supported(self):
        with mock.patch.object(probe, "check_workable", return_value=7):
            entry, count = probe.probe_url(
                "https://apply.workable.com/example/jobs", name="Example")
        self.assertEqual(count, 7)
        self.assertEqual(entry["ats"], "workable")
        self.assertEqual(entry["account"], "example")

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
                    }, 4)):
            result = probe.probe_candidate(candidate)
        self.assertEqual(result["probe_status"], "verified_endpoint")
        self.assertEqual(
            result["discovered_careers_url"],
            "https://jobs.ashbyhq.com/example")

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
                    ]):
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
                return_value=([{"ats": "ashby", "slug": "example", "jobs": 2}], [])):
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

    def test_url_candidate_uses_url_probe_again(self):
        command, reason = add_source.batch_command({
            "name": "Example", "status": "probed", "approved": True,
            "careers_url": "https://example.test/jobs",
            "suggested_entry": {"name": "Example", "ats": "eightfold"},
        }, no_seed=True)
        self.assertIsNone(reason)
        self.assertIn("--url", command)
        self.assertIn("--no-seed", command)

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
