"""Fetchers against saved-style API responses (all network calls mocked)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import io
import json
import re
import unittest
import urllib.error
from unittest import mock

import sources
from common import HOME, parse_salary

FX = {"USD": 83.0}


def ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


def cfg():
    return {"pack": sources.net.pack(), "currency": "INR", "max_age_days": 14}


class BoardFetchersTest(unittest.TestCase):
    def fetch(self, fn, payload, company=None, **kw):
        with mock.patch.object(sources.net, "get_json", return_value=payload) as m:
            out = fn(company or {"name": "Acme", "token": "acme"}, **kw)
        return out, m.call_args[0][0]

    def test_greenhouse(self):
        out, url = self.fetch(sources.fetch_greenhouse, {"jobs": [{
            "id": 101, "title": "Backend Engineer", "location": {"name": "Bengaluru, India"},
            "offices": [{"name": "Bengaluru"}, {"name": "Remote"}], "first_published": "2026-09-20T10:00:00-04:00",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101", "content": "&lt;p&gt;Build &lt;b&gt;APIs&lt;/b&gt;&lt;/p&gt;"}]})
        self.assertIn("boards/acme/jobs", url)
        self.assertEqual(out, [{
            "source": "greenhouse", "source_id": "101", "title": "Backend Engineer", "company": "Acme",
            "location": "Bengaluru, India", "remote": None, "region_text": "Bengaluru, Remote", "eligible_countries": "",
            "posted": "2026-09-20", "salary_text": "", "url": "https://boards.greenhouse.io/acme/jobs/101",
            "description": "Build APIs"}])

    def test_lever(self):
        posting = {"id": "lv1", "text": "Platform Engineer", "categories": {"allLocations": ["Bengaluru", "Remote"]},
                   "workplaceType": "remote", "country": "IN", "createdAt": 1758326400000,
                   "salaryRange": {"min": 3000000, "max": 4000000, "currency": "INR", "interval": "year"},
                   "hostedUrl": "https://jobs.lever.co/acme/lv1", "descriptionPlain": "Own the platform.",
                   "lists": [{"text": "Requirements", "content": "<li>Java</li>"}], "additionalPlain": "Apply today."}
        out, url = self.fetch(sources.fetch_lever, [posting])
        r = out[0]
        self.assertIn("api.lever.co/v0/postings/acme", url)
        self.assertEqual((r["location"], r["remote"], r["posted"]), ("Bengaluru; Remote", True, "2025-09-20"))
        self.assertEqual(r["job_type"], "")
        posting["categories"]["commitment"] = "Contract"
        self.assertEqual(self.fetch(sources.fetch_lever, [posting])[0][0]["job_type"], "Contract")
        self.assertEqual(parse_salary(r["salary_text"], FX), (3_000_000, 4_000_000))
        for part in ("Own the platform.", "Requirements", "- Java", "Apply today."):
            self.assertIn(part, r["description"])

    def test_lever_regions_and_workplace_types(self):
        hybrid = {"id": "a", "text": "T", "categories": {"location": "Pune"}, "workplaceType": "hybrid"}
        plain = {"id": "b", "text": "T", "categories": {"location": "Pune"}}
        out, url = self.fetch(sources.fetch_lever, [hybrid, plain], {"name": "Acme", "token": "acme", "region": "eu"})
        self.assertIn("api.eu.lever.co", url)
        self.assertEqual([r["remote"] for r in out], [False, None])
        self.assertEqual(out[0]["location"], "Pune")

    def test_ashby_skips_unlisted_and_reads_salary(self):
        out, _ = self.fetch(sources.fetch_ashby, {"jobs": [
            {"id": "a1", "title": "Security Engineer", "location": "Remote - India", "secondaryLocations": [{"location": "Pune"}],
             "isRemote": True, "publishedAt": "2026-09-25T00:00:00.000+00:00", "jobUrl": "https://jobs.ashbyhq.com/acme/a1",
             "descriptionPlain": "Secure things.", "address": {"postalAddress": {"addressCountry": "India"}},
             "compensation": {"scrapeableCompensationSalarySummary": "₹30L - ₹40L"}},
            {"id": "a2", "title": "Hidden", "isListed": False}]})
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual((r["location"], r["remote"], r["region_text"], r["posted"]),
                         ("Remote - India; Pune", True, "India", "2026-09-25"))
        self.assertEqual(r["job_type"], "")
        ct, _ = self.fetch(sources.fetch_ashby, {"jobs": [{"id": "a3", "title": "T", "employmentType": "Contract"}]})
        self.assertEqual(ct[0]["job_type"], "Contract")
        self.assertEqual(parse_salary(r["salary_text"], FX), (3_000_000, 4_000_000))

    def test_workable(self):
        out, _ = self.fetch(sources.fetch_workable, {"jobs": [
            {"shortcode": "W1", "title": "Java Developer", "telecommuting": True, "locations": [{"city": "Pune", "country": "India"}],
             "published_on": "2026-09-24", "url": "https://apply.workable.com/acme/j/W1/", "description": "<p>Hi</p>"},
            {"shortcode": "W2", "title": "QA", "telecommuting": False, "city": "Delhi", "country": "India"}]})
        self.assertEqual((out[0]["location"], out[0]["remote"], out[0]["description"]), ("Remote; Pune, India", True, "Hi"))
        self.assertEqual((out[1]["location"], out[1]["remote"]), ("Delhi, India", False))
        ct, _ = self.fetch(sources.fetch_workable, {"jobs": [{"shortcode": "W3", "title": "T", "employment_type": "Contract"}]})
        self.assertEqual((out[0]["job_type"], ct[0]["job_type"]), ("", "Contract"))

    def test_smartrecruiters_paginates_and_fetches_description_lazily(self):
        def page(i):
            return {"totalFound": 101, "content": [{"id": f"p{i}", "name": f"Engineer {i}", "releasedDate": "2026-09-20T00:00:00Z",
                                                     "ref": f"https://api.smartrecruiters.com/v1/companies/acme/postings/p{i}",
                                                     "location": {"remote": i == 1, "fullLocation": "Pune, India", "country": "in"}}]}
        with mock.patch.object(sources.net, "get_json", side_effect=[page(0), page(1)]) as m:
            out = sources.fetch_smartrecruiters({"name": "Acme", "token": "acme"})
        self.assertEqual([c[0][0].rsplit("offset=", 1)[1] for c in m.call_args_list], ["0", "100"])
        self.assertEqual([(r["title"], r["remote"], r["location"]) for r in out],
                         [("Engineer 0", False, "Pune, India"), ("Engineer 1", True, "Remote; Pune, India")])
        self.assertEqual(out[0]["description"], "")
        self.assertEqual(out[0]["job_type"], "")
        with mock.patch.object(sources.net, "get_json", return_value={"totalFound": 1, "content": [
                {"id": "c1", "name": "T", "typeOfEmployment": {"id": "contract", "label": "Contract"}}]}):
            self.assertEqual(sources.fetch_smartrecruiters({"name": "Acme", "token": "acme"})[0]["job_type"], "contract Contract")
        detail = {"jobAd": {"sections": {"jobDescription": {"title": "Job Description", "text": "<p>Do things</p>"},
                                         "qualifications": {"title": "Qualifications", "text": "<ul><li>Java</li></ul>"}}}}
        with mock.patch.object(sources.net, "get_json", return_value=detail):
            text = sources.smartrecruiters_description(out[0]["detail_url"])
        self.assertIn("Job Description\nDo things", text)
        self.assertIn("Qualifications\n- Java", text)


class WorkdayDarwinboxTest(unittest.TestCase):
    CO = {"name": "Litware", "token": "litware", "host": "litware.wd3.myworkdayjobs.com", "site": "Careers"}

    def test_country_facet_variants(self):
        f = sources._workday_country_facet
        self.assertEqual(f([{"facetParameter": "locationCountry",
                             "values": [{"id": "c1", "descriptor": "India"}, {"id": "c2", "descriptor": "Germany"}]}], "india"),
                         {"locationCountry": ["c1"]})
        self.assertEqual(f([{"facetParameter": "locations", "values": [
            {"id": "l1", "descriptor": "Gurgaon, India"}, {"id": "l2", "descriptor": "Paris, France"}]}], "india"),
            {"locations": ["l1"]})
        nested = [{"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locations", "values": [{"id": "l9", "descriptor": "Hyderabad"}]}]}]
        self.assertEqual(f(nested, "india"), {"locations": ["l9"]})
        self.assertEqual(f([{"facetParameter": "a", "values": [{"id": "x", "descriptor": "India"}]}], "india"), {"a": ["x"]})
        self.assertIsNone(f([{"facetParameter": "jobFamily", "values": [{"id": "1", "descriptor": "Sales"}]}], "india"))
        self.assertIsNone(f(None, "india"))

    def posting(self, n, loc="2 Locations"):
        return {"title": f"Application Security Engineer {n}", "externalPath": f"/job/Gurgaon/AppSec_R{n}",
                "locationsText": loc, "postedOn": "Posted 3 Days Ago"}

    def test_fetch_with_country_facet(self):
        bodies = []

        def post(url, body, headers=None):
            bodies.append(body)
            if not body["appliedFacets"]:
                return {"total": 2, "facets": [{"facetParameter": "locationCountry",
                                                "values": [{"id": "c1", "descriptor": "India"}]}], "jobPostings": []}
            return {"total": 1, "jobPostings": [self.posting(1), self.posting(2, "Remote, India")]}
        with mock.patch.object(sources.net, "post_json", side_effect=post):
            out = sources.fetch_workday(self.CO)
        self.assertEqual(bodies[1]["appliedFacets"], {"locationCountry": ["c1"]})
        a, b = out
        self.assertEqual((a["location"], a["region_text"], a["posted"], a["remote"]),
                         ("India (2 Locations)", "India", ago(3), None))
        self.assertEqual(b["remote"], True)
        self.assertEqual(a["url"], "https://litware.wd3.myworkdayjobs.com/Careers/job/Gurgaon/AppSec_R1")
        self.assertEqual(a["detail_url"], "https://litware.wd3.myworkdayjobs.com/wday/cxs/litware/Careers/job/Gurgaon/AppSec_R1")

    def test_fetch_without_facet_reuses_first_page(self):
        with mock.patch.object(sources.net, "post_json",
                               return_value={"total": 1, "facets": [], "jobPostings": [self.posting(1, "Pune, India")]}) as m:
            out = sources.fetch_workday(self.CO)
        self.assertEqual(m.call_count, 1)
        self.assertEqual((out[0]["location"], out[0]["region_text"]), ("Pune, India", ""))

    def test_workday_description(self):
        with mock.patch.object(sources.net, "get_json", return_value={"jobPostingInfo": {"jobDescription": "<p>Secure the code</p>"}}):
            self.assertEqual(sources.workday_description("https://x/y"), "Secure the code")

    def test_darwinbox_paginates(self):
        def row(i):
            return {"id": f"d{i}", "title": f"Java Developer {i}", "locations": "Gurugram\r", "is_remote": 0,
                    "experience": "3-5 years", "jd": "<p>Build</p>", "posted_on": "2026-09-22", "country": "India"}
        calls = []

        def post(url, body, headers=None):
            calls.append((url, body["page"], headers["Origin"]))
            return {"data": [row(body["page"])], "job_counts": 2}
        with mock.patch.object(sources.net, "post_json", side_effect=post):
            out = sources.fetch_darwinbox({"name": "Proseware", "token": "proseware"})
        self.assertEqual([c[1] for c in calls], [1, 2])
        self.assertIn("proseware.darwinbox.in", calls[0][0])
        self.assertEqual(calls[0][2], "https://proseware.darwinbox.in")
        r = out[0]
        self.assertEqual((r["location"], r["region_text"], r["posted"]), ("Gurugram", "India", "2026-09-22"))
        self.assertEqual(r["description"], "Experience: 3-5 years\n\nBuild")
        self.assertTrue(r["url"].endswith("/careers/jobDetails/d1"))


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Acme: Senior Backend Engineer</title><guid>g1</guid><link>https://weworkremotely.com/jobs/1</link>
<region>Anywhere in the World</region><country>India</country><pubDate>Fri, 25 Sep 2026 08:00:00 +0000</pubDate>
<expires_at>2026-10-25</expires_at><description><![CDATA[<p>URL: https://acme.example</p><p>Build things</p>]]></description></item>
<item><title>Backend Engineer</title><link>https://weworkremotely.com/jobs/2</link><description></description></item>
</channel></rss>"""


