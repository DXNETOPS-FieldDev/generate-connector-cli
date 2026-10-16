"""Tests for support/json_to_toml.py — backup JSON → TOML conversion."""
import io
import json
import os
import sys
import tempfile
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "support"))
import json_to_toml as jtt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_JSON = {
    "id": 12345,
    "name": "Test Connector",
    "enabled": True,
    "notifyOn": ["ALARM_RAISED", "ALARM_CLEARED"],
    "connectors": [{
        "url": "http://splunk.example.com/services/collector/raw",
        "method": "POST",
        "ignoreSslErrors": False,
        "headers": [
            {"key": "Content-Type", "value": "application/json"},
            {"key": "Authorization", "value": "Splunk abc123"},
        ],
        "auth": {"type": "NONE"},
    }],
}

FULL_JSON = {
    **MINIMAL_JSON,
    "description": "Full test connector",
    "include": {
        "monitoringPointName": ["mp-glob-*", "specific-host"],
        "monitoringPointType": ["macOS", "Windows"],
        "monitoringPointId": [111111, 222222],
        "monitoringPolicyId": [333],
        "monitoringPolicyGroupId": [444],
        "tags": [
            {"category": "Cloud", "value": "GCP"},
            {"category": "Location", "value": "Seattle WA"},
        ],
    },
    "exclude": {
        "monitoringPointType": ["c50 Container"],
    },
}

MASKED_JSON = {
    **MINIMAL_JSON,
    "connectors": [{
        **MINIMAL_JSON["connectors"][0],
        "headers": [
            {"key": "Authorization", "value": "******"},
        ],
    }],
}


# ---------------------------------------------------------------------------
# scan_masked
# ---------------------------------------------------------------------------

class TestScanMasked(unittest.TestCase):

    def test_no_masked(self):
        self.assertEqual(jtt.scan_masked(MINIMAL_JSON), [])

    def test_header_masked(self):
        paths = jtt.scan_masked(MASKED_JSON)
        self.assertEqual(len(paths), 1)
        self.assertIn("connectors[0].headers[0].value", paths)

    def test_nested_masked(self):
        data = {"auth": {"token": "***", "type": "TOKEN"}}
        paths = jtt.scan_masked(data)
        self.assertEqual(paths, ["auth.token"])

    def test_partial_stars_not_masked(self):
        # Only pure *** strings are masked; real values with stars are fine
        data = {"value": "Bearer ***token"}
        self.assertEqual(jtt.scan_masked(data), [])

    def test_multiple_masked(self):
        data = {
            "connectors": [{"headers": [
                {"key": "Auth", "value": "****"},
                {"key": "Other", "value": "ok"},
                {"key": "Pass", "value": "***"},
            ]}]
        }
        paths = jtt.scan_masked(data)
        self.assertEqual(len(paths), 2)


# ---------------------------------------------------------------------------
# json_to_toml — basic structure
# ---------------------------------------------------------------------------

