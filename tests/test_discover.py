"""Company discovery: ATS validation, careers-page/probe detection, merging, seeds and the CLI (network mocked)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import discover
import sources
from common import load_config


def capture(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


class TmpCase(unittest.TestCase):
    """Redirects the companies file and user seed folder to a temp dir."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="js-disc-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.seeds = self.dir / "seeds"
        self.plugin_seeds = self.dir / "plugin-seeds"
        self.seeds.mkdir()
        self.plugin_seeds.mkdir()
        self.companies = self.dir / "companies.json"
        for name, val in [("COMPANIES_PATH", self.companies), ("SEEDS", self.seeds), ("SEEDS_DIR", self.plugin_seeds)]:
            p = mock.patch.object(discover, name, val)
            p.start()
            self.addCleanup(p.stop)


class HttpHelpersTest(unittest.TestCase):
    def test_get_returns_status_and_body(self):
        resp = mock.MagicMock(status=200)
        resp.read.return_value = b"body"
        resp.__enter__.return_value = resp
        with mock.patch.object(discover.urllib.request, "urlopen", return_value=resp) as uo:
            self.assertEqual(discover._get("https://x.example"), (200, b"body"))
        self.assertIn("trawlnet", uo.call_args[0][0].get_header("User-agent"))

    def test_json_swallows_network_and_parse_errors(self):
        with mock.patch.object(discover, "_get", return_value=(200, b'{"a": 1}')):
            self.assertEqual(discover._json("u"), {"a": 1})
        with mock.patch.object(discover, "_get", return_value=(404, b"{}")):
            self.assertIsNone(discover._json("u"))
        with mock.patch.object(discover, "_get", return_value=(200, b"<html>")):
            self.assertIsNone(discover._json("u"))
        with mock.patch.object(discover, "_get", side_effect=urllib.error.URLError("dns")):
            self.assertIsNone(discover._json("u"))

    def test_post_returns_none_on_error(self):
        with mock.patch.object(discover, "_post_json", return_value={"ok": 1}):
            self.assertEqual(discover._post("u", {}), {"ok": 1})
        with mock.patch.object(discover, "_post_json", side_effect=OSError):
            self.assertIsNone(discover._post("u", {}))

    def test_name_match(self):
        self.assertTrue(discover.name_match("Acme Inc.", "acme"))
        self.assertTrue(discover.name_match("Acme", "Acme Payments Pvt Ltd"))
        self.assertFalse(discover.name_match("Acme", "Globex"))
        self.assertFalse(discover.name_match("", "Globex"))

    def test_blocklist_merges_plugin_and_user_files(self):
        plugin, user = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plugin, True)
        self.addCleanup(shutil.rmtree, user, True)
        (plugin / "blocklist.json").write_text('[{"ats": "lever", "token": "Bad"}]')
        (user / "blocklist.json").write_text('[{"ats": "ashby", "token": "worse"}]')
        with mock.patch.object(discover, "SEEDS_DIR", plugin), mock.patch.object(discover, "SEEDS", user):
            self.assertEqual(discover._blocked(), {("lever", "bad"), ("ashby", "worse")})

    def test_undocumented_gate(self):
        with mock.patch.object(discover, "_cfg", return_value={"sources": {"undocumented_ats": True}}):
            self.assertTrue(discover._undocumented_ok())
        with mock.patch.object(discover, "_cfg", return_value={"sources": {}}):
            self.assertFalse(discover._undocumented_ok())
        with mock.patch.object(discover, "_cfg", side_effect=SystemExit):
            self.assertFalse(discover._undocumented_ok())


