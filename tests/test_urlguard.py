"""urlguard.py: which addresses count as public, and redirects checked hop by hop (local server, no network)."""
import _home  # noqa: F401  (must be first: sets JOB_SEARCH_HOME)

import http.server
import ipaddress
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import urlguard
from sources import net


class PublicTest(unittest.TestCase):
    def test_ipv6_forms_embedding_ipv4_are_judged_by_the_ipv4(self):
        verdicts = {
            "8.8.8.8": True, "2606:4700::1111": True, "::ffff:8.8.8.8": True,
            "10.0.0.1": False, "127.0.0.1": False, "169.254.169.254": False, "224.0.0.1": False, "::1": False,
            "::ffff:10.0.0.1": False,
            "64:ff9b::808:808": True, "64:ff9b::a00:1": False, "64:ff9b::7f00:1": False,  # NAT64
            "64:ff9b:1::a00:1": False,  # local-use NAT64: always a gateway to private ranges
            "::7f00:1": False, "::a00:1": False, "::808:808": True, "::": False,  # IPv4-compatible
            "::ffff:0:808:808": True, "::ffff:0:a00:1": False,  # IPv4-translated (SIIT)
            "fec0::1": False,  # site-local
            "2002:808:808::1": True, "2002:a00:1::1": False, "2002:7f00:1::1": False,  # 6to4, same on every Python
        }
        for text, expected in verdicts.items():
            self.assertEqual(urlguard.public(ipaddress.ip_address(text)), expected, text)


class Redirects(unittest.TestCase):
    def setUp(self):
        env = {k: "" for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy")}  # local server: no proxy
        patcher = mock.patch.dict("os.environ", {**env, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        patcher.start()
        self.addCleanup(patcher.stop)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ok":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"posting")
                else:
                    self.send_response(302)
                    self.send_header("Location", self.server.target if self.path == "/go" else "/ok")
                    self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.server.target = "http://10.0.0.1/admin"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_a_redirect_to_a_non_public_address_is_refused(self):
        with self.assertRaisesRegex(urllib.error.URLError, r"refused redirect to http://10.0.0.1/admin: redirect must be "
                                                           r"a public address"):
            urlguard.opener().open(f"{self.base}/go", timeout=5)
        self.server.target = "file:///etc/passwd"
        with self.assertRaises(urllib.error.URLError):  # the scheme is checked too
            urlguard.opener().open(f"{self.base}/go", timeout=5)

    def test_a_checked_redirect_is_followed(self):
        self.server.target = f"{self.base}/ok"
        with mock.patch.object(urlguard, "check_url") as check:  # the local server itself is not public
            self.assertEqual(urlguard.opener().open(f"{self.base}/go", timeout=5).read(), b"posting")
        check.assert_called_once_with(f"{self.base}/ok", "redirect")

    def test_net_get_checks_redirects_only_when_asked(self):
        with mock.patch.object(urlguard, "check_url", side_effect=ValueError("no")):
            with self.assertRaises(urllib.error.URLError):
                net.get(f"{self.base}/go", public_only=True)
        self.server.target = f"{self.base}/ok"
        self.assertEqual(net.get(f"{self.base}/go"), b"posting")  # the pipeline's own fetches are unchanged


if __name__ == "__main__":
    unittest.main()
