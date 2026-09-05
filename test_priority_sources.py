"""Offline contract tests for the priority-company crawler sources."""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import yaml

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_DIR)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

import hybrid_hunter as hh
import testing_support

setUpModule = testing_support.block_network
tearDownModule = testing_support.restore_network


def response(payload=None, text="", status=200):
    res = mock.Mock()
    res.status_code = status
    res.ok = status < 400
    res.text = text
    res.url = "https://example.test/results"
    res.json.return_value = payload
    if status >= 400:
        res.raise_for_status.side_effect = Exception(f"{status} Client Error")
    else:
        res.raise_for_status.return_value = None
    return res


def hunter(keywords=None, locations=None, excludes=None):
    return hh.ATSHunter({
        "keywords": keywords or ["intern", "internship", "summer", "new grad"],
        "exclude_keywords": excludes or ["phd"],
        "locations": locations or ["india", "bengaluru", "hyderabad", "remote"],
    })


class TestEightfold(unittest.TestCase):
    def test_qualcomm_seniority_and_exact_filter(self):
        payload = {"data": {"count": 2, "positions": [
            {"id": 446718114628, "name": "Interim Engineering Intern_Systems-2026",
             "locations": ["Bangalore, Karnataka, India"],
             "positionUrl": "/careers/job/446718114628"},
            {"id": 99, "name": "CPU Engineering Operations Manager",
             "locations": ["Bangalore, Karnataka, India"],
             "positionUrl": "/careers/job/99"},
        ]}}
        get = mock.Mock(return_value=response(payload))
        with mock.patch.object(hh.requests, "get", get):
            jobs = hunter().hunt_eightfold(
                "https://careers.qualcomm.com", "qualcomm.com",
                query="intern", location="India", seniority="Intern")
        self.assertEqual([j["id"] for j in jobs], ["446718114628"])
        self.assertEqual(get.call_args.kwargs["params"]["filter_seniority"], "Intern")

    def test_recognized_zero_and_malformed_are_distinct(self):
        with mock.patch.object(hh.requests, "get", return_value=response({"data": {"count": 0, "positions": []}})):
            self.assertEqual(hunter().hunt_microsoft(), [])
        with mock.patch.object(hh.requests, "get", return_value=response({"data": {}})):
            with self.assertRaisesRegex(ValueError, "missing"):
                hunter().hunt_microsoft()

    def test_later_zero_count_cannot_shorten_the_first_page_total(self):
        first_page = {"data": {"count": 3, "positions": [
            {"id": 1, "name": "Software Intern",
             "locations": ["Bengaluru, India"], "positionUrl": "/careers/job/1"},
            {"id": 2, "name": "Data Intern",
             "locations": ["Hyderabad, India"], "positionUrl": "/careers/job/2"},
        ]}}
        # Some tenants report count=0 after the first offset. That must not
        # overwrite the first page's authoritative total and make two of three
        # positions look like a complete read.
        later_page = {"data": {"count": 0, "positions": []}}
        get = mock.Mock(side_effect=[response(first_page), response(later_page)])
        with mock.patch.object(hh.requests, "get", get):
            with self.assertRaisesRegex(RuntimeError, r"truncated \(2/3\)"):
                hunter().hunt_eightfold(
                    "https://careers.example.test", "example.test",
                    query="intern", location="India")
        self.assertEqual(get.call_count, 2)


