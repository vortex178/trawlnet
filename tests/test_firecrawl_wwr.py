"""Firecrawl budget/scrape behaviour and WWR free-source verification (network mocked)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import io
import json
import unittest
import urllib.error
from unittest import mock

import db
import discover
import firecrawl
import wwr_verify
from common import HOME

KEY = HOME / ".secrets" / "fc-test.key"
CFG = {"firecrawl": {"enabled": True, "api_key_file": ".secrets/fc-test.key", "base_credits_per_run": 30,
                     "reserve_credits": 50, "min_seconds_between_requests": 0}}


def http_error(code, body=b""):
    return urllib.error.HTTPError("u", code, "e", {}, io.BytesIO(body))


def usage(remaining, days=9):
    end = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    return {"data": {"remainingCredits": remaining, "billingPeriodEnd": end + "T00:00:00Z"}}


class FirecrawlTest(unittest.TestCase):
    def setUp(self):
        KEY.parent.mkdir(exist_ok=True)
        KEY.write_text("fc-test-key\n")
        self.addCleanup(lambda: KEY.unlink(missing_ok=True))
        sleep = mock.patch.object(firecrawl.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def budget(self, date, usage_resp=None, cfg=None, **kw):
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=usage_resp or [usage(1000)]):
            return firecrawl.Budget(cfg or CFG, date)

    def test_dynamic_allowance_from_usage(self):
        b = self.budget("2001-01-01")
        self.assertTrue(b.enabled)
        self.assertEqual((b.allowance, b.start_remaining), (95, 1000))  # (1000-50)/10 days
        self.assertIn("remaining 1000", b.note)
        self.assertEqual(b.left(), 95)

    def test_allowance_never_dips_into_reserve(self):
        self.assertEqual(self.budget("2001-01-02", [usage(60)]).allowance, 10)
        self.assertEqual(self.budget("2001-01-03", [usage(20)]).allowance, 0)

    def test_v1_fallback_and_failures(self):
        b = self.budget("2001-01-04", [http_error(404), usage(500)])
        self.assertEqual(b.start_remaining, 500)
        b = self.budget("2001-01-05", [OSError("down")])
        self.assertEqual(b.allowance, 30)
        self.assertIn("unavailable", b.note)
        b = self.budget("2001-01-06", [{"data": {"remainingCredits": 500}}])
        self.assertIn("no billing period", b.note)
        self.assertEqual(b.allowance, 30)

    def test_fixed_budget_and_persistence(self):
        cfg = {"firecrawl": {**CFG["firecrawl"], "dynamic_budget": False}}
        b = firecrawl.Budget(cfg, "2001-01-07")
        self.assertEqual((b.allowance, b.note), (30, "fixed 30"))
        b.spent = 4
        b._save()
        again = firecrawl.Budget(cfg, "2001-01-07")  # same day: resumes from the ledger
        self.assertEqual((again.spent, again.allowance, again.left()), (4, 30, 26))

    def test_scrape_success_cache_and_budget(self):
        b = self.budget("2001-01-08")
        resp = {"data": {"markdown": "# Jobs", "metadata": {"creditsUsed": 2}}}
        with mock.patch.object(firecrawl.Budget, "_req", return_value=resp) as req:
            self.assertEqual(b.scrape_markdown("https://x.example/a"), "# Jobs")
            self.assertEqual(b.spent, 2)
            self.assertIn("# Jobs", b.scrape_markdown("https://x.example/a"))  # cached, no second request
            self.assertEqual(req.call_count, 1)
            self.assertEqual(json.loads(b.ledger.read_text())["spent"], 2)
            b.spent = b.allowance
            self.assertIsNone(b.scrape_markdown("https://x.example/b"))  # over budget
            self.assertEqual(req.call_count, 1)

    def test_rate_limit_retries_once_without_billing(self):
        b = self.budget("2001-01-09")
        ok = {"data": {"markdown": "ok"}}
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=[http_error(429, b"retry after 3s"), ok]):
            self.assertEqual(b.scrape_markdown("https://x.example/a"), "ok")
        self.sleep.assert_any_call(4)
        self.assertEqual(b.spent, 1)
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=[http_error(429), http_error(429)]):
            self.assertIsNone(b.scrape_markdown("https://x.example/b"))
        self.sleep.assert_any_call(15)
        self.assertEqual(b.spent, 1)  # rate-limited requests are free

    def test_other_failures_count_conservatively(self):
        b = self.budget("2001-01-10")
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=http_error(500)):
            self.assertIsNone(b.scrape_markdown("https://x.example/a"))
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=OSError("timeout")):
            self.assertIsNone(b.scrape_markdown("https://x.example/b"))
        self.assertEqual(b.spent, 2)

    def test_reconcile(self):
        b = self.budget("2001-01-11")
        b.spent = 99
        with mock.patch.object(firecrawl.Budget, "_req", return_value=usage(940)):
            b.reconcile()
        self.assertEqual(b.spent, 60)
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=OSError):
            b.reconcile()  # failure keeps the previous estimate
        self.assertEqual(b.spent, 60)
        with mock.patch.object(firecrawl.Budget, "_req", side_effect=[http_error(404), usage(900)]):
            b.reconcile()
        self.assertEqual(b.spent, 100)
        off = firecrawl.Budget({"firecrawl": {"enabled": False}}, "2001-01-12")
        off.reconcile()
        self.assertEqual(off.spent, 0)

    def test_summary(self):
        self.assertIn("firecrawl off", firecrawl.Budget({"firecrawl": {"enabled": False}}, "2001-01-13").summary())
        b = self.budget("2001-01-14")
        self.assertRegex(b.summary(), r"^firecrawl 0/95 credits \(remaining 1000")

    def test_req_builds_authorised_request(self):
        b = self.budget("2001-01-15")

        class Resp(io.BytesIO):
            __enter__ = lambda s: s  # noqa: E731
            __exit__ = lambda s, *a: None  # noqa: E731
        with mock.patch.object(firecrawl.urllib.request, "urlopen", return_value=Resp(b'{"ok": 1}')) as uo:
            self.assertEqual(b._req("POST", "/v2/scrape", {"url": "u"}), {"ok": 1})
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.firecrawl.dev/v2/scrape")
        self.assertEqual(req.get_header("Authorization"), "Bearer fc-test-key")
        self.assertEqual(json.loads(req.data), {"url": "u"})


class WwrVerifyTest(unittest.TestCase):
    def rec(self, company, title="Backend Engineer", site="https://acme.example"):
        return {"company": company, "title": title, "description": f"About us. URL: {site}", "source": "wwr",
                "url": "https://weworkremotely.com/x", "key": company + title}

    def patch(self, **kw):
        out = {}
        for name, val in {"from_pages": None, "from_probes": None, "_get": OSError("no"), **kw}.items():
            m = mock.patch.object(discover, name, side_effect=val if isinstance(val, Exception) else None,
                                  return_value=None if isinstance(val, Exception) else val)
            out[name] = m.start()
            self.addCleanup(m.stop)
        return out

    def test_similar_and_pages(self):
        self.assertTrue(wwr_verify._similar("Senior Backend Engineer (Remote)", "Backend Engineer"))
        self.assertTrue(wwr_verify._similar("Java Backend Developer", "Backend Developer Java"))
        self.assertFalse(wwr_verify._similar("Backend Engineer", "Marketing Manager"))
        self.assertFalse(wwr_verify._similar("Remote", "Engineer"))
        self.assertEqual(wwr_verify._pages("https://www.acme.example/about"),
                         ["https://acme.example/careers", "https://www.acme.example/careers",
                          "https://acme.example/jobs", "https://acme.example/"])

    def test_ats_board_match_and_cache(self):
        job = {"title": "Senior Backend Engineer", "url": "https://boards.greenhouse.io/acme/jobs/1"}
        m = self.patch(from_probes=("greenhouse", "acme", None, {}))
        with mock.patch.dict("sources.ATS_FETCHERS", {"greenhouse": lambda c: [job]}):
            self.assertEqual(wwr_verify.verify(self.rec("Acme Verify One")), (job["url"], "greenhouse board"))
            self.assertEqual(wwr_verify.verify(self.rec("Acme Verify One")), (job["url"], "greenhouse board"))
        self.assertEqual(m["from_probes"].call_count, 1)  # second call served from the cache
        self.assertEqual(db.cache_get("wwr", "acme verify one")["board"]["token"], "acme")

    def test_stale_cache_is_refreshed(self):
        db.cache_put("wwr", "stale co", {"board": None, "site": "", "checked": "2000-01-01"})
        m = self.patch()
        self.assertEqual(wwr_verify.verify(self.rec("Stale Co", site="")), (None, "not found on a free source"))
        self.assertEqual(m["from_probes"].call_count, 1)

    def test_careers_page_fallback_after_board_error(self):
        self.patch(from_probes=("lever", "acme", None, {}))
        body = b"<html><h2>Open roles</h2><li>Backend Engineer</li></html>"
        calls = []

        def get(url):
            calls.append(url)
            if url.endswith("/careers") and "www" not in url:
                raise OSError("404")
            return 200, body
        with mock.patch.object(discover, "_get", side_effect=get), \
                mock.patch.dict("sources.ATS_FETCHERS", {"lever": mock.Mock(side_effect=OSError)}):
            out = wwr_verify.verify(self.rec("Careers Page Co"))
        self.assertEqual(out, ("https://www.acme.example/careers", "company careers page"))
        self.assertEqual(len(calls), 2)

    def test_not_found(self):
        self.patch(_get=(200, b"<p>Nothing here</p>"))
        self.assertEqual(wwr_verify.verify(self.rec("Nothing Co")), (None, "not found on a free source"))

    def test_run_splits_records(self):
        keep_rec = {"source": "indeed", "key": "k0", "company": "X", "title": "T"}
        ok, bad = self.rec("Run Ok Co"), self.rec("Run Bad Co", "Chef")
        with mock.patch.object(wwr_verify, "verify",
                               side_effect=lambda r: ("https://free.example/1", "lever board") if r is ok
                               else (None, "not found on a free source")):
            keep, rejected, report = wwr_verify.run([keep_rec, ok, bad], [])
        self.assertEqual(keep, [keep_rec, ok])
        self.assertEqual(ok["url"], "https://free.example/1")
        self.assertEqual(ok["wwr_url"], "https://weworkremotely.com/x")
        self.assertIn("wwr-verified-free", ok["flags"])
        self.assertEqual(rejected, [{"key": bad["key"], "reason": "wwr-only-paywalled"}])
        self.assertEqual([r[:3] for r in report], ["OK ", "REJ"])


if __name__ == "__main__":
    unittest.main()