class ValidateTest(unittest.TestCase):
    def check(self, ats, resp, strict, name="Acme", token="acme", extra=None, via="_json"):
        with mock.patch.object(discover, via, return_value=resp):
            return discover.validate(ats, token, name, strict, extra)

    def test_blocklisted_never_passes(self):
        with mock.patch.object(discover, "BLOCKED", {("greenhouse", "acme")}):
            self.assertFalse(self.check("greenhouse", {"name": "Acme"}, False, token="ACME"))

    def test_greenhouse(self):
        self.assertTrue(self.check("greenhouse", {"name": "Acme Inc"}, True))
        self.assertFalse(self.check("greenhouse", {"name": "Someone Else"}, True))
        self.assertTrue(self.check("greenhouse", {"name": "Someone Else"}, False))
        self.assertFalse(self.check("greenhouse", None, False))

    def test_lever_ashby(self):
        self.assertTrue(self.check("lever", [{"id": 1}], True))
        self.assertFalse(self.check("lever", [], True))
        self.assertTrue(self.check("lever", [], False))
        self.assertFalse(self.check("lever", {"error": 1}, False))
        self.assertTrue(self.check("ashby", {"jobs": [{}]}, True))
        self.assertFalse(self.check("ashby", {"jobs": []}, True))
        self.assertTrue(self.check("ashby", {"jobs": []}, False))

    def test_workable_needs_name_and_open_jobs_when_strict(self):
        self.assertTrue(self.check("workable", {"name": "Acme", "jobs": [{}]}, True))
        self.assertFalse(self.check("workable", {"name": "Acme", "jobs": []}, True))  # dormant namesake
        self.assertFalse(self.check("workable", {"name": "Other", "jobs": [{}]}, True))
        self.assertTrue(self.check("workable", {"name": "Other", "jobs": []}, False))

    def test_smartrecruiters(self):
        self.assertTrue(self.check("smartrecruiters", {"content": [{"company": {"name": "Acme"}}]}, True))
        self.assertFalse(self.check("smartrecruiters", {"content": [{"company": {"name": "Zed"}}]}, True))
        self.assertFalse(self.check("smartrecruiters", {"content": []}, False))
        self.assertFalse(self.check("smartrecruiters", None, False))

    def test_workday_and_darwinbox(self):
        wd = {"host": "acme.wd3.myworkdayjobs.com", "site": "External"}
        self.assertTrue(self.check("workday", {"total": 4}, True, extra=wd, via="_post"))
        self.assertFalse(self.check("workday", {"total": 0}, True, extra=wd, via="_post"))
        self.assertTrue(self.check("workday", {"total": 0}, False, extra=wd, via="_post"))
        self.assertFalse(self.check("workday", None, False, extra=wd, via="_post"))
        db = {"host": "acme.darwinbox.in", "site": "main"}
        self.assertTrue(self.check("darwinbox", {"status": "success", "data": [1]}, True, extra=db, via="_post"))
        self.assertFalse(self.check("darwinbox", {"status": "success", "data": []}, True, extra=db, via="_post"))
        self.assertFalse(self.check("darwinbox", {"status": "error"}, False, extra=db, via="_post"))

    def test_unknown_ats(self):
        self.assertFalse(discover.validate("taleo", "acme", "Acme", False))


class DetectionTest(unittest.TestCase):
    def test_from_pages_detects_ats_link_and_hints(self):
        html = b'<a href="https://boards.greenhouse.io/acme">Jobs</a> <a href="https://acme.keka.com/careers">HR</a>'
        c = {"name": "Acme", "domain": "acme.example"}
        with mock.patch.object(discover, "_get", return_value=(200, html)), \
                mock.patch.object(discover, "validate", return_value=True) as v:
            self.assertEqual(discover.from_pages(c), ("greenhouse", "acme", "careers-page", {}))
        v.assert_called_once_with("greenhouse", "acme", "Acme", strict=False)
        self.assertTrue(c["page_ok"])
        self.assertEqual(c["hints"], ["keka"])

    def test_careers_url_that_is_an_ats_link_is_used_directly(self):
        c = {"name": "Acme", "careers_url": "https://jobs.lever.co/acme"}
        with mock.patch.object(discover, "_get", side_effect=AssertionError("no fetch")), \
                mock.patch.object(discover, "validate", return_value=True):
            self.assertEqual(discover.from_pages(c)[:2], ("lever", "acme"))

    def test_bad_tokens_unreachable_pages_and_failed_validation(self):
        c = {"name": "Acme", "domain": "acme.example"}
        with mock.patch.object(discover, "_get", side_effect=OSError):
            self.assertIsNone(discover.from_pages(c))
        self.assertNotIn("page_ok", c)
        html = b"https://boards.greenhouse.io/embed https://jobs.lever.co/acme"
        with mock.patch.object(discover, "_get", return_value=(200, html)), \
                mock.patch.object(discover, "validate", return_value=False) as v:
            self.assertIsNone(discover.from_pages({"name": "Acme", "careers_url": "https://acme.example/c"}))
        self.assertEqual([call.args[:2] for call in v.call_args_list], [("lever", "acme")])  # 'embed' skipped

    def test_hosted_ats_only_when_undocumented_allowed(self):
        html = b"https://acme.wd3.myworkdayjobs.com/en-US/External/jobs https://blog.wd3.myworkdayjobs.com/x"
        c = lambda: {"name": "Acme", "careers_url": "https://acme.example/c"}  # noqa: E731
        with mock.patch.object(discover, "_get", return_value=(200, html)), \
                mock.patch.object(discover, "validate", return_value=True):
            with mock.patch.object(discover, "_undocumented_ok", return_value=False):
                self.assertIsNone(discover.from_pages(c()))
            with mock.patch.object(discover, "_undocumented_ok", return_value=True):
                hit = discover.from_pages(c())
        self.assertEqual(hit, ("workday", "acme", "careers-page", {"host": "acme.wd3.myworkdayjobs.com", "site": "External"}))

    def test_from_probes_tries_slug_variants_per_ats(self):
        seen = []

        def validate(ats, tok, name, strict, extra=None):
            seen.append((ats, tok))
            return (ats, tok) == ("ashby", "acme-corp")
        c = {"name": "Acme Corp", "domain": "acmecorp.io"}
        with mock.patch.object(discover, "validate", validate):
            self.assertEqual(discover.from_probes(c), ("ashby", "acme-corp", "probe", {}))
        self.assertEqual(seen[:3], [("greenhouse", "acmecorp"), ("greenhouse", "acme-corp"), ("lever", "acmecorp")])

    def test_from_probes_smartrecruiters_darwinbox_and_none(self):
        c = {"name": "Acme Corp"}
        with mock.patch.object(discover, "validate", lambda ats, *a, **k: ats == "smartrecruiters"):
            self.assertEqual(discover.from_probes(c)[:2], ("smartrecruiters", "AcmeCorp"))
        with mock.patch.object(discover, "validate", lambda ats, *a, **k: ats == "darwinbox"), \
                mock.patch.object(discover, "_undocumented_ok", return_value=True):
            self.assertEqual(discover.from_probes(c), ("darwinbox", "acmecorp", "probe",
                                                       {"host": "acmecorp.darwinbox.in", "site": "main"}))
        with mock.patch.object(discover, "validate", lambda ats, *a, **k: ats == "darwinbox"), \
                mock.patch.object(discover, "_undocumented_ok", return_value=False):
            self.assertIsNone(discover.from_probes(c))