class FeedFetchersTest(unittest.TestCase):
    def test_wwr(self):
        with mock.patch.object(sources.net, "get", return_value=RSS) as m:
            out = sources.fetch_wwr("remote-jobs")
        self.assertTrue(m.call_args[0][0].endswith("/remote-jobs.rss"))
        a, b = out
        self.assertEqual((a["company"], a["title"], a["source_id"], a["posted"], a["expires"]),
                         ("Acme", "Senior Backend Engineer", "g1", "2026-09-25", "2026-10-25"))
        self.assertEqual((a["region_text"], a["eligible_countries"], a["remote"]), ("Anywhere in the World", "India", True))
        self.assertIn("URL: https://acme.example", a["description"])
        self.assertEqual((b["company"], b["title"], b["source_id"]), ("", "Backend Engineer", "https://weworkremotely.com/jobs/2"))

    def test_remoteok_skips_legal_notice(self):
        payload = [{"legal": "credit Remote OK"},
                   {"id": 1, "position": "Backend Eng", "company": "X", "location": "Worldwide", "date": "2026-09-25T00:00:00+00:00",
                    "salary_min": 100000, "salary_max": 140000, "url": "https://remoteok.com/1", "apply_url": "https://x.example/apply",
                    "description": "<p>Hi</p>"},
                   {"id": 2, "position": "No salary", "company": "Y", "salary_min": 0}]
        with mock.patch.object(sources.net, "get_json", return_value=payload):
            out = sources.fetch_remoteok()
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["salary_text"], out[0]["apply_url"], out[0]["region_text"]),
                         ("USD 100000 - 140000 per year", "https://x.example/apply", "Worldwide"))
        self.assertEqual(out[1]["salary_text"], "")
        with mock.patch.object(sources.net, "get_json", return_value={"error": "rate limited"}):
            self.assertEqual(sources.fetch_remoteok(), [])

    def test_alignerr_prefers_country_copy_and_maps_pay(self):
        def get(url):
            self.assertIn("search=software%20engineer", url)
            return {"total": 3, "jobs": [
                {"id": "j2", "title": "AI Trainer", "description": "US only", "pay": "$30/hr"},
                {"id": "j1", "title": "AI Trainer", "description": "Open to applicants in India", "pay": "$40/hr"},
                {"id": "j3", "title": "Data Rater", "description": "Worldwide", "pay": "$20/hr"}]}
        with mock.patch.object(sources.net, "get_json", side_effect=get):
            out = sources.fetch_alignerr({**cfg(), "alignerr_searches": ["software engineer"]})
        trainer, rater = out
        self.assertEqual((trainer["source_id"], trainer["location"], trainer["region_text"], trainer["salary_text"]),
                         ("j1", "Remote - India", "India", "USD 40 per hour"))
        self.assertEqual((rater["location"], rater["region_text"]), ("Remote", ""))
        self.assertEqual({trainer["job_type"], rater["job_type"]}, {"Contract"})
        self.assertEqual(rater["detail_url"], "https://www.alignerr.com/jobs/j3")

    def test_alignerr_description(self):
        data = {"props": {"pageProps": {"job": {"jobType": "Contract", "salaryType": "hourly", "location": "Remote",
                                                "firstPostDate": "2026-09-01T00:00:00Z", "htmlLongDescription": "<p>Label data</p>"}}}}
        page = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'.encode()
        with mock.patch.object(sources.net, "get", return_value=page):
            text = sources.alignerr_description("https://www.alignerr.com/jobs/j1")
        self.assertIn("Engagement: Contract (hourly)", text)
        self.assertIn("First posted: 2026-09-01", text)
        self.assertTrue(text.endswith("Label data"))
        with mock.patch.object(sources.net, "get", return_value=b"<html>no data</html>"):
            self.assertIn("Engagement:", sources.alignerr_description("https://x"))


