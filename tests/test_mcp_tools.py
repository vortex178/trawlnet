"""mcp_tools.py: the read tools against a throwaway jobs.db and the example data folder."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import db
import homes
import mcp_server as srv
import mcp_content  # noqa: F401  (registers the prompts get_instructions serves)
import mcp_tools  # noqa: F401  (registers the tools)
import urlguard
import yaml


def call(name, **args):
    r = srv.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}}))["result"]
    return json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"]


class UnregisteredFolder(unittest.TestCase):
    def test_outward_and_config_tools_need_a_registered_folder(self):
        with mock.patch.object(mcp_tools, "HOME", Path(tempfile.mkdtemp()) / "clone"):
            for name, args in (("query_tracker", {}), ("fetch_job_description", {"url": "https://example.com/j"}),
                               ("track_job", {"company": "A", "role": "B", "score": 1, "profile": "p",
                                              "url": "https://example.com/j"}),
                               ("save_profile", {"profile": {"id": "p", "label": "P", "target_titles": ["x"]}})):
                self.assertIn("not registered for the MCP server", call(name, **args), name)


class ReadTools(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._saved = (db.DATA, db.DB_PATH, db._conn)
        db.DATA, db.DB_PATH, db._conn = self.dir, self.dir / "jobs.db", None
        patch = mock.patch.object(mcp_tools, "DATA", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        homes.register(common.HOME)
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
        clone = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, clone, True)
        with mock.patch.object(mcp_tools, "HOME", clone), mock.patch.object(common, "HOME", clone):
            self.assertIn("No config.yaml", call("status"))  # not a data folder at all
            (clone / "config.yaml").write_text("country: [", encoding="utf-8")
            self.assertEqual(set(call("status")), {"data_folder", "registered", "next"})  # its config is never read

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
        patch = mock.patch.object(common, "HOME", self.dir)  # the digests live in the (patched) data folder
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(call("get_digest"), {"date": "2026-09-06", "markdown": "# two"})
        self.assertEqual(call("get_digest", date="2026-09-02")["markdown"], "# one")
        self.assertIn("no digest for 2026-09-03", call("get_digest", date="2026-09-03"))
        self.assertIn("YYYY-MM-DD", call("get_digest", date="../config"))

    def test_get_digest_refuses_a_link_out_of_the_folder(self):
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("secret")
        self.addCleanup(shutil.rmtree, outside.parent, True)
        patch = mock.patch.object(common, "HOME", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        (self.dir / "digests").mkdir()
        (self.dir / "digests" / "2099-01-01.md").symlink_to(outside)
        (self.dir / "config.yaml").write_text("secret: 1")
        (self.dir / "digests" / "2099-01-02.md").symlink_to(self.dir / "config.yaml")  # inside the folder, not digests/
        for args in ({}, {"date": "2099-01-01"}, {"date": "2099-01-02"}):
            self.assertIn("symlink", call("get_digest", **args))

    def test_get_digest_refuses_a_linked_digests_directory(self):
        patch = mock.patch.object(common, "HOME", self.dir)
        patch.start()
        self.addCleanup(patch.stop)
        (self.dir / "secrets").mkdir()
        (self.dir / "secrets" / "2026-01-01.md").write_text("SECRETKEY")
        (self.dir / "digests").symlink_to(self.dir / "secrets")
        for args in ({}, {"date": "2026-01-01"}):
            self.assertIn("symlink", call("get_digest", **args))
        elsewhere = Path(tempfile.mkdtemp())  # a DATA outside HOME
        self.addCleanup(shutil.rmtree, elsewhere, True)
        (elsewhere / "digests").mkdir()
        (elsewhere / "digests" / "2026-01-01.md").write_text("x")
        with mock.patch.object(mcp_tools, "DATA", elsewhere):
            self.assertIn("outside the data folder", call("get_digest", date="2026-01-01"))

    def test_out_of_range_integers_and_annotations(self):
        self.assertIn("out of range", call("search_jobs", min_score=10**23))
        tools = {t["name"]: t["annotations"] for t in srv.handle(
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')["result"]["tools"]}
        self.assertFalse(tools["get_digest"]["openWorldHint"])
        self.assertTrue(tools["query_tracker"]["openWorldHint"])
        self.assertFalse(tools["fetch_job_description"]["destructiveHint"])

    def test_get_digest_without_any(self):
        self.assertIn("no digest for any run", call("get_digest"))

    def _tracker(self, rows, **tcfg):
        path = self.dir / "t.csv"
        path.write_text("Company,Role,Match Score,Apply URL,Profile,Date Added,Status\n" + rows)
        cfg = {"tracker": {"backend": "csv", "csv_path": "t.csv", "columns": [
            "company", "role", "score", "url", "profile", "date_added", "status"], **tcfg}}
        for patch in (mock.patch.object(mcp_tools, "load_config", return_value=cfg),
                      mock.patch.object(mcp_tools.tracker, "HOME", self.dir)):  # the config path stays in the folder
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
        return mock.patch.object(urlguard.socket, "getaddrinfo",
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
            "http://[64:ff9b::a00:1]/": "64:ff9b::a00:1", "http://[::7f00:1]/": "::7f00:1",  # embedded IPv4
            "http://[2002:a00:1::1]/": "2002:a00:1::1", "https://nat64.example/": "64:ff9b:1::a00:1",
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
        with mock.patch.object(urlguard.socket, "getaddrinfo", side_effect=OSError("nodename nor servname")):
            self.assertIn("cannot resolve nope.example", call("fetch_job_description", url="https://nope.example/"))
        with mock.patch.object(mcp_tools, "fetch_job", return_value=self.JOB) as fetch, self.resolves("8.8.8.8"):
            call("fetch_job_description", url="https://x.example/Senior Engineer")  # raw space from a listing
            self.assertEqual(fetch.call_args[0][0], "https://x.example/Senior%20Engineer")
            self.assertTrue(fetch.call_args[1]["public_only"])  # redirects are checked too
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
        offline = mock.patch.object(urlguard.socket, "getaddrinfo", side_effect=OSError("offline"))
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


class SaveProfile(unittest.TestCase):
    PROF = {"id": "backend-eng", "label": "Backend Engineer", "years_experience": 6, "summary": "Builds APIs.\nSix years.",
            "target_titles": ["backend engineer"], "seniority_allowed": ["Mid", "senior"], "core_skills": ["go"]}

    def setUp(self):
        homes.register(common.HOME)
        self.dir = Path(tempfile.mkdtemp(dir=common.HOME / "data")) / "profiles"
        self.addCleanup(shutil.rmtree, self.dir.parent, True)
        patch = mock.patch.object(mcp_tools, "PROFILES_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def save(self, prof=None, **args):
        return call("save_profile", profile=prof or self.PROF, **args)

    def load(self, name):
        return yaml.safe_load((self.dir / name).read_text(encoding="utf-8"))

    def test_saves_the_profile_in_schema_order_and_numbers_new_facts(self):
        out = self.save(facts=[{"text": "Built Kafka ingestion.", "org": "Acme", "skills": ["kafka"]},
                               {"text": "BSc CS", "kind": "education"}])
        self.assertEqual(out, {"saved": "data/profiles/backend-eng.yaml", "replaced": False, "facts_added": 2,
                               "facts_total": 2})
        prof = self.load("backend-eng.yaml")
        self.assertEqual(list(prof), ["id", "label", "years_experience", "target_titles", "title_related",
                                      "seniority_allowed", "core_skills", "summary"])
        self.assertEqual(prof["title_related"], [])  # not route()'s broad default
        self.save(dict(self.PROF, title_related=["SRE"]), replace=True)
        self.assertEqual(self.load("backend-eng.yaml")["title_related"], ["SRE"])
        self.assertEqual(prof["seniority_allowed"], ["mid", "senior"])
        self.assertEqual(self.load("master.yaml")["facts"][0], {"id": "F001", "resumes": ["backend-eng"],
                         "kind": "experience", "org": "Acme", "text": "Built Kafka ingestion.", "skills": ["kafka"]})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["backend-eng.yaml", "master.yaml"])  # no .tmp

    def test_existing_profile_needs_replace_and_facts_only_grow(self):
        self.dir.mkdir()
        (self.dir / "master.yaml").write_text("facts:\n- {id: F007, resumes: [appsec], text: Shared bullet}\n"
                                              "- {id: Fx, text: odd}\n- just a string\n", encoding="utf-8")
        (self.dir / "master.yaml").chmod(0o640)
        self.save(facts=[{"text": "Shared bullet"}])
        self.assertIn("already exists", self.save())
        out = self.save(replace=True, facts=[{"text": "Shared bullet"}, {"text": "New one"}])
        self.assertEqual((out["replaced"], out["facts_added"], out["facts_total"]), (True, 1, 4))
        facts = self.load("master.yaml")["facts"]
        self.assertEqual(facts[0]["resumes"], ["appsec", "backend-eng"])
        self.assertEqual(facts[1:3], [{"id": "Fx", "text": "odd"}, "just a string"])  # hand edits are kept
        self.assertEqual(facts[3]["id"], "F008")
        self.assertEqual((self.dir / "master.yaml").stat().st_mode & 0o777, 0o640)  # the file's mode is kept
        umask = os.umask(0o027)
        try:
            self.save(dict(self.PROF, id="fresh"))
        finally:
            os.umask(umask)
        self.assertEqual((self.dir / "fresh.yaml").stat().st_mode & 0o777, 0o640)  # new files follow the umask
        self.assertEqual(self.save(dict(self.PROF, id="other", active=False))["facts_total"], None)  # master untouched
        self.assertIs(self.load("other.yaml")["active"], False)

    def test_hand_edited_facts_keep_their_meaning(self):
        self.dir.mkdir()
        (self.dir / "master.yaml").write_text(
            "facts:\n- {id: F001, resumes: appsec, text: Shared}\n- {id: F002, retired: true, text: Old}\n"
            "- {id: F005, retired: 'false', text: Back}\n"
            "- {id: F003, text: [a, b]}\n- {id: [F009], text: Listed}\n", encoding="utf-8")
        self.save(dict(self.PROF, resume="data/resumes/backend-eng.md"),
                  facts=[{"text": "Shared"}, {"text": "Old"}, {"text": "Back"}])
        facts = self.load("master.yaml")["facts"]
        self.assertEqual(facts[0]["resumes"], ["appsec", "backend-eng"])
        self.assertNotIn("resumes", facts[1])  # a retired fact stays retired; the returning bullet gets a new id
        self.assertEqual(facts[2]["resumes"], ["backend-eng"])  # retired only when literally true
        self.assertEqual((facts[5]["id"], facts[5]["text"]), ("F006", "Old"))  # a non-string id is not counted
        self.assertEqual(self.load("backend-eng.yaml")["resume"], "data/resumes/backend-eng.md")
        self.save(dict(self.PROF, core_skills=["\u0420\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0430"]),
                  replace=True)  # non-Latin skills are fine; the resume link is kept when a replace omits it
        self.assertIn("\u0420\u0430\u0437", (self.dir / "backend-eng.yaml").read_text(encoding="utf-8"))  # unescaped
        (self.dir / "master.yaml").write_text('facts:\n- {id: F001, text: "a\\Nb"}\n', encoding="utf-8")
        self.save(facts=[{"text": "c"}], replace=True)
        self.assertEqual(self.load("master.yaml")["facts"][0]["text"], "a\x85b")  # NEL kept: escaped, not raw
        self.assertEqual(list(self.load("backend-eng.yaml"))[:3], ["id", "label", "resume"])
        (self.dir / "backend-eng.yaml").write_text("id: [\n", encoding="utf-8")
        self.assertEqual(self.save(replace=True)["replaced"], True)  # a broken profile can be replaced

    def test_bad_input_is_refused_and_nothing_is_written(self):
        bad = [("profile must be an object", {"profile": "x"}),
               ("unknown fields: extra", {"profile": dict(self.PROF, extra=1)}),
               ("can only be data/resumes/backend-eng.md", {"profile": dict(self.PROF, resume="/etc/passwd")}),
               ("Latin letters", {"profile": dict(self.PROF, title_exclude=["-"])}),
               ("Latin letters", {"profile": dict(self.PROF, target_titles=["\u958b\u767a"])}),
               ("a letter or digit", {"profile": dict(self.PROF, core_skills=["--"])}),
               ("one line", {"profile": dict(self.PROF, family="a\u2028b")}),
               ("summary must be text", {"profile": dict(self.PROF, summary="a\x85b")}),
               ("kebab-case", {"profile": dict(self.PROF, id="Back_End")}),
               ("kebab-case", {"profile": dict(self.PROF, id="master")}),
               ("kebab-case", {"profile": dict(self.PROF, id="../x")}),
               ("non-empty string", {"profile": dict(self.PROF, id=5)}),
               ("are required", {"profile": dict(self.PROF, target_titles=[])}),
               ("one line", {"profile": dict(self.PROF, label="a\nb")}),
               ("at most 40", {"profile": dict(self.PROF, core_skills=["x"] * 41)}),
               ("some of", {"profile": dict(self.PROF, seniority_allowed=["boss"])}),
               ("some of", {"profile": dict(self.PROF, seniority_allowed=[])}),
               ("0 to 60", {"profile": dict(self.PROF, years_experience=True)}),
               ("true or false", {"profile": dict(self.PROF, active="yes")}),
               ("summary must be text", {"profile": dict(self.PROF, summary="x\x00")}),
               ("kind must be", {"profile": self.PROF, "facts": [{"text": "a", "kind": "hobby"}]}),
               ("non-empty string", {"profile": self.PROF, "facts": [{"org": "a"}]}),
               ("must be a list", {"profile": self.PROF, "facts": {"text": "a"}}),
               ("replace must be", {"profile": self.PROF, "replace": "yes"})]
        for why, args in bad:
            self.assertIn(why, call("save_profile", **args), why)
        self.assertFalse(self.dir.exists())

    def test_damaged_master_or_a_duplicate_id_stops_the_save(self):
        self.dir.mkdir()
        (self.dir / "master.yaml").write_text("- not a mapping\n", encoding="utf-8")
        self.assertIn("no facts list", self.save(facts=[{"text": "a"}]))
        (self.dir / "old.yaml").write_text("id: backend-eng\n", encoding="utf-8")
        (self.dir / "broken.yaml").write_text("id: [\n", encoding="utf-8")
        self.assertIn("old.yaml already uses the id", self.save())
        self.assertFalse((self.dir / "backend-eng.yaml").exists())

    def test_symlinked_files_or_folders_are_refused(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        self.dir.mkdir()
        (self.dir / "backend-eng.yaml").symlink_to(outside / "x.yaml")
        self.assertIn("symlink", self.save(replace=True))
        self.assertIn("symlink", self.save(dict(self.PROF, id="other")))  # its id is unknown, so it could clash
        (self.dir / "backend-eng.yaml").unlink()
        (self.dir / "backend-eng.yaml.tmp").symlink_to(outside / "planted")  # an old fixed temp name, linked out
        self.save()
        self.assertFalse((outside / "planted").exists())
        with mock.patch.object(mcp_tools.os, "replace", side_effect=OSError("disk full")):
            self.assertIn("disk full", self.save(dict(self.PROF, id="other")))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["backend-eng.yaml", "backend-eng.yaml.tmp"])
        shutil.rmtree(self.dir)
        self.dir.symlink_to(outside, target_is_directory=True)
        self.assertIn("symlink", self.save())
        self.assertEqual(list(outside.iterdir()), [])


class GetInstructions(unittest.TestCase):
    def test_serves_prompts_and_rubrics_as_text(self):
        text = call("get_instructions", task="build_profile", role="backend engineer")["text"]
        self.assertIn("for the role: backend engineer", text)
        self.assertIn("save_profile", text)
        self.assertIn("get_job", call("get_instructions", task="tailor_for_job", key="abc")["text"])
        self.assertIn("since=", call("get_instructions", task="weekly_review", days="14")["text"])
        self.assertIn("Calibration", call("get_instructions", task="scoring_rubric")["text"])
        self.assertIn("Never create new experience", call("get_instructions", task="tailoring_rules")["text"])

    def test_every_prompt_is_served(self):
        self.assertEqual(set(mcp_tools.WORKFLOWS), set(srv.PROMPTS))
        spec = srv.TOOLS["get_instructions"][0]["inputSchema"]["properties"]["task"]["enum"]
        self.assertEqual(set(spec), {*srv.PROMPTS, *mcp_tools.DOCS})
        self.assertIn("since=", call("get_instructions", task="weekly_review", days=14, extra="x")["text"])

    def test_bad_task_or_arguments_are_tool_errors(self):
        self.assertIn("unknown task", call("get_instructions", task="x"))
        self.assertIn("must be a string", call("get_instructions", task=5))
        self.assertIn("key is required", call("get_instructions", task="tailor_for_job"))
        self.assertIn("days must be", call("get_instructions", task="weekly_review", days="soon"))


if __name__ == "__main__":
    unittest.main()
