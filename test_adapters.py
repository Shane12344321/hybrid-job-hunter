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

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)  # STATE_FILE is relative; keep the real one untouched

import hybrid_hunter as hh


def _resp(status, payload):
    """Build a fake requests response (matches how Phase 2 fakes them)."""
    res = mock.Mock()
    res.status_code = status
    res.ok = status < 400
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

MICROSOFT_PAYLOAD = {"data": {
    "count": 1,
    "positions": [
        {"id": 1970393556917519, "displayJobId": "200041777",
         "name": "Software Engineering Intern",
         "locations": ["Hyderabad, Telangana, India"],
         "positionUrl": "/careers/job/1970393556917519"},
    ],
}}

SMARTRECRUITERS_PAYLOAD = {
    "totalFound": 2,
    "content": [
        {
            "id": "sr-1", "name": "Software Engineering Intern",
            "location": {"city": "Bengaluru", "region": "Karnataka", "country": "India"},
        },
        {
            "id": "sr-2", "name": "Senior Software Engineer",
            "location": {"city": "Bengaluru", "region": "Karnataka", "country": "India"},
        },
    ],
}


class TestSmartRecruiters(unittest.TestCase):
    def setUp(self):
        self.hunter = hh.ATSHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
        })

    def test_happy_path_filters_and_builds_stable_url(self):
        with mock.patch.object(
                hh.requests, "get", return_value=_resp(200, SMARTRECRUITERS_PAYLOAD)) as get:
            jobs = self.hunter.hunt_smartrecruiters("Example", country="in")
        self.assertEqual([job["id"] for job in jobs], ["sr-1"])
        self.assertEqual(
            jobs[0]["url"], "https://jobs.smartrecruiters.com/Example/sr-1")
        self.assertEqual(get.call_args.kwargs["params"]["country"], "in")
        self.assertEqual(self.hunter.request_count, 1)

    def test_explicit_zero_and_missing_marker_are_distinct(self):
        with mock.patch.object(
                hh.requests, "get",
                return_value=_resp(200, {"totalFound": 0, "content": []})):
            self.assertEqual(self.hunter.hunt_smartrecruiters("Example"), [])
        with mock.patch.object(
                hh.requests, "get", return_value=_resp(200, {"content": []})):
            with self.assertRaisesRegex(ValueError, "totalFound"):
                self.hunter.hunt_smartrecruiters("Example")

    def test_truncated_pagination_raises_at_four_requests(self):
        pages = []
        for page in range(4):
            pages.append(_resp(200, {
                "totalFound": 401,
                "content": [{
                    "id": f"{page}-{index}", "name": "Senior Engineer",
                    "location": {"country": "India"},
                } for index in range(100)],
            }))
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                self.hunter.hunt_smartrecruiters("Example")
        self.assertEqual(get.call_count, 4)


