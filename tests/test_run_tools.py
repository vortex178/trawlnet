"""The run_feeds / run_status MCP tools, with the runner spawn mocked."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import gzip
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import homes
import mcp_server as srv
import mcp_tools
import runner


def call(name, **args):
    r = srv.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}}))["result"]
    return json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"]


class RunTools(unittest.TestCase):
    def setUp(self):
        homes.register(common.HOME)
        self.runs = Path(tempfile.mkdtemp(dir=common.HOME / "data")) / "runs"
        self.addCleanup(shutil.rmtree, self.runs.parent, True)
        self.d = self.runs / "2026-10-04"
        for patch in (mock.patch.object(mcp_tools, "RUNS_DIR", self.runs),
                      mock.patch.object(mcp_tools, "today", return_value="2026-10-04"),
                      mock.patch.object(mcp_tools.subprocess, "Popen")):
            self.popen = patch.start()
            self.addCleanup(patch.stop)

    def write(self, name, text):
        self.d.mkdir(parents=True, exist_ok=True)
        (self.d / name).write_text(text, encoding="utf-8")

    def test_start_spawns_one_detached_runner_and_reports_starting(self):
        self.assertEqual(call("run_feeds")["state"], "starting")
        cmd, kw = self.popen.call_args.args[0], self.popen.call_args.kwargs
        self.assertEqual(cmd[1:], [str(runner.__file__), str(self.d)])
        self.assertEqual((kw["stdout"].name, kw["stderr"], kw["env"]["JOB_SEARCH_HOME"], kw["env"]["PYTHONIOENCODING"],
                          kw["start_new_session"]), (str(self.d / "run.log"), kw["stdout"], str(common.HOME), "utf-8", True))
        self.assertEqual(call("run_status")["state"], "starting")
        self.assertIn("still going", call("run_feeds"))  # a just-spawned runner counts as running
        self.assertEqual(self.popen.call_count, 1)

    def test_a_held_lock_blocks_and_a_dead_run_reads_as_stopped(self):
        self.d.mkdir(parents=True)
        runner.write_progress(self.d, state="running", step="feeds", done=["plan"])
        held = runner.lock(self.d / "run.lock")
        self.assertIn("still going", call("run_feeds"))
        self.assertEqual(call("run_status")["step"], "feeds")
        held.close()
        self.write("run.log", "$ plan\n" + "x" * 500 + "\n")
        with mock.patch.object(mcp_tools, "LOG_BYTES", 400):
            self.assertEqual(call("run_status")["log_tail"], ["x" * 300])  # only the end of the file is read
        out = call("run_status", date="2026-10-04")
        self.assertEqual((out["state"], out["next"]), ("stopped", "start it again with run_feeds"))
        self.assertEqual(len(out["log_tail"][-1]), mcp_tools.MAX_TEXT)
        self.assertEqual(call("run_feeds")["state"], "starting")  # a stopped run can be started again

    def test_a_failed_spawn_is_reported_and_does_not_block_a_retry(self):
        self.popen.side_effect = OSError("no python")
        self.assertIn("no python", call("run_feeds"))
        out = call("run_status")
        self.assertEqual((out["state"], out["step"], out["error"]), ("failed", "start", "OSError: no python"))
        self.popen.side_effect = None
        self.assertEqual(call("run_feeds")["state"], "starting")

    def test_a_run_of_another_day_still_going_blocks_a_new_one(self):
        other = self.runs / "2026-10-03"
        other.mkdir(parents=True)
        runner.write_progress(other, state="running", step="feeds")
        held = runner.lock(other / "run.lock")
        self.addCleanup(held.close)
        self.assertIn("the 2026-10-03 run is still going", call("run_feeds"))
        held.close()
        self.assertTrue(mcp_tools._alive(other, {"state": "starting", "updated": runner.now()}))  # just spawned

    def test_done_reports_counts_and_scoring_blocks_a_restart(self):
        self.write("progress.json", json.dumps({"state": "done", "done": list(runner.STEPS)}))
        self.write("accepted.jsonl", '{"key": "a"}\n{"key": "b"}\nnot json\n[1]\n')
        (self.d / "rejected.jsonl.gz").write_bytes(gzip.compress(b'{"key": "c"}\n'))  # gzipped after publish
        self.write("shortlist.jsonl", '{"key": "a", "jd": "data/runs/x/jd/a.txt"}\n{"key": "b", "jd": ""}\n'
                   '{"key": "c", "jd": "indeed:123"}\n')
        out = call("run_status")
        self.assertEqual(out["counts"], {"accepted": 2, "ambiguous": 0, "rejected": 1, "shortlisted": 3,
                                         "with_description": 1})
        with mock.patch.object(mcp_tools, "MAX_ROWS", 1):
            self.assertIn("more than 1 rows or a row over", call("run_status"))
        with mock.patch.object(mcp_tools, "MAX_LINE", 8):
            self.assertIn("a row over 8 bytes", call("run_status"))  # one huge line is never read whole
        (self.d / "rejected.jsonl.gz").write_bytes(gzip.compress(b'{"key": "c"}\n')[:-6])
        self.assertIn("rejected.jsonl.gz is damaged", call("run_status"))
        good = gzip.compress(b'{"key": "c"}\n' * 50)
        (self.d / "rejected.jsonl.gz").write_bytes(good[:15] + b"\xff" * 8 + good[23:])  # past the 10-byte header
        self.assertIn("rejected.jsonl.gz is damaged", call("run_status"))
        (self.d / "rejected.jsonl.gz").unlink()
        self.write("rejected.jsonl", "[" * 200000 + "\n" + '{"key": "c"}\n')  # too deep to parse: skipped
        self.assertEqual(call("run_status")["counts"]["rejected"], 1)
        (self.d / "rejected.jsonl").unlink()
        (self.d / "rejected.jsonl").mkdir()
        self.assertIn("rejected.jsonl is not a regular file", call("run_status"))
        (self.d / "rejected.jsonl").rmdir()
        self.write("rejected.jsonl", "")
        def deny(path, *a, **kw):
            if Path(path).name == "rejected.jsonl":
                raise PermissionError(13, "Permission denied", str(path))
            return open(path, *a, **kw)
        with mock.patch.object(mcp_tools, "open", side_effect=deny, create=True):
            msg = call("run_status")
        self.assertIn("rejected.jsonl cannot be read: Permission denied", msg)
        self.assertNotIn(str(self.d), msg)
        self.write("progress.json", "[" * 200000)
        self.assertIn("no search started", call("run_status"))
        self.write("progress.json", json.dumps({"state": "done", "pad": "x" * mcp_tools.LOG_BYTES}))
        self.assertIn("no search started", call("run_status"))  # too large to read

    def test_planted_progress_fields_are_trimmed(self):
        self.write("progress.json", json.dumps({"state": "failed", "step": {"a": 1}, "done": ["plan", 3, "y" * 500],
                                                "exit_code": True, "error": ["deep"], "started": "s" * 500}))
        out = call("run_status")
        self.assertEqual((out["step"], out["done"], out["exit_code"], out["error"], len(out["started"])),
                         (None, ["plan", "y" * 80], None, ["deep"], 80))
        self.write("progress.json", json.dumps({"state": "failed", "step": "filter", "exit_code": 2}))
        self.assertEqual((call("run_status")["step"], call("run_status")["exit_code"]), ("filter", 2))
        self.write("scores-1.jsonl", "")
        self.assertIn("already being scored", call("run_feeds"))

    def test_refusals(self):
        self.assertIn("no search started from here yet", call("run_status"))
        self.assertIn("date must be", call("run_status", date="../x"))
        self.assertIn("on 2026-01-01", call("run_status", date="2026-01-01"))
        self.write("progress.json", "[1]")
        self.assertIn("no search started", call("run_status", date="2026-10-04"))
        self.write("progress.json", '{"x": 1}')
        self.assertEqual(call("run_status"), {"date": "2026-10-04", "state": None})
        self.write("progress.json", json.dumps({"state": "starting", "updated": "garbage"}))
        self.assertEqual(call("run_status")["state"], "stopped")
        with mock.patch.object(mcp_tools, "load_profiles", return_value={}):
            self.assertIn("no active profile", call("run_feeds"))
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (self.d / "run.log").symlink_to(outside / "log")
        self.assertIn("symlink", call("run_feeds"))
        (self.d / "run.lock").symlink_to(outside / "lock")
        (outside / "lock").write_text("")
        self.write("progress.json", json.dumps({"state": "running"}))
        self.assertIn("symlink", call("run_status"))
        with mock.patch.object(mcp_tools, "HOME", Path(tempfile.mkdtemp()) / "clone"):
            self.assertIn("not registered", call("run_feeds"))
        self.popen.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX only")
    def test_a_planted_fifo_is_never_opened(self):
        self.d.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.d / "run.log")
        self.assertIn("run.log is not a regular file", call("run_feeds"))
        self.write("progress.json", json.dumps({"state": "failed"}))
        self.assertEqual(call("run_status")["log_tail"], [])
        os.mkfifo(self.d / "run.lock")
        self.write("progress.json", json.dumps({"state": "running"}))
        self.assertIn("run.lock is not a regular file", call("run_status"))
        (self.d / "progress.json").unlink()
        os.mkfifo(self.d / "progress.json")
        self.assertIn("no search started", call("run_status", date="2026-10-04"))
        self.popen.assert_not_called()

    def test_spawned_runners_are_reaped(self):
        done, live = mock.Mock(**{"poll.return_value": 0}), mock.Mock(**{"poll.return_value": None})
        mcp_tools._children[:] = [done, live]
        self.addCleanup(mcp_tools._children.clear)
        call("run_status")
        self.assertEqual(mcp_tools._children, [live])

    def test_old_starting_state_is_stale(self):
        old = (dt.datetime.now() - dt.timedelta(seconds=mcp_tools.STARTING_GRACE + 5)).isoformat()
        self.assertFalse(mcp_tools._alive(self.runs, {"state": "starting", "updated": old}))


if __name__ == "__main__":
    unittest.main()