class CountResolveTest(unittest.TestCase):
    def jobs(self):
        old = "2000-01-01"
        return [{"title": "Eng", "location": "Remote - India", "remote": True, "region_text": "", "source": "greenhouse",
                 "description": "", "posted": None},
                {"title": "Eng", "location": "Bengaluru", "remote": None, "region_text": "", "source": "greenhouse",
                 "description": "", "posted": old},
                {"title": "Eng", "location": "Pune, India", "remote": None, "region_text": "", "source": "greenhouse",
                 "description": "", "posted": None}]

    def test_count_jobs_relevance_and_freshness(self):
        cfg = load_config()
        with mock.patch.dict(sources.ATS_FETCHERS, {"greenhouse": lambda e: self.jobs()}):
            out = discover.count_jobs({"ats": "greenhouse"}, cfg)
        self.assertEqual((out["total_jobs"], out["relevant_jobs"], out["relevant_fresh"]), (3, 2, 1))

    def test_count_jobs_error(self):
        with mock.patch.dict(sources.ATS_FETCHERS, {"greenhouse": mock.Mock(side_effect=OSError)}):
            self.assertEqual(discover.count_jobs({"ats": "greenhouse"}, load_config()),
                             {"total_jobs": None, "relevant_jobs": 0, "error": "OSError"})

    def test_resolve(self):
        c = {"name": "Acme", "domain": "acme.example", "tags": ["t"], "seed": "in"}
        with mock.patch.object(discover, "from_pages", return_value=None), \
                mock.patch.object(discover, "from_probes", return_value=("lever", "acme", "probe", {})), \
                mock.patch.object(discover, "count_jobs", return_value={"total_jobs": 5, "relevant_jobs": 2}):
            e = discover.resolve(c, {})
        self.assertEqual((e["ats"], e["token"], e["detected_by"], e["active"], e["seed"]), ("lever", "acme", "probe", True, "in"))
        with mock.patch.object(discover, "from_pages", return_value=None), \
                mock.patch.object(discover, "from_probes", return_value=None):
            self.assertIsNone(discover.resolve(c, {}))


