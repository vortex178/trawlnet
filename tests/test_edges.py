"""Edge cases: data-folder discovery, config errors, JSONL reading, HTTP wrappers, HN skips, filter rejections."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import contextlib
import datetime as dt
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import common
import firecrawl
import jobsearch
import sources


class TmpCase(unittest.TestCase):
    def tmp(self):
        d = Path(tempfile.mkdtemp(prefix="js-edge-"))
        self.addCleanup(shutil.rmtree, d, True)
        return d


class CommonEdgesTest(TmpCase):
    def test_find_home_prefers_env_then_walks_up_then_cwd(self):
        root = self.tmp().resolve()
        (root / "config.yaml").write_text("")
        (root / "data").mkdir()
        deep = root / "a" / "b"
        deep.mkdir(parents=True)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        env = {k: v for k, v in os.environ.items() if k != "JOB_SEARCH_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            os.chdir(deep)
            self.assertEqual(common._find_home(), root)
            other = self.tmp().resolve()
            os.chdir(other)
            self.assertEqual(common._find_home(), other)  # nothing found: falls back to the cwd
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": str(root)}):
            os.chdir(other)
            self.assertEqual(common._find_home(), root)

    def test_missing_pack_and_config_exit_with_help(self):
        with self.assertRaisesRegex(SystemExit, r"No country pack 'zz.yaml'.*Available: (\w+, )*in, (\w+, )*us"):
            common.load_pack("ZZ")
        with mock.patch.object(common, "HOME", self.tmp()), self.assertRaisesRegex(SystemExit, "No config.yaml"):
            common.load_config()

    def test_ziprecruiter_only_for_us_ca_and_pack_overrides(self):
        home = self.tmp()
        (home / "config.yaml").write_text("country: IN\nsources: {ziprecruiter: true}\npack_overrides: {name: Bharat}\n")
        with mock.patch.object(common, "HOME", home):
            cfg = common.load_config()
        self.assertFalse(cfg["sources"]["ziprecruiter"])
        self.assertEqual(cfg["pack"]["name"], "Bharat")
        (home / "config.yaml").write_text("country: US\nsources: {ziprecruiter: true}\n")
        with mock.patch.object(common, "HOME", home):
            self.assertTrue(common.load_config()["sources"]["ziprecruiter"])

    def test_save_config_value_preserves_comments(self):
        home = self.tmp()
        (home / "config.yaml").write_text("# top\ntracker:\n  gsheets:\n    sheet_tab_gid: null   # set by init\nx: 1\n")
        with mock.patch.object(common, "HOME", home):
            common.save_config_value("tracker.gsheets.sheet_tab_gid", 7)
            with self.assertRaises(KeyError):
                common.save_config_value("tracker.nope", 1)
        text = (home / "config.yaml").read_text()
        self.assertIn("sheet_tab_gid: 7 ", text)
        self.assertTrue(text.startswith("# top\n") and text.endswith("x: 1\n"))

    def test_read_jsonl_gz_missing_and_invalid_lines(self):
        d = self.tmp()
        self.assertEqual(common.read_jsonl(d / "none.jsonl"), [])
        (d / "raw.jsonl.gz").write_bytes(gzip.compress(b'{"a": 1}\n{"a": 2}\n'))
        self.assertEqual(common.read_jsonl(d / "raw.jsonl"), [{"a": 1}, {"a": 2}])  # falls back to the gzipped copy
        (d / "bad.jsonl").write_text('{"ok": 1}\nnot json\n\n{"ok": 2}\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(common.read_jsonl(d / "bad.jsonl"), [{"ok": 1}, {"ok": 2}])
        self.assertIn("WARN bad.jsonl:2 invalid JSON, skipped", buf.getvalue())

    def test_salary_with_only_stray_small_numbers_is_unparsed(self):
        self.assertIsNone(common.parse_salary("USD 50", {"USD": 83}, "INR"))


class HttpWrappersTest(unittest.TestCase):
    def response(self, payload):
        r = mock.MagicMock()
        r.read.return_value = payload
        r.__enter__.return_value = r
        return r

    def test_get_get_json_and_post_json(self):
        with mock.patch.object(sources.urllib.request, "urlopen", return_value=self.response(b'{"a": 1}')) as uo:
            self.assertEqual(sources._get("https://x.example"), b'{"a": 1}')
            self.assertEqual(sources._get_json("https://x.example"), {"a": 1})
            self.assertEqual(sources._post_json("https://x.example", {"q": 1}, {"X-Test": "1"}), {"a": 1})
        req = uo.call_args[0][0]
        self.assertEqual(json.loads(req.data), {"q": 1})
        self.assertEqual(req.get_header("X-test"), "1")
        self.assertEqual(req.get_header("Content-type"), "application/json")


class HnSkipsTest(unittest.TestCase):
    CFG = {"pack": {"country_places": ["india"]}}

    def test_no_story_or_stale_thread(self):
        with mock.patch.object(sources, "_get_json", return_value={"hits": [{"title": "Something else"}]}):
            self.assertEqual(sources.fetch_hn(self.CFG), [])
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with mock.patch.object(sources, "_get_json",
                               return_value={"hits": [{"title": "Who is hiring?", "objectID": "1", "created_at": old}]}):
            self.assertEqual(sources.fetch_hn(self.CFG), [])

    def test_empty_short_and_role_only_headers_are_skipped(self):
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        story = {"hits": [{"title": "Who is hiring?", "objectID": "1", "created_at": now}]}
        texts = [None, "Single header only", "Backend Engineer | Remote", "Acme | Backend Engineer | Remote"]
        item = {"children": [{"id": i, "text": t, "created_at": now} for i, t in enumerate(texts, 1)]}
        with mock.patch.object(sources, "_get_json", side_effect=[story, item]):
            out = sources.fetch_hn(self.CFG)
        self.assertEqual([(r["company"], r["title"]) for r in out], [("Acme", "Backend Engineer")])


class FirecrawlPacingTest(unittest.TestCase):
    def test_waits_between_requests(self):
        key = _home.HOME / ".secrets" / "fc-pace.key"
        key.parent.mkdir(exist_ok=True)
        key.write_text("k")
        self.addCleanup(key.unlink, missing_ok=True)
        cfg = {"firecrawl": {"enabled": True, "api_key_file": ".secrets/fc-pace.key", "dynamic_budget": False,
                             "min_seconds_between_requests": 5}}
        b = firecrawl.Budget(cfg, "2004-01-01")
        b._last = firecrawl.time.time()
        with mock.patch.object(firecrawl.time, "sleep") as sleep, \
                mock.patch.object(firecrawl.Budget, "_req", return_value={"data": {"markdown": "x"}}):
            b.scrape_markdown("https://x.example/pace")
        self.assertGreater(sleep.call_args[0][0], 0)


class FilterRejectionsTest(unittest.TestCase):
    def rec(self, company, **kw):
        base = {"source": "greenhouse", "source_id": company, "title": "Backend Engineer", "company": company,
                "location": "Bengaluru, India", "remote": None, "region_text": "", "eligible_countries": "",
                "posted": (dt.date.today() - dt.timedelta(days=1)).isoformat(), "salary_text": "",
                "url": f"https://x.example/{company}", "description": ""}
        return {**base, **kw}

    def run_filter(self, recs, date, **cfg_over):
        d = common.run_dir(date)
        common.write_jsonl(d / "feeds.jsonl", recs)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            jobsearch.cmd_filter(Namespace(date=date, quiet=False), {**common.load_config(), **cfg_over})
        return d, buf.getvalue(), {r["key"]: r for r in common.read_jsonl(d / "rejected.jsonl")}

    def test_expired_stale_undated_unparsed_salary_and_duplicates(self):
        old = (dt.date.today() - dt.timedelta(days=200)).isoformat()
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        past = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        recs = [self.rec("EdgeExpired", expires=past), self.rec("EdgeStale", posted=old),
                self.rec("EdgeUndated", posted=None), self.rec("EdgeSalary", salary_text="competitive"),
                self.rec("EdgeDup", source="indeed", posted=yesterday), self.rec("EdgeDup", posted=None),
                self.rec("EdgeWwr", source="wwr", location="Remote", remote=True)]
        with mock.patch("wwr_verify.run", side_effect=lambda acc, rej: (acc, rej + [], ["REJ EdgeWwr — stub"])):
            d, out, rejected = self.run_filter(recs, "2004-02-01", keep_undated=False)
        reasons = {r["company"]: r["reason"] for r in rejected.values()}
        self.assertEqual(reasons, {"EdgeExpired": "expired", "EdgeStale": "stale", "EdgeUndated": "undated"})
        accepted = {r["company"]: r for r in common.read_jsonl(d / "accepted.jsonl")}
        self.assertIn("salary-unparsed", accepted["EdgeSalary"]["flags"])
        self.assertEqual(accepted["EdgeDup"]["source"], "greenhouse")  # higher-ranked source wins
        self.assertEqual(accepted["EdgeDup"]["also_on"], ["indeed"])
        self.assertEqual(accepted["EdgeDup"]["posted"], yesterday)  # date inherited from the duplicate
        self.assertIn("duplicates=1", out)
        self.assertIn("WWR REJ EdgeWwr — stub", out)

    def test_ambiguous_titles_are_listed_for_the_classifier(self):
        d, out, _ = self.run_filter([self.rec("EdgeAmbig", title="Engineer")], "2004-02-03")
        self.assertEqual([r["company"] for r in common.read_jsonl(d / "ambiguous.jsonl")], ["EdgeAmbig"])
        self.assertIn("PROFILES (id: label | target titles | seniority allowed):", out)
        self.assertRegex(out, r"(?m)^appsec: .* \| .*\n")
        self.assertIn("AMBIGUOUS (key | title | company | candidate profiles):", out)
        self.assertRegex(out, r"\| Engineer \| EdgeAmbig \| appsec,backend-sde")
        _, quiet, _ = self.run_filter([self.rec("EdgeAmbigQ", title="Engineer")], "2004-02-04", )
        self.assertIn("AMBIGUOUS", quiet)

    def test_indeed_rows_without_title_or_company_are_dropped(self):
        d = common.run_dir("2004-02-02")
        common.write_jsonl(d / "indeed_raw.jsonl", [
            {"id": "1", "title": "Backend Engineer", "company": "IndeedOk", "location": "Bengaluru, India",
             "posted": "2 days ago", "url": "u"}, {"id": "2", "title": "", "company": "NoTitle"},
            {"id": "3", "title": "Backend Engineer", "company": ""}])
        common.write_jsonl(d / "feeds.jsonl", [])
        with contextlib.redirect_stdout(io.StringIO()):
            jobsearch.cmd_filter(Namespace(date="2004-02-02", quiet=True), common.load_config())
        self.assertEqual([r["company"] for r in common.read_jsonl(d / "accepted.jsonl")], ["IndeedOk"])


if __name__ == "__main__":
    unittest.main()
