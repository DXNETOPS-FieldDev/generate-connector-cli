"""Tests for precheck_toml — TOML structural and semantic validation."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate_connector as gc


def write_toml(content: str) -> str:
    """Write content to a temp .toml file; caller must unlink."""
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def errors_for(toml_content: str) -> list[dict]:
    path = write_toml(toml_content)
    try:
        _, issues = gc.precheck_toml(path)
        return [i for i in issues if i["level"] == "ERROR"]
    finally:
        os.unlink(path)


def warnings_for(toml_content: str) -> list[dict]:
    path = write_toml(toml_content)
    try:
        _, issues = gc.precheck_toml(path)
        return [i for i in issues if i["level"] == "WARNING"]
    finally:
        os.unlink(path)


MINIMAL_VALID = """
[connector]
name = "Test Connector"

[webhook]
url = "https://example.com/hook"
"""

MINIMAL_WITH_BASE = """
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Test Connector"

[webhook]
url = "https://example.com/hook"
"""


class TestPrecheckFileHandling(unittest.TestCase):

    def test_missing_file_is_error(self):
        _, issues = gc.precheck_toml("/tmp/does_not_exist_xyz_abc.toml")
        self.assertTrue(any("not found" in i["message"].lower() for i in issues if i["level"] == "ERROR"))

    def test_non_toml_extension_is_error(self):
        _, issues = gc.precheck_toml("config.json")
        self.assertTrue(any(i["level"] == "ERROR" for i in issues))

    def test_syntax_error_is_reported(self):
        path = write_toml("this is not [ valid toml !!!")
        try:
            _, issues = gc.precheck_toml(path)
            self.assertTrue(any(i["level"] == "ERROR" for i in issues))
        finally:
            os.unlink(path)


class TestPrecheckMinimalValid(unittest.TestCase):

    def test_minimal_config_has_no_errors(self):
        self.assertEqual(errors_for(MINIMAL_VALID), [])

    def test_minimal_with_base_url_has_no_errors(self):
        self.assertEqual(errors_for(MINIMAL_WITH_BASE), [])


class TestPrecheckRequiredSections(unittest.TestCase):

    def test_missing_connector_section_is_error(self):
        errs = errors_for("[webhook]\nurl = \"https://example.com/hook\"\n")
        self.assertTrue(any("connector" in i["field"] for i in errs))

    def test_missing_webhook_section_is_error(self):
        errs = errors_for("[connector]\nname = \"Test\"\n")
        self.assertTrue(any("webhook" in i["field"] for i in errs))

    def test_missing_connector_name_is_error(self):
        # Non-empty [connector] section (so precheck enters the name check) but no name key
        errs = errors_for('[connector]\ndescription = "no name here"\n\n[webhook]\nurl = "https://example.com/hook"\n')
        self.assertTrue(any("connector.name" in i["field"] for i in errs))

    def test_empty_connector_name_is_error(self):
        errs = errors_for('[connector]\nname = ""\n\n[webhook]\nurl = "https://example.com/hook"\n')
        self.assertTrue(any("connector.name" in i["field"] for i in errs))


class TestPrecheckUnknownKeys(unittest.TestCase):

    def test_unknown_top_level_key(self):
        errs = errors_for('typo_key = "value"\n' + MINIMAL_VALID)
        self.assertTrue(any("typo_key" in i["field"] for i in errs))

    def test_unknown_connector_key(self):
        errs = errors_for('[connector]\nname = "X"\ntypo = "val"\n\n[webhook]\nurl = "https://example.com/hook"\n')
        self.assertTrue(any("connector.typo" in i["field"] for i in errs))

    def test_unknown_webhook_key(self):
        errs = errors_for('[connector]\nname = "X"\n\n[webhook]\nurl = "https://example.com/hook"\ntypo = "val"\n')
        self.assertTrue(any("webhook.typo" in i["field"] for i in errs))

    def test_unknown_include_key(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nbogus_field = ["x"]\n')
        self.assertTrue(any("bogus_field" in i["field"] for i in errs))


class TestPrecheckBaseUrl(unittest.TestCase):

    def test_https_url_is_valid(self):
        errs = errors_for('base_url = "https://app-01.pm.appneta.com"\n' + MINIMAL_VALID)
        base_errs = [i for i in errs if "base_url" in i["field"]]
        self.assertEqual(base_errs, [])

    def test_http_url_is_error(self):
        errs = errors_for('base_url = "http://app-01.pm.appneta.com"\n' + MINIMAL_VALID)
        self.assertTrue(any("base_url" in i["field"] for i in errs))

    def test_non_string_base_url_is_error(self):
        errs = errors_for('base_url = 12345\n' + MINIMAL_VALID)
        self.assertTrue(any("base_url" in i["field"] for i in errs))


class TestPrecheckOrgField(unittest.TestCase):

    def test_org_integer_valid(self):
        errs = errors_for('org = 4160\n' + MINIMAL_VALID)
        org_errs = [i for i in errs if i["field"] == "org"]
        self.assertEqual(org_errs, [])

    def test_org_list_of_ints_valid(self):
        errs = errors_for('org = [4160, 4275]\n' + MINIMAL_VALID)
        org_errs = [i for i in errs if i["field"] == "org"]
        self.assertEqual(org_errs, [])

    def test_org_name_string_valid(self):
        errs = errors_for('org = "Carousel"\n' + MINIMAL_VALID)
        org_errs = [i for i in errs if i["field"] == "org"]
        self.assertEqual(org_errs, [])

    def test_org_list_with_string_is_error(self):
        errs = errors_for('org = [4160, "not-an-int"]\n' + MINIMAL_VALID)
        self.assertTrue(any(i["field"] == "org" for i in errs))

    def test_org_float_is_error(self):
        errs = errors_for('org = 4160.5\n' + MINIMAL_VALID)
        self.assertTrue(any(i["field"] == "org" for i in errs))


class TestPrecheckCredentials(unittest.TestCase):

    def test_password_in_config_is_error(self):
        errs = errors_for(
            '[connector]\nname = "X"\n\n[webhook]\nurl = "https://example.com/hook"\n'
            '[webhook.auth]\ntype = "BASIC"\nusername = "user"\npassword = "secret"\n'
        )
        self.assertTrue(any("password" in i["field"] for i in errs))

    def test_token_in_config_is_error(self):
        errs = errors_for(
            '[connector]\nname = "X"\n\n[webhook]\nurl = "https://example.com/hook"\n'
            '[webhook.auth]\ntype = "BEARER"\ntoken = "mytoken"\n'
        )
        self.assertTrue(any("token" in i["field"] for i in errs))


class TestPrecheckEnums(unittest.TestCase):

    def test_valid_severity_no_error(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nseverity = ["CRITICAL", "MAJOR"]\n')
        sev_errs = [i for i in errs if "severity" in i["field"]]
        self.assertEqual(sev_errs, [])

    def test_invalid_severity_is_error(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nseverity = ["CRITICAL", "BOGUS"]\n')
        self.assertTrue(any("severity" in i["field"] for i in errs))

    def test_all_valid_severities_accepted(self):
        sevs = '["CLEARED", "CRITICAL", "INDETERMINATE", "MAJOR", "MINOR", "WARNING"]'
        errs = errors_for(MINIMAL_VALID + f'\n[include]\nseverity = {sevs}\n')
        sev_errs = [i for i in errs if "severity" in i["field"]]
        self.assertEqual(sev_errs, [])

    def test_invalid_noise_filter_is_error(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nnoise_filter = ["INTERMITTENT"]\n')
        self.assertTrue(any("noise_filter" in i["field"] for i in errs))

    def test_valid_noise_filter_accepted(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nnoise_filter = ["FREQUENT"]\n')
        nf_errs = [i for i in errs if "noise_filter" in i["field"]]
        self.assertEqual(nf_errs, [])

    def test_invalid_notify_on_is_error(self):
        errs = errors_for('notify_on = ["ALARM_RAISED", "BOGUS"]\n' + MINIMAL_VALID)
        self.assertTrue(any("notify_on" in i["field"] for i in errs))

    def test_invalid_auth_type_is_error(self):
        errs = errors_for(
            '[connector]\nname = "X"\n\n[webhook]\nurl = "https://example.com/hook"\n'
            '[webhook.auth]\ntype = "TOKEN"\n'
        )
        self.assertTrue(any("auth.type" in i["field"] for i in errs))

    def test_invalid_http_method_is_error(self):
        errs = errors_for(
            '[connector]\nname = "X"\n\n[webhook]\nurl = "https://example.com/hook"\nmethod = "CONNECT"\n'
        )
        self.assertTrue(any("method" in i["field"] for i in errs))


class TestPrecheckConnectorId(unittest.TestCase):

    def test_integer_connector_id_valid(self):
        errs = errors_for('[connector]\nname = "X"\nid = 12345\n\n[webhook]\nurl = "https://example.com/hook"\n')
        cid_errs = [i for i in errs if "connector.id" in i["field"]]
        self.assertEqual(cid_errs, [])

    def test_string_connector_id_is_error(self):
        errs = errors_for('[connector]\nname = "X"\nid = "12345"\n\n[webhook]\nurl = "https://example.com/hook"\n')
        self.assertTrue(any("connector.id" in i["field"] for i in errs))


class TestPrecheckHeaders(unittest.TestCase):

    def test_valid_header_no_error(self):
        errs = errors_for(
            MINIMAL_VALID +
            '\n[[webhook.headers]]\nkey = "Content-Type"\nvalue = "application/json"\n'
        )
        hdr_errs = [i for i in errs if "headers" in i["field"]]
        self.assertEqual(hdr_errs, [])

    def test_header_missing_key_is_error(self):
        errs = errors_for(
            MINIMAL_VALID + '\n[[webhook.headers]]\nvalue = "application/json"\n'
        )
        self.assertTrue(any("headers" in i["field"] for i in errs))

    def test_header_missing_value_is_error(self):
        errs = errors_for(
            MINIMAL_VALID + '\n[[webhook.headers]]\nkey = "Content-Type"\n'
        )
        self.assertTrue(any("headers" in i["field"] for i in errs))


class TestPrecheckCRLF(unittest.TestCase):
    """CRLF injection prevention — fields that flow into HTTP headers or URLs.

    TOML escape sequences (\r \n) are used in these tests rather than literal
    CR/LF bytes because TOML single-line basic strings reject literal newlines
    at parse time. The escape sequences produce the actual CR/LF bytes in the
    parsed value, which is what our validation code inspects.
    """

    def test_connector_name_with_crlf_is_error(self):
        # \r\n as TOML escape sequences — parser yields CR+LF in the name value
        errs = errors_for('[connector]\nname = "My\\r\\nConnector"\n[webhook]\nurl = "https://example.com"\n')
        self.assertTrue(any("connector.name" in i["field"] for i in errs))

    def test_connector_name_with_lf_only_is_error(self):
        errs = errors_for('[connector]\nname = "My\\nConnector"\n[webhook]\nurl = "https://example.com"\n')
        self.assertTrue(any("connector.name" in i["field"] for i in errs))

    def test_webhook_url_with_crlf_is_error(self):
        errs = errors_for(
            '[connector]\nname = "Test"\n[webhook]\nurl = "https://example.com/hook\\r\\nX-Injected: evil"\n'
        )
        self.assertTrue(any("webhook.url" in i["field"] for i in errs))

    def test_webhook_url_without_crlf_no_error(self):
        errs = errors_for(MINIMAL_VALID)
        url_errs = [i for i in errs if i["field"] == "webhook.url"]
        self.assertEqual(url_errs, [])

    def test_header_value_with_crlf_is_error(self):
        errs = errors_for(
            MINIMAL_VALID +
            '\n[[webhook.headers]]\nkey = "X-Custom"\nvalue = "val\\r\\nX-Injected: evil"\n'
        )
        self.assertTrue(any("headers" in i["field"] and "value" in i["field"] for i in errs))

    def test_header_value_without_crlf_no_error(self):
        errs = errors_for(
            MINIMAL_VALID + '\n[[webhook.headers]]\nkey = "X-Custom"\nvalue = "safe-value"\n'
        )
        hdr_val_errs = [i for i in errs if "headers" in i["field"] and "value" in i["field"]]
        self.assertEqual(hdr_val_errs, [])

    def test_auth_username_with_crlf_is_error(self):
        errs = errors_for(
            '[connector]\nname = "Test"\n[webhook]\nurl = "https://example.com"\n'
            '[webhook.auth]\ntype = "BASIC"\nusername = "user\\r\\nX-Injected: evil"\n'
        )
        self.assertTrue(any("username" in i["field"] for i in errs))

    def test_auth_username_without_crlf_no_error(self):
        errs = errors_for(
            '[connector]\nname = "Test"\n[webhook]\nurl = "https://example.com"\n'
            '[webhook.auth]\ntype = "BASIC"\nusername = "validuser"\n'
        )
        user_errs = [i for i in errs if "username" in i["field"]]
        self.assertEqual(user_errs, [])


class TestPrecheckGlobWarning(unittest.TestCase):
    """Glob characters in MP names should warn, not error."""

    def test_glob_in_mp_names_is_warning(self):
        path = write_toml(MINIMAL_VALID + '\n[include]\nmonitoring_point_names = ["owncloud-*"]\n')
        try:
            _, issues = gc.precheck_toml(path)
            warns = [i for i in issues if i["level"] == "WARNING" and "monitoring_point_names" in i["field"]]
            self.assertTrue(len(warns) >= 1)
        finally:
            os.unlink(path)

    def test_glob_in_mp_names_is_not_error(self):
        errs = errors_for(MINIMAL_VALID + '\n[include]\nmonitoring_point_names = ["owncloud-*"]\n')
        mp_errs = [i for i in errs if "monitoring_point_names" in i["field"]]
        self.assertEqual(mp_errs, [])

    def test_exact_mp_name_no_warning(self):
        path = write_toml(MINIMAL_VALID + '\n[include]\nmonitoring_point_names = ["branch-office-c50"]\n')
        try:
            _, issues = gc.precheck_toml(path)
            warns = [i for i in issues if i["level"] == "WARNING" and "monitoring_point_names" in i["field"]]
            self.assertEqual(warns, [])
        finally:
            os.unlink(path)


class TestConnectorIdConflict(unittest.TestCase):
    """--connector-id CLI flag must not conflict with connector.id in the TOML."""

    _SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "generate_connector.py")

    def _run(self, toml_content: str, extra_args: list[str]) -> tuple[int, str]:
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(toml_content)
        env = os.environ.copy()
        env.setdefault("APPNETA_API_KEY", "dummy-key-for-test")
        try:
            result = subprocess.run(
                [sys.executable, self._SCRIPT, path] + extra_args,
                capture_output=True, text=True, env=env,
            )
            return result.returncode, result.stderr
        finally:
            os.unlink(path)

    _TOML_WITH_ID = """
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Test"
id = 12345

