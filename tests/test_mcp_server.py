"""mcp_server.py: JSON-RPC framing, the initialize handshake, dispatch and errors, venv re-exec."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import mcp_server as srv
import homes


def tmpdir(case) -> str:
    d = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, d, True)
    return d


def rpc(method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return srv.handle(json.dumps(msg))


class Handshake(unittest.TestCase):
    def test_initialize_echoes_supported_version(self):
        r = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["serverInfo"]["name"], "trawlnet")
        self.assertIn("tools", r["result"]["capabilities"])
        r = rpc("initialize", {"protocolVersion": "1900-01-01"})  # unsupported: offer the newest
        self.assertEqual(r["result"]["protocolVersion"], srv.VERSIONS[0])

    def test_capabilities_follow_registries(self):
        with mock.patch.dict(srv.RESOURCES, clear=True), mock.patch.dict(srv.PROMPTS, clear=True), \
                mock.patch.object(srv, "RESOURCE_SOURCES", []):
            self.assertEqual(set(rpc("initialize", {})["result"]["capabilities"]), {"tools"})
        with mock.patch.dict(srv.RESOURCES, {"x://a": ({"uri": "x://a", "name": "a"}, lambda: "")}), \
                mock.patch.dict(srv.PROMPTS, {"p": ({"name": "p"}, lambda a: "")}):
            self.assertEqual(set(rpc("initialize", {})["result"]["capabilities"]), {"tools", "resources", "prompts"})

    def test_modern_discover_probe_is_method_not_found(self):
        # Claude Code probes server/discover first and falls back to initialize on this error
        self.assertEqual(rpc("server/discover", {})["error"]["code"], srv.METHOD_NOT_FOUND)

    def test_ping(self):
        self.assertEqual(rpc("ping")["result"], {})


class Framing(unittest.TestCase):
    def test_notifications_and_blank_lines_get_no_reply(self):
        self.assertIsNone(srv.handle('{"jsonrpc":"2.0","method":"notifications/initialized"}'))
        self.assertIsNone(srv.handle("  \n"))

    def test_bad_messages(self):
        self.assertEqual(srv.handle("{nope")["error"]["code"], srv.PARSE_ERROR)
        self.assertEqual(srv.handle("[1]")["error"]["code"], srv.INVALID_REQUEST)
        self.assertEqual(srv.handle('{"id":4}'), srv._error(4, srv.INVALID_REQUEST, "Invalid request"))
        self.assertEqual(srv.handle('{"id":5,"method":"ping","params":[1]}')["error"]["code"], srv.INVALID_PARAMS)

    def test_serve_writes_one_line_per_response(self):
        out = io.StringIO()
        srv.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
                              '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                              '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'), out)
        lines = out.getvalue().splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [1, 2])


def _boom(args):
    raise RuntimeError("bug")


def _bad(args):
    raise ValueError("limit must be a number")


def _exits(args):
    raise SystemExit("No country pack 'zz.yaml'")


def _noisy(args):
    print("engine chatter")
    return 1


class Tools(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(srv.TOOLS, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        srv.tool("echo", "Echo args.", {"x": {"type": "string"}}, required=["x"])(lambda a: {"got": a["x"], "é": 1})
        srv.tool("boom", "Fails.", read_only=False)(_boom)
        srv.tool("bad", "Bad args.")(_bad)
        srv.tool("exits", "Exits.")(_exits)
        srv.tool("noisy", "Prints.")(_noisy)

    def test_list(self):
        tools = {t["name"]: t for t in rpc("tools/list")["result"]["tools"]}
        self.assertEqual(tools["echo"]["inputSchema"]["required"], ["x"])
        self.assertTrue(tools["echo"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["boom"]["annotations"]["readOnlyHint"])

    def test_call_returns_compact_json_text(self):
        r = rpc("tools/call", {"name": "echo", "arguments": {"x": "hi"}})["result"]
        self.assertNotIn("isError", r)
        self.assertEqual(r["content"][0]["text"], '{"got":"hi","é":1}')

    def test_unknown_tool_or_bad_arguments_is_protocol_error(self):
        self.assertEqual(rpc("tools/call", {"name": "nope"})["error"]["code"], srv.INVALID_PARAMS)
        self.assertEqual(rpc("tools/call", {"name": "echo", "arguments": [1]})["error"]["code"], srv.INVALID_PARAMS)

    def test_engine_exit_is_tool_error(self):
        r = rpc("tools/call", {"name": "exits"})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("zz.yaml", r["content"][0]["text"])
        with mock.patch.object(srv, "TOOLS", {"x": ({}, lambda a: sys.exit(2))}):
            self.assertEqual(rpc("tools/call", {"name": "x"})["result"]["content"][0]["text"], "engine exited (2)")

    def test_engine_prints_go_to_stderr(self):
        out = io.StringIO()
        with mock.patch("sys.stderr", io.StringIO()) as err:
            srv.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"noisy"}}\n'), out)
        self.assertEqual(json.loads(out.getvalue())["result"]["content"][0]["text"], "1")
        self.assertIn("engine chatter", err.getvalue())

    def test_argument_errors_are_tool_errors(self):
        r = rpc("tools/call", {"name": "bad"})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("limit must be a number", r["content"][0]["text"])
        self.assertTrue(rpc("tools/call", {"name": "echo"})["result"]["isError"])  # KeyError: missing argument

    def test_bugs_are_internal_errors_and_server_survives(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            r = rpc("tools/call", {"name": "boom"})
        self.assertEqual(r["error"]["code"], srv.INTERNAL_ERROR)
        self.assertIn("RuntimeError: bug", err.getvalue())
        self.assertEqual(rpc("ping")["result"], {})

    def test_no_data_folder(self):
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": tmpdir(self)}):
            r = rpc("tools/call", {"name": "echo", "arguments": {"x": "hi"}})["result"]
        self.assertTrue(r["isError"])
        self.assertIn("/trawlnet:setup", r["content"][0]["text"])


class ResourcesPrompts(unittest.TestCase):
    def setUp(self):
        for patch in (mock.patch.dict(srv.RESOURCES, clear=True), mock.patch.dict(srv.PROMPTS, clear=True),
                      mock.patch.object(srv, "RESOURCE_SOURCES", [])):
            patch.start()
            self.addCleanup(patch.stop)
        srv.RESOURCES["trawlnet://a"] = ({"uri": "trawlnet://a", "name": "a"}, lambda: "# A")
        srv.PROMPTS["p"] = ({"name": "p", "description": "P."}, lambda a: f"hello {a.get('who', 'you')}")

    def test_resources(self):
        self.assertEqual(rpc("resources/list")["result"]["resources"], [{"uri": "trawlnet://a", "name": "a"}])
        self.assertEqual(rpc("resources/read", {"uri": "trawlnet://a"})["result"]["contents"],
                         [{"uri": "trawlnet://a", "mimeType": "text/markdown", "text": "# A"}])
        self.assertEqual(rpc("resources/read", {"uri": "trawlnet://b"})["error"]["code"], -32002)

    def test_prompts(self):
        self.assertEqual(rpc("prompts/list")["result"]["prompts"], [{"name": "p", "description": "P."}])
        r = rpc("prompts/get", {"name": "p", "arguments": {"who": "me"}})["result"]
        self.assertEqual(r["messages"][0]["content"]["text"], "hello me")
        self.assertEqual(rpc("prompts/get", {"name": "q"})["error"]["code"], srv.INVALID_PARAMS)
        self.assertEqual(rpc("prompts/get", {"name": "p", "arguments": [1]})["error"]["code"], srv.INVALID_PARAMS)

    def test_engine_exit_and_missing_home_keep_serving(self):
        srv.RESOURCES["trawlnet://x"] = ({"uri": "trawlnet://x", "name": "x"}, lambda: sys.exit("No config.yaml"))
        self.assertEqual(rpc("resources/read", {"uri": "trawlnet://x"})["error"],
                         {"code": srv.INTERNAL_ERROR, "message": "No config.yaml"})
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": tmpdir(self)}):
            for r in (rpc("resources/read", {"uri": "trawlnet://a"}), rpc("prompts/get", {"name": "p"})):
                self.assertIn("/trawlnet:setup", r["error"]["message"])


class FindHome(unittest.TestCase):
    def test_matches_common_for_env_and_cwd_walk(self):
        self.assertEqual(srv.find_home(), common.HOME)
        sub = common.DATA / "profiles"
        with mock.patch.dict(os.environ), mock.patch("pathlib.Path.cwd", return_value=sub):
            os.environ.pop("JOB_SEARCH_HOME")
            self.assertEqual(srv.find_home(), common._find_home())
            self.assertEqual(srv.find_home(), common.HOME.resolve())

    def test_none_without_config(self):
        with mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": tmpdir(self)}):
            self.assertIsNone(srv.find_home())


class Reexec(unittest.TestCase):
    def setUp(self):
        self.home = Path(tmpdir(self)).resolve()
        (self.home / "config.yaml").write_text("")
        (self.home / "data").mkdir()
        patch = mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)

    def _venv(self, register=True):
        if register:
            homes.register(self.home)
        py = self.home / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text("")
        py.chmod(0o755)
        return py

    def test_no_venv_serves_utf8_stdio_in_place(self):
        with mock.patch.object(srv, "serve") as serve, mock.patch("os.execv") as execv, \
                mock.patch.object(sys, "stdin") as i, mock.patch.object(sys, "stdout") as o:
            srv.main()
        execv.assert_not_called()
        serve.assert_called_once()
        i.reconfigure.assert_called_once_with(encoding="utf-8")
        o.reconfigure.assert_called_once_with(encoding="utf-8")

    def test_reexecs_into_data_folder_venv_once(self):
        py = self._venv()
        with mock.patch.dict(os.environ), mock.patch("os.execv") as execv, mock.patch.object(srv, "serve"):
            os.environ.pop("TRAWLNET_MCP_REEXEC", None)
            srv.main()
            self.assertEqual(os.environ["TRAWLNET_MCP_REEXEC"], "1")
            self.assertIsNone(srv._venv_python())  # marker set: never loops
        self.assertEqual(execv.call_args[0][0], str(py))

    def test_unregistered_folder_venv_is_never_run(self):
        """A cloned folder shaped like a data folder must not get its binary run at session start."""
        self._venv(register=False)
        with mock.patch.dict(os.environ):
            os.environ.pop("TRAWLNET_MCP_REEXEC", None)
            self.assertIsNone(srv._venv_python())

    def test_broken_venv_python_serves_in_place(self):
        self._venv()
        with mock.patch.dict(os.environ), mock.patch("os.execv", side_effect=OSError(8, "Exec format error")), \
                mock.patch.object(srv, "serve") as serve, mock.patch.object(sys, "stdin"), \
                mock.patch.object(sys, "stdout"), mock.patch.object(sys, "stderr", io.StringIO()) as err:
            os.environ.pop("TRAWLNET_MCP_REEXEC", None)
            srv.main()
        serve.assert_called_once()
        self.assertIn("cannot run", err.getvalue())

    def test_already_in_venv(self):
        py = self._venv()
        with mock.patch.dict(os.environ), mock.patch.object(sys, "prefix", str(py.parents[1])):
            os.environ.pop("TRAWLNET_MCP_REEXEC", None)
            self.assertIsNone(srv._venv_python())

    def test_reexec_needs_no_pyyaml(self):
        """The launching python may lack PyYAML: finding the venv must not import common."""
        py = self._venv()
        code = ("import sys, os; sys.modules['yaml'] = None; sys.path.insert(0, %r); import mcp_server as s; "
                "os.execv = lambda p, a: print('EXEC', p); s.main()" % str(Path(srv.__file__).parent))
        env = {k: v for k, v in os.environ.items() if k != "TRAWLNET_MCP_REEXEC"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30,
                             stdin=subprocess.DEVNULL)
        self.assertIn(f"EXEC {py}", out.stdout, out.stderr)


class NoEngine(unittest.TestCase):
    """A python without PyYAML (unregistered folder, no .venv, or no data folder) still serves and says why."""

    def tmp(self):
        d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def data_folder(self):
        home = self.tmp()
        (home / "config.yaml").write_text("")
        (home / "data").mkdir()
        return home

    def serve(self, home, *msgs):
        code = ("import sys; sys.modules['yaml'] = None; sys.path.insert(0, %r); import mcp_server as s; s.main()"
                % str(Path(srv.__file__).parent))
        env = {k: v for k, v in os.environ.items() if k != "TRAWLNET_MCP_REEXEC"}
        env["JOB_SEARCH_HOME"] = str(home)
        lines = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p}) + "\n"
                        for i, (m, p) in enumerate(msgs))
        out = subprocess.run([sys.executable, "-c", code], input=lines, capture_output=True, text=True, env=env,
                             timeout=30)
        self.assertIn("engine not loaded", out.stderr)
        return [json.loads(l) for l in out.stdout.splitlines()]

    def test_unregistered_folder_is_told_to_link(self):
        init, tools, call, res = self.serve(self.data_folder(), ("initialize", {}), ("tools/list", {}),
                                            ("tools/call", {"name": "status"}), ("resources/list", {}))
        self.assertEqual(init["result"]["serverInfo"]["name"], "trawlnet")
        self.assertNotIn("resources", init["result"]["capabilities"])  # nothing half-loaded
        self.assertEqual([t["name"] for t in tools["result"]["tools"]], ["status"])
        self.assertTrue(call["result"]["isError"])
        self.assertIn("./js setup link", call["result"]["content"][0]["text"])
        self.assertIn(str(homes.homes_file()), call["result"]["content"][0]["text"])
        self.assertEqual(res["result"]["resources"], [])

    def test_registered_folder_without_env_is_told_to_rebuild_it(self):
        home = self.data_folder()
        homes.register(home)
        (call,) = self.serve(home, ("tools/call", {"name": "status"}))
        self.assertIn("./js setup env", call["result"]["content"][0]["text"])

    def test_no_data_folder(self):
        (call,) = self.serve(self.tmp(), ("tools/call", {"name": "status"}))
        self.assertIn("No trawlnet data folder", call["result"]["content"][0]["text"])

    def test_message_without_a_home_directory(self):
        with mock.patch.object(homes, "homes_file", side_effect=OSError("no home")), mock.patch.dict(srv.TOOLS), \
                mock.patch.dict(srv.RESOURCES), mock.patch.dict(srv.PROMPTS), \
                mock.patch.object(srv, "RESOURCE_SOURCES", list(srv.RESOURCE_SOURCES)), \
                mock.patch.dict(os.environ, {"JOB_SEARCH_HOME": str(self.data_folder())}), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            srv._engine_missing(ModuleNotFoundError("No module named 'yaml'"))
            text = srv._call_tool({"name": "status"})["content"][0]["text"]
        self.assertIn("~/.config/trawlnet/homes", text)

    def test_other_import_errors_still_fail(self):
        """Only a missing module means "no env python"; a broken engine import must not hide behind the stub."""
        real = __import__

        def broken(name, *a, **kw):
            if name == "mcp_content":
                raise ImportError("cannot import name 'gone' from 'jobsearch'")
            return real(name, *a, **kw)

        with mock.patch("builtins.__import__", broken), mock.patch.object(srv, "serve") as serve, \
                mock.patch.object(srv, "_venv_python", return_value=None), \
                mock.patch.dict(srv.TOOLS, {"sentinel": None}, clear=True), \
                mock.patch.object(sys, "stdin"), mock.patch.object(sys, "stdout"):
            with self.assertRaises(ImportError):
                srv.main()
            self.assertEqual(list(srv.TOOLS), ["sentinel"])  # not cleared, no stub
        serve.assert_not_called()


class Stdio(unittest.TestCase):
    def test_subprocess_handshake(self):
        """End to end over real pipes, as a host runs it."""
        msgs = [{"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {}},
                {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
        lines = "".join(json.dumps(m) + "\n" for m in msgs)
        out = subprocess.run([sys.executable, str(Path(srv.__file__))], input=lines, capture_output=True, text=True,
                             timeout=30, check=True).stdout
        replies = [json.loads(l) for l in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], ["d", 0, 1])
        self.assertIn("error", replies[0])
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
        self.assertIn("search_jobs", [x["name"] for x in replies[2]["result"]["tools"]])  # registered via main()


if __name__ == "__main__":
    unittest.main()
