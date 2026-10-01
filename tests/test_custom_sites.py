"""Custom career pages, description enrichment/lazy fetching, and fetch_all orchestration (network mocked)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import io
import unittest
import urllib.error
from unittest import mock

import sources

PAGE = "https://careers.adventure-works.example/openings"
CO = {"name": "Adventure Works", "ats": "custom", "token": "aw", "careers_url": PAGE}
JD = ("Responsibilities: build services. Requirements: 3 years of experience with Java. You will own APIs. "
      "Qualifications include Kafka. ") * 8


class FakeBudget:
    def __init__(self, pages=None, left=10):
        self.pages, self._left, self.scraped, self.enabled = pages or {}, left, [], True

    def left(self):
        return self._left

    def scrape_markdown(self, url):
        self.scraped.append(url)
        self._left -= 1
        return self.pages.get(url)


class CustomFreeTest(unittest.TestCase):
    def test_cards_prefer_heading_and_skip_nav(self):
        html = b"""<nav><a href="/about">About us</a><a href="/openings">All openings</a></nav>
        <a href="/openings/42"><h3>Senior Software Engineer</h3><p>Bengaluru, India - full time role</p></a>
        <a href="https://jobs.lever.co/adventure/abc-123">Backend Developer</a>"""
        with mock.patch.object(sources, "_get", return_value=html):
            out = sources.fetch_custom_free(CO)
        self.assertEqual([(r["title"], r["url"]) for r in out], [
            ("Senior Software Engineer", "https://careers.adventure-works.example/openings/42"),
            ("Backend Developer", "https://jobs.lever.co/adventure/abc-123")])
        self.assertEqual(out[0]["source"], "custom")
        self.assertEqual(out[0]["detail_url"], out[0]["url"])
        self.assertEqual(out[0]["description"], "")

    def test_same_page_url_keeps_titles_distinct(self):
        recs = sources._custom_records(CO, [("Backend Engineer", PAGE, ""), ("SRE", PAGE, "")])
        self.assertEqual([r["source_id"] for r in recs], [f"{PAGE}#Backend Engineer", f"{PAGE}#SRE"])


class CustomFirecrawlTest(unittest.TestCase):
    def test_direct_job_links(self):
        b = FakeBudget({PAGE: "[Senior Software Engineer](/openings/42)\n[About](/about)"})
        out = sources.fetch_custom_firecrawl(dict(CO), b)
        self.assertEqual([r["title"] for r in out], ["Senior Software Engineer"])
        self.assertEqual(len(b.scraped), 1)

    def test_landing_page_follows_openings_link_and_remembers_it(self):
        nxt = "https://careers.adventure-works.example/careers/open-roles"
        b = FakeBudget({PAGE: "Join us! [View all openings](/careers/open-roles)", nxt: "[Backend Engineer](/jobs/701)"})
        co = dict(CO)
        out = sources.fetch_custom_firecrawl(co, b)
        self.assertEqual([r["title"] for r in out], ["Backend Engineer"])
        self.assertEqual(co["careers_url"], nxt)
        self.assertEqual(b.scraped, [PAGE, nxt])

    def test_openings_page_without_links_falls_back_to_its_text_titles(self):
        nxt = "https://careers.adventure-works.example/careers/open-roles"
        b = FakeBudget({PAGE: "[View all openings](/careers/open-roles)", nxt: "- Site Reliability Engineer\n- Data Analyst"})
        co = dict(CO)
        out = sources.fetch_custom_firecrawl(co, b)
        self.assertEqual([(r["title"], r["url"]) for r in out],
                         [("Site Reliability Engineer", nxt), ("Data Analyst", nxt)])
        self.assertEqual(co["careers_url"], PAGE)  # no job links found: the landing URL is kept

    def test_no_second_scrape_without_budget(self):
        b = FakeBudget({PAGE: "[View all openings](/careers/open-roles)"}, left=1)
        self.assertEqual(sources.fetch_custom_firecrawl(dict(CO), b), [])
        self.assertEqual(len(b.scraped), 1)

    def test_text_titles_fallback_and_empty_page(self):
        b = FakeBudget({PAGE: "# Careers\n- Site Reliability Engineer\n- Data Analyst\nWe are a great team."})
        out = sources.fetch_custom_firecrawl(dict(CO), b)
        self.assertEqual([r["title"] for r in out], ["Site Reliability Engineer", "Data Analyst"])
        self.assertEqual(sources.fetch_custom_firecrawl(dict(CO), FakeBudget({})), [])


class DescriptionTest(unittest.TestCase):
    def test_custom_description_free_when_it_looks_like_a_jd(self):
        b = FakeBudget()
        with mock.patch.object(sources, "_get", return_value=f"<p>{JD}</p>".encode()):
            self.assertIn("Requirements", sources.custom_description("https://x.example/j/1", b))
        self.assertEqual(b.scraped, [])

    def test_custom_description_falls_back_to_firecrawl(self):
        b = FakeBudget({"https://x.example/j/1": "# Full JD\n" + JD})
        with mock.patch.object(sources, "_get", return_value=b"<html><nav>Home</nav></html>"):
            self.assertTrue(sources.custom_description("https://x.example/j/1", b).startswith("# Full JD"))
        with mock.patch.object(sources, "_get", side_effect=OSError("boom")):
            self.assertEqual(sources.custom_description("https://x.example/j/2", None), "")

    def test_custom_description_never_returns_page_chrome(self):
        nav = b"<html><style>.a{color:red}</style><nav>Home About Careers</nav></html>"
        with mock.patch.object(sources, "_get", return_value=nav):
            self.assertEqual(sources.custom_description("https://x.example/j/3", FakeBudget()), "")  # no Firecrawl text
            b = FakeBudget({"https://x.example/j/3": "# Careers\nSee all openings"})
            self.assertEqual(sources.custom_description("https://x.example/j/3", b), "")  # Firecrawl got a listing page

    def test_atlassian_description_from_listings_endpoint(self):
        sources._atlassian_cache.clear()
        listing = [{"id": 27432, "overview": "<p>Build things.</p>", "responsibilities": "<ul><li>Own services</li></ul>",
                    "qualifications": "<p>5 years</p>"}, {"id": 1}]
        url = "https://www.atlassian.com/company/careers/details/27432"
        on, off = {"sources": {"undocumented_ats": True}}, {"sources": {}}
        with mock.patch.object(sources, "_get_json", return_value=listing) as g:
            with mock.patch.object(sources, "_cfg", return_value=off):
                self.assertEqual(sources.atlassian_description(url), "")  # opt-in only
            g.assert_not_called()
            with mock.patch.object(sources, "_cfg", return_value=on):
                text = sources.atlassian_description(url)
                self.assertIn("Own services", text)
                self.assertEqual(sources.atlassian_description("https://www.atlassian.com/company/careers/details/999"), "")
                self.assertEqual(sources.atlassian_description("https://x.example/j/1"), "")
            self.assertEqual(g.call_count, 1)  # one request covers every job in the run
        sources._atlassian_cache.clear()

    def test_custom_description_uses_atlassian_then_falls_back(self):
        url = "https://www.atlassian.com/company/careers/details/5"
        with mock.patch.object(sources, "atlassian_description", return_value=JD), \
                mock.patch.object(sources, "_get") as g:
            self.assertEqual(sources.custom_description(url, FakeBudget()), JD)
            g.assert_not_called()
        with mock.patch.object(sources, "atlassian_description", return_value="Short but structured"):
            self.assertEqual(sources.custom_description(url, FakeBudget()), "Short but structured")  # resolver text is trusted
        with mock.patch.object(sources, "atlassian_description", side_effect=OSError("down")), \
                mock.patch.object(sources, "_get", return_value=f"<p>{JD}</p>".encode()):
            self.assertIn("Requirements", sources.custom_description(url, FakeBudget()))

    def test_workable_description_by_account_and_shortlink(self):
        jobs = [{"source_id": "ABC123", "description": "Workable JD"}, {"source_id": "ZZZ", "description": "other"}]
        with mock.patch.object(sources, "fetch_workable", return_value=jobs) as fw:
            self.assertEqual(sources.workable_description("https://apply.workable.com/acme-co/j/ABC123/"), "Workable JD")
            fw.assert_called_with({"name": "", "token": "acme-co"})
            self.assertEqual(sources.workable_description("https://apply.workable.com/acme-co/j/MISSING"), "")
            self.assertEqual(sources.workable_description("https://x.example/j/ABC123"), "")

            class Resp:
                def __init__(self, url): self.url = url
                def geturl(self): return self.url
                def __enter__(self): return self
                def __exit__(self, *a): return False
            with mock.patch.object(sources.urllib.request, "urlopen", return_value=Resp("https://apply.workable.com/acme-co/j/ABC123")):
                self.assertEqual(sources.workable_description("https://apply.workable.com/j/ABC123"), "Workable JD")
            err = urllib.error.HTTPError("https://apply.workable.com/acme-co/j/ABC123", 403, "blocked", {}, io.BytesIO(b""))
            with mock.patch.object(sources.urllib.request, "urlopen", side_effect=err):
                self.assertEqual(sources.workable_description("https://apply.workable.com/j/ABC123"), "Workable JD")
            with mock.patch.object(sources.urllib.request, "urlopen", return_value=Resp("https://apply.workable.com/")):
                self.assertEqual(sources.workable_description("https://apply.workable.com/j/ABC123"), "")  # no account found

    def test_ats_job_ignores_workable_shortlink_as_account(self):
        with mock.patch.dict(sources.ATS_FETCHERS, {"workable": mock.Mock(side_effect=AssertionError("j is not an account"))}):
            self.assertIsNone(sources._ats_job("https://apply.workable.com/j/ABC123", "Backend Engineer", "Acme"))

    def test_title_in(self):
        self.assertTrue(sources._title_in("Senior Backend Engineer (Remote)", "We hire a senior backend engineer"))
        self.assertTrue(sources._title_in("Backend Engineer", "backend and engineer skills"))
        self.assertFalse(sources._title_in("Backend Engineer", "Marketing manager wanted"))
        self.assertFalse(sources._title_in("", "anything"))

    def test_ats_job_lookup(self):
        url = "https://boards.greenhouse.io/acme/jobs/1"
        job = {"title": "Backend Engineer", "url": url, "description": "Full text"}
        with mock.patch.dict(sources.ATS_FETCHERS, {"greenhouse": lambda c: [job]}):
            self.assertEqual(sources._ats_job(url, "Backend Engineer", "Acme"), (url, "Full text"))
            self.assertIsNone(sources._ats_job(url, "Chef", "Acme"))
        with mock.patch.dict(sources.ATS_FETCHERS, {"greenhouse": mock.Mock(side_effect=OSError)}):
            self.assertIsNone(sources._ats_job(url, "Backend Engineer", "Acme"))
        self.assertIsNone(sources._ats_job("https://acme.example/careers/1", "Backend Engineer", "Acme"))

    def test_enrich_short_description(self):
        rec = {"title": "Backend Engineer", "company": "Acme", "description": "Short. Apply at https://acme.example/jobs/9."}
        self.assertEqual(sources.enrich_short_description({**rec, "description": "x" * 1500}), "x" * 1500)
        with mock.patch.object(sources, "_ats_job", return_value=None), \
                mock.patch.object(sources, "custom_description", return_value="Backend Engineer role. " + JD) as cd:
            out = sources.enrich_short_description(rec)
        cd.assert_called_once_with("https://acme.example/jobs/9", None)  # trailing '.' stripped
        self.assertIn("Full posting (https://acme.example/jobs/9)", out)
        skip = {**rec, "description": "See https://news.ycombinator.com/item?id=1 and https://linkedin.com/jobs/1 "
                                      "and https://acme.example/"}
        with mock.patch.object(sources, "_ats_job") as ats, mock.patch.object(sources, "custom_description") as cd:
            self.assertEqual(sources.enrich_short_description(skip), skip["description"])
        ats.assert_not_called()
        cd.assert_not_called()
        with mock.patch.object(sources, "_ats_job", return_value=("https://u", "ATS text")):
            self.assertIn("Full posting (https://u):\nATS text", sources.enrich_short_description(rec))
        with mock.patch.object(sources, "_ats_job", return_value=None), \
                mock.patch.object(sources, "custom_description", return_value="Unrelated cookie banner text"):
            self.assertEqual(sources.enrich_short_description(rec), rec["description"])

    def test_lazy_description_dispatch(self):
        for source, fn in [("smartrecruiters", "smartrecruiters_description"), ("workday", "workday_description"),
                           ("alignerr", "alignerr_description")]:
            with mock.patch.object(sources, fn, return_value="D") as m:
                self.assertEqual(sources.lazy_description({"source": source, "detail_url": "u"}), "D")
            m.assert_called_once_with("u")
        with mock.patch.object(sources, "custom_description", return_value=JD) as m:
            self.assertEqual(sources.lazy_description({"source": "custom", "detail_url": "u"}, "B"), JD)
        m.assert_called_once_with("u", "B")
        with mock.patch.object(sources, "custom_description", return_value="Java Developer at Acme"):
            self.assertTrue(sources.lazy_description({"source": "adzuna", "detail_url": "u", "title": "Java Developer"}))
            self.assertEqual(sources.lazy_description({"source": "adzuna", "detail_url": "u", "title": "Chef"}), "")
        self.assertEqual(sources.lazy_description({"source": "greenhouse"}), "")


class FetchAllTest(unittest.TestCase):
    def rec(self, source, n=1):
        return [{"source": source, "title": f"T{i}"} for i in range(n)]

    def setUp(self):
        self.calls = []

        def make(name, rows=1, fail=False):
            def fn(arg):
                self.calls.append((name, arg["name"] if isinstance(arg, dict) and "name" in arg else arg))
                if fail:
                    raise RuntimeError("boom")
                return self.rec(name, rows)
            return fn
        self.make = make

    def companies(self):
        return [{"name": "GH Co", "ats": "greenhouse", "token": "gh"},
                {"name": "WD Co", "ats": "workday", "token": "wd", "host": "h", "site": "s"},
                {"name": "Off Co", "ats": "lever", "token": "l", "active": False},
                {"name": "Bad Co", "ats": "ashby", "token": "bad"},
                {"name": "No Token", "ats": "workable"},
                {"name": "Custom Co", "ats": "custom", "careers_url": PAGE}]

    def run_all(self, sources_cfg, budget=None):
        cfg = {"sources": sources_cfg, "wwr_feeds": ["remote-jobs", "categories/x"]}
        fetchers = {"greenhouse": self.make("greenhouse"), "workday": self.make("workday"),
                    "lever": self.make("lever"), "ashby": self.make("ashby", fail=True),
                    "workable": self.make("workable")}
        with mock.patch.dict(sources.ATS_FETCHERS, fetchers), \
                mock.patch.object(sources, "fetch_wwr", self.make("wwr")), \
                mock.patch.object(sources, "fetch_remoteok", self.make("remoteok")), \
                mock.patch.object(sources, "fetch_hn", self.make("hn")), \
                mock.patch.object(sources, "fetch_alignerr", self.make("alignerr")), \
                mock.patch.object(sources, "fetch_adzuna", self.make("adzuna")), \
                mock.patch.object(sources, "fetch_custom_free", self.make("custom", rows=0)), \
                mock.patch.object(sources, "fetch_custom_firecrawl",
                                  lambda c, b: self.calls.append(("firecrawl", c["name"])) or self.rec("custom")):
            return sources.fetch_all(cfg, self.companies(), budget)

    def test_source_flags_and_undocumented_gate(self):
        recs, counts, errors = self.run_all({"wwr": True, "remoteok": True, "hn": True, "ats": True})
        self.assertEqual(sorted(counts), ["custom:Custom Co", "greenhouse:GH Co", "hn", "remoteok",
                                          "wwr:categories/x", "wwr:remote-jobs"])
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], r"^ashby:Bad Co: RuntimeError: boom")
        self.assertNotIn("workday", {r["source"] for r in recs})  # undocumented ATS is opt-in
        self.assertNotIn("lever", {r["source"] for r in recs})    # inactive

    def test_undocumented_alignerr_adzuna_opt_in(self):
        _, counts, _ = self.run_all({"alignerr": True, "adzuna": True, "ats": True, "undocumented_ats": True})
        self.assertEqual(sorted(counts), ["adzuna", "alignerr", "custom:Custom Co", "greenhouse:GH Co", "workday:WD Co"])

    def test_ats_off_skips_boards_and_customs(self):
        _, counts, _ = self.run_all({"wwr": False, "ats": False})
        self.assertEqual(counts, {})

    def test_firecrawl_fallback_order_reserve_and_marking(self):
        b = FakeBudget(left=8)
        cos = [{"name": "Old", "ats": "custom", "careers_url": PAGE, "last_firecrawl": "2026-01-01"},
               {"name": "Never", "ats": "custom", "careers_url": PAGE},
               {"name": "Recent", "ats": "custom", "careers_url": PAGE, "last_firecrawl": "2026-09-01"}]

        def spend(c, budget):
            self.calls.append(("firecrawl", c["name"]))
            budget._left -= 2
            return self.rec("custom")
        with mock.patch.object(sources, "fetch_custom_free", return_value=[]), \
                mock.patch.object(sources, "fetch_custom_firecrawl", spend):
            recs, counts, _ = sources.fetch_all({"sources": {"ats": True}, "firecrawl": {"shortlist_reserve": 5}}, cos, b)
        self.assertEqual([c for c in self.calls if c[0] == "firecrawl"], [("firecrawl", "Never"), ("firecrawl", "Old")])
        self.assertEqual(len(recs), 2)  # 8 -> 6 -> 4: stops at the shortlist reserve before 'Recent'
        self.assertEqual(cos[1]["last_firecrawl"], sources.dt.date.today().isoformat())
        self.assertEqual(cos[2]["last_firecrawl"], "2026-09-01")

    def test_disabled_budget_skips_firecrawl(self):
        b = FakeBudget()
        b.enabled = False
        self.run_all({"ats": True}, b)
        self.assertNotIn("firecrawl", [c[0] for c in self.calls])


class BoardHealthTest(unittest.TestCase):
    """fetch_all records ATS board outcomes: only 404/410 count, once per day, reset by a success."""

    def run_day(self, day, co, exc=None):
        err = exc or None

        def fetch(c):
            if err:
                raise err
            return []
        today = mock.Mock(wraps=dt.date)
        today.today.return_value = dt.date(2026, 9, day)
        with mock.patch.dict(sources.ATS_FETCHERS, {"lever": fetch}), mock.patch.object(sources.dt, "date", today):
            return sources.fetch_all({"sources": {"ats": True}}, [co])

    def http(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO())

    def test_gone_three_days_is_dead(self):
        co = {"name": "A", "ats": "lever", "token": "a"}
        for day in (1, 2):
            self.run_day(day, co, self.http(404))
            self.assertFalse(sources.is_dead(co))
        self.run_day(2, co, self.http(404))          # same day again: not counted twice
        self.assertEqual(co["gone_days"], 2)
        self.run_day(3, co, self.http(410))
        self.assertTrue(sources.is_dead(co))

    def test_transient_errors_do_not_count(self):
        co = {"name": "A", "ats": "lever", "token": "a"}
        for code in (429, 500, 503):
            self.run_day(1, co, self.http(code))
        self.run_day(2, co, TimeoutError("slow"))
        self.assertEqual(co.get("gone_days", 0), 0)

    def test_success_resets_and_stamps(self):
        co = {"name": "A", "ats": "lever", "token": "a", "gone_days": 2, "last_gone": "2026-09-02"}
        self.run_day(5, co)
        self.assertEqual((co["gone_days"], co["last_ok"]), (0, "2026-09-05"))
        self.assertNotIn("last_gone", co)

    def test_non_board_sources_untouched(self):
        co = {"name": "Site", "ats": "custom", "careers_url": "https://x.test/careers"}
        with mock.patch.object(sources, "fetch_custom_free", side_effect=self.http(404)):
            sources.fetch_all({"sources": {"ats": True}}, [co])
        self.assertNotIn("gone_days", co)


if __name__ == "__main__":
    unittest.main()
