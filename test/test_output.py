"""Output tests for build_filter — exercises the full resolution pipeline
with a mock client.  Each test asserts on the JSON structure that would
appear in the generated connector payload.

These tests cover both the green path (correct config → correct JSON) and
the common customer mistakes we've seen in real connectors: wrong names, type
strings that aren't deployed, bad tag categories/values, and glob edge cases.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import generate_connector as gc
from fixtures import MONITORING_POINTS, POLICIES, POLICY_GROUPS, TAGS


# ---------------------------------------------------------------------------
# MockClient — same interface as AppNetaClient, backed by static fixtures
# ---------------------------------------------------------------------------

class MockClient:
    org_id   = 99
    org_name = "TestOrg"

    def list_monitoring_points(self):
        return MONITORING_POINTS

    def list_monitoring_policies(self):
        return POLICIES

    def list_monitoring_policy_groups(self):
        return POLICY_GROUPS

    def list_tags(self):
        return TAGS

    def group_ids_for_names(self, names, errors):
        lower_to_orig = {n.strip().lower(): n.strip() for n in names}
        available = sorted(g["name"] for g in POLICY_GROUPS if g.get("name"))
        name_to_id = {}
        for grp in POLICY_GROUPS:
            name = grp.get("name", "").strip()
            gid  = grp.get("id")
            if name and gid and name.lower() in lower_to_orig:
                name_to_id[name.lower()] = int(gid)
        for lower in set(lower_to_orig) - name_to_id.keys():
            errors.append({
                "org": self.org_name, "type": "policy group",
                "value": lower_to_orig[lower], "available": available,
            })
        return sorted(name_to_id.values())


CLIENT = MockClient()


def _build(section, *, mp_names=(), mp_types=(), policies=(), groups=(), tags=()):
    """Call build_filter with no CLI overrides by default; returns (output, errors)."""
    errors = []
    out = gc.build_filter(
        section,
        list(mp_names),
        list(mp_types),
        list(policies),
        list(groups),
        list(tags),
        CLIENT,
        errors,
    )
    return out, errors


# ---------------------------------------------------------------------------
# Correct output — green path
# ---------------------------------------------------------------------------

class TestFilterOutputCorrect(unittest.TestCase):

    def test_empty_section_produces_empty_dict(self):
        out, errors = _build({})
        self.assertEqual(out, {})
        self.assertEqual(errors, [])

    def test_severity_present_in_output(self):
        out, errors = _build({"severity": ["CRITICAL", "MAJOR"]})
        self.assertEqual(out["severity"], ["CRITICAL", "MAJOR"])
        self.assertEqual(errors, [])

    def test_severity_upcased(self):
        out, _errs = _build({"severity": ["critical"]})
        self.assertEqual(out["severity"], ["CRITICAL"])

    def test_mp_name_resolves_to_id(self):
        out, errors = _build({"monitoring_point_names": ["ACME-MP-MesaFlats"]})
        self.assertIn("monitoringPointId", out)
        self.assertIn(100, out["monitoringPointId"])
        self.assertEqual(errors, [])

    def test_multiple_mp_names_resolve(self):
        out, errors = _build({"monitoring_point_names": ["ACME-MP-MesaFlats", "ACME-MP-CanyonRoad"]})
        self.assertIn(100, out["monitoringPointId"])
        self.assertIn(101, out["monitoringPointId"])
        self.assertEqual(errors, [])

    def test_mp_id_passthrough(self):
        # Numeric ID supplied directly — passed through without lookup
        out, errors = _build({"monitoring_point_names": ["100"]})
        self.assertIn(100, out["monitoringPointId"])
        self.assertEqual(errors, [])

    def test_mp_type_resolves(self):
        out, errors = _build({"monitoring_point_types": ["m50"]})
        self.assertIn("monitoringPointType", out)
        self.assertIn("m50 microAppliance", out["monitoringPointType"])
        self.assertEqual(errors, [])

    def test_mp_type_c50_resolves(self):
        out, errors = _build({"monitoring_point_types": ["c50"]})
        self.assertIn("c50 Container", out["monitoringPointType"])
        self.assertEqual(errors, [])

    def test_mp_type_n10_alias_expands(self):
        # n10 alias expands to API filter strings "Windows" and "macOS" — not the
        # full deployed type names like "Windows PathView Agent".  The alias path
        # is a direct expansion, not a substring match against live MPs.
        out, errors = _build({"monitoring_point_types": ["n10"]})
        self.assertIn("Windows", out["monitoringPointType"])
        self.assertIn("macOS", out["monitoringPointType"])
        self.assertEqual(errors, [])

    def test_policy_resolves(self):
        out, errors = _build({"monitoring_policy_names": ["Southwest Desert"]})
        self.assertIn("monitoringPolicyId", out)
        self.assertIn(200, out["monitoringPolicyId"])
        self.assertEqual(errors, [])

    def test_policy_group_resolves(self):
        out, errors = _build({"monitoring_policy_group_names": ["Southwest Desert"]})
        self.assertIn("monitoringPolicyGroupId", out)
        self.assertIn(301, out["monitoringPolicyGroupId"])
        self.assertEqual(errors, [])

    def test_tag_exact_match_in_output(self):
        out, errors = _build({}, tags=["Cloud:GCP"])
        self.assertIn("tag", out)
        self.assertIn({"category": "Cloud", "value": "GCP"}, out["tag"])
        self.assertEqual(errors, [])

    def test_tag_case_insensitive_match(self):
        out, errors = _build({}, tags=["cloud:gcp"])
        self.assertIn("tag", out)
        # Canonical form from the catalog — not lowercased
        self.assertIn({"category": "Cloud", "value": "GCP"}, out["tag"])
        self.assertEqual(errors, [])

    def test_alarm_rule_ids_passthrough(self):
        out, _errors = _build({"alarm_rule_ids": [105, 210]})
        self.assertIn("alarmRuleId", out)
        self.assertIn(105, out["alarmRuleId"])
        self.assertIn(210, out["alarmRuleId"])

    def test_targets_passthrough(self):
        out, _errors = _build({"targets": ["example.com", "api.acme.corp"]})
        self.assertIn("target", out)
        self.assertIn("example.com", out["target"])

    def test_noise_filter_upcased(self):
        out, errors = _build({"noise_filter": ["FREQUENT"]})
        self.assertIn("noiseFilter", out)
        self.assertIn("FREQUENT", out["noiseFilter"])
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Glob passthrough
# ---------------------------------------------------------------------------

class TestGlobPassthrough(unittest.TestCase):

    def test_star_glob_goes_to_mp_name_not_id(self):
        out, errors = _build({"monitoring_point_names": ["ACME-*"]})
        self.assertIn("monitoringPointName", out)
        self.assertIn("ACME-*", out["monitoringPointName"])
        self.assertNotIn("monitoringPointId", out)
        self.assertEqual(errors, [])

    def test_negation_glob_passthrough(self):
        # ! prefix — API accepts it as negation; script must pass through unchanged
        out, errors = _build({"monitoring_point_names": ["!owncloud*"]})
        self.assertIn("monitoringPointName", out)
        self.assertIn("!owncloud*", out["monitoringPointName"])
        self.assertNotIn("monitoringPointId", out)
        self.assertEqual(errors, [])

    def test_double_star_glob_passthrough(self):
        out, _errors = _build({"monitoring_point_names": ["ACME-**"]})
        self.assertIn("monitoringPointName", out)
        self.assertIn("ACME-**", out["monitoringPointName"])

    def test_question_mark_glob_passthrough(self):
        out, _errors = _build({"monitoring_point_names": ["ACME-MP-?"]})
        self.assertIn("monitoringPointName", out)

    def test_plus_glob_passthrough(self):
        out, _errors = _build({"monitoring_point_names": ["ACME-MP-+"]})
        self.assertIn("monitoringPointName", out)

    def test_mixed_plain_and_glob_produces_both_keys(self):
        # Plain name → monitoringPointId; glob → monitoringPointName
        out, errors = _build({"monitoring_point_names": ["ACME-MP-MesaFlats", "ACME-*"]})
        self.assertIn("monitoringPointId", out)
        self.assertIn(100, out["monitoringPointId"])
        self.assertIn("monitoringPointName", out)
        self.assertIn("ACME-*", out["monitoringPointName"])
        self.assertEqual(errors, [])

    def test_mp_type_glob_passthrough(self):
        out, errors = _build({"monitoring_point_types": ["m50*"]})
        self.assertIn("monitoringPointType", out)
        self.assertIn("m50*", out["monitoringPointType"])
        self.assertEqual(errors, [])

    def test_glob_via_cli_override(self):
        out, _errors = _build({}, mp_names=["!owncloud*"])
        self.assertIn("monitoringPointName", out)
        self.assertIn("!owncloud*", out["monitoringPointName"])


# ---------------------------------------------------------------------------
# Tag resolution — category-only, value-only, expansion behavior
# ---------------------------------------------------------------------------

class TestTagResolution(unittest.TestCase):

    def test_category_only_expands_to_all_values(self):
        # --include-tag "Cloud" → Clouds:GCP AND Cloud:AWS
        out, errors = _build({}, tags=["Cloud"])
        self.assertIn("tag", out)
        values = {(t["category"], t["value"]) for t in out["tag"]}
        self.assertIn(("Cloud", "GCP"), values)
        self.assertIn(("Cloud", "AWS"), values)
        self.assertEqual(errors, [])

    def test_category_only_location_expands_all(self):
        out, errors = _build({}, tags=["Location"])
        self.assertIn("tag", out)
        values = {t["value"] for t in out["tag"]}
        self.assertIn("Bremerton WA", values)
        self.assertIn("Seattle WA", values)
        self.assertIn("Los Angeles", values)
        self.assertEqual(errors, [])

    def test_value_only_matches_across_categories(self):
        # Fixture only has Clouds:GCP — ":GCP" should match it
        out, errors = _build({}, tags=[":GCP"])
        self.assertIn("tag", out)
        self.assertIn({"category": "Cloud", "value": "GCP"}, out["tag"])
        self.assertEqual(errors, [])

    def test_value_only_exact_full_string_required(self):
        # ":Bremerton" does NOT match "Bremerton WA" — value match is exact
        out, errors = _build({}, tags=[":Bremerton"])
        self.assertNotIn("tag", out)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "tag value")

    def test_value_only_exact_with_space_matches(self):
        # ":Bremerton WA" matches the full catalog value
        out, errors = _build({}, tags=[":Bremerton WA"])
        self.assertIn("tag", out)
        self.assertIn({"category": "Location", "value": "Bremerton WA"}, out["tag"])
        self.assertEqual(errors, [])

    def test_unknown_category_produces_error(self):
        out, errors = _build({}, tags=["Locaiton"])  # typo
        self.assertNotIn("tag", out)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "tag category")
        self.assertIn("Location", errors[0]["available"])  # suggests correct name

    def test_right_category_wrong_value_produces_error(self):
        out, errors = _build({}, tags=["Cloud:Azure"])
        self.assertNotIn("tag", out)
        self.assertEqual(len(errors), 1)
        self.assertIn("GCP", errors[0]["available"])
        self.assertIn("AWS", errors[0]["available"])

    def test_config_tag_exact_match(self):
        section = {"tags": [{"category": "Cloud", "value": "GCP"}]}
        out, errors = _build(section)
        self.assertIn("tag", out)
        self.assertIn({"category": "Cloud", "value": "GCP"}, out["tag"])
        self.assertEqual(errors, [])

    def test_config_tag_wrong_value_produces_error(self):
        section = {"tags": [{"category": "Cloud", "value": "Azure"}]}
        out, errors = _build(section)
        self.assertNotIn("tag", out)
        self.assertEqual(len(errors), 1)

    def test_no_duplicate_tags_from_combined_config_and_cli(self):
        # Same tag from both config and CLI spec — should appear only once
        section = {"tags": [{"category": "Cloud", "value": "GCP"}]}
        out, _errors = _build(section, tags=["Clouds:GCP"])
        tag_pairs = [(t["category"], t["value"]) for t in out.get("tag", [])]
        self.assertEqual(tag_pairs.count(("Cloud", "GCP")), 1)


# ---------------------------------------------------------------------------
# Customer mistakes — common errors that should produce resolution errors
# ---------------------------------------------------------------------------

class TestCustomerMistakes(unittest.TestCase):

    def test_nonexistent_mp_name_produces_error(self):
        out, errors = _build({"monitoring_point_names": ["lobby-sign-m50"]})
        self.assertNotIn("monitoringPointId", out)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "monitoring point")
        self.assertEqual(errors[0]["value"], "lobby-sign-m50")

    def test_nonexistent_mp_name_lists_available(self):
        # available_names in resolve_to_ids are stored as lowercase keys
        _, errors = _build({"monitoring_point_names": ["lobby-sign-m50"]})
        self.assertIn("available", errors[0])
        self.assertIn("acme-mp-mesaflats", errors[0]["available"])

    def test_nonexistent_mp_type_produces_error(self):
        # m70 microAppliance — not in the fixture, not deployed.  build_filter
        # still sets monitoringPointType but with an empty list; the key is present.
        out, errors = _build({"monitoring_point_types": ["m70 microAppliance"]})
        self.assertEqual(out.get("monitoringPointType"), [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["type"], "MP type")

    def test_nonexistent_mp_type_lists_deployed_types(self):
        _, errors = _build({"monitoring_point_types": ["m70 microAppliance"]})
        self.assertIn("m50 microAppliance", errors[0]["available"])

    def test_nonexistent_policy_produces_error(self):
        out, errors = _build({"monitoring_policy_names": ["Fake Policy"]})
        self.assertNotIn("monitoringPolicyId", out)
        self.assertEqual(len(errors), 1)

    def test_nonexistent_policy_group_produces_error(self):
        out, errors = _build({"monitoring_policy_group_names": ["Fake Group"]})
        self.assertNotIn("monitoringPolicyGroupId", out)
        self.assertEqual(len(errors), 1)
        self.assertIn("Canyon Operations", errors[0]["available"])

    def test_non_numeric_alarm_rule_id_is_skipped(self):
        # Customer puts a string instead of an integer
        out, _ = _build({"alarm_rule_ids": ["abc", "not-a-number"]})
        self.assertNotIn("alarmRuleId", out)

    def test_mixed_valid_and_invalid_alarm_rule_ids(self):
        # Valid IDs pass through; invalid ones are silently dropped
        out, _ = _build({"alarm_rule_ids": [105, "bad"]})
        self.assertIn("alarmRuleId", out)
        self.assertIn(105, out["alarmRuleId"])

    def test_mixed_valid_and_invalid_mp_names(self):
        # One good name, one bad — good one resolved, bad one errors
        out, errors = _build({"monitoring_point_names": ["ACME-MP-MesaFlats", "lobby-sign-m50"]})
        self.assertIn("monitoringPointId", out)
        self.assertIn(100, out["monitoringPointId"])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["value"], "lobby-sign-m50")

    def test_wrong_tag_category_error_lists_categories(self):
        # A tag with an unknown category prefix
        _, errors = _build({}, tags=["Clouds:GCP"])
        self.assertEqual(len(errors), 1)
        available_cats = errors[0]["available"]
        self.assertIn("Cloud", available_cats)
        self.assertIn("Location", available_cats)

    def test_mp_name_case_insensitive_resolves(self):
        # Customer uses wrong case — should still resolve
        out, errors = _build({"monitoring_point_names": ["acme-mp-mesaflats"]})
        self.assertIn(100, out["monitoringPointId"])
        self.assertEqual(errors, [])

    def test_mp_type_case_insensitive_resolves(self):
        out, errors = _build({"monitoring_point_types": ["M50"]})
        self.assertIn("m50 microAppliance", out["monitoringPointType"])
        self.assertEqual(errors, [])

    def test_policy_case_insensitive_resolves(self):
        out, errors = _build({"monitoring_policy_names": ["southwest desert"]})
        self.assertIn(200, out["monitoringPolicyId"])
        self.assertEqual(errors, [])

    def test_multiple_errors_all_captured(self):
        # Two bad names — both should appear in the errors list
        out, errors = _build({"monitoring_point_names": ["bad-mp-one", "bad-mp-two"]})
        self.assertNotIn("monitoringPointId", out)
        self.assertEqual(len(errors), 2)


# ---------------------------------------------------------------------------
# Unfiltered payload detection
# ---------------------------------------------------------------------------

class TestUnfilteredPayload(unittest.TestCase):
    """build_filter returns {} for an empty section — the caller (main()) checks
    for the absence of both include and exclude in the final payload and exits
    non-zero in strict mode.  These tests assert on what build_filter returns
    and verify the structural signal that drives that check."""

    def test_empty_section_produces_empty_dict(self):
        # Already tested above; repeated here to confirm the signal.
        out, errors = _build({})
        self.assertEqual(out, {})
        self.assertEqual(errors, [])

    def test_empty_dict_means_no_include_key_in_payload(self):
        # build_filter produces {} when nothing resolves. main() skips adding
        # 'include' to the payload when the filter block is empty (line 1420).
        out, _ = _build({})
        self.assertNotIn("include", out)
        self.assertNotIn("exclude", out)

    def test_any_resolved_criterion_populates_include(self):
        # As soon as a single criterion is present, the include block is non-empty
        # and main() will add it to the payload — no unfiltered warning fires.
        out, errors = _build({"severity": ["CRITICAL"]})
        self.assertIn("severity", out)
        self.assertEqual(errors, [])

    def test_failed_resolution_still_produces_empty_dict(self):
        # A bad name fails resolution → errors list has an entry → out is still {}
        # In strict mode main() exits on the resolution error before reaching the
        # unfiltered check, so the double-exit does not occur.
        out, errors = _build({"monitoring_point_names": ["lobby-sign-m50"]})
        self.assertNotIn("monitoringPointId", out)
        self.assertEqual(len(errors), 1)
        # The empty output signals catch-all; the resolution error is the earlier
        # exit condition in main() — both signal something wrong with the config.

    def test_glob_only_mp_name_populates_filter(self):
        # Globs pass through — monitoringPointName is set, so include is non-empty
        out, errors = _build({"monitoring_point_names": ["!owncloud*"]})
        self.assertIn("monitoringPointName", out)
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# Precheck — tag field validation (customer mistakes at config parse time)
# ---------------------------------------------------------------------------

# These tests live here rather than test_precheck.py to keep tag-specific
# scenarios co-located with the rest of tag testing.


def _tag_precheck_errors(toml_content: str) -> list[dict]:
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w") as f:
        f.write(toml_content)
    try:
        _, issues = gc.precheck_toml(path)
        return [i for i in issues if i["level"] == "ERROR"]
    finally:
        os.unlink(path)


_MINIMAL = """
[connector]
name = "Test"