class AdzunaTest(unittest.TestCase):
    KEY = HOME / ".secrets" / "adzuna.json"
    C = {"adzuna_searches": ["Java Developer"], "adzuna_locations": ["Gurugram"], "adzuna_remote_query": True,
         "adzuna_max_requests": 5}

    def setUp(self):
        self.KEY.parent.mkdir(exist_ok=True)
        self.KEY.write_text(json.dumps({"app_id": "id", "app_key": "key"}))

    def tearDown(self):
        self.KEY.unlink(missing_ok=True)

    def run_fetch(self, get, **extra):
        with mock.patch.object(sources.net, "get_json", side_effect=get) as m, mock.patch("time.sleep"):
            return sources.fetch_adzuna({**cfg(), **self.C, **extra}), m

    def test_searches_records_and_predicted_salary(self):
        result = {"id": 1, "title": "Java Developer", "company": {"display_name": "Acme"},
                  "location": {"display_name": "Gurgaon, Haryana"}, "description": "Work remote from home.",
                  "salary_min": 2000000, "salary_max": 2500000, "salary_is_predicted": "0",
                  "created": "2026-09-25T10:00:00Z", "redirect_url": "https://www.adzuna.in/land/ad/1"}
        out, m = self.run_fetch(lambda url: {"results": [
            {**result, "contract_type": "contract", "contract_time": "full_time"},
            {**result, "id": 2, "salary_is_predicted": "1"}]})
        urls = [c[0][0] for c in m.call_args_list]
        self.assertEqual(len(urls), 2)
        self.assertIn("/jobs/in/search/1", urls[0])
        self.assertIn("where=Gurgaon", urls[0])  # pack city spelling: Adzuna doesn't know 'Gurugram'
        self.assertIn("what=java+developer+remote", urls[1])
        self.assertNotIn("where=", urls[1])
        r = out[0]
        self.assertEqual((r["source_id"], r["location"], r["remote"], r["posted"]),
                         ("1", "Remote; Gurgaon, Haryana", True, "2026-09-25"))
        self.assertEqual(parse_salary(r["salary_text"], FX), (2_000_000, 2_500_000))
        self.assertEqual(r["detail_url"], "https://www.adzuna.in/details/1")
        self.assertEqual(out[1]["salary_text"], "")
        self.assertEqual((r["job_type"], out[1]["job_type"]), ("contract full_time", ""))

    def test_queries_default_to_profile_search_terms(self):
        profiles = {"a": {"search_queries": ["Java Developer", "Spring Boot Engineer"], "target_titles": ["x"]},
                    "b": {"target_titles": ["Security Engineer", "AppSec Engineer", "Third Title"]}}
        with mock.patch("common.load_profiles", return_value=profiles):
            _, m = self.run_fetch(lambda url: {"results": []}, adzuna_searches=[], adzuna_max_requests=50,
                                  adzuna_remote_query=False)
        whats = {re.search(r"what=([^&]+)", c[0][0]).group(1) for c in m.call_args_list}
        self.assertEqual(whats, {"java+developer", "spring+boot+engineer", "security+engineer", "appsec+engineer"})

    def test_request_cap(self):
        _, m = self.run_fetch(lambda url: {"results": []}, adzuna_max_requests=1)
        self.assertEqual(m.call_count, 1)

    def test_retries_5xx_but_not_4xx(self):
        err = lambda code: urllib.error.HTTPError("u", code, "e", {}, io.BytesIO())  # noqa: E731
        out, m = self.run_fetch([err(503), {"results": []}, {"results": []}])
        self.assertEqual((out, m.call_count), ([], 3))
        with self.assertRaises(urllib.error.HTTPError):
            self.run_fetch([err(429)])

    def test_missing_key(self):
        self.KEY.unlink()
        with self.assertRaisesRegex(RuntimeError, "no key"):
            sources.fetch_adzuna({**cfg(), **self.C})


if __name__ == "__main__":
    unittest.main()
