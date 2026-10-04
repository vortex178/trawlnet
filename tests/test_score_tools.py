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
import jobsearch
import mcp_tools
from test_run_tools import call

PROFILES = {"be": {"id": "be", "label": "Backend", "target_titles": ["Backend Engineer"]},
            "data": {"id": "data", "label": "Data", "target_titles": ["Data Engineer"]}}
GATES = {g: "pass" for g in mcp_tools.GATES}


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
        self.published = []
        for patch in (mock.patch.object(mcp_tools, "RUNS_DIR", self.runs),
                      mock.patch.object(mcp_tools, "load_profiles", return_value=PROFILES),
                      mock.patch.object(mcp_tools, "load_config", return_value={}),
                      mock.patch.object(mcp_tools, "DATA", self.runs.parent),
                      mock.patch.object(mcp_tools, "cmd_publish", side_effect=self.publish)):
            patch.start()
            self.addCleanup(patch.stop)

    def publish(self, a, cfg):
        self.published.append(a.date)
        (self.runs.parent / "digests").mkdir(exist_ok=True)
        (self.runs.parent / "digests" / f"{a.date}.md").write_text("# digest\n", encoding="utf-8")
        print("scored 3/5 | tracker: +2 queued")

    def scores(self):
        return [json.loads(line) for p in sorted(self.d.glob("scores-*.jsonl"))
                for line in p.read_text(encoding="utf-8").splitlines()]

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
        self.assertIn("scored but not published yet", call("next_batch")["next"])
        call("submit_scores", scores=[], finish=True)
        self.assertIn("scored and published", call("next_batch")["next"])

    def test_the_default_batch_is_the_configured_size(self):
        rows = [{"key": f"wwr:{i}", "profiles": ["be"], "jd": "x"} for i in range(1, 8)]
        (self.d / "shortlist.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        for i in range(1, 8):
            (self.d / "jd" / f"wwr:{i}.txt").write_text("Go", encoding="utf-8")
        for size, n in ((None, 3), (2, 2), (12, mcp_tools.MAX_BATCH), (0, 1), (True, 3), ("x", 3)):
            cfg = {} if size is None else {"scorer_batch_size": size}
            with mock.patch.object(mcp_tools, "load_config", return_value=cfg):
                self.assertEqual(len(call("next_batch", context=False)["jobs"]), n, cfg)

    def test_scores_are_saved_then_the_run_is_published(self):
        out = call("submit_scores", scores=[result("wwr:1", gaps=["Kubernetes — not evidenced"]),
                                            result("wwr:2", apply_url="https://jobs.example.com/2")])
        self.assertEqual((out["saved"], out["remaining"], out["errors"], self.published), (2, 1, [], []))
        out = call("submit_scores", scores=[result("wwr:3", profile="data", score=30, verdict="skip",
                                                   flags=["vague-jd"])])
        self.assertEqual((out["remaining"], out["published"], self.published), (0, ["scored 3/5 | tracker: +2 queued"],
                                                                                ["2026-10-04"]))
        saved = self.scores()
        self.assertEqual([s["key"] for s in saved], ["wwr:1", "wwr:2", "wwr:3"])
        self.assertTrue((self.d / "scores-chat-1.jsonl").read_bytes().isascii())
        self.assertEqual(saved[0]["gaps"], ["Kubernetes — not evidenced"])
        self.assertEqual((saved[0]["apply_url"], saved[1]["apply_url"]), ("https://example.com/1",
                                                                          "https://jobs.example.com/2"))
        short = {r["key"]: r for r in common.read_jsonl(self.d / "shortlist.jsonl")}
        self.assertEqual([jobsearch._valid_score(s, short, PROFILES) for s in saved], ["", "", ""])
        self.assertEqual(sorted(p.name for p in self.d.glob("scores-*.jsonl")), ["scores-chat-1.jsonl",
                                                                                  "scores-chat-2.jsonl"])
        self.assertIn("and published", call("submit_scores", scores=[result("wwr:1")])["next"])  # nothing to save

    def test_invalid_results_come_back_and_valid_ones_are_kept(self):
        bad = [result("wwr:9"), result("wwr:1", profile="data"), result("wwr:1", score=True),
               result("wwr:1", score=101), result("wwr:1", verdict="maybe"), result("wwr:1", gates={"location": "pass"}),
               result("wwr:1", gates={**GATES, "salary": "fail"}), result("wwr:1", strengths=["a", "b", "c", "d"]),
               result("wwr:1", gaps=["two\nlines"]), result("wwr:1", flags=["great"]), result("wwr:1", extra=1),
               result("wwr:1", apply_url="javascript:alert(1)"), "x", result("wwr:2"), result("wwr:2")]
        out = call("submit_scores", scores=bad)
        whys = [e["error"] for e in out["errors"]]
        self.assertEqual((out["saved"], out["remaining"], len(whys)), (1, 2, 14))
        for i, part in enumerate(("not a job waiting", "one of the job's profiles", "whole number", "whole number",
                                  "verdict", "gates must", "failed gate", "at most 3", "one line", "must come from",
                                  "only these keys", "apply_url", "not a job waiting", "not a job waiting")):
            self.assertIn(part, whys[i], i)
        self.assertEqual(out["errors"][12], {"index": 12, "key": None, "error": whys[12]})
        self.assertEqual([s["key"] for s in self.scores()], ["wwr:2"])
        self.assertEqual(out["next"], "fix and resend the errors")
        out = call("submit_scores", scores=[result("wwr:1", gates={**GATES, "salary": "fail"}, score=35,
                                                   verdict="skip")])
        self.assertEqual((out["errors"], out["next"]), ([], "call next_batch for the next jobs"))

    def test_a_failed_publish_still_reports_the_saved_scores(self):
        with mock.patch.object(mcp_tools, "load_config", side_effect=SystemExit("bad config")):
            out = call("submit_scores", scores=[result("wwr:1")], finish=True)
        self.assertEqual((out["saved"], out["publish_error"], "published" in out), (1, "SystemExit: bad config", False))
        def half(a, cfg):
            print("  INVALID x: bad score")
            raise PermissionError(13, "Permission denied", "/abs/tracker.csv")
        with mock.patch.object(mcp_tools, "cmd_publish", side_effect=half):
            out = call("submit_scores", scores=[], finish=True)
        self.assertEqual((out["publish_error"], out["published"]), ("PermissionError: Permission denied",
                                                                    ["  INVALID x: bad score"]))
        self.assertIn("scores=[] and finish=true", out["next"])
        out = call("submit_scores", scores=[], finish=True)  # the retry publishes; the rest are retried next run
        self.assertEqual((out["saved"], out["remaining"], self.published), (0, 2, ["2026-10-04"]))

    def test_refusals(self):
        for args, why in (({"n": 0}, "n must be"), ({"n": 6}, "n must be"), ({"context": "no"}, "context must"),
                          ({"date": "x"}, "date must")):
            self.assertIn(why, call("next_batch", **args))
        for args, why in (({"scores": {}}, "scores must be a list"), ({"scores": [{}] * 21}, "at most 20"),
                          ({"scores": [], "finish": "yes"}, "finish must"), ({"scores": []}, "scores is empty")):
            self.assertIn(why, call("submit_scores", **args))
        (self.d / "progress.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
        self.assertIn("has not finished", call("next_batch"))
        with mock.patch.object(mcp_tools, "HOME", Path(tempfile.mkdtemp()) / "clone"):
            self.assertIn("not registered", call("submit_scores", scores=[result("wwr:1")]))
            self.assertIn("not registered", call("next_batch"))
        self.assertEqual(self.scores(), [])

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
        master.write_text("facts:\n- {id: F001, text: Built Go APIs}\n- {id: F002, text: Old, retired: true}\n- x\n"
                          "- {id: F003}\n- {id: [F004], text: t}\n- {id: 5, text: t}\n", encoding="utf-8")
        self.assertEqual(mcp_tools._master_facts(), "F001: Built Go APIs")
        bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n" + "".join(
            f"{c}: &{c} [*{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}]\n" for p, c in zip("abcdefgh", "bcdefghi"))
        master.write_text(bomb + "facts:\n- {id: *i, text: *i}\n- {id: F1, text: *i}\n", encoding="utf-8")
        self.assertEqual(mcp_tools._master_facts(), "")  # 10^9 strings if expanded
        for text in ("[1, 2]", "facts: {a: 1}", "a: [", "[" * 100000, "a: " + "9" * 5000):
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