class TestAtlassian(unittest.TestCase):
    def test_category_and_india_filter_with_stable_portal_id(self):
        payload = [
            {"portalId": 242, "id": 25001, "title": "Software Engineer",
             "locations": ["India - Bengaluru", "Remote - India - Remote"],
             "category": "Graduates",
             "applyUrl": "https://careers-apac-atlassian.icims.com/jobs/25001/apply"},
            {"portalId": 242, "id": 25001, "title": "Software Engineer",
             "locations": ["India - Bengaluru", "Remote - India - Remote"],
             "category": "Graduates",
             "applyUrl": "https://careers-apac-atlassian.icims.com/jobs/25001/apply"},
            {"portalId": 17, "id": 25002, "title": "Software Intern",
             "locations": ["United States - New York"], "category": "Interns",
             "applyUrl": "https://globalcareers-atlassian.icims.com/jobs/25002/apply"},
        ]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            jobs = hunter().hunt_atlassian(
                location="India", categories=["Interns", "Graduates"])
        self.assertEqual([job["id"] for job in jobs], ["242:25001"])

    def test_keyword_title_matches_even_outside_the_wanted_categories(self):
        # Atlassian's feed carried no Interns/Graduates categories at all on
        # 2026-08-02 (Sales, Engineering, ...). Gating on category meant the
        # source could never match while still reporting a healthy zero.
        payload = [
            {"portalId": 242, "id": 1, "title": "Summer Intern, Engineering",
             "locations": ["India - Bengaluru"], "category": "Engineering",
             "applyUrl": "https://example.test/1"},
            {"portalId": 242, "id": 2, "title": "Principal Engineer",
             "locations": ["India - Bengaluru"], "category": "Engineering",
             "applyUrl": "https://example.test/2"},
        ]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            jobs = hunter().hunt_atlassian(
                location="India", categories=["Interns", "Graduates"])
        self.assertEqual([job["id"] for job in jobs], ["242:1"])

    def test_wanted_category_still_matches_a_keywordless_title(self):
        payload = [{"portalId": 242, "id": 3, "title": "Software Engineer",
                    "locations": ["India - Bengaluru"], "category": "Graduates",
                    "applyUrl": "https://example.test/3"}]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            jobs = hunter().hunt_atlassian(
                location="India", categories=["Interns", "Graduates"])
        self.assertEqual([job["id"] for job in jobs], ["242:3"])

    def test_wanted_category_cannot_bypass_excluded_title_keywords(self):
        payload = [{"portalId": 242, "id": 31,
                    "title": "Software Engineer, PhD Graduate",
                    "locations": ["India - Bengaluru"], "category": "Graduates",
                    "applyUrl": "https://example.test/31"}]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            jobs = hunter(excludes=["phd"]).hunt_atlassian(
                location="India", categories=["Interns", "Graduates"])
        self.assertEqual(jobs, [])

    def test_location_still_gates_both_paths(self):
        payload = [{"portalId": 17, "id": 4, "title": "Summer Intern",
                    "locations": ["United States - New York"], "category": "Interns",
                    "applyUrl": "https://example.test/4"}]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            self.assertEqual(hunter().hunt_atlassian(
                location="India", categories=["Interns", "Graduates"]), [])

    def test_india_filter_does_not_match_indiana_in_the_us(self):
        payload = [{"portalId": 17, "id": 41, "title": "Software Intern",
                    "locations": ["Indiana, United States"], "category": "Interns",
                    "applyUrl": "https://example.test/41"}]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            jobs = hunter().hunt_atlassian(
                location="India", categories=["Interns", "Graduates"])
        self.assertEqual(jobs, [])

    def test_empty_locations_is_malformed_not_a_healthy_zero(self):
        payload = [{"portalId": 242, "id": 42, "title": "Software Intern",
                    "locations": [], "category": "Interns",
                    "applyUrl": "https://example.test/42"}]
        with mock.patch.object(hh.requests, "get", return_value=response(payload)):
            with self.assertRaisesRegex(ValueError, "location"):
                hunter().hunt_atlassian(
                    location="India", categories=["Interns", "Graduates"])

    def test_malformed_listing_fails_instead_of_zero(self):
        with mock.patch.object(hh.requests, "get", return_value=response([{"id": 1}])):
            with self.assertRaisesRegex(ValueError, "missing"):
                hunter().hunt_atlassian()


class TestGoogle(unittest.TestCase):
    CARD = """
    <html><body><ul><li class="lLd3Je">
      <a class="WpHeLc" href="jobs/results/143456789-software-engineering-intern"></a>
      <h3 class="QJPWVe">Software Engineering Intern</h3>
      <div class="r0wTof">Bengaluru, Karnataka, India</div>
    </li></ul></body></html>
    """

    def test_card_parsing_and_stable_id(self):
        with mock.patch.object(hh.requests, "get", return_value=response(text=self.CARD)):
            jobs = hunter().hunt_google()
        self.assertEqual(jobs[0]["id"], "143456789")
        self.assertIn("/results/143456789-", jobs[0]["url"])

    def test_explicit_zero_is_healthy(self):
        html = "<html><body><p>0 jobs matched</p></body></html>"
        with mock.patch.object(hh.requests, "get", return_value=response(text=html)):
            self.assertEqual(hunter().hunt_google(), [])

    def test_unrecognized_empty_page_fails(self):
        with mock.patch.object(hh.requests, "get", return_value=response(text="<html><body>Careers</body></html>")):
            with self.assertRaisesRegex(ValueError, "neither job cards"):
                hunter().hunt_google()


