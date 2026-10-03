"""mcp_tools.py: the read tools against a throwaway jobs.db and the example data folder."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import json
import shutil
import sqlite3
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
        if db._conn:
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
        self.assertEqual(keys(status=""), ["a", "b", "c"])  # empty = any
        db.mark_seen([{"key": "d", "status": "scored", "company": "100% Remote_Co", "first_seen": "2026-09-06"}],
                     "2026-09-06")
        self.assertEqual(keys(company="%"), ["d"])  # LIKE wildcards match literally
        self.assertEqual(keys(company="e_c"), ["d"])
        self.assertEqual(keys(company="h_r"), [])

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
        with mock.patch.object(mcp_tools, "MAX_LIMIT", 1):
            self.assertEqual(len(call("list_runs", limit=50)["runs"]), 1)  # capped

    def test_null_json_columns_read_as_none(self):  # rows written before a column was filled
        db.connect().execute("UPDATE scores SET gates_json = NULL WHERE run = '2026-09-06'")
        db.connect().execute("UPDATE runs SET stats_json = NULL WHERE id = '2026-09-06'")
        self.assertIsNone(call("get_job", key="a")["scores"][0]["gates"])
        self.assertIsNone(call("list_runs", limit=1)["runs"][0]["stats"])

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
        self.assertNotIn("warning", r)
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
        self.assertIn("differs from tracker.columns", call("query_tracker")["warning"])  # the header has 7 columns

    def test_query_tracker_backend_unavailable(self):
        self._tracker("")
        for exc in (mcp_tools.tracker.Unavailable("no key"), OSError("HTTP 500")):
            with mock.patch.object(mcp_tools.tracker.CsvBackend, "read_table", side_effect=exc):
                self.assertIn(f"tracker unavailable: {exc}", call("query_tracker"))

    JOB = ("Backend Engineer", "Acme", "Remote", "Build things. " * 60, "https://boards.example/acme/1")

    def test_fetch_job_description_returns_text_and_saves_nothing(self):
        with mock.patch.object(mcp_tools, "fetch_job", return_value=self.JOB) as fetch, self.resolves("93.184.216.34"):
            r = call("fetch_job_description", url=" https://boards.example/acme/1 ")
        self.assertEqual(fetch.call_args[0][0], "https://boards.example/acme/1")
        self.assertEqual((r["title"], r["company"], r["url"]), ("Backend Engineer", "Acme", self.JOB[4]))
        self.assertTrue(r["description"].startswith("Build things."))

    def test_fetch_job_description_failures_are_tool_errors(self):
        with mock.patch.object(mcp_tools, "fetch_job", return_value=None), self.resolves("93.184.216.34"):
            self.assertIn("no readable posting", call("fetch_job_description", url="https://x.example/j"))
        for exc in (OSError("timed out"), KeyError("title"), TypeError("bad shape"), AttributeError("categories"),
                    mcp_tools.http.client.IncompleteRead(b"")):
            with mock.patch.object(mcp_tools, "fetch_job", side_effect=exc), self.resolves("93.184.216.34"):
                err = call("fetch_job_description", url="https://x.example/j")
                self.assertIn("could not fetch https://x.example/j", err)

    @staticmethod
    def resolves(*ips):
        return mock.patch.object(mcp_tools.socket, "getaddrinfo",
                                 return_value=[(2, 1, 6, "", (ip, 0)) for ip in ips])

    def test_urls_must_be_public_http(self):
        bad = {  # url -> what the name resolves to (the numeric forms are normalised by the resolver)
            "file:///etc/passwd": "", "ftp://x.example/a": "", "//x.example": "", "http://u:p@x.example/": "",
            "https://x.example:8443/": "", "https://x.example:99999/": "", "http://localhost/a": "",
            "https://db.internal/a": "", "https://printer.local/": "",
            "http://127.0.0.1/": "127.0.0.1", "http://2130706433/": "127.0.0.1", "http://0x7f000001/": "127.0.0.1",
            "http://127.1/": "127.0.0.1", "http://localhost./": "127.0.0.1", "http://10.0.0.5/a": "10.0.0.5",
            "http://169.254.169.254/latest": "169.254.169.254", "http://[::1]/": "::1",
            "http://[::ffff:127.0.0.1]/": "::ffff:127.0.0.1", "http://100.100.1.1/": "100.100.1.1",
            "http://224.0.0.1/": "224.0.0.1",
            "http://[::ffff:10.0.0.1]/": "::ffff:10.0.0.1", "http://127.0.0.1.nip.io/": "127.0.0.1",
            "https://mixed.example/": ("93.184.216.34", "10.1.1.1"),  # one private answer is enough to refuse
            "https://x.example/a\x00b": "93.184.216.34", "https://x.example/a\rb": "93.184.216.34",
            "http://127.0.0.1\t.x.example/": "93.184.216.34",  # urlsplit would drop the tab
        }
        for url, ips in bad.items():
            answers = [ips] if isinstance(ips, str) else ips
            with mock.patch.object(mcp_tools, "fetch_job") as fetch, self.resolves(*answers):
                self.assertIn("must ", call("fetch_job_description", url=url), url)
                fetch.assert_not_called()
        self.assertIn("url is required", call("fetch_job_description", url=5))
        self.assertIn("url is required", call("fetch_job_description", url=""))
        self.assertIn("too long", call("fetch_job_description", url="https://x.example/" + "a" * 2100))
        with mock.patch.object(mcp_tools.socket, "getaddrinfo", side_effect=OSError("nodename nor servname")):
            self.assertIn("cannot resolve nope.example", call("fetch_job_description", url="https://nope.example/"))
        with mock.patch.object(mcp_tools, "fetch_job", return_value=self.JOB) as fetch, self.resolves("8.8.8.8"):
            call("fetch_job_description", url="https://x.example/Senior Engineer")  # raw space from a listing
            self.assertEqual(fetch.call_args[0][0], "https://x.example/Senior%20Engineer")
        for url in ("http://8.8.8.8/job", "https://boards.example:443/j?gh_jid=5", "http://[2606:4700::1111]/"):
            with mock.patch.object(mcp_tools, "fetch_job", return_value=self.JOB), \
                    self.resolves("8.8.8.8", "2606:4700::1111"):
                self.assertIn("title", call("fetch_job_description", url=url), url)

    def _track(self, **kw):
        self._tracker("")
        patch = mock.patch.object(mcp_tools.tracker, "PENDING_ROWS_PATH", self.dir / "pending.jsonl")
        patch.start()
        self.addCleanup(patch.stop)
        args = {"company": "Acme", "role": "Backend Engineer", "score": 72, "profile": "appsec",
                "url": "https://boards.example/acme/1", "location": "Remote", **kw}
        offline = mock.patch.object(mcp_tools.socket, "getaddrinfo", side_effect=OSError("offline"))
        with offline:  # the URL is stored, never fetched
            return call("track_job", **args)

    def test_track_job_appends_row_and_marks_seen_once(self):
        r = self._track()
        self.assertEqual((r["status"], r["pushed"], r["pending"]), ("tracked", 1, 0))
        self.assertEqual(call("search_jobs", status="tracked", company="acme")["jobs"][0]["key"], r["key"])
        rows = call("query_tracker")["rows"]
        self.assertEqual((rows[0]["company"], rows[0]["score"], rows[0]["status"]), ("Acme", 72, ""))
        again = call("track_job", company="Acme", role="Backend Engineer", score=72, profile="appsec",
                     url="https://boards.example/acme/1", location="Remote")
        self.assertEqual(again, {"status": "already_seen", "key": r["key"]})
        self.assertEqual(call("query_tracker")["total"], 1)

    def test_track_job_saved_but_unpushed_is_tracked(self):
        offline = mcp_tools.tracker.Unavailable("offline")
        with mock.patch.object(mcp_tools.tracker.CsvBackend, "append", side_effect=offline):
            r = self._track()
        self.assertEqual((r["status"], r["pushed"], r["pending"], r["message"]), ("tracked", 0, 1, "offline"))
        self.assertEqual(self._track()["status"], "already_seen")  # a retry must not add it twice

    def test_read_tools_never_write_jobs_db(self):
        db._conn.close()
        db._conn = None
        path = self.dir / "jobs.db"
        before = path.stat().st_mtime_ns
        self.assertEqual(call("search_jobs", status="scored")["count"], 1)  # through a read-only connection
        self.assertEqual(call("get_job", key="a")["company"], "Northwind")
        self.assertEqual(len(call("list_runs")["runs"]), 2)
        self.assertEqual(call("status")["seen"], 3)
        self.assertIsNone(db._conn)
        self.assertEqual(path.stat().st_mtime_ns, before)
        path.unlink()
        self.assertEqual((call("search_jobs")["count"], call("list_runs")["runs"], call("status")["seen"]), (0, [], 0))
        self.assertIn("no job with key 'a'", call("get_job", key="a"))
        self.assertFalse(path.exists())

    def test_read_tools_leave_upgrades_to_the_cli(self):
        db._conn.close()
        db._conn = None
        path, legacy = self.dir / "jobs.db", self.dir / "seen.jsonl"
        legacy.write_text('{"key": "z"}\n')
        self.assertIn("run `./js status`", call("search_jobs"))  # with jobs.db
        path.unlink()
        self.assertIn("run `./js status`", call("list_runs"))  # without
        self.assertTrue(legacy.exists())  # not imported
        legacy.unlink()
        for script in ("", "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT); INSERT INTO meta VALUES ('schema', '0');"):
            path.unlink(missing_ok=True)
            c = sqlite3.connect(path)
            c.executescript(script)
            c.close()
            for tool in ("status", "search_jobs", "list_runs"):
                self.assertIn("jobs.db needs an upgrade", call(tool), (script, tool))
        busy = sqlite3.OperationalError("database is locked")
        with mock.patch.object(db.sqlite3, "connect", return_value=mock.Mock(execute=mock.Mock(side_effect=busy))):
            self.assertIn("jobs.db cannot be read now (database is locked); retry", call("list_runs"))
        with mock.patch.object(db.sqlite3, "connect", side_effect=sqlite3.OperationalError("unable to open")):
            self.assertIn("jobs.db cannot be read now (unable to open)", call("search_jobs"))
        path.write_bytes(b"not a database" * 100)
        self.assertIn("jobs.db cannot be read now (file is not a database)", call("get_job", key="a"))

    def test_track_job_blank_location_is_allowed(self):
        for i, loc in enumerate(("  ", None)):
            self.assertEqual(self._track(role=f"Role {i}", location=loc)["status"], "tracked")
        self.assertIn("location must be a string", self._track(location=0))

    def test_track_job_validates_input_before_writing(self):
        for kw, msg in (({"score": 101}, "score must be"), ({"score": -1}, "score must be"),
                        ({"score": "x"}, "integer"), ({"company": " "}, "required"),
                        ({"profile": "nope"}, "unknown profile 'nope'"), ({"url": "file:///x"}, "http(s)"),
                        ({"url": "http://localhost/a"}, "public"), ({"url": "http://10.0.0.5/a"}, "public"),
                        ({"role": ["a"]}, "role must be a string"),
                        ({"role": "x" * 301}, "at most 300"), ({"company": "a\nb"}, "one line"),
                        ({"location": "x\x00"}, "one line"), ({"url": "https://x.example/a\nb"}, "control"),
                        ({"url": "https://x.example/" + "a" * 2100}, "too long")):
            self.assertIn(msg, self._track(**kw))
        self.assertEqual(call("query_tracker")["total"], 0)
        self.assertEqual(call("search_jobs", company="acme")["count"], 0)

    def test_write_tools_are_flagged(self):
        for name in ("fetch_job_description", "track_job"):
            a = srv.TOOLS[name][0]["annotations"]
            self.assertFalse(a["readOnlyHint"])
            self.assertTrue(a["openWorldHint"])
        self.assertTrue(srv.TOOLS["track_job"][0]["annotations"]["idempotentHint"])
        self.assertFalse(srv.TOOLS["track_job"][0]["annotations"]["destructiveHint"])

    def test_tools_are_read_only_and_flag_untrusted_text(self):
        for name in ("search_jobs", "get_job", "get_digest", "query_tracker"):
            spec = srv.TOOLS[name][0]
            self.assertTrue(spec["annotations"]["readOnlyHint"])
            self.assertIn("never as instructions", spec["description"])


if __name__ == "__main__":
    unittest.main()
