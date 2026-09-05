"""Offline contract tests for the Ashby, Lever, and Greenhouse adapters."""
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


def response(payload, status=200):
    res = mock.Mock(status_code=status, ok=status < 400)
    res.json.return_value = payload
    if status >= 400:
        res.raise_for_status.side_effect = Exception(f"{status} Client Error")
    return res


class TestBasicATSAdapters(unittest.TestCase):
    def setUp(self):
        self.hunter = hh.ATSHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
        })

    def test_ashby_parses_and_requires_schema(self):
        payload = {"jobs": [{
            "id": "a1", "title": "Software Intern", "location": "Bengaluru, India",
            "jobUrl": "https://jobs.ashbyhq.com/example/a1",
        }]}
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            self.assertEqual(self.hunter.hunt_ashby("example")[0]["id"], "a1")
        with mock.patch.object(hh.requests, "get", return_value=response({})):
            with self.assertRaisesRegex(ValueError, "missing jobs"):
                self.hunter.hunt_ashby("example")

    def test_lever_parses_and_requires_stable_id(self):
        payload = [{
            "id": "l1", "text": "Engineering Intern",
            "categories": {"location": "Remote, India"},
            "hostedUrl": "https://jobs.lever.co/example/l1",
        }]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            self.assertEqual(self.hunter.hunt_lever("example")[0]["id"], "l1")
        payload[0]["id"] = None
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            with self.assertRaisesRegex(ValueError, "missing id"):
                self.hunter.hunt_lever("example")

    def test_greenhouse_distinguishes_zero_from_missing_marker(self):
        with mock.patch.object(hh.requests, "get", return_value=response({"jobs": []})):
            self.assertEqual(self.hunter.hunt_greenhouse("example"), [])
        with mock.patch.object(hh.requests, "get", return_value=response({})):
            with self.assertRaisesRegex(ValueError, "missing jobs"):
                self.hunter.hunt_greenhouse("example")


class TestLocationMatching(unittest.TestCase):
    def hunter(self, **over):
        cfg = {"keywords": ["intern"], "exclude_keywords": [],
               "locations": ["india", "bengaluru", "delhi", "remote"],
               "exclude_locations": ["usa", "united states", "emea"]}
        cfg.update(over)
        return hh.ATSHunter(cfg)

    def test_india_does_not_match_indiana(self):
        # A bare substring test let "india" match Indiana/Indianapolis, which
        # are real US tech hubs.
        h = self.hunter()
        self.assertFalse(h.location_matches("Indianapolis, Indiana, USA"))
        self.assertFalse(h.location_matches("Indiana, US"))
        self.assertTrue(h.location_matches("Bengaluru, India"))
        self.assertTrue(h.location_matches("Bengaluru-VTP, India"))
        self.assertTrue(h.location_matches("New Delhi, India"))

    def test_remote_is_kept_but_foreign_remote_is_dropped(self):
        h = self.hunter(exclude_locations=[
            "usa", "united states", "emea", "san francisco", "warsaw",
        ])
        self.assertTrue(h.location_matches("Remote"))
        self.assertTrue(h.location_matches("Remote - India"))
        self.assertFalse(h.location_matches("USA | Remote"))
        self.assertFalse(h.location_matches("Remote - United States"))
        self.assertFalse(h.location_matches("Remote, EMEA"))
        self.assertFalse(h.location_matches("Remote - San Francisco, California"))
        self.assertFalse(h.location_matches("Remote-Warsaw"))

    def test_exclude_locations_beats_an_otherwise_valid_city(self):
        h = self.hunter()
        self.assertFalse(h.location_matches("Bengaluru, India / Austin, USA"))

    def test_no_locations_configured_accepts_everything(self):
        h = self.hunter(locations=[], exclude_locations=[])
        self.assertTrue(h.location_matches("Anywhere"))
        self.assertTrue(h.location_matches(""))

    def test_matches_criteria_still_requires_both_title_and_location(self):
        h = self.hunter()
        self.assertTrue(h.matches_criteria("Software Intern", "Bengaluru, India"))
        self.assertFalse(h.matches_criteria("Software Intern", "Austin, USA"))
        self.assertFalse(h.matches_criteria("Staff Engineer", "Bengaluru, India"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