class TestIntuit(unittest.TestCase):
    def test_india_result_and_last_path_id(self):
        html = """
        <section id="search-results" data-total-results="1">
          <ul class="search-list"><li><a class="sr-item" data-title="Software Intern"
             href="/job/bengaluru/software-intern/27595/97414465360">
             <h2>Software Intern</h2><span class="job-location">Bengaluru, India</span>
          </a></li></ul>
        </section>
        """
        get = mock.Mock(return_value=response(text=html))
        with mock.patch.object(hh.requests, "get", get):
            jobs = hunter().hunt_intuit()
        self.assertEqual(jobs[0]["id"], "97414465360")
        self.assertEqual(get.call_args.kwargs["params"]["lp"], "1269750")

    def test_recognized_zero(self):
        html = '<section id="search-results" data-total-results="0"></section>'
        with mock.patch.object(hh.requests, "get", return_value=response(text=html)):
            self.assertEqual(hunter().hunt_intuit(), [])

    @staticmethod
    def page(total, job_id=None, next_page=False):
        card = ""
        if job_id is not None:
            card = f"""
              <ul class="search-list"><li><a class="sr-item" data-title="Software Intern"
                 href="/job/bengaluru/software-intern/27595/{job_id}">
                 <h2>Software Intern</h2><span class="job-location">Bengaluru, India</span>
              </a></li></ul>
            """
        pagination = ('<nav class="pagination"><a class="next" '
                      'href="/search-jobs/page/2">Next</a></nav>') if next_page else ""
        return (f'<section id="search-results" data-total-results="{total}">'
                f'{card}{pagination}</section>')

    def test_later_zero_total_does_not_discard_a_complete_read(self):
        pages = [
            response(text=self.page(2, "1001", next_page=True)),
            # TalentBrew can reset the total on later offsets even while still
            # returning cards. The first page's total remains authoritative.
            response(text=self.page(0, "1002")),
        ]
        with mock.patch.object(hh.requests, "get", side_effect=pages):
            jobs = hunter().hunt_intuit()
        self.assertEqual([job["id"] for job in jobs], ["1001", "1002"])

    def test_later_zero_total_cannot_make_a_partial_read_healthy(self):
        pages = [
            response(text=self.page(3, "1001", next_page=True)),
            response(text=self.page(0, "1002")),
        ]
        with mock.patch.object(hh.requests, "get", side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, r"truncated \(2/3\)"):
                hunter().hunt_intuit()


