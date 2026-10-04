"""runner.py: the background steps, their lock and progress file, with the steps mocked."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import runner


class Runner(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()) / "2026-10-04"
        self.d.mkdir()
        self.addCleanup(shutil.rmtree, self.d.parent, True)

    def progress(self):
        return json.loads((self.d / "progress.json").read_text(encoding="utf-8"))

    def test_runs_the_steps_in_order_with_the_run_date(self):
        def step(cmd, **kw):
            kw["stdout"].write(f"ran {cmd[2]}\n")
            self.assertTrue(runner.running(self.d))  # the lock is held throughout
            return 0
        self.assertFalse(runner.running(self.d))  # no lock file yet
        (self.d / "run.log").write_text("started by run_feeds\n", encoding="utf-8")
        with mock.patch.object(runner.subprocess, "call", side_effect=step) as call_:
            self.assertEqual(runner.run(self.d), 0)
        self.assertEqual([c.args[0][2:] for c in call_.call_args_list],
                         [[s, "--date", "2026-10-04", "--quiet"] for s in runner.STEPS])
        prog = self.progress()
        self.assertEqual((prog["state"], prog["done"]), ("done", list(runner.STEPS)))
        log = (self.d / "run.log").read_text(encoding="utf-8")
        self.assertTrue(log.startswith("started by run_feeds\n$ plan\nran plan"))  # appended, not truncated
        self.assertIn("$ feeds\nran feeds", log)
        self.assertFalse(runner.running(self.d))

    def test_a_failed_step_stops_the_run(self):
        with mock.patch.object(runner.subprocess, "call", side_effect=[0, 0, 2]):
            self.assertEqual(runner.run(self.d), 2)
        prog = self.progress()
        self.assertEqual((prog["state"], prog["step"], prog["exit_code"], prog["done"]),
                         ("failed", "filter", 2, ["plan", "feeds"]))

    def test_the_runners_own_error_is_appended_to_the_log(self):
        (self.d / "run.log").write_text("started by run_feeds\n", encoding="utf-8")
        with mock.patch.object(runner.subprocess, "call", return_value=0), \
                mock.patch.object(runner, "write_progress", side_effect=[None, OSError("disk full")]):
            self.assertEqual(runner.run(self.d), 1)
        log = (self.d / "run.log").read_text(encoding="utf-8")
        self.assertTrue(log.startswith("started by run_feeds\n$ plan\nTraceback"), log)
        self.assertIn("OSError: disk full", log)
        self.assertFalse(runner.running(self.d))

    def test_a_second_runner_leaves_the_first_alone(self):
        held = runner.lock(self.d / "run.lock")
        self.addCleanup(held.close)
        with mock.patch.object(runner.time, "sleep"), mock.patch.object(runner.subprocess, "call") as call_:
            self.assertEqual(runner.run(self.d), 1)
        call_.assert_not_called()
        self.assertFalse((self.d / "progress.json").exists())
        self.assertTrue(runner.running(self.d))


if __name__ == "__main__":
    unittest.main()
