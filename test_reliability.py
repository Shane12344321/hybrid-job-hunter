"""Offline reliability tests for state, delivery, CLI, and custom pages."""
import json
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

import hybrid_hunter as hh
import testing_support

setUpModule = testing_support.block_network
_sleep_patcher = mock.patch.object(hh, "sleep")
_sleep_patcher.start()


def tearDownModule():
    _sleep_patcher.stop()
    testing_support.restore_network()


class TestHTTPRetry(unittest.TestCase):
    @staticmethod
    def response(status, retry_after=None):
        response = mock.Mock(status_code=status)
        response.headers = {}
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        response.raise_for_status.side_effect = (
            hh.requests.HTTPError(f"HTTP {status}") if status >= 400 else None)
        return response

    def setUp(self):
        self.hunter = hh.ATSHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
        })

    def test_retries_transient_http_error_then_succeeds(self):
        with mock.patch.object(hh.requests, "get", side_effect=[
                self.response(503), self.response(200)]), \
                mock.patch.object(hh, "sleep") as pause:
            result = self.hunter._get("https://example.test")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.hunter.request_count, 2)
        pause.assert_called_once_with(1)

    def test_gives_up_after_three_attempts(self):
        with mock.patch.object(hh.requests, "get", side_effect=[
                self.response(503), self.response(503), self.response(503)]), \
                mock.patch.object(hh, "sleep") as pause:
            with self.assertRaises(hh.requests.HTTPError):
                self.hunter._get("https://example.test")
        self.assertEqual(self.hunter.request_count, 3)
        self.assertEqual([call.args[0] for call in pause.call_args_list], [1, 2])

    def test_does_not_retry_non_transient_http_error(self):
        response = self.response(404)
        with mock.patch.object(hh.requests, "get", return_value=response), \
                mock.patch.object(hh, "sleep") as pause:
            result = self.hunter._get("https://example.test")
        self.assertIs(result, response)
        self.assertEqual(self.hunter.request_count, 1)
        pause.assert_not_called()

    def test_honors_numeric_retry_after_with_cap(self):
        with mock.patch.object(hh.requests, "get", side_effect=[
                self.response(429, "27"), self.response(200)]), \
                mock.patch.object(hh, "sleep") as pause:
            self.hunter._get("https://example.test")
        pause.assert_called_once_with(20.0)

    def test_connection_retries_count_against_request_budget(self):
        with mock.patch.object(
                hh.requests, "get",
                side_effect=[hh.requests.ConnectionError("reset"), self.response(200)]), \
                mock.patch.object(hh, "sleep"):
            self.hunter._get("https://example.test")
        self.assertEqual(self.hunter.request_count, 2)


class TestATSConcurrency(unittest.TestCase):
    def test_same_vendor_is_limited_to_two_concurrent_sources(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def fake_hunt(hunter, slug, keywords=None):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return [{"id": slug, "title": "Intern", "location": "India",
                     "url": f"https://example.test/{slug}"}]

        config = {"keywords": ["intern"], "locations": ["india"]}
        comps = [{"name": f"Company {index}", "ats": "greenhouse",
                  "slug": f"company-{index}"} for index in range(8)]
        semaphores = {
            hh.ats_request_group(comp): threading.Semaphore(2) for comp in comps
        }
        with mock.patch.object(hh.ATSHunter, "hunt_greenhouse", fake_hunt):
            with hh.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(
                    lambda comp: hh._hunt_ats_entry(comp, config, semaphores), comps))
        self.assertEqual(maximum, 2)
        self.assertTrue(all(result["error"] is None for result in results))
        self.assertEqual([result["matches"][0]["id"] for result in results],
                         [comp["slug"] for comp in comps])

    def test_worker_failures_are_isolated_and_results_are_reorderable(self):
        config = {"keywords": ["intern"], "locations": ["india"]}
        comps = [{"name": "First", "ats": "ashby", "slug": "first"},
                 {"name": "Broken", "ats": "ashby", "slug": "broken"},
                 {"name": "Last", "ats": "ashby", "slug": "last"}]
        semaphores = {"ashby": threading.Semaphore(2)}

        def fake_hunt(hunter, slug, keywords=None):
            if slug == "broken":
                raise RuntimeError("source down")
            return [{"id": slug, "title": "Intern", "location": "India",
                     "url": f"https://example.test/{slug}"}]

        with mock.patch.object(hh.ATSHunter, "hunt_ashby", fake_hunt):
            with hh.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(hh._hunt_ats_entry, comp, config, semaphores)
                           for comp in comps]
                results = [future.result() for future in futures]
        self.assertEqual([result["comp"]["name"] for result in results],
                         ["First", "Broken", "Last"])
        self.assertIsNotNone(results[1]["error"])
        self.assertEqual(results[0]["matches"][0]["id"], "first")
        self.assertEqual(results[2]["matches"][0]["id"], "last")


