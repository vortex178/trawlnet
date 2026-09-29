import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import csv
import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import firecrawl
import tracker
from common import salary_floor
from jobsearch import _adjust_score, _rank


def ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


class DbTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._saved = (db.DATA, db.DB_PATH, db._conn)
        db.DATA, db.DB_PATH, db._conn = self.dir, self.dir / "jobs.db", None

    def tearDown(self):
        if db._conn:
            db._conn.close()
        db.DATA, db.DB_PATH, db._conn = self._saved
        shutil.rmtree(self.dir, True)

    def test_legacy_import_and_first_decision_wins(self):
        (self.dir / "seen.jsonl").write_text(
            json.dumps({"key": "k1", "date": "2026-09-01", "status": "rejected", "reason": "location"}) + "\n"
            + json.dumps({"key": "k2", "date": "2026-09-01", "status": "scored", "score": 71, "profile": "appsec"}) + "\n")
        (self.dir / "wwr_verify.json").write_text(json.dumps({"https://wwr.example/1": {"free": True}}))
        self.assertEqual(db.seen_keys(), {"k1", "k2"})
        self.assertTrue((self.dir / "seen.jsonl.migrated").exists())
        self.assertFalse((self.dir / "seen.jsonl").exists())
        self.assertEqual(db.cache_get("wwr", "https://wwr.example/1"), {"free": True})
        added = db.mark_seen([{"key": "k1", "status": "scored"}, {"key": "k3", "status": "rejected", "company": "X"}],
                             "2026-09-02")
        self.assertEqual(added, 1)
        row = db.connect().execute("SELECT status, run FROM jobs WHERE key='k1'").fetchone()
        self.assertEqual(tuple(row), ("rejected", None))
        self.assertEqual(db.seen_count(), 3)

    def test_scores_runs_schema(self):
        db.save_scores("r1", [{"key": "k", "profile": "p", "score": 70, "raw_score": 75, "verdict": "apply",
                               "flags": ["posted 12d ago: -5"]}])
        db.save_run("r1", {"scored": 1})
        c = db.connect()
        self.assertEqual(c.execute("SELECT raw_score FROM scores").fetchone()[0], 75)
        self.assertEqual(json.loads(c.execute("SELECT stats_json FROM runs").fetchone()[0]), {"scored": 1})
        self.assertEqual(c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()[0], str(db.SCHEMA))


class TrackerCsvTest(unittest.TestCase):
    def setUp(self):
        self.path = _home.HOME / "tracker-test.csv"
        self.cfg = {"tracker": {"backend": "csv", "csv_path": self.path.name,
                                "columns": ["company", "role", "score", "url", "status"]}}
        tracker.write_jsonl(tracker.PENDING_ROWS_PATH, [])

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        tracker.PENDING_ROWS_PATH.unlink(missing_ok=True)

    def test_queue_flush_and_legacy_rows(self):
        tracker.queue_rows([{"company": "Contoso", "role": "Backend Engineer", "score": 80,
                             "url": "https://jobs.example.com/1", "profile": "backend-sde", "status": ""}])
        legacy = tracker.PENDING_ROWS_PATH.with_name("pending_sheet_rows.jsonl")
        legacy.write_text(json.dumps({"row": ["Fabrikam", "AppSec Engineer", 75, "https://jobs.example.com/2",
                                              "appsec", ""]}) + "\n")
        self.assertEqual(tracker.flush(self.cfg)[:2], (2, 0))
        self.assertFalse(legacy.exists())
        rows = list(csv.reader(self.path.read_text().splitlines()))
        self.assertEqual(rows[0], ["Company", "Role", "Match Score", "Apply URL", "Status"])
        self.assertEqual(rows[1:], [["Contoso", "Backend Engineer", "80", "https://jobs.example.com/1", ""],
                                    ["Fabrikam", "AppSec Engineer", "75", "https://jobs.example.com/2", ""]])
        self.assertEqual(tracker.flush(self.cfg), (0, 0, "nothing queued"))

    def test_unavailable_keeps_queue(self):
        cfg = {"tracker": {"backend": "gsheets", "gsheets": {"sheet_id": ""}}}
        tracker.queue_rows([{"company": "A", "role": "B"}])
        pushed, pending, msg = tracker.flush(cfg)
        self.assertEqual((pushed, pending), (0, 1))
        self.assertIn("sheet_id", msg)

    def test_legacy_sheet_config(self):
        self.assertEqual(tracker.tracker_cfg({"sheet": {"sheet_id": "x"}})["backend"], "gsheets")
        self.assertEqual(tracker.tracker_cfg({})["backend"], "csv")


