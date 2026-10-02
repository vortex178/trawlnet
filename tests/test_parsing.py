import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import unittest
from unittest import mock

import sources
from common import html_to_text, job_key, norm_company, parse_date, parse_salary

FX = {"USD": 83.0, "EUR": 90.0}


def ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


class SalaryTest(unittest.TestCase):
    def test_inr_rupee_range(self):
        self.assertEqual(parse_salary("₹12,00,000 - ₹18,00,000 a year", FX), (1_200_000, 1_800_000))

    def test_lpa(self):
        self.assertEqual(parse_salary("12-18 LPA", FX), (1_200_000, 1_800_000))
        self.assertEqual(parse_salary("INR 25 lakhs", FX), (2_500_000, 2_500_000))

    def test_usd_converted(self):
        self.assertEqual(parse_salary("$257K – $335K", FX), (257_000 * 83, 335_000 * 83))

    def test_hourly_annualised(self):
        self.assertEqual(parse_salary("$50 - $70 an hour", FX), (50 * 2080 * 83, 70 * 2080 * 83))

    def test_monthly(self):
        self.assertEqual(parse_salary("₹1,50,000 per month", FX), (1_800_000, 1_800_000))

    def test_local_currency_other_than_inr(self):
        self.assertEqual(parse_salary("$120,000 - $150,000", {}, "USD"), (120_000, 150_000))

    def test_unparseable(self):
        for text in (None, "", "N/A", "Not disclosed", "Competitive", "£40k"):  # GBP rate missing -> None
            self.assertIsNone(parse_salary(text, FX), text)


class DateTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("2026-09-11T10:00:00Z"), "2026-09-11")
        self.assertEqual(parse_date("September 11, 2026"), "2026-09-11")
        self.assertEqual(parse_date("Fri, 11 Sep 2026 08:00:00 +0000"), "2026-09-11")
        self.assertEqual(parse_date(1756684800000), "2025-09-01")  # epoch ms
        self.assertEqual(parse_date(1756684800), "2025-09-01")     # epoch s
        self.assertIsNone(parse_date("N/A"))
        self.assertIsNone(parse_date("soon"))

    def test_workday_relative(self):
        self.assertEqual(sources._workday_posted("Posted Today"), ago(0))
        self.assertEqual(sources._workday_posted("Posted Yesterday"), ago(1))
        self.assertEqual(sources._workday_posted("Posted 3 Days Ago"), ago(3))
        self.assertEqual(sources._workday_posted("Posted 30+ Days Ago"), ago(31))
        self.assertIsNone(sources._workday_posted(""))


class NormalizeTest(unittest.TestCase):
    def test_company_suffixes(self):
        self.assertEqual(norm_company("Northwind Payments Pvt. Ltd."), "northwind payments")
        self.assertEqual(norm_company("Contoso Technologies India"), "contoso")

    def test_job_key_ignores_parenthetical_and_case(self):
        self.assertEqual(job_key("Contoso Inc", "Backend Engineer (IND, Hybrid)", "bengaluru"),
                         job_key("contoso", "backend engineer", "bengaluru"))
        self.assertNotEqual(job_key("contoso", "backend engineer", "bengaluru"),
                            job_key("contoso", "backend engineer", "remote"))

    def test_html_to_text(self):
        self.assertEqual(html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>a</li></ul>"), "Hello world\n\n- a")


class HNTest(unittest.TestCase):
    def _run(self, comments):
        story = {"hits": [{"title": "Ask HN: Who is hiring? (September 2026)", "objectID": "1",
                           "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}]}
        item = {"children": [{"id": i, "text": t, "created_at": story["hits"][0]["created_at"]}
                             for i, t in enumerate(comments, 100)]}
        cfg = {"pack": {"country_places": ["india", "bengaluru", "bangalore"]}}
        with mock.patch.object(sources.net, "get_json", side_effect=[story, item]):
            return sources.fetch_hn(cfg)

    def test_header_parsing(self):
        out = self._run([
            "Northwind Payments | Backend Engineer, Security Engineer | Remote (India) | Full-time<p>We build ledgers.",
            "REMOTE | Contoso | Senior Engineer",                        # location in the company slot: skipped
            "Fabrikam | We are looking for great people to join us | Remote",  # sentence, not a role: skipped
            "Tailspin Labs | Platform Engineer | Bengaluru, India",
        ])
        got = [(r["company"], r["title"], r["location"], r["remote"]) for r in out]
        self.assertEqual(got, [
            ("Northwind Payments", "Backend Engineer", "Remote (India)", True),
            ("Northwind Payments", "Security Engineer", "Remote (India)", True),
            ("Tailspin Labs", "Platform Engineer", "Bengaluru, India", False),
        ])


class CustomLinksTest(unittest.TestCase):
    PAGE = "https://careers.adventure-works.example/openings"

    def test_job_links(self):
        pairs = [("Senior Software Engineer\nBengaluru, India", "/openings/42"),
                 ("Apply now", "/openings/43"),                      # generic text only
                 ("About us", "/about"),                             # not a job path
                 ("Backend Developer", "https://jobs.lever.co/adventure/abc-123"),  # ATS host
                 ("Senior Software Engineer\nBengaluru, India", "/openings/42")]    # duplicate
        self.assertEqual(sources._job_links(pairs, self.PAGE), [
            ("Senior Software Engineer", "https://careers.adventure-works.example/openings/42", "Bengaluru, India"),
            ("Backend Developer", "https://jobs.lever.co/adventure/abc-123", ""),
        ])

    def test_text_titles(self):
        md = "# Careers\nWe are hiring!\n- Site Reliability Engineer\n- Our team of engineers is great.\n### Data Analyst"
        self.assertEqual([t for t, _, _ in sources._text_titles(md, self.PAGE)],
                         ["Site Reliability Engineer", "Data Analyst"])


if __name__ == "__main__":
    unittest.main()
