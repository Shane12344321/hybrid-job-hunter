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


if __name__ == "__main__":
    unittest.main(verbosity=2)