class BudgetTest(unittest.TestCase):
    def make(self, usage):
        cfg = {"firecrawl": {"enabled": False, "base_credits_per_run": 20, "reserve_credits": 50}}
        b = firecrawl.Budget(cfg, "2000-01-01")
        b.enabled, b.key = True, "test"
        with mock.patch.object(b, "_req", return_value={"data": usage}):
            return b._compute_allowance()

    def tearDown(self):
        shutil.rmtree(_home.HOME / "data" / "runs" / "2000-01-01", True)

    def test_spreads_remaining_over_period(self):
        end = (dt.date.today() + dt.timedelta(days=9)).isoformat()  # 10 days incl. today
        self.assertEqual(self.make({"remainingCredits": 550, "billingPeriodEnd": end})[0], 50)

    def test_floor_is_base_but_never_eats_reserve(self):
        end = (dt.date.today() + dt.timedelta(days=29)).isoformat()
        self.assertEqual(self.make({"remainingCredits": 350, "billingPeriodEnd": end})[0], 20)  # avg 10 < base
        self.assertEqual(self.make({"remainingCredits": 60, "billingPeriodEnd": end})[0], 10)   # only 10 above reserve

    def test_no_period_falls_back_to_base(self):
        self.assertEqual(self.make({"remainingCredits": 500})[0], 20)

    def test_disabled_without_key(self):
        b = firecrawl.Budget({"firecrawl": {"enabled": True, "api_key_file": ".secrets/none.key"}}, "2000-01-01")
        self.assertFalse(b.enabled)
        self.assertEqual(b.left(), 0)
        self.assertIsNone(b.scrape_markdown("https://example.com"))


class ScoringAdjustTest(unittest.TestCase):
    cfg = {"stale_after_days": 7, "stale_penalty_per_day": 1, "stale_penalty_max": 10,
           "aggregator_companies": ["foundit", "naukri"]}

    def test_stale_penalty_capped(self):
        for age, want in [(3, 80), (7, 80), (10, 77), (40, 70)]:
            s = {"score": 80}
            _adjust_score(s, {"company": "Contoso", "posted": ago(age)}, self.cfg)
            self.assertEqual(s["score"], want, age)

    def test_undated_untouched_and_aggregator_flag(self):
        s = {"score": 70}
        _adjust_score(s, {"company": "Foundit (Monster India)", "posted": None}, self.cfg)
        self.assertEqual(s["score"], 70)
        s = {"score": 70}
        _adjust_score(s, {"company": "Foundit", "posted": None}, self.cfg)
        self.assertTrue(any("aggregator" in f for f in s["flags"]))

    def test_rank_prefers_fresh_remote(self):
        base = {"routes": [("p", 3)], "flags": [], "loc_bucket": "bengaluru"}
        prefs = {"work": {"remote_bias": "strong"}}
        fresh_remote = _rank({**base, "loc_bucket": "remote", "posted": ago(1)}, prefs)
        fresh_onsite = _rank({**base, "posted": ago(1)}, prefs)
        stale_onsite = _rank({**base, "posted": ago(13)}, prefs)
        self.assertGreater(fresh_remote, fresh_onsite)
        self.assertGreater(fresh_onsite, stale_onsite)
        self.assertEqual(_rank({**base, "loc_bucket": "remote"}, {"work": {"remote_bias": "none"}}), _rank(base, {}))

    def test_salary_floor(self):
        prefs = {"salary": {"default": {"min_lpa": 26}, "appsec": {"min_annual": 3_000_000}}}
        self.assertEqual(salary_floor(prefs, "sde", "INR"), 2_600_000)
        self.assertEqual(salary_floor(prefs, "appsec", "INR"), 3_000_000)
        self.assertIsNone(salary_floor(prefs, "sde", "USD"))  # LPA only means something in INR
        self.assertEqual(salary_floor({"salary": {"default": {"min_inr_lpa": 20}}}, "x", "INR"), 2_000_000)


if __name__ == "__main__":
    unittest.main()