class TestJsonToToml(unittest.TestCase):

    def _parse(self, data):
        content = jtt.json_to_toml(data)
        return tomllib.loads(content), content

    def test_minimal_parses_as_valid_toml(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        self.assertIn("connector", parsed)
        self.assertIn("webhook", parsed)

    def test_connector_fields(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        c = parsed["connector"]
        self.assertEqual(c["id"], 12345)
        self.assertEqual(c["name"], "Test Connector")
        self.assertTrue(c["enabled"])

    def test_notify_on(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        self.assertEqual(parsed["notify_on"], ["ALARM_RAISED", "ALARM_CLEARED"])

    def test_webhook_fields(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        wh = parsed["webhook"]
        self.assertEqual(wh["url"], "http://splunk.example.com/services/collector/raw")
        self.assertEqual(wh["method"], "POST")
        self.assertFalse(wh["ignore_ssl_errors"])

    def test_webhook_headers(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        headers = parsed["webhook"]["headers"]
        self.assertEqual(len(headers), 2)
        self.assertEqual(headers[0]["key"], "Content-Type")
        self.assertEqual(headers[1]["key"], "Authorization")
        self.assertEqual(headers[1]["value"], "Splunk abc123")

    def test_webhook_auth(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        self.assertEqual(parsed["webhook"]["auth"]["type"], "NONE")

    def test_description_included(self):
        parsed, _ = self._parse(FULL_JSON)
        self.assertEqual(parsed["connector"]["description"], "Full test connector")

    def test_no_description_omitted(self):
        parsed, _ = self._parse(MINIMAL_JSON)
        self.assertNotIn("description", parsed["connector"])

    def test_disabled_connector(self):
        data = {**MINIMAL_JSON, "enabled": False}
        parsed, _ = self._parse(data)
        self.assertFalse(parsed["connector"]["enabled"])


# ---------------------------------------------------------------------------
# json_to_toml — filter blocks
# ---------------------------------------------------------------------------

class TestFilterBlocks(unittest.TestCase):

    def _parse(self, data):
        return tomllib.loads(jtt.json_to_toml(data))

    def test_include_name_globs(self):
        parsed = self._parse(FULL_JSON)
        names = parsed["include"]["monitoring_point_names"]
        self.assertIn("mp-glob-*", names)
        self.assertIn("specific-host", names)

    def test_include_types(self):
        parsed = self._parse(FULL_JSON)
        self.assertEqual(parsed["include"]["monitoring_point_types"], ["macOS", "Windows"])

    def test_include_tags(self):
        parsed = self._parse(FULL_JSON)
        tags = parsed["include"]["tags"]
        self.assertEqual(len(tags), 2)
        self.assertEqual(tags[0], {"category": "Cloud", "value": "GCP"})

    def test_exclude_block(self):
        parsed = self._parse(FULL_JSON)
        self.assertIn("exclude", parsed)
        self.assertEqual(parsed["exclude"]["monitoring_point_types"], ["c50 Container"])

    def test_ids_are_emitted_as_real_id_keys(self):
        inc = self._parse(FULL_JSON)["include"]
        self.assertEqual(inc["monitoring_point_ids"], [111111, 222222])
        self.assertEqual(inc["monitoring_policy_ids"], [333])
        self.assertEqual(inc["monitoring_policy_group_ids"], [444])

    def test_severity_noise_rules_and_targets_round_trip(self):
        data = {**MINIMAL_JSON, "include": {
            "severity": ["CRITICAL"], "noiseFilter": ["FREQUENT"],
            "alarmRuleId": [105], "target": ["192.0.2.1"]}}
        inc = self._parse(data)["include"]
        self.assertEqual(inc["severity"], ["CRITICAL"])
        self.assertEqual(inc["noise_filter"], ["FREQUENT"])
        self.assertEqual(inc["alarm_rule_ids"], [105])
        self.assertEqual(inc["targets"], ["192.0.2.1"])

    def test_generator_style_singular_tag_key_is_read(self):
        data = {**MINIMAL_JSON, "include": {"tag": [{"category": "A", "value": "b"}]}}
        self.assertEqual(self._parse(data)["include"]["tags"], [{"category": "A", "value": "b"}])

    def test_ignore_ssl_errors_reads_the_api_spelling(self):
        conn = {**MINIMAL_JSON["connectors"][0]}
        conn.pop("ignoreSslErrors", None)
        conn["ignoreSSLErrors"] = True
        parsed = self._parse({**MINIMAL_JSON, "connectors": [conn]})
        self.assertTrue(parsed["webhook"]["ignore_ssl_errors"])

    def test_credentials_are_never_written(self):
        conn = {**MINIMAL_JSON["connectors"][0], "auth": {"type": "BASIC", "username": "u", "password": "hunter2"}}
        content = jtt.json_to_toml({**MINIMAL_JSON, "connectors": [conn]})
        self.assertNotIn("hunter2", content)
        self.assertIn("CUSTOMER_WEBHOOK_PASSWORD", content)

    def test_control_characters_are_escaped_and_still_parse(self):
        data = {**MINIMAL_JSON, "name": "a\nb\tc\x01"}
        self.assertEqual(self._parse(data)["connector"]["name"], "a\nb\tc\x01")

    def test_non_object_input_is_a_clean_error(self):
        for bad in ([], None, "x", 5):
            with self.subTest(bad), self.assertRaises(SystemExit):
                jtt.json_to_toml(bad)

    def test_written_file_is_private(self):
        import stat
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "c.json")
            with open(src, "w") as fh:
                json.dump(MINIMAL_JSON, fh)
            sys.argv = ["json_to_toml.py", src]
            jtt.main()
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "c.toml")).st_mode), 0o600)

    def test_no_filter_blocks_when_absent(self):
        parsed = self._parse(MINIMAL_JSON)
        self.assertNotIn("include", parsed)
        self.assertNotIn("exclude", parsed)


# ---------------------------------------------------------------------------
# CLI entry points (via main() with mocked argv)
# ---------------------------------------------------------------------------

class TestCLI(unittest.TestCase):

    def _write_json(self, data, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_stdout_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "backup.json")
            self._write_json(MINIMAL_JSON, src)
            sys.argv = ["json_to_toml.py", src, "--stdout"]
            buf = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = buf
            try:
                jtt.main()
            finally:
                sys.stdout = old_stdout
            output = buf.getvalue()
        self.assertIn("[connector]", output)
        self.assertIn("[webhook]", output)

    def test_default_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "20260716-093045-connector-12345.json")
            self._write_json(MINIMAL_JSON, src)
            sys.argv = ["json_to_toml.py", src]
            jtt.main()
            expected = os.path.join(tmpdir, "20260716-093045-connector-12345.toml")
            self.assertTrue(os.path.exists(expected))

    def test_custom_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "backup.json")
            out = os.path.join(tmpdir, "reverted.toml")
            self._write_json(MINIMAL_JSON, src)
            sys.argv = ["json_to_toml.py", src, "-o", out]
            jtt.main()
            self.assertTrue(os.path.exists(out))

    def test_masked_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "masked.json")
            self._write_json(MASKED_JSON, src)
            sys.argv = ["json_to_toml.py", src, "--stdout"]
            with self.assertRaises(SystemExit) as ctx:
                jtt.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_file_exits_nonzero(self):
        sys.argv = ["json_to_toml.py", "/nonexistent/path/backup.json", "--stdout"]
        with self.assertRaises(SystemExit) as ctx:
            jtt.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_invalid_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "bad.json")
            with open(src, "w") as f:
                f.write("{not valid json")
            sys.argv = ["json_to_toml.py", src, "--stdout"]
            with self.assertRaises(SystemExit) as ctx:
                jtt.main()
            self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
