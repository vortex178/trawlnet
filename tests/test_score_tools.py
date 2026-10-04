"""The next_batch / submit_scores MCP tools: chat-model scoring of a finished run, with publish mocked."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import homes
import mcp_tools
from test_run_tools import call

PROFILES = {"be": {"id": "be", "label": "Backend", "target_titles": ["Backend Engineer"]},
            "data": {"id": "data", "label": "Data", "target_titles": ["Data Engineer"]}}
GATES = {g: "pass" for g in ("location", "must_have", "seniority", "salary", "deal_breaker")}


def result(key, **kw):
    return {"key": key, "profile": "be", "score": 72, "verdict": "apply", "gates": dict(GATES),
            "strengths": ["Go services <- built Go APIs"], "gaps": [], "flags": [], **kw}


class ScoreTools(unittest.TestCase):
    def setUp(self):
        homes.register(common.HOME)
        self.runs = Path(tempfile.mkdtemp(dir=common.HOME / "data")) / "runs"
        self.addCleanup(shutil.rmtree, self.runs.parent, True)
        self.d = self.runs / "2026-10-04"
        (self.d / "jd").mkdir(parents=True)
        (self.d / "progress.json").write_text(json.dumps({"state": "done"}), encoding="utf-8")
        rows = [{"key": f"wwr:{i}", "profiles": ["be"], "title": f"Backend {i}", "company": f"C{i}", "location": "Remote",
                 "url": f"https://example.com/{i}", "source": "wwr", "jd": f"data/runs/2026-10-04/jd/wwr:{i}.txt",
                 "flags": []} for i in range(1, 5)]
        rows[3]["jd"] = ""  # no description: a lead, never scored (even with a stale jd file from an earlier shortlist)
        rows[2]["profiles"] = ["data"]  # in a later batch: its profile still comes with the first call's context
        rows.append({**rows[0], "key": "indeed:9", "jd": "indeed:9"})
        (self.d / "shortlist.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        for i in range(1, 5):
            (self.d / "jd" / f"wwr:{i}.txt").write_text(f"Backend {i}\n\nGo and Postgres." + "x" * (i == 3) * 9000,
                                                        encoding="utf-8")
        for patch in (mock.patch.object(mcp_tools, "RUNS_DIR", self.runs),
                      mock.patch.object(mcp_tools, "load_profiles", return_value=PROFILES)):
            patch.start()
            self.addCleanup(patch.stop)

    def write_scores(self, name, *results):
        (self.d / name).write_text("".join(json.dumps(r) + "\n" for r in results), encoding="utf-8")

    def test_batches_leave_out_what_is_scored(self):
        out = call("next_batch", n=2)
        self.assertEqual((out["remaining"], [j["key"] for j in out["jobs"]]), (3, ["wwr:1", "wwr:2"]))
        self.assertIn("Go and Postgres", out["jobs"][0]["description"])
        self.assertIn("Gates", out["context"]["rubric"])
        self.assertEqual((list(out["context"]["profiles"]), list(out["context"]["evidence"])),
                         (["be", "data"], ["master_facts"]))  # neither has its own resume: the facts, once
        self.write_scores("scores-chat-1.jsonl", result("wwr:1"), result("wwr:2"))
        out = call("next_batch", context=False)
        self.assertEqual(([j["key"] for j in out["jobs"]], "context" in out), (["wwr:3"], False))
        self.assertTrue(out["jobs"][0]["description"].endswith("[cut]"))
        self.assertEqual(len(out["jobs"][0]["description"]), mcp_tools.MAX_JD + len("\n[cut]"))
        self.write_scores("scores-1.jsonl", result("wwr:3", score="high"), result(["wwr:3"]),  # publish refuses
                          result("wwr:3", profile=["data"]))  # them: still to score
        self.assertEqual(call("next_batch")["remaining"], 1)
        self.write_scores("scores-1.jsonl", result("wwr:3", profile="data"))
        self.assertIn("every described job is scored", call("next_batch")["next"])

    def test_refusals(self):
        for args, why in (({"n": 0}, "n must be"), ({"n": 6}, "n must be"), ({"context": "no"}, "context must"),
                          ({"date": "x"}, "date must")):
            self.assertIn(why, call("next_batch", **args))
        (self.d / "progress.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
        self.assertIn("has not finished", call("next_batch"))

    def test_a_key_never_names_a_file_outside_jd(self):
        row = {"key": "../progress", "profiles": ["be"], "jd": "x"}
        (self.d / "shortlist.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        self.assertEqual(call("next_batch")["remaining"], 0)

    def test_evidence_is_the_profiles_own_resume_else_the_master_facts(self):
        data = self.runs.parent
        for patch in (mock.patch.object(mcp_tools, "PROFILES_DIR", data / "profiles"),
                      mock.patch.object(mcp_tools, "DATA", data)):
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(mcp_tools._master_facts(), "")
        (data / "profiles").mkdir()
        master = data / "profiles" / "master.yaml"
        master.write_text("facts:\n- {id: F001, text: Built Go APIs}\n- {id: F002, text: Old, retired: true}\n- x\n",
                          encoding="utf-8")
        self.assertEqual(mcp_tools._master_facts(), "F001: Built Go APIs")
        for text in ("[1, 2]", "facts: {a: 1}", "a: [", "[" * 100000):
            master.write_text(text, encoding="utf-8")
            self.assertEqual(mcp_tools._master_facts(), "", text[:10])
        (data / "resumes").mkdir()
        (data / "resumes" / "be.md").write_text("Go engineer", encoding="utf-8")
        self.assertEqual(mcp_tools._resume("be", {"resume": "data/resumes/be.md"}), "Go engineer")
        for pid, resume in (("be", "config.yaml"), ("be", ".secrets/sa.json"), ("be", "data/resumes/x.md"),
                            ("../x", "data/resumes/../x.md"), ("x", "data/resumes/x.md")):
            self.assertIsNone(mcp_tools._resume(pid, {"resume": resume}), resume)  # only its own file, if present
        with mock.patch.object(mcp_tools, "load_profiles", return_value={"be": {"resume": "data/resumes/be.md"}}):
            self.assertEqual(call("next_batch", n=1)["context"]["evidence"], {"be": "Go engineer"})


if __name__ == "__main__":
    unittest.main()
