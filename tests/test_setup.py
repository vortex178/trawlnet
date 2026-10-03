"""setup.py: init, link (rendered agents, ./js, dev symlink), env and doctor on throwaway folders."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import yaml

import setup as js_setup


def init_args(home, **kw):
    return Namespace(**{"home": str(home), "country": "IN", "cities": "delhi ncr,bengaluru", "remote_bias": "strong",
                        "timezone": None, "forbidden_shift": "00:00-06:00", "salary_floor": None,
                        "salary_floor_lpa": 26.0, "max_yoe": 5.0, "tracker": "csv", "indeed": None, "no_env": True,
                        "cmd": "init", "dev": False, **kw})


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **kw)
    return buf.getvalue()


class TmpCase(unittest.TestCase):
    def tmp(self):
        d = Path(tempfile.mkdtemp(prefix="js-setup-"))
        self.addCleanup(shutil.rmtree, d, True)
        return d


class HelpersTest(unittest.TestCase):
    def test_render_keeps_unknown_placeholders(self):
        self.assertEqual(js_setup._render("{{A}} and {{B}}", {"A": 1}), "1 and {{B}}")

    def test_write_if_changed(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "sub" / "f.txt"
            self.assertTrue(js_setup._write_if_changed(p, "x"))
            self.assertFalse(js_setup._write_if_changed(p, "x"))
            self.assertTrue(js_setup._write_if_changed(p, "y"))

    def test_read_yaml_scalar(self):
        text = 'a: true\nb: false\nc: null\nd: "quoted"  # note\ne:\n  f: 12\n'
        s = js_setup._read_yaml_scalar
        self.assertIs(s(text, "a"), True)
        self.assertIs(s(text, "b"), False)
        self.assertIsNone(s(text, "c"))
        self.assertEqual(s(text, "d"), "quoted")
        self.assertIsNone(s(text, "e"))
        self.assertEqual(s(text, "f"), "12")
        self.assertEqual(s("models: {fetcher_model: opus, scorer_model: sonnet}", "scorer_model"), "sonnet")
        self.assertIsNone(s(text, "missing"))

    def test_home_resolution(self):
        self.assertEqual(js_setup._home(Namespace(home="~")), Path.home().resolve())
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": "/tmp"}):
            self.assertEqual(js_setup._home(Namespace(home=None)), Path("/tmp").resolve())


class InitTest(TmpCase):
    def test_init_creates_a_working_folder(self):
        home = self.tmp() / "jobs"
        out = capture(js_setup.init, init_args(home, tracker="gsheets", indeed="mcp__abc"))
        self.assertIn(f"data folder: {home.resolve()}", out)
        cfg = yaml.safe_load((home / "config.yaml").read_text())
        self.assertEqual(cfg["country"], "IN")
        self.assertEqual(sorted(cfg["accept_cities"]), ["bengaluru", "delhi ncr"])
        self.assertEqual(cfg["tracker"]["backend"], "gsheets")
        self.assertEqual(cfg["connectors"]["indeed_tool_prefix"], "mcp__abc")
        self.assertTrue(cfg["sources"]["indeed"])
        self.assertEqual(cfg["search_locations"], ["remote", "Delhi Ncr", "Bengaluru"])
        prefs = (home / "data/profiles/preferences.yaml").read_text()
        self.assertIn("min_lpa: 26", prefs)  # %g formatting: 26.0 -> 26
        self.assertIn("Asia/Kolkata", prefs)
        claude = (home / "CLAUDE.md").read_text()
        self.assertIn("26 INR LPA", claude)
        self.assertIn("delhi ncr, bengaluru", claude)
        self.assertTrue((home / ".gitignore").exists())
        self.assertEqual((home / ".secrets").stat().st_mode & 0o777, 0o700)
        self.assertIn("mcp__abc__search_jobs", (home / ".claude/agents/job-fetcher.md").read_text())
        self.assertTrue(os.access(home / "js", os.X_OK))
        self.assertIn("linked:", out)

    def test_init_us_annual_floor_and_no_floor(self):
        h1 = self.tmp() / "us"
        capture(js_setup.init, init_args(h1, country="us", cities="austin", salary_floor_lpa=None, salary_floor=120000.0,
                                         max_yoe=None, forbidden_shift=None))
        self.assertIn("min_annual: 120000", (h1 / "data/profiles/preferences.yaml").read_text())
        self.assertIn("120000 USD / year", (h1 / "CLAUDE.md").read_text())
        self.assertIn("America/New_York", (h1 / "data/profiles/preferences.yaml").read_text())
        h2 = self.tmp() / "none"
        capture(js_setup.init, init_args(h2, cities="", salary_floor_lpa=None, max_yoe=None))
        self.assertIn("min_annual: null", (h2 / "data/profiles/preferences.yaml").read_text())
        self.assertIn("none (remote only)", (h2 / "CLAUDE.md").read_text())
        self.assertEqual(yaml.safe_load((h2 / "config.yaml").read_text())["accept_cities"] or {}, {})

    def test_init_refuses_bad_input(self):
        home = self.tmp() / "x"
        with self.assertRaisesRegex(SystemExit, "no pack for zz"):
            js_setup.init(init_args(home, country="zz"))
        with self.assertRaisesRegex(SystemExit, r"unknown cities \['atlantis'\]"):
            js_setup.init(init_args(home, cities="atlantis"))
        capture(js_setup.init, init_args(home))
        with self.assertRaisesRegex(SystemExit, "exists"):
            js_setup.init(init_args(home))

    def test_init_runs_env_unless_disabled(self):
        home = self.tmp() / "envd"
        with mock.patch.object(js_setup, "env", return_value="env: fake ready") as env:
            out = capture(js_setup.init, init_args(home, no_env=False))
        env.assert_called_once()
        self.assertIn("env: fake ready", out)


class LinkTest(TmpCase):
    def test_link_is_idempotent_and_rerenders_on_change(self):
        home = self.tmp()
        (home / "config.yaml").write_text("models: {fetcher_model: opus}\nconnectors:\n  indeed_tool_prefix: \"\"\n")
        (home / "data").mkdir()
        changed = js_setup.link(home)
        self.assertEqual(changed[0], "js")
        self.assertIn(f"registered {home.resolve()} for the MCP server", changed[1])
        self.assertEqual(len(changed), 5)
        agents = home / ".claude" / "agents"
        self.assertIn("model: opus", (agents / "job-fetcher.md").read_text())
        self.assertIn("Indeed connector is not configured", (agents / "job-fetcher.md").read_text())
        self.assertIn(js_setup.GENERATED, (agents / "job-scorer.md").read_text())
        self.assertNotIn("{{", (agents / "job-scorer.md").read_text())
        self.assertEqual(js_setup.link(home), [])
        (home / "config.yaml").write_text("models: {fetcher_model: haiku}\nconnectors:\n  indeed_tool_prefix: mcp__z\n")
        # only agents whose rendering changed (the fetcher and scorer each embed an Indeed tool name)
        self.assertEqual(js_setup.link(home), [".claude/agents/job-fetcher.md", ".claude/agents/job-scorer.md"])
        self.assertEqual(js_setup.link(home), [])
        self.assertIn("mcp__z__search_jobs", (agents / "job-fetcher.md").read_text())

    def test_registers_only_a_data_folder(self):
        plain = self.tmp()
        js_setup.link(plain)  # no config.yaml + data/: never listed (a later clone there must not be trusted)
        self.assertFalse(js_setup.homes.is_registered(plain))
        home = self.tmp()
        (home / "config.yaml").write_text("")
        (home / "data" / "runs").mkdir(parents=True)
        js_setup.link(home / "data" / "runs")  # JOB_SEARCH_HOME set: like the server, no walk up
        self.assertFalse(js_setup.homes.is_registered(home))
        with mock.patch.dict(os.environ):
            del os.environ["JOB_SEARCH_HOME"]
            js_setup.link(home / "data" / "runs")  # started below the data folder: the folder itself is listed
        self.assertTrue(js_setup.homes.is_registered(home))
        self.assertFalse(js_setup.homes.is_registered(home / "data" / "runs"))

    def test_unwritable_config_dir_warns_and_links(self):
        home = self.tmp()
        (home / "config.yaml").write_text("")
        (home / "data").mkdir()
        err = io.StringIO()
        with mock.patch.object(js_setup.homes, "register", side_effect=PermissionError("denied")), \
                contextlib.redirect_stderr(err):
            changed = js_setup.link(home)
        self.assertIn("could not register", err.getvalue())
        self.assertEqual(len(changed), 4)  # ./js and the agents still refreshed

    def test_damaged_list_warns_and_is_left_alone(self):
        home = self.tmp()
        (home / "config.yaml").write_text("")
        (home / "data").mkdir()
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp())})
        patch.start()
        self.addCleanup(patch.stop)
        js_setup.homes.homes_file().parent.mkdir(parents=True)
        js_setup.homes.homes_file().write_bytes(b"\xff damaged")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            js_setup.link(home)
        self.assertIn("damaged", err.getvalue())  # warned, nothing appended
        self.assertEqual(js_setup.homes.homes_file().read_bytes(), b"\xff damaged")

    def test_indeed_disabled_in_config_skips_tools(self):
        home = self.tmp()
        (home / "config.yaml").write_text("sources:\n  indeed: false\nconnectors:\n  indeed_tool_prefix: mcp__z\n")
        vals = js_setup._agent_values(home)
        self.assertEqual(vals["INDEED_SEARCH_TOOL"], "")
        self.assertEqual(vals["SCORER_MODEL"], "sonnet")  # defaults without model config
        self.assertEqual(js_setup._agent_values(self.tmp())["FETCHER_MODEL"], "haiku")  # no config at all

    def test_dev_symlink(self):
        home = self.tmp()
        (home / "config.yaml").write_text("")
        self.assertIn(".claude/skills/job-search -> plugin (dev)", js_setup.link(home, dev=True))
        dst = home / ".claude/skills/job-search"
        self.assertEqual(dst.resolve(), js_setup.SKILL)
        self.assertNotIn(".claude/skills/job-search -> plugin (dev)", js_setup.link(home, dev=True))
        dst.unlink()
        dst.symlink_to(home)  # stale link elsewhere is replaced
        self.assertIn(".claude/skills/job-search -> plugin (dev)", js_setup.link(home, dev=True))
        dst.unlink()
        dst.mkdir()
        with self.assertRaisesRegex(SystemExit, "real folder"):
            js_setup.link(home, dev=True)


class EnvTest(TmpCase):
    def test_uv_creates_venv_and_installs(self):
        home = self.tmp()
        with mock.patch.object(js_setup.shutil, "which", return_value="/usr/bin/uv"), \
                mock.patch.object(js_setup.subprocess, "run") as run:
            self.assertEqual(js_setup.env(home), "env: uv venv ready")
        cmds = [c.args[0] for c in run.call_args_list]
        self.assertEqual(cmds[0][:3], ["/usr/bin/uv", "venv", "--python"])
        self.assertEqual(cmds[1][:3], ["/usr/bin/uv", "pip", "install"])
        (home / ".venv").mkdir()
        with mock.patch.object(js_setup.shutil, "which", return_value="/usr/bin/uv"), \
                mock.patch.object(js_setup.subprocess, "run") as run:
            js_setup.env(home)
        self.assertEqual(run.call_count, 1)  # existing venv: install only

    def test_plain_venv_and_old_python(self):
        home = self.tmp()
        with mock.patch.object(js_setup.shutil, "which", return_value=None), \
                mock.patch.object(js_setup, "MIN_PY", (3, 0)), mock.patch.object(js_setup.subprocess, "run") as run:
            self.assertEqual(js_setup.env(home), "env: venv ready")
        self.assertEqual(run.call_args_list[0].args[0][1:3], ["-m", "venv"])
        self.assertEqual(run.call_args_list[1].args[0][1:4], ["-m", "pip", "install"])
        with mock.patch.object(js_setup.shutil, "which", return_value=None), \
                mock.patch.object(js_setup, "MIN_PY", (99, 0)), self.assertRaisesRegex(SystemExit, "Python >= 99.0"):
            js_setup.env(self.tmp())


class DoctorTest(TmpCase):
    def test_missing_config_stops_early(self):
        home = self.tmp()
        out = capture(js_setup.doctor, home)
        self.assertIn("!! env", out)
        self.assertIn("!! config.yaml", out)
        self.assertNotIn("pack", out)

    def make_home(self):
        home = self.tmp() / "h"
        capture(js_setup.init, init_args(home))
        return home

    def test_healthy_folder(self):
        home = self.make_home()
        (home / ".venv/bin").mkdir(parents=True)
        (home / ".venv/bin/python").write_text("")
        (home / "data/profiles/backend.yaml").write_text("id: backend\n")
        done = subprocess.CompletedProcess([], 0, "3.12.1\n", "")
        with mock.patch.object(js_setup.subprocess, "run", return_value=done):
            out = capture(js_setup.doctor, home)
        self.assertIn("ok python 3.12.1", out)
        self.assertIn("ok pack in", out)
        self.assertIn("ok profiles: backend", out)
        self.assertIn("ok tracker csv", out)
        self.assertIn("-- indeed connector: not configured", out)
        self.assertIn("ok agents rendered", out)
        self.assertNotIn("!!", out)

    def test_broken_env_missing_keys_and_gsheets(self):
        home = self.make_home()
        (home / ".venv/bin").mkdir(parents=True)
        (home / ".venv/bin/python").write_text("")
        cfg = home / "config.yaml"
        text = cfg.read_text()
        text = re.sub(r"(?ms)(^firecrawl:[^\n]*\n\s*enabled:) false", r"\1 true", text)
        text = re.sub(r"(?m)^(\s*adzuna:) false", r"\1 true", text)
        text = text.replace("backend: csv", "backend: gsheets")
        cfg.write_text(text)
        (home / ".claude/agents/job-scorer.md").unlink()
        bad = subprocess.CompletedProcess([], 1, "", "ModuleNotFoundError: yaml")
        with mock.patch.object(js_setup.subprocess, "run", return_value=bad):
            out = capture(js_setup.doctor, home)
        self.assertIn("!! python ModuleNotFoundError: yaml", out)
        self.assertIn("!! profiles: none", out)
        self.assertIn("!! firecrawl enabled, key MISSING at .secrets/firecrawl.key", out)
        self.assertIn("!! adzuna enabled, key MISSING at .secrets/adzuna.json", out)
        self.assertIn("!! tracker gsheets, key MISSING", out)
        self.assertIn("!! agents rendered", out)
        (home / ".secrets/service-account.json").write_text("{}")
        (home / ".secrets/firecrawl.key").write_text("k")
        with mock.patch.object(js_setup.subprocess, "run", return_value=bad):
            out = capture(js_setup.doctor, home)
        self.assertIn("ok tracker gsheets, key present", out)
        self.assertIn("ok firecrawl enabled, key present", out)


class MainTest(TmpCase):
    def run_main(self, *argv):
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["setup.py", *argv]), contextlib.redirect_stdout(buf):
            js_setup.main()
        return buf.getvalue()

    def test_cli_dispatch(self):
        home = self.tmp() / "m"
        out = self.run_main("init", "--home", str(home), "--country", "IN", "--cities", "delhi ncr",
                            "--salary-floor-lpa", "26", "--max-yoe", "5", "--no-env")
        self.assertIn("data folder:", out)
        self.assertIn("up to date", self.run_main("link", "--home", str(home)))
        with mock.patch.object(js_setup, "env", return_value="env: ok"):
            self.assertEqual(self.run_main("env", "--home", str(home)).strip(), "env: ok")
        self.assertIn("ok config.yaml", self.run_main("doctor", "--home", str(home)))

    def test_runs_as_a_script(self):
        home = self.tmp()
        p = subprocess.run([sys.executable, str(js_setup.SCRIPTS / "setup.py"), "doctor", "--home", str(home)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("!! config.yaml", p.stdout)



class SeedWarningsTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / "data/seeds").mkdir(parents=True)

    def companies(self, rows):
        (self.home / "data/companies.json").write_text(json.dumps(rows))

    def boards(self, seed, n, dead):
        return [{"name": f"C{i}", "ats": "lever", "token": f"c{i}", "seed": seed, "gone_days": 3 if i < dead else 0}
                for i in range(n)]

    def test_no_companies_file(self):
        self.assertEqual(js_setup.seed_warnings(self.home), [])

    def test_dead_rate_needs_enough_boards_and_threshold(self):
        today = dt.date(2026, 10, 1)
        self.companies(self.boards("in", 10, 2) + self.boards("us", 9, 9) + self.boards("eu", 10, 1))
        w = js_setup.seed_warnings(self.home, today)
        self.assertEqual(len(w), 1)
        self.assertIn("seed 'in': 2 of 10 boards are dead -> ./js discover refresh --seed in", w[0])

    def test_age_from_manifest_and_own_file_and_custom_ignored(self):
        self.companies(self.boards("india", 2, 0) + self.boards("remoteintech", 2, 0)
                       + [{"name": "S", "ats": "custom", "token": "s", "seed": "old", "gone_days": 9}])
        (self.home / "data/seeds/remoteintech.json").write_text("[]")   # own copy, fresh mtime
        w = js_setup.seed_warnings(self.home, dt.date(2027, 6, 1))     # manifest says 2026-09-27: >180 days
        self.assertEqual(len(w), 2)
        self.assertIn("seed 'in': list dates from 2026-09-27", w[0])
        self.assertIn("seed 'remoteintech': list dates from", w[1])
        self.assertEqual(js_setup.seed_warnings(self.home, dt.date(2026, 10, 1)), [])

    def test_dead_after_matches_engine(self):
        import sources
        self.assertEqual(js_setup.DEAD_AFTER, sources.DEAD_AFTER)

    def test_doctor_lists_warnings(self):
        (self.home / "config.yaml").write_text("country: in\n")
        self.companies(self.boards("in", 10, 5))
        self.assertIn("!! seed 'in': 5 of 10 boards are dead", capture(js_setup.doctor, self.home))


if __name__ == "__main__":
    unittest.main()
