"""Offline unit tests for the enterprise ATS adapters (Workday / Amazon /
Microsoft). Responses are mocked, so nothing hits the network. Mirrors the
style of the existing Phase 1/2 suites: patch hh.requests, assert on parsed
matches, raise-on-failure, filtering, pagination cap, and the Workday
"N Locations" inclusion rule."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "/Users/shanesarosh/Desktop/crawler")

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)  # STATE_FILE is relative; keep the real one untouched

import hybrid_hunter as hh


def _resp(status, payload):
    """Build a fake requests response (matches how Phase 2 fakes them)."""
    res = mock.Mock()
    res.status_code = status
    if status >= 400:
        res.raise_for_status.side_effect = Exception(f"{status} Client Error")
    else:
        res.raise_for_status.return_value = None
    res.json.return_value = payload
    return res


# --- Realistic fixtures ------------------------------------------------------

WORKDAY_PAGE = {
    "total": 3,
    "jobPostings": [
        {"title": "Machine Learning Intern - 2026",
         "locationsText": "India, Bengaluru",
         "externalPath": "/job/India-Bengaluru/Machine-Learning-Intern---2026_JR1988855"},
        {"title": "Senior Staff Engineer",  # no keyword -> dropped
         "locationsText": "India, Hyderabad",
         "externalPath": "/job/India-Hyderabad/Senior-Staff-Engineer_JR1988900"},
        {"title": "2026 AI/ML Intern - Research",  # multi-location -> inclusion rule
         "locationsText": "3 Locations",
         "externalPath": "/job/Multiple/2026-AI-ML-Intern---Research_JR1990001"},
    ],
}

AMAZON_PAYLOAD = {
    "hits": 2,
    "jobs": [
        {"title": "Software Dev Engineer Intern",
         "normalized_location": "Bengaluru, Karnataka, IND",
         "id_icims": "10464536", "id": "abc-1",
         "job_path": "/en/jobs/10464536/software-dev-engineer-intern"},
        {"title": "Engineering Intern",  # Italy -> location filter drops it
         "normalized_location": "Novara, Piedmont, ITA",
         "id_icims": "3083050", "id": "abc-2",
         "job_path": "/en/jobs/3083050/engineering-intern"},
    ],
}

MICROSOFT_PAYLOAD = {
    "operationResult": {"result": {
        "totalJobs": 1,
        "jobs": [
            {"jobId": "1834567", "title": "Software Engineering Intern",
             "properties": {"locations": ["Hyderabad, Telangana, India"]}},
        ],
    }},
}


class TestWorkday(unittest.TestCase):
    def _hunter(self, **cfg):
        base = {"keywords": ["intern"], "exclude_keywords": [], "locations": ["india", "bengaluru", "remote"]}
        base.update(cfg)
        return hh.ATSHunter(base)

    def test_happy_path_parses_and_filters(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("nvidia", "Site", "wd5", include_multi_location=True)
        # intern (Bengaluru) + multi-location intern via inclusion rule; the
        # non-keyword "Senior Staff Engineer" is dropped.
        titles = [x["title"] for x in m]
        self.assertIn("Machine Learning Intern - 2026", titles)
        self.assertIn("2026 AI/ML Intern - Research", titles)
        self.assertNotIn("Senior Staff Engineer", titles)

    def test_stable_requisition_id(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("nvidia", "Site", "wd5", include_multi_location=True)
        first = next(x for x in m if x["title"].startswith("Machine Learning"))
        self.assertEqual(first["id"], "JR1988855")  # trailing req id, not path index

    def test_url_construction(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("nvidia", "NVIDIAExternalCareerSite", "wd5", include_multi_location=True)
        first = next(x for x in m if x["id"] == "JR1988855")
        self.assertEqual(
            first["url"],
            "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
            "/job/India-Bengaluru/Machine-Learning-Intern---2026_JR1988855",
        )

    def test_n_locations_included_when_include_multi_location(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        self.assertIn("2026 AI/ML Intern - Research", [x["title"] for x in m])

    def test_n_locations_dropped_when_not_include_multi_location(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("x", "S", "wd5", include_multi_location=False)
        # "3 Locations" can't match a city, so with strict location it drops.
        self.assertNotIn("2026 AI/ML Intern - Research", [x["title"] for x in m])

    def test_exclude_keyword_rejects(self):
        h = self._hunter(exclude_keywords=["2026"])
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        # every intern posting here carries "2026" -> all excluded
        self.assertEqual(m, [])

    def test_keyword_override(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, WORKDAY_PAGE)):
            m = h.hunt_workday("x", "S", "wd5", include_multi_location=True, keywords=["staff"])
        self.assertEqual([x["title"] for x in m], ["Senior Staff Engineer"])

    def test_non_200_raises(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(500, {})):
            with self.assertRaises(Exception):
                h.hunt_workday("x", "S", "wd5")

    def test_malformed_json_raises(self):
        h = self._hunter()
        bad = _resp(200, None)
        bad.json.side_effect = ValueError("no json")
        with mock.patch.object(hh.requests, "post", return_value=bad):
            with self.assertRaises(Exception):
                h.hunt_workday("x", "S", "wd5")

    def test_pagination_stops_at_request_cap(self):
        # total never reached and every page is full -> loop must stop at 4.
        full_page = {"total": 10 ** 6, "jobPostings": [
            {"title": "Intern", "locationsText": "India, Pune",
             "externalPath": "/job/x/Intern_JR1"} for _ in range(20)]}
        h = self._hunter()
        post = mock.Mock(return_value=_resp(200, full_page))
        with mock.patch.object(hh.requests, "post", post):
            h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        self.assertEqual(post.call_count, 4)  # hard cap (plan §3), never more

    def test_single_page_stops_early(self):
        h = self._hunter()
        post = mock.Mock(return_value=_resp(200, WORKDAY_PAGE))  # total=3 < limit
        with mock.patch.object(hh.requests, "post", post):
            h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        self.assertEqual(post.call_count, 1)  # one page covers total


class TestAmazon(unittest.TestCase):
    def _hunter(self):
        return hh.ATSHunter({"keywords": ["intern"], "exclude_keywords": [], "locations": ["india", "ind"]})

    def test_happy_path_and_location_filter(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, AMAZON_PAYLOAD)):
            m = h.hunt_amazon("intern", "India")
        self.assertEqual(len(m), 1)  # Italy role filtered out client-side
        self.assertEqual(m[0]["id"], "10464536")  # id_icims preferred
        self.assertEqual(m[0]["url"], "https://www.amazon.jobs/en/jobs/10464536/software-dev-engineer-intern")

    def test_falls_back_to_id_when_no_icims(self):
        payload = {"jobs": [{"title": "SDE Intern", "location": "Remote, India",
                             "id": "xyz-9", "job_path": "/en/jobs/xyz-9/sde"}]}
        h = hh.ATSHunter({"keywords": ["intern"], "exclude_keywords": [], "locations": ["india"]})
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, payload)):
            m = h.hunt_amazon()
        self.assertEqual(m[0]["id"], "xyz-9")

    def test_categories_appended_to_url(self):
        h = self._hunter()
        get = mock.Mock(return_value=_resp(200, {"jobs": []}))
        with mock.patch.object(hh.requests, "get", get):
            h.hunt_amazon("intern", "India",
                          categories=["software-development", "machine-learning-science"])
        url = get.call_args[0][0]
        self.assertIn("category[]=software-development", url)
        self.assertIn("category[]=machine-learning-science", url)

    def test_no_categories_means_no_param(self):
        h = self._hunter()
        get = mock.Mock(return_value=_resp(200, {"jobs": []}))
        with mock.patch.object(hh.requests, "get", get):
            h.hunt_amazon()
        self.assertNotIn("category[]", get.call_args[0][0])

    def test_config_categories_plumb_through_dispatch(self):
        # A config entry's `categories` must survive main()'s dispatch and land
        # in the request URL (offline: requests.get mocked, --test mode so no
        # state writes).
        with open("config.yaml", "w") as f:
            f.write("""
