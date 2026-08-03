"""Offline reliability tests for state, delivery, CLI, and custom pages."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

import hybrid_hunter as hh
import testing_support

setUpModule = testing_support.block_network
tearDownModule = testing_support.restore_network


class TestStateAndDelivery(unittest.TestCase):
    def tearDown(self):
        for name in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, name))

    def test_corrupt_state_fails_closed(self):
        with open(hh.STATE_FILE, "w") as stream:
            stream.write("{broken")
        with self.assertRaisesRegex(RuntimeError, "refusing to discard"):
            hh.StateManager()

    def test_state_save_is_atomic_and_round_trips(self):
        state = hh.StateManager()
        state.mark_job("Example", "1")
        state.save()
        with open(hh.STATE_FILE) as stream:
            self.assertEqual(json.load(stream)["Example"]["jobs"], ["1"])
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(tmpdir)))

    def test_explicit_shard_state_is_isolated(self):
        first = hh.StateManager("state.shard-0-of-2.json")
        first.mark_job("Example", "1")
        first.save()
        second = hh.StateManager("state.shard-1-of-2.json")
        self.assertFalse(second.state)
        self.assertTrue(first.is_new_job("Example", "2"))

    def test_pending_alert_is_never_dropped(self):
        state = hh.StateManager()
        state.state = {"_pending": [{"message": "important", "attempts": 4}]}
        notifier = mock.Mock(send=mock.Mock(return_value=False))
        state.flush_pending(notifier)
        self.assertEqual(state.pending_count(), 1)
        self.assertEqual(state.state["_pending"][0]["attempts"], 5)

    def test_source_health_tracks_requests_latency_and_zero_streak(self):
        state = hh.StateManager()
        state.record_source_run("Example", "greenhouse", True, 0, 0.125, 1)
        state.record_source_run("Example", "greenhouse", True, 0, 0.250, 2)
        health = state.state["_health"]["Example"]
        self.assertEqual(health["successful_zero_streak"], 2)
        self.assertEqual(health["duration_ms"], 250)
        self.assertEqual(health["request_count"], 2)
        state.record_source_run("Example", "greenhouse", False, None, 0.5, 1, "down")
        health = state.state["_health"]["Example"]
        self.assertFalse(health["success"])
        self.assertEqual(health["successful_zero_streak"], 2)
        self.assertEqual(health["reason"], "down")

    def test_missing_credentials_are_not_delivery(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            notifier = hh.Notifier({"telegram": {
                "token": "${TELEGRAM_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}",
            }})
            self.assertFalse(notifier.enabled)
            self.assertFalse(notifier.send("test"))

    def test_http_200_without_telegram_ok_is_not_delivery(self):
        notifier = hh.Notifier({"telegram": {"token": "token", "chat_id": "123"}})
        response = mock.Mock(ok=True)
        response.json.return_value = {"ok": False}
        with mock.patch.object(hh.requests, "post", return_value=response):
            self.assertFalse(notifier.send("test"))

    def test_digest_chunks_stay_within_telegram_limit(self):
        jobs = [{
            "company": "Example", "id": "1", "title": "T" * 10000,
            "location": "India", "url": "https://example.test/job/1",
        }]
        chunks = hh.build_digest(jobs, [])
        self.assertTrue(chunks)
        self.assertTrue(all(0 < len(chunk) <= hh.TELEGRAM_MAX_LEN - 100 for chunk in chunks))


class TestCustomPageReliability(unittest.TestCase):
    def setUp(self):
        self.hunter = hh.CustomWebHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
        })

    def test_missing_configured_location_selector_raises(self):
        config = {
            "url": "https://example.test/jobs", "job_selector": ".job",
            "title_selector": ".title", "job_location_selector": ".location",
        }
        content = '<body><div class="job"><a href="/1"><span class="title">Intern</span></a></div></body>'
        with self.assertRaisesRegex(ValueError, "location selector"):
            self.hunter.parse_structured_jobs(content, config)

    def test_http_error_page_is_failure(self):
        manager = mock.MagicMock()
        playwright = mock.MagicMock()
        manager.start.return_value = playwright
        browser = playwright.chromium.launch.return_value
        page = browser.new_page.return_value
        page.goto.return_value = mock.Mock(ok=False, status=500)
        with mock.patch.object(hh, "sync_playwright", return_value=manager):
            result = self.hunter.hunt({
                "name": "Broken", "url": "https://example.test/error",
                "keyword_filter": False, "location_filter": False,
            })
            self.hunter.close()
        self.assertIn("HTTP 500", result["failed"])

    def test_browser_is_reused_and_each_page_is_closed(self):
        manager = mock.MagicMock()
        playwright = mock.MagicMock()
        manager.start.return_value = playwright
        browser = playwright.chromium.launch.return_value
        first_page, second_page = mock.MagicMock(), mock.MagicMock()
        browser.new_page.side_effect = [first_page, second_page]
        for page in (first_page, second_page):
            page.goto.return_value = mock.Mock(ok=True, status=200)
            page.content.return_value = "<body>" + ("internships in India " * 20) + "</body>"
        config = {
            "name": "Page", "url": "https://example.test",
            "keyword_filter": False, "location_filter": False, "render_delay": 0,
        }
        with mock.patch.object(hh, "sync_playwright", return_value=manager):
            self.assertNotIn("failed", self.hunter.hunt(config))
            self.assertNotIn("failed", self.hunter.hunt(config))
            self.hunter.close()
        self.assertEqual(playwright.chromium.launch.call_count, 1)
        self.assertEqual(browser.new_page.call_count, 2)
        first_page.close.assert_called_once()
        second_page.close.assert_called_once()
        browser.close.assert_called_once()
        playwright.stop.assert_called_once()


class TestCLIAndValidation(unittest.TestCase):
    def tearDown(self):
        for name in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, name))

    @staticmethod
    def write_config():
        with open("config.yaml", "w") as stream:
            stream.write(
                "keywords: [intern]\nlocations: [india]\n"
                "ats_companies:\n- {name: Broken, ats: amazon}\ncustom_pages: []\n")

    def test_failed_selected_source_exits_nonzero(self):
        self.write_config()
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {}
        with mock.patch.object(hh.sys, "argv", [
                "hybrid_hunter.py", "--test", "--ats-only", "--company", "Broken"]), \
                mock.patch.object(hh.requests, "get", return_value=response):
            with self.assertRaisesRegex(SystemExit, "1"):
                hh.main()

    def test_live_run_requires_telegram_credentials(self):
        self.write_config()
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": ""}), \
                mock.patch.object(hh.sys, "argv", ["hybrid_hunter.py", "--ats-only"]):
            with self.assertRaisesRegex(SystemExit, "required"):
                hh.main()

    def test_live_source_failure_is_persisted_before_nonzero_exit(self):
        self.write_config()
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {}
        with mock.patch.dict(os.environ, {
                "TELEGRAM_TOKEN": "token", "TELEGRAM_CHAT_ID": "123"}), \
                mock.patch.object(hh.sys, "argv", ["hybrid_hunter.py", "--ats-only"]), \
                mock.patch.object(hh.requests, "get", return_value=response):
            with self.assertRaisesRegex(SystemExit, "1"):
                hh.main()
        with open(hh.STATE_FILE) as stream:
            state = json.load(stream)
        self.assertEqual(state["_failures"]["Broken"]["count"], 1)

    def test_validation_rejects_excess_page_budget_and_bad_regex(self):
        config = {
            "keywords": ["intern"], "locations": ["india"],
            "ats_companies": [{
                "name": "Oracle", "ats": "oracle_hcm", "host": "example.test",
                "site_number": "Site", "max_pages": 20,
            }],
            "custom_pages": [{
                "name": "Page", "url": "https://example.test", "job_selector": ".job",
                "id_regex": "no-capture-group",
            }],
        }
        problems = hh.validate_config(config)
        self.assertTrue(any("max_pages" in item for item in problems))
        self.assertTrue(any("capture group" in item for item in problems))

    def test_validation_rejects_unsafe_custom_page_timeouts(self):
        config = {
            "keywords": ["intern"], "locations": ["india"], "ats_companies": [],
            "custom_pages": [{
                "name": "Page", "url": "https://example.test",
                "navigation_timeout": 0, "selector_timeout": 31, "render_delay": 20,
            }],
        }
        problems = hh.validate_config(config)
        self.assertEqual(
            sum("must be a number" in item for item in problems), 3)

    def test_shard_settings_and_related_source_assignment(self):
        index, count, path = hh.shard_settings_from_argv([
            "--shard-index=2", "--shard-count", "4",
        ])
        self.assertEqual((index, count, path), (2, 4, "state.shard-2-of-4.json"))
        parent = {"name": "Example"}
        supplement = {"name": "Example Program", "supplement_for": "Example"}
        self.assertEqual(hh.source_shard(parent, 8), hh.source_shard(supplement, 8))

    def test_invalid_shard_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "between"):
            hh.shard_settings_from_argv(["--shard-count", "2", "--shard-index", "2"])
        with self.assertRaisesRegex(ValueError, "filename"):
            hh.shard_settings_from_argv(["--state-file", "nested/state.json"])

    def test_pruning_drops_only_sources_missing_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                json.dump({
                    "Kept": {"jobs": ["1"], "hash": ""},
                    "Gone": {"jobs": ["2"], "hash": ""},
                    "_failures": {"Kept": {"count": 1, "alerted": False},
                                  "Gone": {"count": 3, "alerted": True}},
                    "_health": {"Kept": {"success": True}, "Gone": {"success": False}},
                }, fh)
            sm = hh.StateManager(path)
            self.assertEqual(sm.prune_removed_sources(["Kept"]), ["Gone"])
            self.assertEqual(list(sm.state["_failures"]), ["Kept"])
            self.assertEqual(list(sm.state["_health"]), ["Kept"])
            # Job ids survive: a source removed and re-added must not re-alert
            # its entire board.
            self.assertEqual(sm.state["Gone"]["jobs"], ["2"])

    def test_pruning_is_case_insensitive_and_drops_empty_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as fh:
                json.dump({"_failures": {"Dream Sports": {"count": 3}}}, fh)
            sm = hh.StateManager(path)
            self.assertEqual(sm.prune_removed_sources(["dream sports"]), [])
            self.assertEqual(sm.prune_removed_sources(["Something Else"]),
                             ["Dream Sports"])
            self.assertNotIn("_failures", sm.state)

    def test_every_supported_flag_is_accepted(self):
        self.assertEqual(hh.unknown_flags_from_argv([
            "--test", "--ats-only", "--company", "Adobe", "--company=Groww",
            "--shard-count", "2", "--shard-index=1", "--state-file", "s.json",
        ]), [])

    def test_typo_flags_are_rejected_rather_than_ignored(self):
        # A dropped "--test" would turn an intended dry run into a live
        # alerting run, so an unrecognised flag has to abort the run.
        self.assertEqual(hh.unknown_flags_from_argv(["--ats_only"]), ["--ats_only"])
        self.assertEqual(hh.unknown_flags_from_argv(["--tst"]), ["--tst"])
        self.assertEqual(hh.unknown_flags_from_argv(["--seed", "extra"]), ["extra"])

    def test_option_values_are_not_scanned_as_flags(self):
        # The value slot is skipped, so a source name is never reported as an
        # unknown flag. (--company itself still rejects a "--"-leading value.)
        self.assertEqual(hh.unknown_flags_from_argv(["--company", "Dream Sports"]), [])
        self.assertEqual(hh.unknown_flags_from_argv(["--state-file", "state.json"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