class TestOracle(unittest.TestCase):
    @staticmethod
    def payload(total, jobs):
        return {"items": [{"TotalJobsCount": total, "requisitionList": jobs}]}

    def test_location_and_finder_offset_pagination(self):
        pages = [
            response(self.payload(2, [{"Id": "1", "Title": "Internal Communications Lead",
                                       "PrimaryLocation": "Bengaluru, India"}])),
            response(self.payload(2, [{"Id": "2", "Title": "Software Engineering Intern",
                                       "PrimaryLocation": "Hyderabad, India"}])),
        ]
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            jobs = hunter().hunt_oracle_hcm("UberCareers", host="tenant.example", location="India")
        self.assertEqual([j["id"] for j in jobs], ["2"])
        self.assertIn("location=India", get.call_args_list[0].args[0])
        self.assertIn("offset=25", get.call_args_list[1].args[0])

    def test_query_that_never_ran_is_not_reported_as_truncated(self):
        # The 4-request budget is shared across queries. When an earlier query
        # consumes it, the next one makes no request at all — it must not be
        # blamed for "truncated (0/None)".
        pages = [response(self.payload(100, [
            {"Id": str(page * 25 + i), "Title": "Software Intern",
             "PrimaryLocation": "Bengaluru, India"} for i in range(25)]))
            for page in range(4)]
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            with self.assertRaises(RuntimeError) as caught:
                hunter().hunt_oracle_hcm(
                    "UberCareers", location="India",
                    queries=["internship", "summer analyst", "graduate"])
        message = str(caught.exception)
        self.assertIn("exhausted its 4-request budget", message)
        self.assertIn("summer analyst", message)   # names the query that was skipped
        self.assertNotIn("0/None", message)        # never blames it for truncation
        self.assertEqual(get.call_count, 4)        # budget still respected

    def test_truncation_fails(self):
        pages = []
        for page in range(4):
            jobs = [{"Id": str(page * 25 + i), "Title": "Internal Role",
                     "PrimaryLocation": "India"} for i in range(25)]
            pages.append(response(self.payload(101, jobs)))
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                hunter().hunt_oracle_hcm("UberCareers", location="India")
        self.assertEqual(get.call_count, 4)

    def test_multiple_queries_share_four_request_budget(self):
        graduate_page_one = [
            {"Id": str(i), "Title": "Internal Role", "PrimaryLocation": "India"}
            for i in range(25)
        ]
        pages = [
            response(self.payload(0, [])),
            response(self.payload(0, [])),
            response(self.payload(26, graduate_page_one)),
            response(self.payload(26, [{"Id": "25", "Title": "Internal Role",
                                        "PrimaryLocation": "India"}])),
        ]
        get = mock.Mock(side_effect=pages)
        with mock.patch.object(hh.requests, "get", get):
            jobs = hunter().hunt_oracle_hcm(
                "CX_1001", location="India",
                queries=["internship", "summer analyst", "graduate"])
        self.assertEqual(jobs, [])
        self.assertEqual(get.call_count, 4)
        self.assertIn("keyword=graduate,location=India,offset=25", get.call_args_list[-1].args[0])


class TestGoldman(unittest.TestCase):
    def test_campus_role_parsing(self):
        payload = {"data": {"roleSearch": {"totalCount": 1, "items": [{
            "roleId": "170619_GS_CAMPUS",
            "jobTitle": "2027 | APAC | Mumbai | Engineering | Summer Analyst",
            "locations": [{"city": "Mumbai", "state": "Maharashtra", "country": "India"}],
        }]}}}
        with mock.patch.object(hh.requests, "post", return_value=response(payload)):
            jobs = hunter(keywords=["summer", "new analyst"]).hunt_goldman()
        self.assertEqual(jobs[0]["id"], "170619_GS_CAMPUS")
        self.assertEqual(jobs[0]["url"], "https://higher.gs.com/roles/170619_GS_CAMPUS")

    def test_later_zero_total_cannot_shorten_the_first_page_total(self):
        first_page = {"data": {"roleSearch": {"totalCount": 2, "items": [{
            "roleId": "first", "jobTitle": "Summer Analyst",
            "locations": [{"city": "Mumbai", "country": "India"}],
        }]}}}
        later_page = {"data": {"roleSearch": {"totalCount": 0, "items": []}}}
        post = mock.Mock(side_effect=[response(first_page), response(later_page)])
        with mock.patch.object(hh.requests, "post", post):
            with self.assertRaisesRegex(RuntimeError, r"truncated \(1/2\)"):
                hunter(keywords=["summer"]).hunt_goldman()
        self.assertEqual(post.call_count, 2)


class TestDEShaw(unittest.TestCase):
    def test_worldwide_internships_and_internal_false_positive(self):
        html = """
        <a class="parent-arrow-long" href="/careers/internal-communications-associate-5902">
          iconInternal Communications Associate: description</a>
        <a class="parent-arrow-long" href="/careers/software-developer-intern-new-york-summer-2027-5894">
          iconSoftware Developer Intern (New York) – Summer 2027: description</a>
        <a class="parent-arrow-long" href="/careers/software-developer-ph-d-intern-new-york-summer-2027-5893">
          iconSoftware Developer, Ph.D. Intern (New York) – Summer 2027: description</a>
        """
        with mock.patch.object(hh.requests, "get", return_value=response(text=html)):
            jobs = hunter().hunt_deshaw()
        self.assertEqual([j["id"] for j in jobs], ["5894"])
        self.assertEqual(jobs[0]["location"], "New York")


