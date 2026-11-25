"""Tests for the HTTP layer's safety properties: base_url validation and redirect refusal."""
import http.server
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate_connector as gc


class TestValidateBaseUrl(unittest.TestCase):

    def test_accepts_plain_https_hosts(self):
        for ok in ("https://app-01.pm.appneta.com", "https://192.0.2.10", "https://nis.example.com:8443",
                   "https://nis.example.com/", "https://[::1]"):
            with self.subTest(ok):
                gc._validate_base_url(ok)

    def test_rejects_unsafe_forms(self):
        for bad in ("http://nis.example.com", "https://user:pw@nis.example.com", "https://nis.example.com?x=",
                    "https://nis.example.com#f", "https://nis.example.com/some/path",
                    "https://nis.example.com:0", "https://nis.example.com:99999",
                    "https://nis.example.com\r\nX: y", ""):
            with self.subTest(bad), self.assertRaises(SystemExit):
                gc._validate_base_url(bad)


class TestRedirectsAreRefused(unittest.TestCase):

    def test_redirect_is_not_followed_and_key_is_not_forwarded(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *a): pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
                self.end_headers()
            def log_message(self, *a): pass

        origin = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        for srv in (target, origin):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        req = urllib.request.Request(f"http://127.0.0.1:{origin.server_port}/",
                                     headers={"Authorization": "Token SEKRET"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 302)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
