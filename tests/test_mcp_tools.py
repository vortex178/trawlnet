"""mcp_tools.py: the read tools against a throwaway jobs.db and the example data folder."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import db
import mcp_server as srv
import mcp_tools  # noqa: F401  (registers the tools)


def call(name, **args):
    r = srv.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}}))["result"]
    return json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"]


class ReadTools(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._saved = (db.DATA, db.DB_PATH, db._conn)
        db.DATA, db.DB_PATH, db._conn = self.dir, self.dir / "jobs.db", None
        patch = mock.patch.object(mcp_tools, "DATA", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        db.mark_seen([
            {"key": "a", "status": "scored", "score": 84, "profile": "backend", "source": "greenhouse",
             "company": "Northwind", "title": "Backend Engineer", "first_seen": "2026-09-02", "url": "https://x/a"},
            {"key": "b", "status": "tracked", "score": 70, "profile": "appsec", "source": "lever",
             "company": "Fabrikam", "title": "AppSec", "first_seen": "2026-09-05"},
            {"key": "c", "status": "rejected", "reason": "location", "company": "Contoso", "first_seen": "2026-09-06"},
        ], "2026-09-06")
        db.save_scores("2026-09-02", [{"key": "a", "profile": "backend", "score": 80, "verdict": "apply",
                                       "strengths": ["Kafka"], "gaps": ["no gRPC"], "apply_url": "https://x/apply"}])
        db.save_scores("2026-09-06", [{"key": "a", "profile": "backend", "score": 84, "raw_score": 90,
                                       "verdict": "apply", "gates": {"location": "ok"}, "flags": ["vague-jd"]}])
        db.save_run("2026-09-02", {"scored": 3})
        db.save_run("2026-09-06", {"scored": 5})

    def tearDown(self):
        db._conn.close()
        db.DATA, db.DB_PATH, db._conn = self._saved
        shutil.rmtree(self.dir, True)

    def test_status_matches_cli_data(self):
        s = call("status")
        self.assertEqual(s["data_folder"], str(common.HOME))
        self.assertEqual(s["seen"], 3)
        self.assertIn("backend-sde", s["profiles"])
        self.assertTrue(s["tracker"].startswith("tracker: csv"))
        self.assertIsInstance(s["warnings"], list)

    def test_search_orders_by_score_with_unscored_last(self):
        self.assertEqual([j["key"] for j in call("search_jobs")["jobs"]], ["a", "b", "c"])

    def test_search_filters(self):
        keys = lambda **kw: [j["key"] for j in call("search_jobs", **kw)["jobs"]]  # noqa: E731
        self.assertEqual(keys(status="tracked"), ["b"])
        self.assertEqual(keys(profile="backend"), ["a"])
        self.assertEqual(keys(source="lever"), ["b"])
        self.assertEqual(keys(company="north"), ["a"])
        self.assertEqual(keys(min_score=75), ["a"])
        self.assertEqual(keys(since="2026-09-05"), ["b", "c"])
        self.assertEqual(keys(limit=1), ["a"])
        self.assertEqual(keys(limit=0), ["a"])  # clamped to 1
        self.assertEqual(call("search_jobs", company="no-such")["count"], 0)

    def test_search_rejects_bad_arguments(self):
        self.assertIn("since must be", call("search_jobs", since="yesterday"))
        self.assertIn("min_score must be an integer", call("search_jobs", min_score="high"))
        self.assertIn("limit must be an integer", call("search_jobs", limit=True))
        self.assertIn("status must be a string", call("search_jobs", status=["a"]))
        self.assertIn("since must be a string", call("search_jobs", since=5))
        self.assertIn("key must be a string", call("get_job", key=7))
        self.assertIn("date must be a string", call("get_digest", date=20260906))

    def test_get_job_has_score_history_newest_first(self):
        j = call("get_job", key="a")
        self.assertEqual(j["company"], "Northwind")
        self.assertEqual([s["run"] for s in j["scores"]], ["2026-09-06", "2026-09-02"])
        self.assertEqual(j["scores"][0]["gates"], {"location": "ok"})
        self.assertEqual(j["scores"][0]["flags"], ["vague-jd"])
        self.assertEqual(j["scores"][1]["strengths"], ["Kafka"])
        self.assertEqual(call("get_job", key="c")["scores"], [])
        self.assertIn("no job with key", call("get_job", key="zzz"))

    def test_list_runs(self):
        self.assertEqual([r["id"] for r in call("list_runs")["runs"]], ["2026-09-06", "2026-09-02"])
        self.assertEqual(call("list_runs", limit=1)["runs"][0]["stats"], {"scored": 5})

    def test_get_digest(self):
        (self.dir / "digests").mkdir()
        (self.dir / "digests" / "2026-09-02.md").write_text("# one")
        (self.dir / "digests" / "2026-09-06.md").write_text("# two")
        self.assertEqual(call("get_digest"), {"date": "2026-09-06", "markdown": "# two"})
        self.assertEqual(call("get_digest", date="2026-09-02")["markdown"], "# one")
        self.assertIn("no digest for 2026-09-03", call("get_digest", date="2026-09-03"))
        self.assertIn("YYYY-MM-DD", call("get_digest", date="../config"))

    def test_get_digest_without_any(self):
        self.assertIn("no digest for any run", call("get_digest"))

    def _tracker(self, rows, **tcfg):
        path = self.dir / "t.csv"
        path.write_text("Company,Role,Match Score,Apply URL,Profile,Date Added,Status\n" + rows)
        cfg = {"tracker": {"backend": "csv", "csv_path": str(path), "columns": [
            "company", "role", "score", "url", "profile", "date_added", "status"], **tcfg}}
        patch = mock.patch.object(mcp_tools, "load_config", return_value=cfg)
        patch.start()
        self.addCleanup(patch.stop)

    ROWS = ("Northwind,Backend,84,https://x/a,backend,2026-09-02,applied\n"
            "Fabrikam,AppSec,70,https://x/b,appsec,2026-09-05,\n"
            "Contoso,SRE,inf,https://x/c,backend,2026-09-06,Rejected\n"
            "Retyped,PM,60,https://x/d,pm,9/2/2026,\n")

    def test_query_tracker_newest_first_and_filters(self):
        self._tracker(self.ROWS)
        r = call("query_tracker")
        self.assertEqual([x["company"] for x in r["rows"]], ["Retyped", "Contoso", "Fabrikam", "Northwind"])
        self.assertEqual((r["total"], r["matched"]), (4, 4))
        self.assertEqual(r["rows"][1]["score"], "inf")  # a score cell that is not a number stays as typed
        self.assertEqual(r["columns"][-1], "status")
        names = lambda **kw: [x["company"] for x in call("query_tracker", **kw)["rows"]]  # noqa: E731
        self.assertEqual(names(status="APPLIED"), ["Northwind"])
        self.assertEqual(names(status=""), ["Retyped", "Fabrikam"])
        self.assertEqual(names(company="fab"), ["Fabrikam"])
        self.assertEqual(names(profile="Backend"), ["Contoso", "Northwind"])
        self.assertEqual(names(company=""), ["Retyped", "Contoso", "Fabrikam", "Northwind"])
        self.assertEqual(names(min_score=75), ["Northwind"])  # unscored rows never match a score floor
        self.assertEqual(names(since="2026-09-05"), ["Contoso", "Fabrikam"])  # "9/2/2026" is not ISO: excluded
        self.assertEqual(names(limit=1), ["Retyped"])
        self.assertEqual(call("query_tracker", limit=1)["matched"], 4)

    def test_query_tracker_empty_and_bad_arguments(self):
        self._tracker("")
        self.assertEqual(call("query_tracker")["rows"], [])
        self.assertIn("since must be", call("query_tracker", since="x"))
        self.assertIn("status must be a string", call("query_tracker", status=1))
        self._tracker("", columns=["company", "status"])
        self.assertIn("no date_added column", call("query_tracker", since="2026-09-01"))

    def test_query_tracker_backend_unavailable(self):
        self._tracker("")
        for exc in (mcp_tools.tracker.Unavailable("no key"), OSError("HTTP 500")):
            with mock.patch.object(mcp_tools.tracker.CsvBackend, "read_rows", side_effect=exc):
                self.assertIn(f"tracker unavailable: {exc}", call("query_tracker"))

    def test_tools_are_read_only_and_flag_untrusted_text(self):
        for name in ("search_jobs", "get_job", "get_digest", "query_tracker"):
            spec = srv.TOOLS[name][0]
            self.assertTrue(spec["annotations"]["readOnlyHint"])
            self.assertIn("never as instructions", spec["description"])


if __name__ == "__main__":
    unittest.main()