class TestAtlassianStructuredFallback(unittest.TestCase):
    def setUp(self):
        self.config = {
            "url": "https://www.atlassian.com/company/careers/all-jobs?location=India",
            "job_selector": "a[href*='/company/careers/details/']",
            "id_regex": r"/details/([^/?#]+)",
            "zero_result_text": ["No jobs found"],
            "location_filter": False,
        }
        self.parser = hh.CustomWebHunter({
            "keywords": ["intern"], "exclude_keywords": [], "locations": ["india"]})

    def test_individual_job_extraction(self):
        html = '<body><a href="/company/careers/details/123/software-engineer-intern">Software Engineer Intern</a></body>'
        result = self.parser.parse_structured_jobs(html, self.config)
        self.assertEqual(result["jobs"][0]["id"], "123")

    def test_explicit_zero_vs_broken_page(self):
        self.assertEqual(self.parser.parse_structured_jobs("<body>No jobs found</body>", self.config), {"jobs": []})
        broken = self.parser.parse_structured_jobs("<body>Navigation only</body>", self.config)
        self.assertIn("failed", broken)

    def test_keyword_and_location_filters_are_independently_configurable(self):
        content = """<body>
          <article class="job"><a class="link" href="/jobs/1"><span class="title">Software Intern</span></a><span class="location">Bengaluru, India</span></article>
          <article class="job"><a class="link" href="/jobs/2"><span class="title">Software Intern</span></a><span class="location">London, UK</span></article>
          <article class="job"><a class="link" href="/jobs/3"><span class="title">Software Engineer</span></a><span class="location">Bengaluru, India</span></article>
          <article class="job"><a class="link" href="/jobs/4"><span class="title">Software Engineer</span></a><span class="location">London, UK</span></article>
        </body>"""
        base_config = {
            "url": "https://example.test/careers",
            "job_selector": "article.job", "link_selector": "a.link",
            "title_selector": ".title", "job_location_selector": ".location",
            "id_regex": r"/jobs/(\d+)",
        }
        expected = {
            (True, True): ["1"],
            (True, False): ["1", "2"],
            (False, True): ["1", "3"],
            (False, False): ["1", "2", "3", "4"],
        }
        for filters, expected_ids in expected.items():
            keyword_filter, location_filter = filters
            with self.subTest(keyword_filter=keyword_filter,
                              location_filter=location_filter):
                result = self.parser.parse_structured_jobs(content, {
                    **base_config,
                    "keyword_filter": keyword_filter,
                    "location_filter": location_filter,
                })
                self.assertEqual([job["id"] for job in result["jobs"]], expected_ids)