[webhook]
url = "https://example.com/hook"
"""

    _TOML_NO_ID = """
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Test"

[webhook]
url = "https://example.com/hook"
"""

    def test_conflicting_ids_are_rejected(self):
        rc, stderr = self._run(self._TOML_WITH_ID, ["--connector-id", "99999"])
        self.assertNotEqual(rc, 0)
        self.assertIn("Conflicting connector IDs", stderr)
        self.assertIn("12345", stderr)
        self.assertIn("99999", stderr)

    def test_matching_ids_are_not_rejected(self):
        # Same ID in both TOML and CLI — no conflict
        rc, stderr = self._run(self._TOML_WITH_ID, ["--connector-id", "12345"])
        self.assertNotIn("Conflicting connector IDs", stderr)

    def test_cli_id_with_no_toml_id_is_allowed(self):
        # TOML has no connector.id — --connector-id is fine
        rc, stderr = self._run(self._TOML_NO_ID, ["--connector-id", "12345"])
        self.assertNotIn("Conflicting connector IDs", stderr)

    def test_toml_id_with_no_cli_flag_is_allowed(self):
        # TOML-only connector.id — no issue
        rc, stderr = self._run(self._TOML_WITH_ID, [])
        self.assertNotIn("Conflicting connector IDs", stderr)


class TestMixedFilterBlocking(unittest.TestCase):
    """CLI filter flags + TOML filter blocks cannot be mixed.

    This check runs in main() after the config is loaded, so these tests
    invoke the script via subprocess and inspect the exit code and stderr.
    No API key is needed — the check fires before any API calls.
    """

    _SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "generate_connector.py")

    def _run(self, toml_content: str, extra_args: list[str]) -> tuple[int, str]:
        """Write a temp TOML, run the script, return (exit_code, stderr)."""
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".toml")
        with os.fdopen(fd, "w") as f:
            f.write(toml_content)
        env = os.environ.copy()
        env.setdefault("APPNETA_API_KEY", "dummy-key-for-test")
        try:
            result = subprocess.run(
                [sys.executable, self._SCRIPT, path] + extra_args,
                capture_output=True, text=True, env=env,
            )
            return result.returncode, result.stderr
        finally:
            os.unlink(path)

    _TOML_WITH_INCLUDE = """
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Test"