class MergeSeedsTest(TmpCase):
    def test_merge_adds_updates_and_orders_active_first(self):
        self.companies.write_text(json.dumps([
            {"name": "Zed", "ats": "lever", "token": "Zed", "active": False, "tags": ["a"]},
            {"name": "Man", "ats": "ashby", "token": "man", "active": False, "manual": True, "tags": []}]))
        new = [{"name": "Zed", "ats": "lever", "token": "zed", "active": True, "relevant_jobs": 3, "total_jobs": 9,
                "checked": "2026-01-01", "tags": ["b"]},
               {"name": "Man", "ats": "ashby", "token": "man", "active": True, "tags": []},
               {"name": "Acme", "ats": "greenhouse", "token": "acme", "active": True, "tags": []}]
        self.assertEqual(discover.merge(new), (1, 2))
        rows = json.loads(self.companies.read_text())
        self.assertEqual([r["name"] for r in rows], ["Acme", "Zed", "Man"])  # active first, then by name
        zed = rows[1]
        self.assertEqual((zed["active"], zed["relevant_jobs"], zed["tags"]), (True, 3, ["a", "b"]))
        self.assertFalse(rows[2]["active"])  # manual override survives

    def test_load_companies_missing_file(self):
        self.assertEqual(discover.load_companies(), [])

    def test_load_seed_yaml_json_and_precedence(self):
        (self.plugin_seeds / "s.yaml").write_text("fintech:\n  - [Acme, acme.example]\n  - [Globex, globex.example]\n")
        rows = discover.load_seed("s")
        self.assertEqual(rows[0], {"name": "Acme", "domain": "acme.example", "tags": ["fintech"], "seed": "s"})
        (self.seeds / "s.json").write_text('[{"name": "Mine", "domain": "m.example", "tags": [], "seed": "s"}]')
        self.assertEqual([r["name"] for r in discover.load_seed("s")], ["Mine"])  # the user's seed wins
        with self.assertRaisesRegex(SystemExit, "seed 'nope' not found"):
            discover.load_seed("nope")

    def test_import_remoteintech(self):
        repo = self.dir / "repo" / "src" / "companies"
        repo.mkdir(parents=True)
        (repo / "a.md").write_text("---\ntitle: Alpha\nwebsite: https://www.alpha.example/x\nregion: Worldwide\n"
                                   "remote_policy: fully\n---\nbody")
        (repo / "b.md").write_text("---\ntitle: Beta\nregion: worldwide\nslug: beta\ncareers_url: https://b.example/c\n---\n")
        (repo / "c.md").write_text("---\ntitle: Gamma\nregion: europe\n---\n")
        (repo / "d.md").write_text("---\n: [unbalanced\n---\n")
        (repo / "e.md").write_text("no front matter")
        (repo / "f.md").write_text("---\nslug: fslug\n---\n")  # no region -> kept (None), name falls back to slug
        out = capture(discover.cmd_import_remoteintech, str(self.dir / "repo"))
        rows = json.loads((self.seeds / "remoteintech.json").read_text())
        self.assertEqual([r["name"] for r in rows], ["Beta", "fslug"])
        self.assertIn("2 companies", out)
        self.assertEqual(rows[0]["careers_url"], "https://b.example/c")
        self.assertEqual(rows[0]["tags"][:2], ["remoteintech", "region:worldwide"])


