"""Tracker backends: CSV file, Google Sheets (fake session and fake google-auth), and the CLI."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

import tracker
from common import HOME

COLS = ["company", "role", "score", "url", "status"]
HEADER = ["Company", "Role", "Match Score", "Apply URL", "Status"]


def out_of(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue()


class ConfigHelpersTest(unittest.TestCase):
    def test_columns_default_and_unknown(self):
        self.assertEqual(tracker.columns({}), tracker.DEFAULT_COLUMNS)
        self.assertEqual(tracker.header({"tracker": {"columns": ["role", "posted"]}}), ["Role", "Posted"])
        with self.assertRaises(SystemExit) as cm:
            tracker.columns({"tracker": {"columns": ["role", "salary"]}})
        self.assertIn("salary", str(cm.exception))

    def test_backend_selection(self):
        self.assertIsInstance(tracker.backend({}), tracker.CsvBackend)
        self.assertIsInstance(tracker.backend({"tracker": {"backend": "gsheets"}}), tracker.SheetsBackend)
        with self.assertRaises(SystemExit):
            tracker.backend({"tracker": {"backend": "airtable"}})

    def test_row_handles_none_dicts_and_legacy_lists(self):
        self.assertEqual(tracker._row({"company": "A", "score": None, "role": 0}, ["company", "score", "role"]), ["A", "", 0])
        self.assertEqual(tracker._row(["A", "B", 70], ["company", "role", "score", "url"]), ["A", "B", 70, ""])


class CsvBackendTest(unittest.TestCase):
    def setUp(self):
        self.path = HOME / "tracker-csv2.csv"
        self.path.unlink(missing_ok=True)
        self.addCleanup(lambda: self.path.unlink(missing_ok=True))
        self.cfg = {"tracker": {"backend": "csv", "csv_path": self.path.name, "columns": COLS}}
        self.b = tracker.CsvBackend(self.cfg)

    def test_check_before_and_after_init(self):
        self.assertIn("not created yet", out_of(self.b.check))
        self.assertIn("tracker-csv2.csv", out_of(self.b.init))
        self.assertIn("header OK", out_of(self.b.check))
        out_of(self.b.init)  # idempotent: does not overwrite an existing file
        self.assertEqual(self.path.read_text().count("Company"), 1)

    def test_check_flags_header_mismatch_and_empty_file(self):
        self.path.write_text("A,B\n1,2\n")
        self.assertIn("HEADER MISMATCH: ['A', 'B']", out_of(self.b.check))
        self.path.write_text("")
        self.assertIn("HEADER MISMATCH: []", out_of(self.b.check))

    def test_append_writes_header_once(self):
        self.b.append([{"company": "A", "role": "B"}])
        self.b.append([{"company": "C", "role": "D", "score": 7}])
        rows = list(csv.reader(self.path.read_text().splitlines()))
        self.assertEqual(rows, [HEADER, ["A", "B", "", "", ""], ["C", "D", "7", "", ""]])

    def test_read_rows(self):
        self.assertEqual(self.b.read_rows(), [])  # no file yet
        self.b.append([{"company": "A", "role": "R", "score": "80", "url": "u", "status": "applied"},
                       {"company": "B", "score": "n/a"}])
        with self.path.open("a", newline="") as f:
            f.write(",,,,\n")  # blank row
        rows = self.b.read_rows()
        self.assertEqual(rows[0], {"company": "A", "role": "R", "score": 80, "url": "u", "status": "applied"})
        self.assertEqual((rows[1]["company"], rows[1]["score"], len(rows)), ("B", "n/a", 2))

    def test_describe(self):
        self.assertEqual(self.b.describe(), "tracker: csv tracker-csv2.csv")
        tracker.write_jsonl(tracker.PENDING_ROWS_PATH, [])
        self.assertTrue(tracker.describe(self.cfg).endswith("queued rows: 0"))


class Resp:
    def __init__(self, data=None, status=200, text=""):
        self.status_code, self._data, self.text = status, data if data is not None else {}, text
        self.ok = status < 400

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, tab_resp=None, header_values=None):
        self.calls = []
        self.credentials = types.SimpleNamespace(service_account_email="sa@proj.example")
        self.tab_resp = tab_resp or Resp({"sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}},
                                                     {"properties": {"sheetId": 5, "title": "Jobs"}}]})
        self.header_values = header_values

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if "/values/" in url:
            if "A1%3AE1" in url:
                return Resp({"values": [self.header_values]} if self.header_values else {})
            return Resp({"values": [["Company"], ["a"], ["b"]]})
        return self.tab_resp

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.post_resp if hasattr(self, "post_resp") else Resp({})

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        return Resp({})


class SheetsBackendTest(unittest.TestCase):
    def make(self, **g):
        cfg = {"tracker": {"backend": "gsheets", "columns": COLS,
                           "gsheets": {"sheet_id": "SID", "service_account_key": ".secrets/sa-test.json", **g}}}
        return tracker.SheetsBackend(cfg)

    def use(self, b, sess):
        p = mock.patch.object(b, "_session", return_value=(sess, "sa@proj.example"))
        p.start()
        self.addCleanup(p.stop)

    def test_last_col_and_range_quoting(self):
        b = self.make()
        self.assertEqual(b.last_col, "E")
        self.assertEqual(b._range({"title": "My Tab"}, "A:E"), "%27My%20Tab%27%21A%3AE")

    def test_session_requires_sheet_id_and_key(self):
        with self.assertRaisesRegex(tracker.Unavailable, "sheet_id"):
            tracker.SheetsBackend({"tracker": {"backend": "gsheets"}})._session()
        with self.assertRaisesRegex(tracker.Unavailable, "key not found"):
            self.make()._session()

    def test_session_builds_authorised_session(self):
        key = HOME / ".secrets" / "sa-test.json"
        key.parent.mkdir(exist_ok=True)
        key.write_text("{}")
        self.addCleanup(lambda: key.unlink(missing_ok=True))
        creds = types.SimpleNamespace(service_account_email="sa@proj.example")
        sa = types.SimpleNamespace(Credentials=types.SimpleNamespace(from_service_account_file=mock.Mock(return_value=creds)))
        sess_cls = mock.Mock(return_value="SESSION")
        mods = {"google": types.ModuleType("google"), "google.auth": types.ModuleType("google.auth"),
                "google.auth.transport": types.ModuleType("google.auth.transport"),
                "google.auth.transport.requests": types.SimpleNamespace(AuthorizedSession=sess_cls),
                "google.oauth2": types.ModuleType("google.oauth2"), "google.oauth2.service_account": sa}
        mods["google"].oauth2 = mods["google.oauth2"]
        mods["google.oauth2"].service_account = sa
        with mock.patch.dict(sys.modules, mods):
            self.assertEqual(self.make()._session(), ("SESSION", "sa@proj.example"))
        sess_cls.assert_called_once_with(creds)

    def test_tab_selection_and_403s(self):
        b = self.make(sheet_tab_gid=5)
        self.assertEqual(b._tab(FakeSession())["title"], "Jobs")
        self.assertEqual(self.make(sheet_tab_gid=99)._tab(FakeSession())["title"], "Sheet1")  # unknown gid -> first tab
        self.assertEqual(self.make()._tab(FakeSession())["title"], "Sheet1")
        disabled = Resp({"error": {"details": [{"reason": "SERVICE_DISABLED"}]}}, 403)
        with self.assertRaisesRegex(tracker.Unavailable, "not enabled"):
            b._tab(FakeSession(disabled))
        with self.assertRaisesRegex(tracker.Unavailable, "share the sheet with sa@proj.example"):
            b._tab(FakeSession(Resp({"error": {}}, 403)))
        with self.assertRaises(RuntimeError):
            b._tab(FakeSession(Resp({}, 500)))

    def test_append_posts_rows_in_column_order(self):
        b, sess = self.make(sheet_tab_gid=5), FakeSession()
        self.use(b, sess)
        self.assertEqual(b.append([{"company": "A", "role": "B", "score": 80, "url": "u", "status": ""}]),
                         "appended to tab 'Jobs'")
        _, url, kw = sess.calls[-1]
        self.assertIn("/values/%27Jobs%27%21A%3AE:append", url)
        self.assertEqual(kw["json"], {"values": [["A", "B", 80, "u", ""]]})
        self.assertEqual(kw["params"]["insertDataOption"], "INSERT_ROWS")

    def test_append_failure_is_unavailable(self):
        b, sess = self.make(), FakeSession()
        sess.post_resp = Resp({}, 400, "bad request")
        self.use(b, sess)
        with self.assertRaisesRegex(tracker.Unavailable, "append failed: 400 bad request"):
            b.append([{"company": "A"}])

    def test_read_rows_maps_columns_and_pads(self):
        b = self.make(sheet_tab_gid=5)
        sess = FakeSession()
        self.use(b, sess)
        rows = b.read_rows()
        self.assertEqual([r["company"] for r in rows], ["a", "b"])  # header skipped
        self.assertEqual(rows[0], {"company": "a", "role": "", "score": "", "url": "", "status": ""})
        self.assertIn("/values/%27Jobs%27%21A%3AE", sess.calls[-1][1])

    def test_check_reports_header(self):
        b = self.make(sheet_tab_gid=5)
        self.use(b, FakeSession(header_values=HEADER))
        out = out_of(b.check)
        self.assertIn("service account: sa@proj.example", out)
        self.assertIn("tab: 'Jobs' (gid 5), rows incl. header: 3", out)
        self.assertIn("header OK", out)
        b2 = self.make()
        self.use(b2, FakeSession(header_values=["x"]))
        self.assertIn("HEADER MISMATCH: ['x']", out_of(b2.check))

    def test_init_formats_tab_writes_header_and_saves_gid(self):
        b, sess = self.make(sheet_tab_gid=5), FakeSession()
        self.use(b, sess)
        with mock.patch.object(tracker, "save_config_value") as save:
            out = out_of(b.init)
        save.assert_called_once_with("tracker.gsheets.sheet_tab_gid", 5)
        self.assertIn("initialized tab 'Tracker' (gid 5)", out)
        methods = [c[0] for c in sess.calls]
        self.assertIn("PUT", methods)
        batch = next(c for c in sess.calls if c[1].endswith(":batchUpdate"))
        self.assertEqual(batch[2]["json"]["requests"][0]["updateSheetProperties"]["properties"]["title"], "Tracker")
        self.assertEqual(next(c for c in sess.calls if c[0] == "PUT")[2]["json"], {"values": [HEADER]})

    def test_init_keeps_existing_header(self):
        b, sess = self.make(sheet_tab_gid=5), FakeSession(header_values=HEADER)
        self.use(b, sess)
        with mock.patch.object(tracker, "save_config_value"):
            out_of(b.init)
        self.assertNotIn("PUT", [c[0] for c in sess.calls])

    def test_describe(self):
        self.assertIn("MISSING", self.make(sheet_url="https://sheet.example/x").describe())
        self.assertIn("https://sheet.example/x", self.make(sheet_url="https://sheet.example/x").describe())
        self.assertIn("(not set)", tracker.SheetsBackend({"tracker": {"backend": "gsheets"}}).describe())


class CliTest(unittest.TestCase):
    def setUp(self):
        self.home = _home.make_home()
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_cli(self, *args):
        env = {**os.environ, "JOB_SEARCH_HOME": str(self.home)}
        return subprocess.run([sys.executable, str(_home.SCRIPTS / "tracker.py"), *args], env=env,
                              capture_output=True, text=True, cwd=str(self.home))

    def test_csv_check_init_flush(self):
        p = self.run_cli("check")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("not created yet", p.stdout)
        self.assertIn("queued rows: 0", p.stdout)
        self.assertEqual(self.run_cli("init").returncode, 0)
        self.assertIn("header OK", self.run_cli("check").stdout)
        p = self.run_cli("flush")
        self.assertEqual(json.loads(p.stdout), {"pushed": 0, "pending": 0, "msg": "nothing queued"})

    def test_unknown_command_prints_usage(self):
        p = self.run_cli("bogus")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Tracker backends", p.stderr)

    def test_unavailable_backend_exits_with_message(self):
        cfg = self.home / "config.yaml"
        cfg.write_text(cfg.read_text().replace("backend: csv", "backend: gsheets", 1))
        p = self.run_cli("check")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("tracker unavailable: tracker.gsheets.sheet_id is not set", p.stderr)


if __name__ == "__main__":
    unittest.main()