class TestCustomPageRobustness(unittest.TestCase):
    def hunter(self):
        return hh.CustomWebHunter({
            "keywords": ["intern"], "exclude_keywords": [],
            "locations": ["india", "remote"]})

    def test_retries_once_then_succeeds(self):
        h = self.hunter()
        attempts = []

        def flaky(page_config):
            attempts.append(page_config["name"])
            if len(attempts) == 1:
                return {"failed": "page load crashed: connection reset"}
            return {"hash": "abc", "is_match": True}

        with mock.patch.object(h, "_hunt_once", side_effect=flaky), \
                mock.patch.object(h, "close") as closed:
            result = h.hunt({"name": "Example", "url": "https://example.test"})
        self.assertEqual(result["hash"], "abc")
        self.assertEqual(len(attempts), 2)
        # the shared browser is dropped between attempts in case it was the
        # thing that died
        self.assertEqual(closed.call_count, 1)

    def test_a_genuinely_broken_page_still_fails(self):
        h = self.hunter()
        with mock.patch.object(
                h, "_hunt_once",
                return_value={"failed": "page returned HTTP 500"}) as once, \
                mock.patch.object(h, "close"):
            result = h.hunt({"name": "Example", "url": "https://example.test"})
        self.assertIn("failed", result)
        self.assertEqual(once.call_count, 2)  # tried twice, then reported

    def test_retry_can_be_disabled(self):
        h = self.hunter()
        with mock.patch.object(
                h, "_hunt_once", return_value={"failed": "boom"}) as once:
            h.hunt({"name": "Example", "url": "https://x.test"}, attempts=1)
        self.assertEqual(once.call_count, 1)

    def _fake_browser(self, html, selector_times_out):
        page = mock.Mock()
        page.goto.return_value = mock.Mock(ok=True, status=200)
        page.content.return_value = html
        if selector_times_out:
            page.wait_for_selector.side_effect = Exception("timeout")
        browser = mock.Mock()
        browser.new_page.return_value = page
        return browser

    def test_selector_timeout_fails_a_structured_page_instead_of_parsing_it(self):
        # A timeout means the board never finished rendering. Parsing anyway
        # returns a short job list indistinguishable from a complete one — the
        # silent partial read this crawler exists to prevent.
        html = ('<body><a href="/company/careers/details/1/software-intern">'
                'Software Intern</a></body>')
        h = self.hunter()
        with mock.patch.object(h, "_ensure_browser",
                               return_value=self._fake_browser(html, True)):
            result = h._hunt_once({
                "name": "Example", "url": "https://example.test",
                "wait_for_selector": ".jobs",
                "job_selector": "a[href*='/details/']",
                "id_regex": r"/details/([^/?#]+)",
                "location_filter": False,
            })
        self.assertIn("failed", result)
        self.assertIn("timeout", result["failed"])
        self.assertNotIn("jobs", result)

    def test_structured_page_parses_when_the_selector_resolves(self):
        html = ('<body><a href="/company/careers/details/1/software-intern">'
                'Software Intern</a></body>')
        h = self.hunter()
        with mock.patch.object(h, "_ensure_browser",
                               return_value=self._fake_browser(html, False)):
            result = h._hunt_once({
                "name": "Example", "url": "https://example.test",
                "wait_for_selector": ".jobs",
                "job_selector": "a[href*='/details/']",
                "id_regex": r"/details/([^/?#]+)",
                "location_filter": False,
            })
        self.assertEqual([j["id"] for j in result["jobs"]], ["1"])

    def test_page_location_match_is_word_boundary(self):
        h = self.hunter()
        # "india" must not match "Indiana" in page text
        self.assertTrue(any(re.search(r'\b' + l + r'\b', "roles in bengaluru, india")
                            for l in h.locations))
        self.assertFalse(any(re.search(r'\b' + l + r'\b', "our indianapolis office")
                             for l in h.locations))


class TestPriorityCompanyConfig(unittest.TestCase):
    def test_all_sixteen_have_one_primary_source(self):
        config_path = os.path.join(REPO_DIR, "config.yaml")
        with open(config_path) as stream:
            config = yaml.safe_load(stream)
        primaries = list(config["ats_companies"])
        primaries.extend(page for page in config["custom_pages"]
                         if not page.get("fallback_for") and not page.get("supplement_for"))
        requested = [
            "Google", "Microsoft", "Amazon", "Adobe", "LinkedIn", "Atlassian",
            "Stripe", "Salesforce", "Qualcomm", "NVIDIA", "Uber", "Intuit",
            "Goldman Sachs Engineering", "J.P. Morgan Technology", "D. E. Shaw",
            "Tower Research",
        ]
        for requested_name in requested:
            hits = [entry for entry in primaries
                    if requested_name in [entry["name"], *(entry.get("aliases") or [])]]
            self.assertEqual(len(hits), 1, requested_name)

    def test_target_filter_includes_fallbacks_and_aliases(self):
        self.assertTrue(hh.source_selected(
            {"name": "JP Morgan Chase", "aliases": ["J.P. Morgan Technology"]},
            ["j.p. morgan technology"]))
        self.assertTrue(hh.source_selected(
            {"name": "Atlassian Early Careers", "fallback_for": "Atlassian"},
            ["atlassian"]))
        config = {
            "ats_companies": [{"name": "Goldman Sachs Engineering", "aliases": ["Goldman Sachs"]}],
            "custom_pages": [{"name": "Program monitor", "supplement_for": "Goldman Sachs Engineering"}],
        }
        expanded = hh.resolve_company_filters(config, ["goldman sachs"])
        self.assertTrue(hh.source_selected(config["custom_pages"][0], expanded))
        with self.assertRaisesRegex(ValueError, "unknown"):
            hh.resolve_company_filters(config, ["missing company"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
