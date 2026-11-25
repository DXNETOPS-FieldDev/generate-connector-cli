"""Tests for _remove_contradictions, merge_with_existing, and diff_connector."""
import os
import sys
import unittest
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate_connector as gc


class TestRemoveContradictions(unittest.TestCase):

    def test_no_overlap_leaves_both_blocks_unchanged(self):
        inc = {"severity": ["CRITICAL"], "monitoringPointId": [100]}
        exc = {"severity": ["MINOR"],    "monitoringPointId": [200]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        self.assertEqual(inc["severity"], ["CRITICAL"])
        self.assertEqual(exc["severity"], ["MINOR"])
        self.assertEqual(errors, [])

    def test_severity_contradiction_exclude_wins(self):
        inc = {"severity": ["CRITICAL", "MAJOR"]}
        exc = {"severity": ["MAJOR", "MINOR"]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        # MAJOR removed from include, kept in exclude
        self.assertNotIn("MAJOR", inc["severity"])
        self.assertIn("MAJOR", exc["severity"])
        self.assertIn("CRITICAL", inc["severity"])
        self.assertIn("MINOR", exc["severity"])

    def test_contradiction_appends_error_record(self):
        inc = {"severity": ["CRITICAL", "MAJOR"]}
        exc = {"severity": ["MAJOR"]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "MyOrg")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "contradiction")
        self.assertEqual(errors[0]["org"], "MyOrg")
        self.assertIn("kept in exclude", errors[0]["note"])

    def test_all_include_values_contradicted_removes_include_field(self):
        inc = {"severity": ["CRITICAL"]}
        exc = {"severity": ["CRITICAL"]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        # Include field deleted (empty after removal); exclude field preserved
        self.assertNotIn("severity", inc)
        self.assertIn("CRITICAL", exc["severity"])

    def test_mp_id_contradiction_exclude_wins(self):
        inc = {"monitoringPointId": [100, 101]}
        exc = {"monitoringPointId": [101, 102]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        self.assertNotIn(101, inc.get("monitoringPointId", []))
        self.assertIn(101, exc["monitoringPointId"])
        self.assertIn(100, inc["monitoringPointId"])
        self.assertIn(102, exc["monitoringPointId"])

    def test_policy_group_id_contradiction_exclude_wins(self):
        inc = {"monitoringPolicyGroupId": [10, 20]}
        exc = {"monitoringPolicyGroupId": [20, 30]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        self.assertNotIn(20, inc.get("monitoringPolicyGroupId", []))
        self.assertIn(20, exc["monitoringPolicyGroupId"])

    def test_tag_equal_count_is_unresolvable_conflict(self):
        # Both sides have one value each (equal count) — cannot determine list vs exception.
        # Reported as "conflict" (not "contradiction"); value removed from include as safe fallback.
        inc = {"tag": [{"category": "Region", "value": "West"}]}
        exc = {"tag": [{"category": "Region", "value": "West"}]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        self.assertNotIn("tag", inc)
        self.assertEqual(exc["tag"], [{"category": "Region", "value": "West"}])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "conflict")

    def test_tag_case1_include_is_list_exclude_wins(self):
        # Include has multiple values for the key (the list); exclude has one (the exception).
        # Exception removed from include (list), kept in exclude.
        inc = {"tag": [
            {"category": "Cat", "value": "A"},
            {"category": "Cat", "value": "B"},
            {"category": "Cat", "value": "C"},
        ]}
        exc = {"tag": [{"category": "Cat", "value": "B"}]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        inc_values = [t["value"] for t in inc.get("tag", [])]
        self.assertNotIn("B", inc_values)
        self.assertIn("A", inc_values)
        self.assertIn("C", inc_values)
        self.assertEqual(exc["tag"], [{"category": "Cat", "value": "B"}])
        self.assertEqual(len(errors), 1)

    def test_tag_case2_exclude_is_list_include_wins(self):
        # Exclude has multiple values for the key (the list); include has one (the exception).
        # Exception removed from exclude (list), kept in include.
        inc = {"tag": [{"category": "Cat", "value": "B"}]}
        exc = {"tag": [
            {"category": "Cat", "value": "A"},
            {"category": "Cat", "value": "B"},
            {"category": "Cat", "value": "C"},
        ]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        exc_values = [t["value"] for t in exc.get("tag", [])]
        self.assertNotIn("B", exc_values)
        self.assertIn("A", exc_values)
        self.assertIn("C", exc_values)
        self.assertEqual(inc["tag"], [{"category": "Cat", "value": "B"}])
        self.assertEqual(len(errors), 1)

    def test_empty_blocks_do_not_raise(self):
        gc._remove_contradictions({}, {}, [], "TestOrg")

    def test_noise_filter_contradiction_exclude_wins(self):
        inc = {"noiseFilter": ["FREQUENT"]}
        exc = {"noiseFilter": ["FREQUENT"]}
        errors = []
        gc._remove_contradictions(inc, exc, errors, "TestOrg")
        self.assertNotIn("noiseFilter", inc)
        self.assertIn("FREQUENT", exc["noiseFilter"])


class TestMergeWithExisting(unittest.TestCase):

    EXISTING: ClassVar[dict] = {
        "name": "Old Name",
        "enabled": True,
        "description": "Old description",
        "include": {
            "severity": ["CRITICAL"],
            "monitoringPointId": [100, 101],
        },
        "exclude": {
            "noiseFilter": ["FREQUENT"],
        },
        "notifyOn": ["ALARM_RAISED"],
        "connectors": [{"type": "WEBHOOK", "url": "https://old.example.com"}],
    }

    def test_payload_name_overrides_existing(self):
        merged = gc.merge_with_existing({"name": "New Name"}, self.EXISTING)
        self.assertEqual(merged["name"], "New Name")

    def test_payload_enabled_overrides_existing(self):
        merged = gc.merge_with_existing({"name": "X", "enabled": False}, self.EXISTING)
        self.assertEqual(merged["enabled"], False)

    def test_existing_description_preserved_when_absent_from_payload(self):
        merged = gc.merge_with_existing({"name": "X"}, self.EXISTING)
        self.assertEqual(merged["description"], "Old description")

    def test_payload_description_overrides_existing(self):
        merged = gc.merge_with_existing({"name": "X", "description": "New desc"}, self.EXISTING)
        self.assertEqual(merged["description"], "New desc")

    def test_include_block_merged(self):
        payload = {"name": "X", "include": {"severity": ["MAJOR"]}}
        merged = gc.merge_with_existing(payload, self.EXISTING)
        # payload severity overrides existing severity
        self.assertEqual(merged["include"]["severity"], ["MAJOR"])
        # existing monitoringPointId preserved since payload did not specify it
        self.assertEqual(merged["include"]["monitoringPointId"], [100, 101])

    def test_exclude_block_merged(self):
        payload = {"name": "X", "exclude": {"severity": ["MINOR"]}}
        merged = gc.merge_with_existing(payload, self.EXISTING)
        self.assertEqual(merged["exclude"]["severity"], ["MINOR"])
        # existing noiseFilter preserved
        self.assertEqual(merged["exclude"]["noiseFilter"], ["FREQUENT"])

    def test_payload_without_include_preserves_existing_include(self):
        payload = {"name": "X"}
        merged = gc.merge_with_existing(payload, self.EXISTING)
        self.assertEqual(merged["include"]["severity"], ["CRITICAL"])

    def test_original_payload_not_mutated(self):
        payload = {"name": "X", "include": {"severity": ["MAJOR"]}}
        original_payload = dict(payload)
        gc.merge_with_existing(payload, self.EXISTING)
        self.assertEqual(payload["name"], original_payload["name"])

    def test_subconnector_id_preserved_by_index(self):
        # AppNeta rejects a PUT whose connectors[n] entries are missing 'id'
        # -- merge must carry it over from the
        # existing connector, matched by list index.
        existing = {**self.EXISTING, "connectors": [{"id": 88001, "type": "WEBHOOK", "url": "https://old.example.com"}]}
        payload = {"name": "X", "connectors": [{"type": "WEBHOOK", "url": "https://new.example.com"}]}
        merged = gc.merge_with_existing(payload, existing)
        self.assertEqual(merged["connectors"][0]["id"], 88001)
        self.assertEqual(merged["connectors"][0]["url"], "https://new.example.com")

    def test_subconnector_explicit_id_in_payload_overrides_existing(self):
        existing = {**self.EXISTING, "connectors": [{"id": 88001, "type": "WEBHOOK", "url": "https://old.example.com"}]}
        payload = {"name": "X", "connectors": [{"id": 99999, "type": "WEBHOOK", "url": "https://new.example.com"}]}
        merged = gc.merge_with_existing(payload, existing)
        self.assertEqual(merged["connectors"][0]["id"], 99999)


class TestDiffConnector(unittest.TestCase):

    def test_identical_payloads_produce_no_diff(self):
        p = {"name": "Test", "enabled": True, "include": {"severity": ["CRITICAL"]}}
        self.assertEqual(gc.diff_connector(p, p), [])

    def test_name_change_detected(self):
        current  = {"name": "Old"}
        proposed = {"name": "New"}
        lines = gc.diff_connector(current, proposed)
        self.assertTrue(any("name" in ln for ln in lines))

    def test_enabled_change_detected(self):
        current  = {"name": "X", "enabled": True}
        proposed = {"name": "X", "enabled": False}
        lines = gc.diff_connector(current, proposed)
        self.assertTrue(any("enabled" in ln for ln in lines))

    def test_new_description_marked_as_added(self):
        current  = {"name": "X"}
        proposed = {"name": "X", "description": "New desc"}
        lines = gc.diff_connector(current, proposed)
        self.assertTrue(any("+" in ln for ln in lines))

    def test_removed_field_marked_as_removed(self):
        current  = {"name": "X", "description": "Old desc"}
        proposed = {"name": "X"}
        lines = gc.diff_connector(current, proposed)
        self.assertTrue(any("-" in ln for ln in lines))

    def test_severity_change_in_include_detected(self):
        current  = {"name": "X", "include": {"severity": ["CRITICAL"]}}
        proposed = {"name": "X", "include": {"severity": ["MAJOR"]}}
        lines = gc.diff_connector(current, proposed)
        self.assertTrue(any("severity" in ln for ln in lines))

    def test_no_diff_when_include_unchanged(self):
        p = {"name": "X", "include": {"severity": ["CRITICAL"]}, "notifyOn": ["ALARM_RAISED"]}
        self.assertEqual(gc.diff_connector(p, p), [])


class TestMergeContradictionsAndSsl(unittest.TestCase):

    EXISTING: ClassVar[dict] = {"name": "Old", "include": {"severity": ["CRITICAL"]}, "exclude": {},
                "connectors": [{"id": 9, "type": "WEBHOOK", "url": "https://old.example.com"}]}

    def test_contradiction_created_by_merge_is_resolved_exclude_wins(self):
        payload = {"name": "X", "exclude": {"severity": ["CRITICAL"]},
                   "connectors": [{"type": "WEBHOOK", "url": "https://n.example.com"}]}
        errors = []
        merged = gc.merge_with_existing(payload, self.EXISTING, errors=errors, org_name="Acme")
        self.assertNotIn("severity", merged.get("include", {}))
        self.assertEqual(merged["exclude"]["severity"], ["CRITICAL"])
        self.assertTrue(any(e.get("type") == "contradiction" for e in errors))

    def test_explicit_ignore_ssl_false_overrides_existing_true(self):
        existing = {**self.EXISTING, "connectors": [{"id": 9, "type": "WEBHOOK", "url": "https://o", "ignoreSSLErrors": True}]}
        payload = {"name": "X", "connectors": [{"type": "WEBHOOK", "url": "https://n", "ignoreSSLErrors": False}]}
        self.assertIs(gc.merge_with_existing(payload, existing)["connectors"][0]["ignoreSSLErrors"], False)

    def test_toml_ignore_ssl_errors_false_is_emitted_explicitly(self):
        from unittest.mock import MagicMock
        for flag, expected in ((False, False), (True, True)):
            cfg = {"connector": {"name": "N"}, "webhook": {"url": "https://h.example.com", "ignore_ssl_errors": flag}}
            client = MagicMock()
            client.org_name = "Acme"
            payload = gc.build_payload(cfg, MagicMock(), client, [])
            self.assertIs(payload["connectors"][0]["ignoreSSLErrors"], expected)

    def test_ignore_ssl_errors_is_omitted_when_not_configured(self):
        from unittest.mock import MagicMock
        cfg = {"connector": {"name": "N"}, "webhook": {"url": "https://h.example.com"}}
        client = MagicMock()
        client.org_name = "Acme"
        payload = gc.build_payload(cfg, MagicMock(), client, [])
        self.assertNotIn("ignoreSSLErrors", payload["connectors"][0])


if __name__ == "__main__":
    unittest.main()
