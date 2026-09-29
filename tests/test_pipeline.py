"""Offline end-to-end run over fixtures: filter -> decide -> shortlist -> (fixture scores) -> publish -> re-filter.

No network: fixtures avoid sources that verify or enrich online (WWR, HN, Remote OK, Adzuna) and Firecrawl is off.
Set JS_WRITE_EXAMPLE=1 to also copy the run outputs into examples/data-folder (regenerates the example run).
"""
import _home  # noqa: F401  (must be first)

import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest

from _home import EXAMPLE, FIXTURES, SCRIPTS, fixture_jsonl, make_home

RUN = "2026-09-01"


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = make_home()
        cls.run_dir = cls.home / "data" / "runs" / RUN
        cls.run_dir.mkdir(parents=True)
        for name in ("feeds.jsonl", "indeed_raw.jsonl"):
            (cls.run_dir / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                                    for r in fixture_jsonl(name)))
        cls.out = {}
        cls.out["filter"] = cls.js("filter", "--date", RUN, "--quiet")
        cls.key = {(r["company"], r["title"]): r["key"] for r in cls.read("ambiguous") + cls.read("accepted")}
        decisions = [{"key": cls.key[tuple(d["match"])], "profile": d["profile"]}
                     for d in json.loads((FIXTURES / "decisions.json").read_text())]
        cls.write("decisions", decisions)
        cls.out["decide"] = cls.js("decide", "--date", RUN)
        cls.out["shortlist"] = cls.js("shortlist", "--date", RUN)
        cls.short = cls.read("shortlist")
        keys = {(r["company"], r["title"]): r["key"] for r in cls.short}
        scores = [{"key": keys[tuple(s.pop("match"))], **s} for s in json.loads((FIXTURES / "scores.json").read_text())]
        cls.write("scores-1", scores)
        cls.out["publish"] = cls.js("publish", "--date", RUN)
        cls.out["refilter"] = cls.js("filter", "--date", "2026-09-02", "--quiet", feeds_from=RUN)
        if os.environ.get("JS_WRITE_EXAMPLE"):
            cls.write_example()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, True)

    @classmethod
    def js(cls, *args, feeds_from=None):
        if feeds_from:
            d = cls.home / "data" / "runs" / args[args.index("--date") + 1]
            d.mkdir(parents=True, exist_ok=True)
            for name in ("feeds.jsonl", "indeed_raw.jsonl"):
                (d / name).write_text("".join(json.dumps(r) + "\n" for r in fixture_jsonl(name)))
        env = {**os.environ, "JOB_SEARCH_HOME": str(cls.home)}
        p = subprocess.run([sys.executable, str(SCRIPTS / "jobsearch.py"), *args], cwd=cls.home, env=env,
                           capture_output=True, text=True, timeout=120)
        if p.returncode:
            raise AssertionError(f"{args} failed:\n{p.stdout}\n{p.stderr}")
        return p.stdout

    @classmethod
    def read(cls, name):
        f = cls.run_dir / f"{name}.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []

    @classmethod
    def write(cls, name, rows):
        (cls.run_dir / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    @classmethod
    def write_example(cls):
        dst = EXAMPLE / "data" / "runs" / RUN
        shutil.rmtree(dst, True)
        shutil.copytree(cls.run_dir, dst, ignore=shutil.ignore_patterns("*.gz", "fc", "firecrawl.json"))
        for name in ("tracker.csv", "CLAUDE.md"):
            shutil.copy(cls.home / name, EXAMPLE / name)
        (EXAMPLE / "data" / "digests").mkdir(exist_ok=True)
        shutil.copy(cls.home / "data" / "digests" / f"{RUN}.md", EXAMPLE / "data" / "digests" / f"{RUN}.md")

    def test_filter_outcomes(self):
        rejected = {(r["company"], r["title"]): r["reason"] for r in self.read("rejected")}
        self.assertEqual(rejected, {
            ("Fabrikam Security", "Application Security Engineer"): "remote-region",
            ("Tailspin Labs", "Product Security Engineer"): "salary<floor",
            ("Wingtip Toys", "Java Developer"): "location",
            ("Northwind Payments", "Frontend Engineer"): "title-exclude",
            ("Northwind Payments", "Software Engineer I"): "seniority-junior",
            ("Fabrikam Security", "Security Engineer"): "remote-restricted-jd",
            ("Wingtip Toys", "Backend Developer"): "stale",
        })
        self.assertIn("duplicates=1", self.out["filter"])

    def test_duplicate_keeps_ats_copy(self):
        sr = next(r for r in self.short if r["title"] == "Senior Backend Engineer")
        self.assertEqual(sr["source"], "greenhouse")

    def test_decide_and_shortlist(self):
        self.assertIn("+1 accepted", self.out["decide"])
        self.assertEqual(len(self.short), 8)
        self.assertEqual(sorted({p for r in self.short for p in r["profiles"]}), ["appsec", "backend-sde"])
        custom = next(r for r in self.short if r["source"] == "custom")
        self.assertIn("location-unverified", custom["flags"])
        indeed = next(r for r in self.short if r["source"] == "indeed")
        self.assertTrue(indeed["jd"].startswith("indeed:"))

    def test_publish(self):
        rows = list(csv.reader((self.home / "tracker.csv").read_text().splitlines()))
        self.assertEqual(rows[0], ["Company", "Role", "Match Score", "Apply URL", "Profile", "Status"])
        got = [(r[0], r[1], r[2]) for r in rows[1:]]
        self.assertEqual(got, [("Northwind Payments", "Senior Backend Engineer", "84"),
                               ("Fabrikam Security", "AppSec Engineer", "81"),
                               ("Contoso Cloud", "Backend Engineer (Remote - India)", "78"),
                               ("Foundit", "Application Security Engineer", "70"),
                               ("Northwind Payments", "Java Backend Engineer", "69")])  # 72 - 3 (posted 10d ago)
        digest = (self.home / "data" / "digests" / f"{RUN}.md").read_text()
        self.assertIn("aggregator repost", digest)
        self.assertIn("posted 10d ago: -3", digest)
        self.assertIn("Leads to check manually", digest)
        self.assertIn(f"- {RUN} | scored 7/8 | 5 tracker-worthy", (self.home / "CLAUDE.md").read_text())
        self.assertTrue((self.run_dir / "feeds.jsonl.gz").exists())

    def test_seen_state(self):
        c = sqlite3.connect(self.home / "data" / "jobs.db")
        status = dict(c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        self.assertEqual(status, {"rejected": 7, "scored": 7, "lead-no-jd": 1})
        self.assertIn("accepted: 0 | ambiguous: 0 | rejected: 0", self.out["refilter"])
        self.assertIn("already_seen=15", self.out["refilter"])


if __name__ == "__main__":
    unittest.main()