keywords: [intern]
locations: [india]
ats_companies:
  - name: Amazon
    ats: amazon
    query: intern
    location: India
    categories: [software-development, data-science]
custom_pages: []
""")
        get = mock.Mock(return_value=_resp(200, {"jobs": []}))
        with mock.patch.object(hh.requests, "get", get), \
                mock.patch.object(hh.sys, "argv", ["hybrid_hunter.py", "--test", "--ats-only"]):
            hh.main()
        url = get.call_args[0][0]
        self.assertIn("category[]=software-development", url)
        self.assertIn("category[]=data-science", url)

    def test_403_raises(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(403, {})):
            with self.assertRaises(Exception):
                h.hunt_amazon()

    def test_empty_jobs_returns_list(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, {"jobs": []})):
            self.assertEqual(h.hunt_amazon(), [])


class TestMicrosoft(unittest.TestCase):
    def _hunter(self):
        return hh.ATSHunter({"keywords": ["intern"], "exclude_keywords": [], "locations": ["india", "hyderabad"]})

    def test_happy_path(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, MICROSOFT_PAYLOAD)):
            m = h.hunt_microsoft("intern", "India")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["id"], "1834567")
        self.assertEqual(m[0]["url"], "https://jobs.careers.microsoft.com/global/en/job/1834567")

    def test_location_joins_list(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, MICROSOFT_PAYLOAD)):
            m = h.hunt_microsoft()
        self.assertEqual(m[0]["location"], "Hyderabad, Telangana, India")

    def test_non_200_raises(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(404, {})):
            with self.assertRaises(Exception):
                h.hunt_microsoft()

    def test_pagination_stops_at_cap(self):
        full = {"operationResult": {"result": {"totalJobs": 10 ** 6, "jobs": [
            {"jobId": str(i), "title": "Intern",
             "properties": {"locations": ["Hyderabad, India"]}} for i in range(20)]}}}
        h = self._hunter()
        get = mock.Mock(return_value=_resp(200, full))
        with mock.patch.object(hh.requests, "get", get):
            h.hunt_microsoft()
        self.assertEqual(get.call_count, 4)  # pages 1-4, request cap


if __name__ == "__main__":
    unittest.main(verbosity=2)