class TestStateAndDelivery(unittest.TestCase):
    def tearDown(self):
        for name in os.listdir(tmpdir):
            os.unlink(os.path.join(tmpdir, name))

    def test_corrupt_state_fails_closed(self):
        with open(hh.STATE_FILE, "w") as stream:
            stream.write("{broken")
        with self.assertRaisesRegex(RuntimeError, "refusing to discard"):
            hh.StateManager()

    def test_valid_json_with_non_list_jobs_fails_closed(self):
        with open(hh.STATE_FILE, "w") as stream:
            json.dump({"Example": {"jobs": "123", "hash": ""}}, stream)
        with self.assertRaisesRegex(RuntimeError, "Example.*jobs|jobs.*Example"):
            hh.StateManager()

    def test_malformed_reserved_state_buckets_fail_closed(self):
        malformed = {
            "_pending": {},
            "_failures": [],
            "_health": [],
            "_stats": [],
        }
        for bucket, value in malformed.items():
            with self.subTest(bucket=bucket):
                with open(hh.STATE_FILE, "w") as stream:
                    json.dump({bucket: value}, stream)
                with self.assertRaisesRegex(RuntimeError, bucket):
                    hh.StateManager()
                os.unlink(hh.STATE_FILE)

    def test_state_save_is_atomic_and_round_trips(self):
        state = hh.StateManager()
        state.mark_job("Example", "1")
        state.save()
        with open(hh.STATE_FILE) as stream:
            self.assertEqual(json.load(stream)["Example"]["jobs"], ["1"])
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(tmpdir)))

    def test_save_if_dirty_checkpoints_health_without_seen_jobs(self):
        path = os.path.join(tmpdir, "incremental-state.json")
        state = hh.StateManager(path)
        state.record_source_run("Completed", "greenhouse", True, 0, 0.1, 1)
        state.save_if_dirty()
        with open(path) as stream:
            persisted = json.load(stream)
        self.assertIn("Completed", persisted["_health"])
        self.assertNotIn("Completed", persisted)
        self.assertFalse(state.dirty)

    def test_save_if_dirty_skips_clean_state(self):
        state = hh.StateManager(os.path.join(tmpdir, "clean-state.json"))
        with mock.patch.object(state, "save") as save:
            state.save_if_dirty()
        save.assert_not_called()

    def test_prune_expired_jobs_keeps_current_and_recent_ids(self):
        path = os.path.join(tmpdir, "prune-state.json")
        now = int(time.time())
        with open(path, "w") as stream:
            json.dump({"Example": {
                "jobs": ["old", "recent", "listed", "legacy"], "hash": "",
                "seen": {"old": now - 61 * 86400, "recent": now - 2 * 86400,
                         "listed": now - 61 * 86400},
            }}, stream)
        state = hh.StateManager(path)
        removed = state.prune_expired_jobs(
            {"Example": {"recent", "listed", "legacy"}}, days=60)
        self.assertEqual(removed, [("Example", "old")])
        self.assertEqual(state.state["Example"]["jobs"],
                         ["recent", "listed", "legacy"])
        self.assertGreaterEqual(state.state["Example"]["seen"]["legacy"], now)

    def test_fuzzy_repost_is_tagged_without_suppressing_new_id(self):
        state = hh.StateManager()
        now = int(time.time())
        state.mark_job("Example", "old", "Software Intern!", "Bengaluru, India")
        self.assertTrue(state.is_repost(
            "Example", "software intern", "Bengaluru, India", now=now))
        self.assertFalse(state.is_repost(
            "Example", "software intern", "Pune, India", now=now))
        digest = hh.build_digest([{
            "company": "Example", "id": "new", "title": "Software Intern",
            "location": "Bengaluru, India", "url": "https://example.test/new",
            "repost": True,
        }], [])
        self.assertTrue(any("↻" in chunk for chunk in digest))

    def test_explicit_shard_state_is_isolated(self):
        first = hh.StateManager("state.shard-0-of-2.json")
        first.mark_job("Example", "1")
        first.save()
        second = hh.StateManager("state.shard-1-of-2.json")
        self.assertFalse(second.state)
        self.assertTrue(first.is_new_job("Example", "2"))

    def test_case_only_source_rename_reuses_state_without_replay(self):
        with open(hh.STATE_FILE, "w") as stream:
            json.dump({"Microsoft": {"jobs": ["1"], "hash": "old"}}, stream)

        state = hh.StateManager()
        self.assertFalse(state.is_new_job("MICROSOFT", "1"))
        state.mark_job("MICROSOFT", "2")

        matching_keys = [key for key in state.state if key.casefold() == "microsoft"]
        self.assertEqual(len(matching_keys), 1)
        self.assertEqual(state.state[matching_keys[0]]["jobs"], ["1", "2"])

    def test_ambiguous_casefold_source_keys_fail_closed(self):
        with open(hh.STATE_FILE, "w") as stream:
            json.dump({
                "Microsoft": {"jobs": ["1"], "hash": ""},
                "MICROSOFT": {"jobs": ["2"], "hash": ""},
            }, stream)
        with self.assertRaisesRegex(RuntimeError, "case|collision|ambiguous"):
            hh.StateManager()

    def test_pending_alert_is_never_dropped(self):
        state = hh.StateManager()
        state.state = {"_pending": [{"message": "important", "attempts": 4}]}
        notifier = mock.Mock(send=mock.Mock(return_value=False))
        state.flush_pending(notifier)
        self.assertEqual(state.pending_count(), 1)
        self.assertEqual(state.state["_pending"][0]["attempts"], 5)

    def test_successful_pending_flush_is_checkpointed_before_later_failure(self):
        state = hh.StateManager()
        state.queue_alert("important")
        state.save()
        with open("config.yaml", "w") as stream:
            json.dump({
                "keywords": ["intern"], "locations": ["india"],
                "telegram": {
                    "token": "${TELEGRAM_TOKEN}",
                    "chat_id": "${TELEGRAM_CHAT_ID}",
                },
                "ats_companies": [], "custom_pages": [],
            }, stream)

        with mock.patch.dict(os.environ, {
                "TELEGRAM_TOKEN": "token", "TELEGRAM_CHAT_ID": "123"}), \
                mock.patch.object(hh.sys, "argv", ["hybrid_hunter.py", "--ats-only"]), \
                mock.patch.object(hh.Notifier, "send", return_value=True) as send, \
                mock.patch.object(
                    hh.StateManager, "prune_removed_sources",
                    side_effect=RuntimeError("later crawl failure")):
            with self.assertRaisesRegex(RuntimeError, "later crawl failure"):
                hh.main()

        send.assert_called_once_with("important")
        self.assertEqual(hh.StateManager().pending_count(), 0)

    def test_source_health_tracks_requests_latency_and_zero_streak(self):
        state = hh.StateManager()
        state.record_source_run("Example", "greenhouse", True, 5, 0.125, 1, raw_count=30)
        state.record_source_run("Example", "greenhouse", True, 0, 0.250, 2, raw_count=5)
        health = state.state["_health"]["Example"]
        self.assertEqual(health["successful_zero_streak"], 1)
        self.assertEqual(health["duration_ms"], 250)
        self.assertEqual(health["request_count"], 2)
        self.assertIn("suspect", health)
        state.record_source_run("Example", "greenhouse", True, 1, 0.1, 1, raw_count=5)
        self.assertIn("raw postings", state.state["_health"]["Example"]["suspect"])
        state.record_source_run("Example", "greenhouse", True, 1, 0.1, 1, raw_count=30)
        self.assertNotIn("suspect", state.state["_health"]["Example"])
        state.record_source_run("Example", "greenhouse", False, None, 0.5, 1, "down")
        health = state.state["_health"]["Example"]
        self.assertFalse(health["success"])
        self.assertEqual(health["successful_zero_streak"], 0)
        self.assertEqual(health["reason"], "down")

    def test_raw_posting_collapse_is_suspect_but_not_failure(self):
        state = hh.StateManager()
        state.record_source_run("Example", "greenhouse", True, 1, 0.1, 1, raw_count=25)
        state.record_source_run("Example", "greenhouse", True, 1, 0.1, 1, raw_count=4)
        health = state.state["_health"]["Example"]
        self.assertIn("suspect", health)
        self.assertNotIn("_failures", state.state)

    def test_alerted_source_backoff_expires_after_six_hours(self):
        state = hh.StateManager()
        state.state["_failures"] = {
            "Example": {"count": 3, "alerted": True,
                         "last_failure": 1_000_000},
        }
        self.assertTrue(state.source_in_backoff("Example", now=1_000_001))
        self.assertFalse(state.source_in_backoff(
            "Example", now=1_000_000 + hh.FAILING_SOURCE_RETRY_SECONDS))
        self.assertFalse(state.source_in_backoff("Unknown", now=1_000_001))

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

    def test_digest_chunks_use_telegram_utf16_limit_and_keep_normal_html(self):
        # Telegram counts UTF-16 code units, so code-point length alone is not
        # sufficient for emoji-heavy heartbeat warnings.
        warning = "<b>" + ("🚀" * 5000) + "</b>"
        chunks = hh.build_digest([], [], warnings=[warning])
        self.assertTrue(chunks)
        self.assertTrue(all(
            0 < hh._telegram_length(chunk) <= hh.TELEGRAM_MAX_LEN - 100
            for chunk in chunks))
        self.assertIn("🚀", "".join(chunks))
        self.assertNotIn("<b>", "".join(chunks))

        entity_warning = "<b>" + ("&amp;🚀" * 1300) + "</b>"
        entity_chunks = hh.build_digest([], [], warnings=[entity_warning])
        self.assertEqual(
            "".join(hh.html.unescape(chunk) for chunk in entity_chunks),
            "&🚀" * 1300)
        for chunk in entity_chunks:
            self.assertNotRegex(chunk, r"&(?!amp;|lt;|gt;|quot;|#x27;)")

        jobs = [{
            "company": "Example", "id": "1", "title": "<R&D intern>",
            "location": "India", "url": "https://example.test/job/1",
        }]
        digest = "\n".join(hh.build_digest(jobs, []))
        self.assertIn("<a href='https://example.test/job/1'>", digest)
        self.assertIn("&lt;R&amp;D intern&gt;", digest)