[include]
severity = ["CRITICAL"]

[webhook]
url = "https://example.com/hook"
"""

    _TOML_NO_FILTERS = """
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Test"

[webhook]
url = "https://example.com/hook"
"""

    def test_include_flag_with_toml_include_block_is_rejected(self):
        rc, stderr = self._run(self._TOML_WITH_INCLUDE, ["--include-tag", "Cloud:GCP"])
        self.assertNotEqual(rc, 0)
        self.assertIn("CLI filter flags cannot be used with a TOML file", stderr)

    def test_exclude_flag_with_toml_include_block_is_rejected(self):
        rc, stderr = self._run(self._TOML_WITH_INCLUDE, ["--exclude-mp-type", "m50"])
        self.assertNotEqual(rc, 0)
        self.assertIn("CLI filter flags cannot be used with a TOML file", stderr)

    def test_cli_flag_with_no_toml_filters_is_rejected(self):
        # Any TOML file puts the run in TOML mode — CLI filter flags are blocked
        # even when the TOML has no [include]/[exclude] sections.  Add filters to
        # the TOML; they cannot be passed as flags alongside a TOML file.
        rc, stderr = self._run(self._TOML_NO_FILTERS, ["--include-tag", "Cloud:GCP"])
        self.assertNotEqual(rc, 0)
        self.assertIn("CLI filter flags cannot be used with a TOML file", stderr)

    def test_toml_with_filters_and_no_cli_flags_is_allowed(self):
        # TOML-only — no issue
        rc, stderr = self._run(self._TOML_WITH_INCLUDE, [])
        self.assertNotIn("Cannot mix", stderr)

    def test_error_message_names_the_offending_flags(self):
        rc, stderr = self._run(self._TOML_WITH_INCLUDE, ["--include-mp-names", "ACME-*"])
        self.assertIn("--include-mp-names", stderr)

    def test_error_message_names_the_toml_blocks(self):
        rc, stderr = self._run(self._TOML_WITH_INCLUDE, ["--include-tag", "Location"])
        self.assertIn("[include]", stderr)


class TestPrecheckNumericIdFields(unittest.TestCase):
    """monitoring_point_ids / monitoring_policy_ids / monitoring_policy_group_ids —
    dedicated numeric-only ID fields (mirrors alarm_rule_ids validation)."""

    _FIELDS = ("monitoring_point_ids", "monitoring_policy_ids", "monitoring_policy_group_ids")

    def test_list_of_positive_ints_is_valid(self):
        for field in self._FIELDS:
            with self.subTest(field=field):
                toml = f"""
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
{field} = [2199023285464, 2199023289104]
"""
                self.assertEqual(errors_for(toml), [])

    def test_single_bare_integer_is_valid(self):
        for field in self._FIELDS:
            with self.subTest(field=field):
                toml = f"""
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
{field} = 2199023285464
"""
                self.assertEqual(errors_for(toml), [])

    def test_string_value_is_rejected(self):
        for field in self._FIELDS:
            with self.subTest(field=field):
                toml = f"""
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
{field} = ["sv-trident"]
"""
                errors = errors_for(toml)
                self.assertEqual(len(errors), 1)
                self.assertIn(field, errors[0]["field"])

    def test_quoted_numeric_string_is_rejected(self):
        # TOML distinguishes "123" (string) from 123 (integer) -- the point of
        # this field is to be unambiguous, so a quoted numeral is still rejected
        # rather than silently coerced.
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
monitoring_point_ids = ["2199023285464"]
"""
        errors = errors_for(toml)
        self.assertEqual(len(errors), 1)

    def test_zero_is_rejected(self):
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
monitoring_point_ids = [0]
"""
        errors = errors_for(toml)
        self.assertEqual(len(errors), 1)
        self.assertIn("positive integers", errors[0]["message"])

    def test_negative_is_rejected(self):
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
monitoring_point_ids = [-5]
"""
        errors = errors_for(toml)
        self.assertEqual(len(errors), 1)
        self.assertIn("positive integers", errors[0]["message"])

    def test_mixed_valid_and_invalid_reports_the_invalid_ones(self):
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
monitoring_point_ids = [2199023285464, "bad", -1]
"""
        errors = errors_for(toml)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad", errors[0]["message"])

    def test_works_on_exclude_block_too(self):
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[exclude]
monitoring_point_ids = [2199023285464]
"""
        self.assertEqual(errors_for(toml), [])

    def test_dict_value_is_rejected(self):
        toml = """
[connector]
name = "Test Connector"
[webhook]
url = "https://example.com/hook"
[include]
monitoring_point_ids = { foo = "bar" }
"""
        errors = errors_for(toml)
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