class CommandsTest(TmpCase):
    def test_run_resolves_new_companies_and_lists_unresolved(self):
        (self.seeds / "s.json").write_text(json.dumps([{"name": n, "domain": "", "tags": [], "seed": "s"}
                                                       for n in ("Known", "Found", "Lost")]))
        self.companies.write_text(json.dumps([{"name": "Known", "ats": "lever", "token": "known"}]))

        def resolve(c, cfg):
            return {"name": c["name"], "ats": "lever", "token": c["name"].lower(), "active": True, "tags": []} \
                if c["name"] == "Found" else None
        with mock.patch.object(discover, "resolve", resolve):
            out = capture(discover.cmd_run, "s", None)
        self.assertIn("3 candidates, 1 already known | resolved 1 (lever=1) | active (eligible jobs) 1 | unresolved 1", out)
        self.assertEqual([c["name"] for c in json.loads((self.seeds / "unresolved-s.json").read_text())], ["Lost"])
        self.assertEqual({c["name"] for c in json.loads(self.companies.read_text())}, {"Known", "Found"})

    def test_run_alias_and_limit(self):
        (self.seeds / "in.json").write_text(json.dumps([{"name": f"C{i}", "domain": "", "tags": [], "seed": "in"}
                                                        for i in range(5)]))
        with mock.patch.object(discover, "resolve", return_value=None) as r:
            out = capture(discover.cmd_run, "india", 2)  # alias india -> in
        self.assertEqual(r.call_count, 2)
        self.assertIn("in: 2 candidates", out)
        self.assertTrue((self.seeds / "unresolved-in.json").exists())

    def test_verify_agent_findings(self):
        f = self.dir / "found.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in [
            {"name": "Good", "ats": "lever", "token": "good"}, {"name": "Bad", "ats": "lever", "token": "bad"},
            {"name": "Odd", "ats": "taleo", "token": "x"}, {"name": "NoTok", "ats": "lever"}]) + "\n\n")
        with mock.patch.object(discover, "validate", lambda ats, tok, *a, **k: tok == "good"), \
                mock.patch.object(discover, "count_jobs", return_value={"relevant_jobs": 1}):
            out = capture(discover.cmd_verify, str(f))
        self.assertIn("verified 1/4 (+1 new, 1 active) | rejected: Bad, Odd, NoTok", out)
        self.assertEqual(json.loads(self.companies.read_text())[0]["detected_by"], "research")

    def test_listing_url(self):
        pages = {"https://a.example/careers": b'<a href="https://acme.keka.com/careers/jobs">x</a>',
                 "https://b.example/careers": b"<p>plain</p>"}

        def get(url, **kw):
            if url in pages:
                return 200, pages[url]
            raise OSError
        with mock.patch.object(discover, "_get", get):
            self.assertEqual(discover._listing_url({"domain": "a.example"}), "https://acme.keka.com/careers/jobs")
            self.assertEqual(discover._listing_url({"domain": "b.example"}), "https://b.example/careers")
            self.assertIsNone(discover._listing_url({"domain": "c.example"}))
            self.assertIsNone(discover._listing_url({}))
            self.assertEqual(discover._listing_url({"careers_url": "https://b.example/careers"}), "https://b.example/careers")

    def test_custom_adds_reachable_unresolved(self):
        (self.seeds / "unresolved-s.json").write_text(json.dumps([
            {"name": "Reach Able", "domain": "r.example", "tags": ["x"]}, {"name": "Dead Co"},
            {"name": "Known Co"}, {"name": "Broken Free"}]))
        self.companies.write_text(json.dumps([{"name": "Known Co", "ats": "lever", "token": "kc"}]))
        urls = {"Reach Able": "https://r.example/careers", "Broken Free": "https://bf.example/jobs"}

        def free(e):
            if e["name"] == "Broken Free":
                raise OSError
            return [1, 2]
        with mock.patch.object(discover, "_listing_url", lambda c: urls.get(c["name"])), \
                mock.patch.object(sources, "fetch_custom_free", free):
            out = capture(discover.cmd_custom, "s")
        self.assertIn("2 of 3 unresolved have a reachable careers page (+2); 1 list jobs in plain HTML (free), 1 need Firecrawl", out)
        rows = {r["name"]: r for r in json.loads(self.companies.read_text())}
        self.assertEqual((rows["Reach Able"]["token"], rows["Reach Able"]["free_links"]), ("reach-able", 2))
        self.assertEqual(rows["Broken Free"]["free_links"], 0)

    def test_summary(self):
        self.companies.write_text(json.dumps([{"name": "A", "ats": "lever", "token": "a", "active": True, "seed": "in"},
                                              {"name": "B", "ats": "ashby", "token": "b", "seed": "x"}]))
        out = capture(discover.cmd_summary)
        self.assertIn("companies: 2 | active: 1", out)
        self.assertIn("'lever': 1", out)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = _home.make_home()
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(_home.SCRIPTS / "discover.py"), *args], capture_output=True,
                              text=True, cwd=str(self.home), env={**os.environ, "JOB_SEARCH_HOME": str(self.home)})

    def test_dispatch_offline_commands(self):
        p = self.run_cli("summary")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("companies: 9", p.stdout)
        (self.home / "data/seeds/empty.json").write_text("[]")
        p = self.run_cli("run", "--seed", "empty")
        self.assertIn("empty: 0 candidates", p.stdout)
        (self.home / "data/seeds/unresolved-empty.json").write_text("[]")
        self.assertIn("0 of 0 unresolved", self.run_cli("custom", "--seed", "empty").stdout)
        (self.home / "found.jsonl").write_text("")
        self.assertIn("verified 0/0", self.run_cli("verify", "found.jsonl").stdout)
        repo = self.home / "ri" / "src" / "companies"
        repo.mkdir(parents=True)
        self.assertIn("remoteintech seed: 0 companies", self.run_cli("import-remoteintech", str(self.home / "ri")).stdout)

    def test_usage_and_missing_seed(self):
        for argv in ((), ("bogus",)):
            p = self.run_cli(*argv)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("Company discovery", p.stderr)
        p = self.run_cli("run", "--seed", "nope")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("seed 'nope' not found", p.stderr)


if __name__ == "__main__":
    unittest.main()