[webhook]
url = "https://example.com/hook"
"""


class TestPrecheckTagValidation(unittest.TestCase):

    def test_tag_with_both_fields_no_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[include.tags]]\ncategory = "Cloud"\nvalue = "GCP"\n'
        )
        tag_errs = [e for e in errs if "tags" in e["field"]]
        self.assertEqual(tag_errs, [])

    def test_tag_missing_value_is_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[include.tags]]\ncategory = "Cloud"\n'
        )
        self.assertTrue(any("tags" in e["field"] for e in errs))

    def test_tag_missing_category_is_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[include.tags]]\nvalue = "GCP"\n'
        )
        self.assertTrue(any("tags" in e["field"] for e in errs))

    def test_tag_with_empty_value_is_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[include.tags]]\ncategory = "Cloud"\nvalue = ""\n'
        )
        self.assertTrue(any("tags" in e["field"] for e in errs))

    def test_tag_with_empty_category_is_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[include.tags]]\ncategory = ""\nvalue = "GCP"\n'
        )
        self.assertTrue(any("tags" in e["field"] for e in errs))

    def test_exclude_tag_missing_value_is_error(self):
        errs = _tag_precheck_errors(
            _MINIMAL + '\n[[exclude.tags]]\ncategory = "Environment"\n'
        )
        self.assertTrue(any("tags" in e["field"] for e in errs))


# ---------------------------------------------------------------------------
# Dedicated numeric-only ID fields (monitoring_point_ids, monitoring_policy_ids,
# monitoring_policy_group_ids) -- direct passthrough, no API lookup, merged
# with whatever the *_names fields' numeric passthrough already contributed.
# ---------------------------------------------------------------------------

class TestNumericIdFields(unittest.TestCase):

    def test_monitoring_point_ids_single_value_passthrough(self):
        out, errors = _build({"monitoring_point_ids": 100})
        self.assertEqual(out["monitoringPointId"], [100])
        self.assertEqual(errors, [])

    def test_monitoring_point_ids_list_passthrough(self):
        out, errors = _build({"monitoring_point_ids": [100, 101]})
        self.assertEqual(out["monitoringPointId"], [100, 101])
        self.assertEqual(errors, [])

    def test_monitoring_policy_ids_passthrough(self):
        out, errors = _build({"monitoring_policy_ids": [200, 201]})
        self.assertEqual(out["monitoringPolicyId"], [200, 201])
        self.assertEqual(errors, [])

    def test_monitoring_policy_group_ids_passthrough(self):
        out, errors = _build({"monitoring_policy_group_ids": [300]})
        self.assertEqual(out["monitoringPolicyGroupId"], [300])
        self.assertEqual(errors, [])

    def test_id_not_in_live_catalog_is_accepted_without_error(self):
        # Unlike monitoring_point_names' numeric passthrough (which validates
        # membership via resolve_to_ids and errors on an unknown ID), the
        # dedicated ID fields are a direct passthrough with no API lookup at
        # all -- an ID for a resource that doesn't exist in the fixture data
        # is accepted as-is, no error appended.
        out, errors = _build({"monitoring_point_ids": [999999]})
        self.assertEqual(out["monitoringPointId"], [999999])
        self.assertEqual(errors, [])

    def test_merges_with_names_field_numeric_passthrough(self):
        # monitoring_point_names=["ACME-MP-MesaFlats"] resolves to id 100 via
        # resolve_to_ids; monitoring_point_ids=[999] is a direct passthrough.
        # Both must land in the same monitoringPointId output key, unioned.
        out, errors = _build({
            "monitoring_point_names": ["ACME-MP-MesaFlats"],
            "monitoring_point_ids": [999],
        })
        self.assertEqual(sorted(out["monitoringPointId"]), [100, 999])
        self.assertEqual(errors, [])

    def test_duplicate_id_across_both_mechanisms_is_deduplicated(self):
        out, _errors = _build({
            "monitoring_point_names": ["ACME-MP-MesaFlats"],  # resolves to 100
            "monitoring_point_ids": [100],                     # same id, direct
        })
        self.assertEqual(out["monitoringPointId"], [100])

    def test_absent_field_produces_no_key(self):
        out, _errors = _build({})
        self.assertNotIn("monitoringPointId", out)
        self.assertNotIn("monitoringPolicyId", out)
        self.assertNotIn("monitoringPolicyGroupId", out)

    def test_non_integer_in_list_is_silently_skipped(self):
        # Defensive re-check in build_filter -- precheck should already have
        # rejected this at the TOML level, but build_filter doesn't trust that
        # blindly (mirrors the alarm_rule_ids passthrough's own defensiveness).
        out, _errors = _build({"monitoring_point_ids": [100, "bad", -5, 0]})
        self.assertEqual(out["monitoringPointId"], [100])

    def test_all_three_fields_independent(self):
        out, errors = _build({
            "monitoring_point_ids": [100],
            "monitoring_policy_ids": [200],
            "monitoring_policy_group_ids": [300],
        })
        self.assertEqual(out["monitoringPointId"], [100])
        self.assertEqual(out["monitoringPolicyId"], [200])
        self.assertEqual(out["monitoringPolicyGroupId"], [300])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