class TestWorkable(unittest.TestCase):
    def setUp(self):
        self.hunter = hh.ATSHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"],
        })

    def test_happy_path_and_multi_location(self):
        payload = {
            "name": "Example",
            "jobs": [{
                "shortcode": "ABC123", "title": "Software Engineering Intern",
                "url": "https://apply.workable.com/j/ABC123",
                "telecommuting": True,
                "locations": [{
                    "city": "Bengaluru", "region": "Karnataka", "country": "India",
                }],
            }, {
                "shortcode": "DEF456", "title": "Senior Engineer",
                "url": "https://apply.workable.com/j/DEF456",
                "country": "India", "city": "Pune",
            }],
        }
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, payload)):
            jobs = self.hunter.hunt_workable("example")
        self.assertEqual([job["id"] for job in jobs], ["ABC123"])
        self.assertIn("Bengaluru", jobs[0]["location"])
        self.assertIn("Remote", jobs[0]["location"])

    def test_explicit_zero_and_malformed_schema_are_distinct(self):
        with mock.patch.object(
                hh.requests, "get",
                return_value=_resp(200, {"name": "Example", "jobs": []})):
            self.assertEqual(self.hunter.hunt_workable("example"), [])
        with mock.patch.object(
                hh.requests, "get",
                return_value=_resp(200, {"name": "Example"})):
            with self.assertRaisesRegex(ValueError, "jobs"):
                self.hunter.hunt_workable("example")

    def test_missing_stable_id_raises(self):
        payload = {
            "name": "Example",
            "jobs": [{
                "title": "Engineering Intern",
                "url": "https://apply.workable.com/j/ABC",
                "country": "India",
            }],
        }
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, payload)):
            with self.assertRaisesRegex(ValueError, "shortcode"):
                self.hunter.hunt_workable("example")


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
        # total never reached and every page is full -> fail after 4 requests.
        full_page = {"total": 10 ** 6, "jobPostings": [
            {"title": "Intern", "locationsText": "India, Pune",
             "externalPath": f"/job/x/Intern_JR{i}"} for i in range(20)]}
        h = self._hunter()
        pages = []
        for page in range(4):
            payload = dict(full_page)
            payload["jobPostings"] = [
                {"title": "Intern", "locationsText": "India, Pune",
                 "externalPath": f"/job/x/Intern_JR{page * 20 + i}"}
                for i in range(20)]
            pages.append(_resp(200, payload))
        post = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "post", post):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        self.assertEqual(post.call_count, 4)  # hard cap (plan §3), never more

    def test_missing_result_markers_raise(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, {})):
            with self.assertRaisesRegex(ValueError, "missing total"):
                h.hunt_workday("x", "S", "wd5")

    def test_single_page_stops_early(self):
        h = self._hunter()
        post = mock.Mock(return_value=_resp(200, WORKDAY_PAGE))  # total=3 < limit
        with mock.patch.object(hh.requests, "post", post):
            h.hunt_workday("x", "S", "wd5", include_multi_location=True)
        self.assertEqual(post.call_count, 1)  # one page covers total

    def _paged(self, total, per_page=20):
        """Pages of `per_page` postings, last one partial, as Workday returns."""
        pages = []
        for offset in range(0, total, per_page):
            count = min(per_page, total - offset)
            pages.append(_resp(200, {"total": total, "jobPostings": [
                {"title": "Intern", "locationsText": "India, Pune",
                 "externalPath": f"/job/x/Intern_JR{offset + i}"}
                for i in range(count)]}))
        return pages

    def test_max_pages_reads_result_sets_larger_than_the_default_cap(self):
        # BlackRock: "intern" also matches internal/international, so the set
        # runs past 4 pages. Workday caps limit at 20, so the only way to read
        # it in full is more pages.
        post = mock.Mock(side_effect=self._paged(164))
        with mock.patch.object(hh.requests, "post", post):
            m = self._hunter().hunt_workday(
                "blackrock", "S", "wd1", include_multi_location=True, max_pages=10)
        self.assertEqual(post.call_count, 9)  # ceil(164/20), not the default 4
        self.assertEqual(len(m), 164)

    def test_max_pages_still_truncates_when_the_budget_is_too_small(self):
        post = mock.Mock(side_effect=self._paged(164))
        with mock.patch.object(hh.requests, "post", post):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                self._hunter().hunt_workday(
                    "blackrock", "S", "wd1", include_multi_location=True, max_pages=5)
        self.assertEqual(post.call_count, 5)

    def test_max_pages_is_bounds_checked(self):
        h = self._hunter()
        for bad in (0, 13, True, "5", 2.0):
            with self.assertRaisesRegex(ValueError, "max_pages"):
                h.hunt_workday("x", "S", "wd5", max_pages=bad)

    def test_null_location_does_not_fail_the_whole_source(self):
        # Fractal Analytics returns locationsText: null on some postings. That
        # is a real Workday state, not schema drift: one such row must not cost
        # us the other 35 postings on the board.
        payload = {"total": 2, "jobPostings": [
            {"title": "Automation Test Engineer Intern", "locationsText": None,
             "externalPath": "/job/Automation-Test-Engineer_SR-20170"},
            {"title": "Intern - Data", "locationsText": "India, Pune",
             "externalPath": "/job/Intern-Data_SR-20171"},
        ]}
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, payload)):
            m = self._hunter().hunt_workday("fractal", "Careers", "wd1",
                                            include_multi_location=True)
        titles = [x["title"] for x in m]
        self.assertIn("Intern - Data", titles)
        # Unknown location behaves like "N Locations": included on a title match
        # rather than silently dropped.
        self.assertIn("Automation Test Engineer Intern", titles)

    def test_null_location_honours_the_location_filter_when_not_including_multi(self):
        payload = {"total": 1, "jobPostings": [
            {"title": "Intern - Data", "locationsText": None,
             "externalPath": "/job/Intern-Data_SR-20171"},
        ]}
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, payload)):
            m = self._hunter().hunt_workday("fractal", "Careers", "wd1",
                                            include_multi_location=False)
        self.assertEqual(m, [])

    def test_non_string_location_still_raises_as_schema_drift(self):
        payload = {"total": 1, "jobPostings": [
            {"title": "Intern", "locationsText": 42,
             "externalPath": "/job/x/Intern_JR1"}]}
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, payload)):
            with self.assertRaisesRegex(ValueError, "non-string locationsText"):
                self._hunter().hunt_workday("x", "S", "wd5")

    def test_missing_title_still_raises_as_schema_drift(self):
        payload = {"total": 1, "jobPostings": [
            {"title": None, "locationsText": "India, Pune",
             "externalPath": "/job/x/Intern_JR1"}]}
        with mock.patch.object(hh.requests, "post", return_value=_resp(200, payload)):
            with self.assertRaisesRegex(ValueError, "missing title/externalPath"):
                self._hunter().hunt_workday("x", "S", "wd5")


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
        payload = {"hits": 1, "jobs": [{"title": "SDE Intern", "location": "Remote, India",
                             "id": "xyz-9", "job_path": "/en/jobs/xyz-9/sde"}]}
        h = hh.ATSHunter({"keywords": ["intern"], "exclude_keywords": [], "locations": ["india"]})
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, payload)):
            m = h.hunt_amazon()
        self.assertEqual(m[0]["id"], "xyz-9")

    def test_categories_appended_to_url(self):
        h = self._hunter()
        get = mock.Mock(return_value=_resp(200, {"hits": 0, "jobs": []}))
        with mock.patch.object(hh.requests, "get", get):
            h.hunt_amazon("intern", "India",
                          categories=["software-development", "machine-learning-science"])
        url = get.call_args[0][0]
        self.assertIn("category[]=software-development", url)
        self.assertIn("category[]=machine-learning-science", url)

    def test_no_categories_means_no_param(self):
        h = self._hunter()
        get = mock.Mock(return_value=_resp(200, {"hits": 0, "jobs": []}))
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
        get = mock.Mock(return_value=_resp(200, {"hits": 0, "jobs": []}))
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
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, {"hits": 0, "jobs": []})):
            self.assertEqual(h.hunt_amazon(), [])

    def test_missing_markers_and_truncation_raise(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, {})):
            with self.assertRaisesRegex(ValueError, "missing hits"):
                h.hunt_amazon()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, {"hits": 2, "jobs": []})):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                h.hunt_amazon()


class TestMicrosoft(unittest.TestCase):
    def _hunter(self):
        return hh.ATSHunter({"keywords": ["intern"], "exclude_keywords": [], "locations": ["india", "hyderabad"]})

    def test_happy_path(self):
        h = self._hunter()
        with mock.patch.object(hh.requests, "get", return_value=_resp(200, MICROSOFT_PAYLOAD)):
            m = h.hunt_microsoft("intern", "India")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["id"], "1970393556917519")
        self.assertEqual(m[0]["url"], "https://apply.careers.microsoft.com/careers/job/1970393556917519")

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
        pages = []
        for page in range(4):
            full = {"data": {"count": 10 ** 6, "positions": [
                {"id": str(page * 20 + i), "name": "Intern",
                 "locations": ["Hyderabad, India"],
                 "positionUrl": f"/careers/job/{page * 20 + i}"}
                for i in range(20)]}}
            pages.append(_resp(200, full))
        h = self._hunter()
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                h.hunt_microsoft()
        self.assertEqual(get.call_count, 4)  # pages 1-4, request cap


if __name__ == "__main__":
    unittest.main(verbosity=2)
