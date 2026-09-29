"""jobsearch.py commands not covered by the end-to-end pipeline test: context, plan, feeds, decide, shortlist,
publish edge cases, retention, run-log, fetch-url, track, status and the CLI entry point (offline)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import contextlib
import csv
import datetime as dt
import gzip
import io
import json
import os
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import db
import jobsearch
import sources
from common import DATA, HOME, RUNS_DIR, load_config, load_pack, read_jsonl, run_dir, write_jsonl


def args(**kw):
    return Namespace(**{"date": None, "quiet": False, "dry": False, "top": 10, "url": None, "company": None,
                        "role": None, "profile": None, "location": None, "score": None, **kw})


def run(fn, a, cfg=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(a, cfg or load_config())
    return buf.getvalue()


def days_ago(n):
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


class ContextPlanStatusTest(unittest.TestCase):
    def test_context_lists_country_floors_and_work_rules(self):
        out = run(jobsearch.cmd_context, args())
        self.assertEqual(out.strip(), "data/scoring-context.md")
        text = (DATA / "scoring-context.md").read_text()
        self.assertIn("Country: India (IN). Currency INR", text)
        self.assertIn("On-site/hybrid accepted only in: bengaluru (bangalore)", text)
        self.assertRegex(text, r"Salary floor \(annual, INR\): default 30 LPA, appsec 32 LPA")
        self.assertIn("Forbidden working hours: 00:00-06:00 in Asia/Kolkata", text)
        self.assertIn("Max required experience (the JD's minimum years): 6", text)
        self.assertIn("Remote bias: some; reject strict work-from-office: no", text)

    def test_context_for_a_us_setup_without_cities(self):
        cfg = {**load_config(), "pack": load_pack("us"), "currency": "USD", "fx_to_local": {"INR": 0.012},
               "country": "US", "accept_cities": {}, "remote_scope": "global_ok"}
        cfg.pop("accept_any_india_location", None)
        cfg["accept_any_country_location"] = False
        jobsearch.write_context(cfg)
        text = (DATA / "scoring-context.md").read_text()
        self.assertIn("nowhere (remote only)", text)
        self.assertIn("any location is fine", text)
        self.assertIn("Currency USD", text)
        jobsearch.write_context(load_config())  # leave the India context in place

    def test_plan_dedupes_queries_across_profiles(self):
        cfg = {**load_config(), "sources": {"indeed": True}}
        profiles = {"a": {"target_titles": ["Java Developer", "Backend Engineer"], "search_queries": ["Java Developer"]},
                    "b": {"target_titles": ["Java developer"]}}
        with mock.patch.object(jobsearch, "load_profiles", return_value=profiles):
            out = run(jobsearch.cmd_plan, args(date="2003-01-01"), cfg)
        q = json.loads((RUNS_DIR / "2003-01-01" / "queries.json").read_text())["indeed"]
        self.assertEqual(len(q), 2)  # one query per location, shared by both profiles
        self.assertEqual(q[0]["profiles"], ["a", "b"])
        self.assertIn("indeed queries: 2", out)
        self.assertIn("ZipRecruiter off", out)

    def test_plan_without_indeed_or_profiles(self):
        cfg = {**load_config(), "sources": {"indeed": False}, "max_indeed_queries": 1}
        run(jobsearch.cmd_plan, args(date="2003-01-02"), cfg)
        self.assertEqual(json.loads((RUNS_DIR / "2003-01-02" / "queries.json").read_text()), {"indeed": []})
        with mock.patch.object(jobsearch, "load_profiles", return_value={}), self.assertRaises(SystemExit):
            run(jobsearch.cmd_plan, args(date="2003-01-03"))

    def test_status_and_main(self):
        out = run(jobsearch.cmd_status, args())
        self.assertIn("country IN (INR)", out)
        self.assertIn("profiles: appsec, backend-sde", out)
        self.assertIn("tracker: csv", out)
        with mock.patch("setup.seed_warnings", return_value=["seed 'in': list is old"]):
            self.assertIn("warning: seed 'in': list is old", run(jobsearch.cmd_status, args()))
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["jobsearch.py", "status"]), contextlib.redirect_stdout(buf):
            jobsearch.main()
        self.assertIn("seen:", buf.getvalue())


class FeedsTest(unittest.TestCase):
    def test_feeds_dry_writes_records_and_reports(self):
        recs = [{"source": "custom", "title": "T"}]
        fake = mock.Mock(return_value=(recs, {"custom:A": 1, "custom:B": 0, "wwr": 3}, ["greenhouse:X: boom"]))
        with mock.patch.object(sources, "fetch_all", fake):
            out = run(jobsearch.cmd_feeds, args(date="2003-02-01", dry=True))
        self.assertEqual(read_jsonl(RUNS_DIR / "2003-02-01" / "feeds.jsonl"), recs)
        self.assertIn("feeds: 1 jobs from 3 sources; 1 errors", out)
        self.assertIn("custom sites: 1/2 returned jobs (1 listings)", out)
        self.assertIn("ERR greenhouse:X: boom", out)
        self.assertIn("--dry", out)
        self.assertFalse(fake.call_args[0][2].enabled)  # Firecrawl disabled by --dry


class DecideTest(unittest.TestCase):
    def test_decisions_including_undecided_invalid_and_duplicates(self):
        d = run_dir("2003-02-02")
        amb = [{"key": f"dec-{i}", "title": "Engineer", "company": "C", "ambiguous_profiles": ["backend-sde", "appsec"],
                "source": "greenhouse", "location": "", "posted": None, "url": "u"} for i in range(5)]
        write_jsonl(d / "ambiguous.jsonl", amb)
        write_jsonl(d / "accepted.jsonl", [{"key": "dec-3", "routes": [["appsec", 2]]}])
        write_jsonl(d / "rejected.jsonl", [])
        write_jsonl(d / "decisions.jsonl", [
            {"key": "dec-0", "profile": "appsec"},        # accepted
            {"key": "dec-1", "profile": None},            # rejected by the title classifier
            {"key": "dec-2", "profile": "unknown-prof"},  # invalid profile -> rejected
            {"key": "dec-3", "profile": "backend-sde"},   # already accepted: not added twice
            {"key": "not-ambiguous", "profile": "appsec"}])  # ignored; dec-4 stays undecided
        out = run(jobsearch.cmd_decide, args(date="2003-02-02"))
        self.assertIn("+1 accepted, 2 rejected, 1 undecided", out)
        self.assertEqual(sorted(r["key"] for r in read_jsonl(d / "accepted.jsonl")), ["dec-0", "dec-3"])
        self.assertEqual([(r["key"], r["reason"]) for r in read_jsonl(d / "rejected.jsonl")],
                         [("dec-1", "title-llm"), ("dec-2", "title-llm")])


class ShortlistTest(unittest.TestCase):
    def rec(self, key, source="greenhouse", profile="backend-sde", **kw):
        return {"key": key, "source": source, "source_id": key, "title": f"Title {key}", "company": f"Co {key}",
                "location": "Remote", "posted": days_ago(1), "url": f"https://x.example/{key}", "flags": [],
                "routes": [[profile, 3]], "loc_bucket": "remote", "description": "", **kw}

    def test_caps_enrichment_lazy_descriptions_and_batches(self):
        d = run_dir("2003-02-03")
        write_jsonl(d / "accepted.jsonl", [
            self.rec("sl-hn", "hn", description="short"), self.rec("sl-hn2", "hn"),
            self.rec("sl-adz", "adzuna", detail_url="https://adz.example/1"),
            self.rec("sl-cust", "custom", detail_url="https://c.example/1"),
            self.rec("sl-al1", "alignerr"), self.rec("sl-al2", "alignerr"),
            self.rec("sl-app", profile="appsec", description="Full JD text"),
            self.rec("sl-ind", "indeed")])
        (d / "batch-9.jsonl").write_text("stale")
        cfg = {**load_config(), "shortlist_size": 10, "scorer_batch_size": 3, "shortlist_source_caps": {"alignerr": 1},
               "adzuna_detail_delay_seconds": 0}

        def enrich(rec, budget):
            if rec["key"] == "sl-hn2":
                raise RuntimeError("bad link")
            return "enriched text"

        def lazy(rec, budget):
            if rec["source"] == "custom":
                raise RuntimeError("blocked")
            return "adzuna full text"
        with mock.patch.object(sources, "enrich_short_description", enrich), \
                mock.patch.object(sources, "lazy_description", lazy), mock.patch.object(time, "sleep"):
            out = run(jobsearch.cmd_shortlist, args(date="2003-02-03"), cfg)
        rows = {r["key"]: r for r in read_jsonl(d / "shortlist.jsonl")}
        self.assertEqual(len(rows), 7)  # one alignerr dropped by the cap
        self.assertEqual(sum(k.startswith("sl-al") for k in rows), 1)
        self.assertEqual((d / "jd" / "sl-hn.txt").read_text().split("\n\n", 1)[1], "enriched text")
        self.assertEqual(rows["sl-hn"]["jd"], "data/runs/2003-02-03/jd/sl-hn.txt")
        self.assertIn("adzuna full text", (d / "jd" / "sl-adz.txt").read_text())
        self.assertEqual(rows["sl-ind"]["jd"], "indeed:sl-ind")
        self.assertTrue(rows["sl-cust"]["jd"].startswith("indeed:"))  # detail fetch failed -> undescribed
        self.assertIn("WARN enrich failed for sl-hn2", out)
        self.assertIn("WARN custom detail failed for sl-cust", out)
        self.assertIn("1 accepted not shortlisted", out)
        self.assertEqual(sorted(p.name for p in d.glob("batch-*.jsonl")), ["batch-1.jsonl", "batch-2.jsonl", "batch-3.jsonl"])
        self.assertEqual(out.count("BATCH "), 3)

    def test_adzuna_detail_failure_is_reported(self):
        d = run_dir("2003-02-04")
        write_jsonl(d / "accepted.jsonl", [self.rec("sl-adz2", "adzuna", detail_url="https://adz.example/2")])
        cfg = {**load_config(), "adzuna_detail_delay_seconds": 0}
        with mock.patch.object(sources, "lazy_description", side_effect=OSError("429")), mock.patch.object(time, "sleep"):
            out = run(jobsearch.cmd_shortlist, args(date="2003-02-04"), cfg)
        self.assertIn("WARN adzuna detail failed for sl-adz2: OSError", out)


class ScoreValidationTest(unittest.TestCase):
    def test_valid_score(self):
        short, profiles = {"k": {}}, {"appsec": {}}
        ok = {"key": "k", "profile": "appsec", "score": 70, "verdict": "apply"}
        self.assertEqual(jobsearch._valid_score(ok, short, profiles), "")
        for patch, err in [({"key": "x"}, "unknown key"), ({"profile": "zzz"}, "unknown profile"),
                           ({"score": 101}, "bad score"), ({"score": "70"}, "bad score"),
                           ({"score": -1}, "bad score"), ({"verdict": "maybe"}, "bad verdict")]:
            self.assertEqual(jobsearch._valid_score({**ok, **patch}, short, profiles), err)


class PublishEdgeCasesTest(unittest.TestCase):
    def test_publish_branches_digest_and_bookkeeping(self):
        run_id = "2003-03-03"
        d = run_dir(run_id)
        (d / "feeds.jsonl").write_text('{"x": 1}\n')

        def job(key, source="greenhouse", company=None, posted=None, **kw):
            return {"key": key, "profiles": ["backend-sde"], "title": f"Title {key}", "company": company or f"Co {key}",
                    "location": "Remote", "posted": posted or days_ago(1), "salary_text": "", "url": f"https://x.example/{key}",
                    "source": source, "jd": "j", "flags": [], "expires": None, **kw}

        def score(key, n, verdict="apply", **kw):
            return {"key": key, "profile": "backend-sde", "score": n, "verdict": verdict,
                    "gates": {"salary": "pass"}, "strengths": [], "gaps": [], "flags": [], **kw}
        write_jsonl(d / "shortlist.jsonl", [
            job("pub-stale", posted=days_ago(10)), job("pub-wwr", "wwr", expires="2030-01-01"),
            job("pub-al", "alignerr"), job("pub-gate"), job("pub-skip"), job("pub-lead", "custom"),
            job("pub-indeed-vague", "indeed"), job("pub-agg", company="Naukri"), job("pub-seen"),
            job("pub-hn", "hn"), job("pub-missing")])
        write_jsonl(d / "scores-1.jsonl", [
            score("pub-stale", 80, strengths=["Java"], gaps=["no k8s"], apply_url="https://apply.example/s"),
            score("pub-stale", 60),  # lower duplicate is ignored
            score("pub-wwr", 70, "consider", flags=["remote"]), score("pub-al", 65),
            score("pub-gate", 90, gates={"salary": "fail"}), score("pub-skip", 75, "skip"),
            score("pub-lead", 0, flags=["vague-jd"]), score("pub-indeed-vague", 0, flags=["vague-jd"]),
            score("pub-agg", 72), score("pub-seen", 88), score("pub-hn", 66),
            score("nope", 70), score("pub-stale-bad", 101)])
        write_jsonl(d / "rejected.jsonl", [{"key": "pub-rej", "reason": "title", "company": "R", "title": "Chef"}])
        db.mark_seen([{"key": "pub-seen", "status": "scored", "company": "S", "title": "T"}])
        out = run(jobsearch.cmd_publish, args(date=run_id, top=3))

        digest = (DATA / "digests" / f"{run_id}.md").read_text()
        self.assertIn("Scored 8 of 11 shortlisted; 5 added to tracker", digest)  # pub-seen is scored but already tracked
        self.assertIn("posted 10d ago: -3", digest)
        self.assertIn("[Title pub-stale — Co pub-stale](https://apply.example/s)", digest)
        self.assertIn("strengths: Java", digest)
        self.assertIn("gaps: no k8s", digest)
        self.assertIn("via We Work Remotely: apply through the listing (may not be on the company's careers page); "
                      "listing expires 2030-01-01", digest)
        self.assertIn("via Hacker News 'Who is hiring?'", digest)
        self.assertIn("Alignerr: hourly CONTRACT", digest)
        self.assertIn("aggregator repost", digest)
        self.assertIn("## Leads to check manually", digest)
        self.assertIn("Title pub-lead — Co pub-lead", digest)
        self.assertNotIn("pub-indeed-vague", digest)  # Indeed vague JDs are silently retried next run

        tracker_rows = list(csv.DictReader((HOME / "tracker.csv").open()))
        added = {r["Company"] for r in tracker_rows}
        self.assertTrue({"Co pub-stale", "Co pub-wwr", "Co pub-al", "Naukri", "Co pub-hn"} <= added)
        self.assertFalse({"Co pub-gate", "Co pub-skip", "Co pub-seen", "Co pub-lead"} & added)
        self.assertEqual(next(r for r in tracker_rows if r["Company"] == "Co pub-stale")["Apply URL"], "https://apply.example/s")

        seen = db.seen_keys()
        self.assertTrue({"pub-stale", "pub-lead", "pub-rej", "pub-gate"} <= seen)
        self.assertNotIn("pub-indeed-vague", seen)  # left unseen
        self.assertNotIn("pub-missing", seen)
        self.assertIn("INVALID nope: unknown key", out)
        self.assertIn("LEAD (no JD): Co pub-lead", out)
        self.assertRegex(out, r"NOT SCORED \(\d+\): .*pub-missing")
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("  ") and "—" in l and "LEAD" not in l]), 3)  # --top 3
        self.assertTrue((d / "feeds.jsonl.gz").exists() and not (d / "feeds.jsonl").exists())
        self.assertIn("| scored 8/11", (HOME / "CLAUDE.md").read_text())
        self.assertIn("invalid", (HOME / "CLAUDE.md").read_text())


class HousekeepingTest(unittest.TestCase):
    def test_retention_compresses_current_and_purges_old_raw_data(self):
        cur = run_dir("2003-04-01")
        (cur / "feeds.jsonl").write_text("x" * 5000)
        old, recent = RUNS_DIR / "1999-01-01", RUNS_DIR / "1999-01-02"
        for p in (old / "fc", recent):
            p.mkdir(parents=True, exist_ok=True)
        (old / "fc" / "page.md").write_text("cached")
        (old / "feeds.jsonl.gz").write_bytes(gzip.compress(b"old"))
        (recent / "indeed_raw.jsonl.gz").write_bytes(gzip.compress(b"new"))
        long_ago = time.time() - 30 * 86400
        for p in (old / "fc", old / "fc" / "page.md", old / "feeds.jsonl.gz"):
            os.utime(p, (long_ago, long_ago))
        msg = jobsearch.apply_retention({"retention_days_raw": 7}, cur)
        self.assertTrue(msg.startswith("retention: freed"))
        self.assertEqual(gzip.decompress((cur / "feeds.jsonl.gz").read_bytes()), b"x" * 5000)
        self.assertFalse((cur / "feeds.jsonl").exists())
        self.assertFalse((old / "fc").exists() or (old / "feeds.jsonl.gz").exists())
        self.assertTrue((recent / "indeed_raw.jsonl.gz").exists())

    def test_update_claude_md_keeps_newest_first_and_caps(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(jobsearch, "HOME", Path(tmp)):
            md = Path(tmp) / "CLAUDE.md"
            jobsearch.update_claude_md("2003-01-01", "| a")  # no file: no-op
            self.assertFalse(md.exists())
            md.write_text("intro\nno markers here\n")
            jobsearch.update_claude_md("2003-01-01", "| a")
            self.assertEqual(md.read_text(), "intro\nno markers here\n")
            md.write_text("head\n<!-- AUTO:RUNLOG:START -->\n<!-- AUTO:RUNLOG:END -->\ntail\n")
            for i, day in enumerate(["2003-01-01", "2003-01-02", "2003-01-03", "2003-01-02"], 1):
                jobsearch.update_claude_md(day, f"| run {i}", keep=2)
            text = md.read_text()
            self.assertTrue(text.startswith("head\n") and text.endswith("tail\n"))
            self.assertEqual([l for l in text.splitlines() if l.startswith("- ")],
                             ["- 2003-01-02 | run 4", "- 2003-01-03 | run 3"])


class Resp(io.BytesIO):
    pass


class FetchUrlTest(unittest.TestCase):
    def fetch(self, url, routes=None, body=None):
        def urlopen(req, timeout=None):
            return Resp(json.dumps(routes[req.full_url]).encode())
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(sources, "custom_description", return_value=body or ""):
            return run(jobsearch.cmd_fetch_url, args(url=url, date="2003-05-01"))

    def test_greenhouse(self):
        out = self.fetch("https://boards.greenhouse.io/acme/jobs/123?gh_src=x", {
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123": {
                "title": "Backend Engineer", "company_name": "Acme Inc", "location": {"name": "Remote"},
                "content": "<p>Build APIs.</p>", "absolute_url": "https://boards.greenhouse.io/acme/jobs/123"}})
        self.assertIn("Backend Engineer — Acme Inc — Remote", out)
        jd = (DATA / "tailoring" / "acme-inc-backend-engineer" / "jd.txt").read_text()
        self.assertIn("URL: https://boards.greenhouse.io/acme/jobs/123", jd)
        self.assertIn("Build APIs.", jd)

    def test_lever_eu_region(self):
        uid = "0123abcd-0123-abcd-0123-0123456789ab"
        out = self.fetch(f"https://jobs.eu.lever.co/globex/{uid}", {
            f"https://api.eu.lever.co/v0/postings/globex/{uid}?mode=json": {
                "text": "SRE", "categories": {"location": "Berlin"}, "descriptionPlain": "Keep it up.",
                "lists": [{"text": "Requirements", "content": "<li>Linux</li>"}], "additionalPlain": "Perks",
                "hostedUrl": f"https://jobs.eu.lever.co/globex/{uid}"}})
        self.assertIn("SRE — globex — Berlin", out)
        jd = (DATA / "tailoring" / "globex-sre" / "jd.txt").read_text()
        self.assertIn("Requirements", jd)
        self.assertIn("Perks", jd)

    def test_ashby_found_and_missing(self):
        uid = "11111111-2222-3333-4444-555555555555"
        board = {"jobs": [{"id": uid, "title": "Platform Engineer", "location": "Remote", "descriptionPlain": "Do it",
                           "jobUrl": f"https://jobs.ashbyhq.com/initech/{uid}"}]}
        routes = {"https://api.ashbyhq.com/posting-api/job-board/initech": board}
        self.assertIn("Platform Engineer — initech — Remote", self.fetch(f"https://jobs.ashbyhq.com/initech/{uid}", routes))
        other = "99999999-2222-3333-4444-555555555555"
        self.assertIn("FALLBACK", self.fetch(f"https://jobs.ashbyhq.com/initech/{other}", routes))

    def test_generic_page_and_fallbacks(self):
        body = "Responsibilities and requirements. " * 30
        out = self.fetch("https://www.careers-example.test/jobs/42", body=body)
        self.assertIn("(see description) — careers-example.test", out)
        self.assertIn("FALLBACK", self.fetch("https://www.careers-example.test/jobs/43", body="too short"))
        with mock.patch.object(sources, "custom_description") as cd:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                jobsearch.cmd_fetch_url(args(url="https://www.linkedin.com/jobs/view/1"), load_config())
        cd.assert_not_called()  # LinkedIn is never fetched
        self.assertIn("FALLBACK", buf.getvalue())


class TrackCommandTest(unittest.TestCase):
    def test_track_requires_fields_then_adds_once(self):
        with self.assertRaises(SystemExit):
            run(jobsearch.cmd_track, args(company="Co", role="R"))
        a = args(company="Track Co", role="Backend Engineer", url="https://x.example/t", profile="backend-sde",
                 score=77, location="Remote - India")
        out = run(jobsearch.cmd_track, a)
        self.assertIn("tracked Track Co — Backend Engineer: 1 pushed, 0 pending", out)
        rows = list(csv.DictReader((HOME / "tracker.csv").open()))
        self.assertEqual(sum(r["Company"] == "Track Co" for r in rows), 1)
        self.assertIn("already tracked/seen", run(jobsearch.cmd_track, a))
        self.assertEqual(sum(r["Company"] == "Track Co" for r in csv.DictReader((HOME / "tracker.csv").open())), 1)


if __name__ == "__main__":
    unittest.main()