class TestRuntimeRegressions(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = hh.StateManager(os.path.join(self.directory.name, "state.json"))

    def tearDown(self):
        self.directory.cleanup()

    def test_pruning_distinguishes_unobserved_from_empty(self):
        for name in ("Skipped", "Empty", "Listed"):
            self.state.mark_job(name, "old")
            self.state.state[name]["seen"]["old"] = time.time() - 61 * 86400
        self.assertEqual(self.state.prune_expired_jobs({}), [])
        removed = self.state.prune_expired_jobs({"empty": set(), "Listed": {"old"}})
        self.assertEqual(removed, [("Empty", "old")])
        self.assertFalse(self.state.is_new_job("Skipped", "old"))
        self.assertFalse(self.state.is_new_job("Listed", "old"))

    def test_legacy_timestamp_is_checkpointed_without_other_changes(self):
        self.state.mark_job("Legacy", "id")
        self.state.state["Legacy"].pop("seen")
        self.state.save()
        self.state.prune_expired_jobs({"Legacy": {"id"}})
        self.state.save_if_dirty()
        self.assertIn("id", hh.StateManager(self.state.path).state["Legacy"]["seen"])

    def test_collapses_persist_through_repeats_failure_and_reload(self):
        for kind, initial, degraded, recovered in (
                ("raw", (1, 100), (1, 10), (1, 20)),
                ("matches", (10, 100), (0, 100), (1, 100))):
            with self.subTest(kind=kind):
                def record(counts, success=True):
                    self.state.record_source_run(
                        kind, "ashby", success, counts[0], .1, 1,
                        raw_count=counts[1], reason="offline" if not success else None)
                record(initial)
                record(degraded)
                record(degraded)
                record((None, None), False)
                self.state.save()
                self.state = hh.StateManager(self.state.path)
                record(degraded)
                health = self.state.state["_health"][kind]
                self.assertEqual(health["suspect_kind"], kind)
                self.assertEqual(health["healthy_raw_baseline"], 100)
                record(recovered)
                self.assertNotIn("suspect", self.state.state["_health"][kind])

    def heartbeat(self, config):
        notifier = mock.Mock()
        notifier.send.return_value = True
        before = json.dumps(self.state.state, sort_keys=True)
        hh.send_heartbeat(config, self.state, notifier)
        self.assertEqual(json.dumps(self.state.state, sort_keys=True), before)
        return [call.args[0] for call in notifier.send.call_args_list]

    def test_heartbeat_requires_fresh_success_for_every_configured_source(self):
        config = {"ats_companies": [{"name": "ATS", "ats": "ashby"}],
                  "custom_pages": [{"name": "Page"}]}
        message = "\n".join(self.heartbeat(config))
        self.assertIn("no successful check", message)
        self.assertNotIn("All sources healthy", message)
        for name, family in (("ATS", "ashby"), ("Page", "custom_page")):
            self.state.record_source_run(name, family, True, 1, 0, 1)
        self.assertIn("All sources healthy", "\n".join(self.heartbeat(config)))
        now = time.time()
        def stamp(hours):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now-hours*3600))
        self.state.state["_health"]["ATS"]["last_success"] = stamp(7)
        self.state.state["_health"]["Page"]["last_success"] = stamp(11)
        message = "\n".join(self.heartbeat(config))
        self.assertIn("ATS: stale", message)
        self.assertNotIn("Page: stale", message)
        self.state.state["_health"]["Page"]["last_success"] = stamp(13)
        self.assertIn("Page: stale", "\n".join(self.heartbeat(config)))

    def test_heartbeat_chunks_large_catalog_and_reports_delivery_failure(self):
        config = {"ats_companies": [{"name": f"Company {i}", "ats": "ashby"}
                                    for i in range(300)]}
        chunks = self.heartbeat(config)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= hh.TELEGRAM_MAX_LEN - 100 for chunk in chunks))
        self.assertIn("Company 299", "\n".join(chunks))
        notifier = mock.Mock()
        notifier.send.side_effect = [True, False]
        with self.assertRaisesRegex(RuntimeError, "could not be delivered"):
            hh.send_heartbeat(config, self.state, notifier)

    def test_main_checkpoints_before_final_worker_finishes_and_orders_digest(self):
        # Source 0 cannot finish until 25 later sources have been checkpointed.
        # This fails deterministically if main waits for all futures first.
        checkpointed = threading.Event()
        config = {"keywords": ["intern"], "locations": ["india"],
                  "ats_companies": [{"name": f"Source {i:02}", "ats": "ashby", "slug": f"s{i}"}
                                    for i in range(26)], "custom_pages": []}
        old_cwd = os.getcwd()
        os.chdir(self.directory.name)
        with open("config.yaml", "w") as stream:
            json.dump(config, stream)
        snapshots = []
        original_save = hh.StateManager.save
        original_record = hh.StateManager.record_source_run
        recorded = []
        def record(state, source, *args, **kwargs):
            recorded.append(source)
            return original_record(state, source, *args, **kwargs)
        def save(state):
            original_save(state)
            if len(state.state.get("_health", {})) == 25:
                snapshots.append(json.loads(json.dumps(state.state)))
                checkpointed.set()
        def hunt(comp, config, semaphores):
            if comp["name"] == "Source 00" and not checkpointed.wait(5):
                raise AssertionError("No checkpoint while last source was pending")
            return {"comp": comp, "error": None,
                    "matches": [{"id": comp["slug"], "title": "Intern", "location": "India",
                                 "url": "https://example.test/job"}],
                    "raw_count": 1, "raw_ids": {comp["slug"]},
                    "duration": .1, "request_count": 1}
        notifier = mock.Mock(enabled=True)
        notifier.send.return_value = True
        try:
            with mock.patch.object(hh, "_hunt_ats_entry", side_effect=hunt), \
                    mock.patch.object(hh.StateManager, "save", save), \
                    mock.patch.object(hh.StateManager, "record_source_run", record), \
                    mock.patch.object(hh, "Notifier", return_value=notifier), \
                    mock.patch.object(hh, "write_step_summary") as summary, \
                    mock.patch.object(sys, "argv", ["hunter", "--ats-only"]), \
                    mock.patch.object(sys, "stdout", io.StringIO()):
                hh.main()
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(sorted(recorded), [comp["name"] for comp in config["ats_companies"]])
            self.assertFalse(any(not key.startswith("_") for key in snapshots[0]))
            jobs = summary.call_args.args[1]
            self.assertEqual([job["company"] for job in jobs],
                             [comp["name"] for comp in config["ats_companies"]])
            state = hh.StateManager()
            self.assertEqual(len(state.state["_health"]), 26)
            self.assertTrue(all(not state.is_new_job(job["company"], job["id"]) for job in jobs))
        finally:
            checkpointed.set()
            os.chdir(old_cwd)


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

    @staticmethod
    def write_empty_config():
        with open("config.yaml", "w") as stream:
            json.dump({
                "keywords": ["intern"], "locations": ["india"],
                "ats_companies": [], "custom_pages": [],
            }, stream)

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

    def test_validation_rejects_whitespace_filters_and_zero_markers(self):
        config = {
            "keywords": [" "], "exclude_keywords": ["\t"],
            "locations": ["\n"], "exclude_locations": [""],
            "ats_companies": [{
                "name": "Example", "ats": "ashby", "slug": "example",
                "keywords": ["   "],
            }],
            "custom_pages": [{
                "name": "Page", "url": "https://example.test",
                "zero_result_text": ["  "],
            }],
        }
        problems = hh.validate_config(config)
        for field in ("keywords", "exclude_keywords", "locations",
                      "exclude_locations", "zero_result_text"):
            with self.subTest(field=field):
                self.assertTrue(any(field in problem for problem in problems), problems)

    def test_validation_rejects_duplicate_board_targets(self):
        config = {
            "keywords": ["intern"], "locations": ["india"],
            "ats_companies": [
                {"name": "Perplexity", "ats": "ashby", "slug": "perplexity"},
                {"name": "Perplexity AI", "ats": "ashby", "slug": "perplexity"},
                {"name": "Tower Research", "ats": "greenhouse", "slug": "towerresearchcapital"},
            ],
        }
        problems = hh.validate_config(config)
        self.assertTrue(any("duplicate board target" in item for item in problems), problems)

    def test_validation_allows_distinct_scopes_of_same_tenant(self):
        config = {
            "keywords": ["intern"], "locations": ["india"],
            "ats_companies": [
                # Same Workday tenant shape, different sites:
                {"name": "A", "ats": "workday", "tenant": "acme", "wd_host": "wd5", "site": "One"},
                {"name": "B", "ats": "workday", "tenant": "acme", "wd_host": "wd5", "site": "Two"},
                # Same SmartRecruiters company_id scoped to different countries:
                {"name": "C", "ats": "smartrecruiters", "company_id": "Acme", "country": "in"},
                {"name": "D", "ats": "smartrecruiters", "company_id": "Acme", "country": "us"},
            ],
        }
        self.assertEqual(hh.validate_config(config), [])

    def test_shard_settings_and_related_source_assignment(self):
        index, count, path = hh.shard_settings_from_argv([
            "--shard-index=2", "--shard-count", "4",
        ])
        self.assertEqual((index, count, path), (2, 4, "state.shard-2-of-4.json"))
        parent = {"name": "Example"}
        supplement = {"name": "Example Program", "supplement_for": "Example"}
        self.assertEqual(hh.source_shard(parent, 8), hh.source_shard(supplement, 8))

    def test_relationship_target_alias_rides_with_canonical_parent(self):
        parent = {
            "name": "Example", "aliases": ["Example Inc"],
            "ats": "ashby", "slug": "example",
        }
        supplement = {
            "name": "Example Program", "supplement_for": "Example Inc",
            "url": "https://example.test/program",
            "keyword_filter": False, "location_filter": False,
        }
        with open("config.yaml", "w") as stream:
            json.dump({
                "keywords": ["intern"], "locations": ["india"],
                "ats_companies": [parent], "custom_pages": [supplement],
            }, stream)

        parent_shard = hh.source_shard(parent, 2)
        page_result = {
            "hash": "a" * 32, "matches_keywords": True,
            "matches_locations": True, "is_match": False,
        }
        with mock.patch.object(hh.sys, "argv", [
                "hybrid_hunter.py", "--test", "--company", "Example",
                "--shard-count", "2", "--shard-index", str(parent_shard)]), \
                mock.patch.object(hh.ATSHunter, "hunt_ashby", return_value=[]), \
                mock.patch.object(
                    hh.CustomWebHunter, "hunt", return_value=page_result) as hunt_page:
            hh.main()

        hunt_page.assert_called_once()

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
            "--workers", "4",
        ]), [])

    def test_boolean_flags_reject_equals_values(self):
        for flag in ("--test", "--seed", "--validate", "--heartbeat",
                     "--ats-only", "--pages-only", "--help"):
            argument = f"{flag}=false"
            with self.subTest(flag=flag):
                self.assertEqual(hh.unknown_flags_from_argv([argument]), [argument])

    def test_run_modes_are_mutually_exclusive(self):
        self.write_empty_config()
        for modes in (("--test", "--seed"),
                      ("--test", "--heartbeat"),
                      ("--seed", "--heartbeat")):
            with self.subTest(modes=modes), \
                    mock.patch.object(hh.sys, "argv", ["hybrid_hunter.py", *modes]):
                with self.assertRaisesRegex(SystemExit, "mutually exclusive|only one"):
                    hh.main()

    def test_ats_only_and_pages_only_are_mutually_exclusive(self):
        self.write_empty_config()
        with mock.patch.object(hh.sys, "argv", [
                "hybrid_hunter.py", "--test", "--ats-only", "--pages-only"]):
            with self.assertRaisesRegex(SystemExit, "mutually exclusive|only one"):
                hh.main()

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
