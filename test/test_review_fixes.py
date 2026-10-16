"""Regression tests for the security/robustness review of generate_connector.py."""
import os
import stat
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_connector as gc
from test_precheck import MINIMAL_VALID, errors_for


class TestEmptiedInclude(unittest.TestCase):
    """exclude-wins can empty the include block, silently turning 'only X' into 'everything except X'."""

    def _client(self, name):
        return SimpleNamespace(org_name=name)

    def test_flags_orgs_whose_include_vanished_through_a_contradiction(self):
        results = [(self._client("A"), {"exclude": {"severity": ["CRITICAL"]}}),
                   (self._client("B"), {"include": {"severity": ["MAJOR"]}, "exclude": {"severity": ["CRITICAL"]}})]
        errors = [{"type": "contradiction", "org": "A", "value": "severity: CRITICAL"}]
        self.assertEqual(gc._orgs_with_emptied_include(results, errors), ["A"])

    def test_no_contradiction_means_nothing_flagged(self):
        results = [(self._client("A"), {"exclude": {"severity": ["MINOR"]}})]
        self.assertEqual(gc._orgs_with_emptied_include(results, []), [])


class TestUnreadableExisting(unittest.TestCase):

    def test_put_without_a_readable_existing_connector_is_fatal(self):
        with self.assertRaises(SystemExit) as ctx:
            gc._require_existing(None, 123, "Acme", lenient=False)
        self.assertIn("123", str(ctx.exception))

    def test_lenient_lets_it_through(self):
        gc._require_existing(None, 123, "Acme", lenient=True)

    def test_readable_existing_is_fine(self):
        gc._require_existing({"id": 1}, 123, "Acme", lenient=False)


class TestEnvInterpolationRejectsControlChars(unittest.TestCase):

    def test_crlf_in_expanded_value_is_fatal(self):
        with patch.dict(os.environ, {"TOK": "abc\r\nX-Evil: 1"}), self.assertRaises(SystemExit):
            gc.interpolate_env("Bearer ${TOK}")

    def test_plain_value_still_expands(self):
        with patch.dict(os.environ, {"TOK": "abc"}):
            self.assertEqual(gc.interpolate_env("Bearer ${TOK}"), "Bearer abc")


class TestValidateConfigCatchesCliOnlyInjection(unittest.TestCase):

    def _cfg(self, **webhook):
        return {"connector": {"name": "c"}, "webhook": {"url": "https://h.example.com", **webhook}}

    def test_crlf_in_name_url_and_headers_is_fatal(self):
        cases = [
            {"connector": {"name": "c\r\nd"}, "webhook": {"url": "https://h.example.com"}},
            {"connector": {"name": "c"}, "webhook": {"url": "https://h\r\nHost: x"}},
            self._cfg(headers=[{"key": "X-A", "value": "v\r\nEvil: 1"}]),
            self._cfg(headers=[{"key": "X-A\r\nEvil", "value": "v"}]),
        ]
        for cfg in cases:
            with self.subTest(cfg), self.assertRaises(SystemExit):
                gc.validate_config(cfg)

    def test_clean_config_passes(self):
        gc.validate_config(self._cfg(headers=[{"key": "X-A", "value": "v"}]))


class TestMalformedTomlIsAnErrorNotATraceback(unittest.TestCase):

    def test_bad_types_are_reported(self):
        cases = {
            "monitoring_point_types": MINIMAL_VALID + "\n[include]\nmonitoring_point_types = [50]\n",
            "tag category": MINIMAL_VALID + "\n[[include.tags]]\ncategory = 1\nvalue = \"x\"\n",
            "header value": MINIMAL_VALID + "\n[[webhook.headers]]\nkey = \"X-A\"\nvalue = 5\n",
            "header key": MINIMAL_VALID + "\n[[webhook.headers]]\nkey = 5\nvalue = \"v\"\n",
            "org bool": "org = true\n" + MINIMAL_VALID,
            "description": MINIMAL_VALID.replace('name = "Test Connector"', 'name = "T"\ndescription = 5', 1),
            "targets": MINIMAL_VALID + "\n[include]\ntargets = [1]\n",
        }
        for label, toml in cases.items():
            with self.subTest(label):
                self.assertTrue(errors_for(toml), label)


class TestBackupFile(unittest.TestCase):

    def test_backup_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd(); os.chdir(d)
            try:
                path = gc._write_backup({"id": 5, "name": "x"}, 5)
                mode = stat.S_IMODE(os.stat(path).st_mode)
            finally:
                os.chdir(old)
        self.assertEqual(mode, 0o600)


class TestDryRunWritesNothing(unittest.TestCase):

    def test_generate_toml_is_not_written_under_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "gen.toml")
            argv = ["generate_connector.py", "--connector-name", "x", "--webhook-url",
                    "https://h.example.com", "--generate-toml", out, "--dry-run"]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "stderr", sys.stdout), self.assertRaises(SystemExit):
                gc.main()
            self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
