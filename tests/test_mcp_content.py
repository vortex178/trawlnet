"""mcp_content.py: resources and prompts against the example data folder."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import mcp_content
import mcp_server as srv


def rpc(method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return srv.handle(json.dumps(msg))


class Resources(unittest.TestCase):
    def setUp(self):
        self.home = _home.make_home()
        self.data = self.home / "data"
        self.addCleanup(shutil.rmtree, self.home, True)
        for patch in (mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": str(self.home)}),
                      mock.patch.object(common, "HOME", self.home),
                      mock.patch.object(mcp_content, "DATA", self.data),
                      mock.patch.object(mcp_content, "PROFILES_DIR", self.data / "profiles"),
                      mock.patch.object(common, "PROFILES_DIR", self.data / "profiles")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_symlinks_out_of_the_folder_are_not_served(self):
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("secret")
        self.addCleanup(shutil.rmtree, outside.parent, True)
        self.make("digests/2099-01-01.md", "ok")
        (self.data / "digests" / "2099-01-02.md").symlink_to(outside)
        (self.data / "scoring-context.md").symlink_to(outside)
        (self.data / "profiles" / "master.yaml").unlink(missing_ok=True)
        (self.data / "profiles" / "master.yaml").symlink_to(outside)
        uris = self.uris()
        self.assertIn("trawlnet://digests/2099-01-01", uris)
        for gone in ("trawlnet://digests/2099-01-02", "trawlnet://scoring-context", "trawlnet://master"):
            self.assertNotIn(gone, uris)
        self.assertNotIn("secret", json.dumps(rpc("resources/list")))
        (self.data / "digests" / "2099-01-01.md").unlink()  # listed earlier, swapped for a link before it is read
        (self.data / "digests" / "2099-01-01.md").symlink_to(outside)
        with mock.patch.object(mcp_content, "_inside", return_value=True):
            err = rpc("resources/read", {"uri": "trawlnet://digests/2099-01-01"})["error"]
        self.assertIn("symlink", err["message"])

    def test_links_to_the_folders_own_secrets_are_not_served(self):
        (self.home / ".secrets").mkdir(exist_ok=True)
        (self.home / ".secrets" / "key").write_text("SECRETKEY")
        self.make("digests/2099-01-01.md", "ok")
        (self.data / "digests" / "2099-01-02.md").symlink_to(self.home / ".secrets" / "key")  # inside HOME, outside digests/
        (self.data / "scoring-context.md").unlink(missing_ok=True)
        (self.data / "scoring-context.md").symlink_to(self.home / "config.yaml")
        uris = self.uris()
        self.assertIn("trawlnet://digests/2099-01-01", uris)
        self.assertNotIn("trawlnet://digests/2099-01-02", uris)
        self.assertNotIn("trawlnet://scoring-context", uris)

    def test_directory_links_are_not_followed(self):
        (self.home / ".secrets").mkdir(exist_ok=True)
        (self.home / ".secrets" / "2026-01-01.md").write_text("SECRETKEY")
        (self.home / ".secrets" / "master.yaml").write_text("secret: 1\n")
        for d in ("digests", "profiles"):
            shutil.rmtree(self.data / d, ignore_errors=True)
            (self.data / d).symlink_to(self.home / ".secrets")
        uris = self.uris()
        self.assertEqual([u for u in uris if "digests" in u or "master" in u or "/profiles/" in u], [])
        self.assertIn("trawlnet://tailoring-rules", uris)

    def test_a_profile_link_out_hides_the_profiles_but_not_the_rest(self):
        outside = Path(tempfile.mkdtemp()) / "p.yaml"
        outside.write_text("id: x\n")
        self.addCleanup(shutil.rmtree, outside.parent, True)
        (self.data / "profiles" / "evil.yaml").symlink_to(outside)
        uris = self.uris()
        self.assertFalse([u for u in uris if "/profiles/" in u])
        self.assertIn("trawlnet://tailoring-rules", uris)

    def make(self, rel, text):
        path = self.data / rel
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def uris(self):
        return [r["uri"] for r in rpc("resources/list")["result"]["resources"]]

    def read(self, uri):
        return rpc("resources/read", {"uri": uri})

    def test_profiles_master_and_rules_listed(self):
        uris = self.uris()
        for u in ("trawlnet://profiles/appsec", "trawlnet://profiles/backend-sde", "trawlnet://master",
                  "trawlnet://tailoring-rules"):
            self.assertIn(u, uris)
        self.assertNotIn("trawlnet://profiles/master", uris)  # master is its own resource, preferences are not exposed
        self.assertNotIn("trawlnet://profiles/preferences", uris)
        self.assertNotIn("trawlnet://scoring-context", uris)  # not generated yet

    def test_read_each_kind(self):
        prof = self.read("trawlnet://profiles/appsec")["result"]["contents"][0]
        self.assertEqual(prof["mimeType"], "application/yaml")
        self.assertIn("id: appsec", prof["text"])
        self.assertIn("F0", self.read("trawlnet://master")["result"]["contents"][0]["text"])
        rules = self.read("trawlnet://tailoring-rules")["result"]["contents"][0]["text"]
        self.assertIn("Never create new experience", rules)

    def test_scoring_context_and_digests_appear_when_present(self):
        self.make("scoring-context.md", "# ctx")
        self.make("digests/2026-09-01.md", "# one é")
        self.assertIn("trawlnet://scoring-context", self.uris())
        self.assertEqual(self.read("trawlnet://digests/2026-09-01")["result"]["contents"][0]["text"], "# one é")
        self.assertEqual(self.read("trawlnet://scoring-context")["result"]["contents"][0]["mimeType"], "text/markdown")

    def test_broken_profile_keeps_other_resources(self):
        self.make("profiles/broken.yaml", "id: [unclosed")
        with mock.patch("sys.stderr", mock.MagicMock()):
            uris = self.uris()
        self.assertIn("trawlnet://tailoring-rules", uris)
        self.assertFalse([u for u in uris if "/profiles/" in u])

    def test_profile_ids_are_url_encoded(self):
        self.make("profiles/odd.yaml", "id: my role/x\ntarget_titles: [a]\n")
        self.assertIn("trawlnet://profiles/my%20role%2Fx", self.uris())
        self.assertIn("id: my role/x", self.read("trawlnet://profiles/my%20role%2Fx")["result"]["contents"][0]["text"])

    def test_digest_list_is_newest_first_and_capped(self):
        for i in range(mcp_content.MAX_DIGESTS + 3):
            self.make(f"digests/2026-08-{i + 1:02d}.md", "d")
        digests = [u for u in self.uris() if "/digests/" in u]
        self.assertEqual(len(digests), mcp_content.MAX_DIGESTS)
        self.assertEqual(digests[0], f"trawlnet://digests/2026-08-{mcp_content.MAX_DIGESTS + 3:02d}")

    def test_unknown_resource_and_no_home(self):
        self.assertEqual(self.read("trawlnet://digests/1999-01-01")["error"]["code"], -32002)
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": tempfile.mkdtemp()}):
            self.assertEqual(self.uris(), [])  # dynamic sources need a data folder
            self.assertIn("/trawlnet:setup", self.read("trawlnet://master")["error"]["message"])

    def test_capabilities_advertise_resources_and_prompts(self):
        caps = rpc("initialize", {})["result"]["capabilities"]
        self.assertTrue({"tools", "resources", "prompts"} <= set(caps))


class Prompts(unittest.TestCase):
    def get(self, name, **args):
        return rpc("prompts/get", {"name": name, "arguments": args})

    def test_list_declares_arguments(self):
        prompts = {p["name"]: p for p in rpc("prompts/list")["result"]["prompts"]}
        self.assertEqual(prompts["tailor_for_job"]["arguments"],
                         [{"name": "key", "description": "job key from search_jobs", "required": True}])
        self.assertFalse(prompts["weekly_review"]["arguments"][0]["required"])

    def test_tailor_names_the_job_resources_and_rules(self):
        text = self.get("tailor_for_job", key="abc123")["result"]["messages"][0]["content"]["text"]
        for part in ("`abc123`", "get_job", "fetch_job_description", "trawlnet://tailoring-rules", "trawlnet://master",
                     "untrusted"):
            self.assertIn(part, text)

    def test_weekly_review_uses_the_window(self):
        since = (dt.date.today() - dt.timedelta(days=14)).isoformat()
        text = self.get("weekly_review", days="14")["result"]["messages"][0]["content"]["text"]
        self.assertIn(f"since={since}", text)
        self.assertIn("no date_added column", text)  # the default tracker columns have none: query_tracker says so
        default = self.get("weekly_review")["result"]["messages"][0]["content"]["text"]
        self.assertIn((dt.date.today() - dt.timedelta(days=7)).isoformat(), default)

    def test_bad_arguments_are_invalid_params(self):
        bad = [self.get("tailor_for_job"), self.get("tailor_for_job", key=""), self.get("tailor_for_job", key="a`\nb"),
               self.get("tailor_for_job", key=5)]
        bad += [self.get("weekly_review", days=d) for d in ("soon", "0", "91", "1.5", [7], True, "-3", 0, [], False, "\u00b2", "9" * 5000)]
        for r in bad:
            self.assertEqual(r["error"]["code"], srv.INVALID_PARAMS, r)
        for ok in (7, "7", " 30 ", None, ""):
            self.assertIn("since", self.get("weekly_review", days=ok)["result"]["messages"][0]["content"]["text"])

    def test_bug_in_a_prompt_is_internal_error_not_bad_arguments(self):
        srv.PROMPTS["buggy"] = ({"name": "buggy"}, lambda a: {}["missing"])
        self.addCleanup(srv.PROMPTS.pop, "buggy")
        with mock.patch("sys.stderr", mock.MagicMock()):
            self.assertEqual(self.get("buggy")["error"]["code"], srv.INTERNAL_ERROR)


if __name__ == "__main__":
    unittest.main()
