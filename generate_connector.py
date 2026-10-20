#!/usr/bin/env python3
"""
AppNeta Alarm Connector JSON Generator

Reads a TOML config file, calls the AppNeta API to resolve monitoring
point/policy/group names to IDs, then writes the JSON payload suitable for
POST /api/v4/connector/alarm (CREATE) or PUT /api/v4/connector/alarm (UPDATE).

Default mode is POST (CREATE). PUT (UPDATE) mode is only activated when
connector.id is set in the TOML or --connector-id is passed on the CLI.
--connector-id is OPTIONAL — do not set it unless you are updating an
existing connector.

Discovers organizations via GET /api/v4/organization and generates one payload
per organization. API keys scoped to a single organization produce one output
file; keys with access to multiple organizations produce one file per org.

IMPORTANT — API key scope:
  API keys must be scoped to specific organizations. Keys that span several
  unrelated organization hierarchies are not supported — the script will exit
  with an error if one is detected.
  Create a dedicated read-only API key restricted to the target organization(s).

Requires Python 3.11+ (tomllib is stdlib as of 3.11). No external dependencies.

Base URL resolution order (first found wins):
  1. --base-url CLI flag
  2. base_url field in config file
  3. APPNETA_BASE_URL environment variable

Org restriction (optional — filters to a single org after discovery):
  1. --org CLI flag
  2. org field in config file
  If neither is set, all orgs accessible to the API key are processed.

Environment variables:
  APPNETA_API_KEY   - API key (required for name→ID resolution)
  APPNETA_BASE_URL  - Base URL (fallback if not set in config or CLI)

Usage:
  # Write named file (connector_<orgId>_<orgName>.json):
  python generate_connector.py config.toml

  # Write named files with custom prefix:
  python generate_connector.py config.toml -o myconnector
  # → writes myconnector_47291_Acme_Corp.json, etc.

  # Write to stdout:
  python generate_connector.py config.toml --stdout > connector.json

Resolution errors (names/IDs not found) are written to <config-stem>-connector-error.txt.
"""

import sys

if sys.version_info < (3, 11):  # pyright: ignore[reportUnnecessaryComparison]
    found = sys.version.split()[0]  # pyright: ignore[reportUnreachable]
    print("=" * 70, file=sys.stderr)
    print("  Python Version Error", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(f"  Required : Python 3.11 or higher", file=sys.stderr)
    print(f"  Found    : Python {found}", file=sys.stderr)
    print(file=sys.stderr)
    print("  tomllib (built-in TOML support) was added in Python 3.11.", file=sys.stderr)
    print("  Python 3.11 has been available since October 2022.", file=sys.stderr)
    print(file=sys.stderr)
    print("  Upgrade: https://www.python.org/downloads/", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    sys.exit(1)

import argparse
import datetime
import difflib
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


# ---------------------------------------------------------------------------
# API schema constants
# ---------------------------------------------------------------------------

VALID_SEVERITIES    = frozenset({"CLEARED", "CRITICAL", "INDETERMINATE", "MAJOR", "MINOR", "WARNING"})
VALID_NOISE_FILTERS = frozenset({"FREQUENT"})
VALID_NOTIFY_ON     = frozenset({"ALARM_RAISED", "ALARM_CLEARED"})
VALID_METHODS       = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
VALID_AUTH_TYPES    = frozenset({"NONE", "BASIC", "BEARER"})
_GLOB_CHARS         = frozenset("*?!+")

# The n10 is AppNeta's software agent; the API represents it as two separate
# type strings depending on the host OS. "n10" is not a value the API returns.
_MP_TYPE_ALIASES: dict[str, list[str]] = {
    "n10": ["Windows", "macOS"],
}


# ---------------------------------------------------------------------------
# TOML precheck — parse + structural validation, no API calls
# ---------------------------------------------------------------------------

def _has_ctrl(s: str) -> bool:
    """True if s contains null bytes or ASCII control chars other than tab/newline/CR."""
    return any(ord(c) < 0x20 and c not in "\t\n\r" for c in s)


def _has_crlf(s: str) -> bool:
    """True if s contains CR or LF. Use for fields that flow into HTTP headers or URLs."""
    return "\r" in s or "\n" in s


_KNOWN_TOP_LEVEL = frozenset({"base_url", "org", "connector", "include", "exclude", "webhook", "notify_on"})
_KNOWN_CONNECTOR = frozenset({"id", "name", "description", "enabled"})
_KNOWN_FILTER    = frozenset({
    "severity", "noise_filter", "monitoring_point_names", "monitoring_point_types",
    "monitoring_policy_names", "monitoring_policy_group_names",
    "monitoring_point_ids", "monitoring_policy_ids", "monitoring_policy_group_ids",
    "alarm_rule_ids", "targets", "tags",
})

# Dedicated numeric-only ID fields — (toml_key, output_key, singular_label).
# These exist so a filter value's intent (an ID) is explicit at the config
# level, instead of relying on the *_names fields' numeric-string passthrough
# (which still works for backward compatibility, but reads as ambiguous —
# since mixing an ID and a name for the same resource can trigger a
# server-side v3/v4 ID substitution).
_NUMERIC_ID_FIELDS = (
    ("monitoring_point_ids",         "monitoringPointId",       "monitoring_point_ids"),
    ("monitoring_policy_ids",        "monitoringPolicyId",      "monitoring_policy_ids"),
    ("monitoring_policy_group_ids",  "monitoringPolicyGroupId", "monitoring_policy_group_ids"),
)
_KNOWN_WEBHOOK   = frozenset({"url", "method", "ignore_ssl_errors", "headers", "auth"})
_KNOWN_AUTH      = frozenset({"type", "username", "password", "token"})
_KNOWN_HEADER    = frozenset({"key", "name", "value"})
_ENV_VAR_RE      = re.compile(r"\$\{([^}]+)\}")


def _closest_key(key: str, known: frozenset[str]) -> str | None:
    matches = difflib.get_close_matches(key.lower(), [k.lower() for k in known], n=1, cutoff=0.75)
    if matches:
        for k in known:
            if k.lower() == matches[0]:
                return k
    return None


def precheck_toml(path: str) -> tuple[dict | None, list[dict]]:
    """Parse and structurally validate a TOML config file.

    Returns (config_dict_or_None, issues).  issues is a list of dicts with keys:
      level   — "ERROR" or "WARNING"
      field   — dotted TOML key path
      message — what is wrong
      fix     — suggested fix (may be multi-line, separated by '\\n')
    An empty issues list means all checks passed.
    """
    issues: list[dict] = []

    def err(field: str, message: str, fix: str = "") -> None:
        issues.append({"level": "ERROR", "field": field, "message": message, "fix": fix})

    def warn(field: str, message: str, fix: str = "") -> None:
        issues.append({"level": "WARNING", "field": field, "message": message, "fix": fix})

    def _unknown(parent: str, key: str, known: frozenset[str]) -> None:
        hint = ""
        suggestion = _closest_key(key, known)
        if suggestion:
            hint = f"  Did you mean '{suggestion}'?"
        err(f"{parent}.{key}" if parent else key,
            f"Unknown key '{key}' under [{parent}].{hint}" if parent else f"Unknown top-level key '{key}'.{hint}",
            f"Remove or rename '{parent + '.' if parent else ''}{key}'.")

    if not path.endswith(".toml"):
        err("(file)", f"Config file must have a .toml extension, got: {path!r}",
            "Rename the file with a .toml extension.")
        return None, issues

    try:
        with open(path, "rb") as f:
            config = tomllib.load(f)
    except FileNotFoundError:
        err("(file)", f"File not found: {path}",
            "Check the path and try again.")
        return None, issues
    except tomllib.TOMLDecodeError as e:
        err("(syntax)", f"TOML parse error: {e}",
            "Fix the syntax error above, then rerun.")
        return None, issues

    # ── Top-level keys ─────────────────────────────────────────────────────
    for key in config:
        if key not in _KNOWN_TOP_LEVEL:
            _unknown("", key, _KNOWN_TOP_LEVEL)

    # ── org ────────────────────────────────────────────────────────────────
    org = config.get("org")
    if org is not None:
        if isinstance(org, int) and not isinstance(org, bool):
            pass  # single numeric ID
        elif isinstance(org, list):
            non_int = [x for x in org if isinstance(x, bool) or not isinstance(x, int)]
            if non_int:
                err("org",
                    f"org must be a list of integer org IDs, found non-integer value(s): {non_int}",
                    "Fix: org = [4160, 4275]  # integers, no quotes")
        elif isinstance(org, str) and org.strip():
            pass  # name substring (single org only)
        else:
            err("org",
                f"org must be an integer org ID, a list of integer org IDs, or a name substring string. "
                f"Got {type(org).__name__}: {org!r}",
                'Fix: org = 4160              # single org ID\n'
                '     org = [4160, 4275]      # multiple org IDs (must share a root)\n'
                '     org = "Carousel"        # name substring (case-insensitive, single org)')

    # ── base_url ───────────────────────────────────────────────────────────
    base_url = config.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str):
            err("base_url",
                f"base_url must be a string, got {type(base_url).__name__}: {base_url!r}",
                'Fix: base_url = "https://app-01.pm.appneta.com"')
        elif not base_url.startswith("https://"):
            stripped = re.sub(r"^https?://", "", base_url)
            err("base_url",
                f"base_url must use HTTPS. Got: {base_url!r}",
                f'Fix: base_url = "https://{stripped}"')

    # ── notify_on ──────────────────────────────────────────────────────────
    notify_on = config.get("notify_on")
    if notify_on is not None:
        if not isinstance(notify_on, list):
            err("notify_on", "notify_on must be a list of strings.",
                'Fix: notify_on = ["ALARM_RAISED", "ALARM_CLEARED"]')
        else:
            invalid = [n for n in notify_on if not isinstance(n, str) or n.upper() not in VALID_NOTIFY_ON]
            if invalid:
                err("notify_on",
                    f"Invalid notify_on value(s): {invalid}",
                    f"Valid values: {', '.join(sorted(VALID_NOTIFY_ON))}")

    # ── [connector] ────────────────────────────────────────────────────────
    connector = config.get("connector", {})
    if not config.get("connector"):
        err("connector", "[connector] section is required.",
            "[connector]\nname = \"My Connector Name\"")
    elif not isinstance(connector, dict):
        err("connector", "[connector] must be a TOML table.",
            "[connector]\nname = \"My Connector Name\"")
    else:
        for key in connector:
            if key not in _KNOWN_CONNECTOR:
                if key in _KNOWN_TOP_LEVEL:
                    err(f"connector.{key}",
                        f"'{key}' is a top-level key but was read as part of [connector].",
                        f"In TOML, keys after a section header belong to that section.\n"
                        f"Move '{key}' to before the [connector] line:\n"
                        f"  {key} = ...\n"
                        f"  [connector]\n"
                        f"  name = ...")
                else:
                    _unknown("connector", key, _KNOWN_CONNECTOR)
        name = connector.get("name", "")
        if not isinstance(name, str) or not name.strip():
            err("connector.name", "connector.name is required and must be a non-empty string.",
                'Fix: name = "My Connector Name"')
        elif len(name) > 255:
            err("connector.name", "connector.name is too long (max 255 characters).")
        elif _has_crlf(name) or _has_ctrl(name):
            err("connector.name", "connector.name must not contain line breaks or invalid control characters.")
        desc = connector.get("description", "")
        if not isinstance(desc, str):
            err("connector.description", f"connector.description must be a string, got {type(desc).__name__}: {desc!r}",
                'Fix: description = "What this connector is for"')
        elif desc:
            if len(desc) > 2000:
                err("connector.description", "connector.description is too long (max 2000 characters).")
            elif _has_ctrl(desc):
                err("connector.description", "connector.description contains invalid control characters.")
        cid = connector.get("id")
        if cid is not None and (isinstance(cid, bool) or not isinstance(cid, int)):
            err("connector.id",
                f"connector.id must be an integer (int64 connector ID), got {type(cid).__name__}: {cid!r}",
                "Fix: id = 12345678  # integer, no quotes")
        enabled = connector.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            err("connector.enabled",
                f"connector.enabled must be true or false, got {type(enabled).__name__}: {enabled!r}",
                "Fix: enabled = true")

    # ── [webhook] ──────────────────────────────────────────────────────────
    webhook = config.get("webhook")
    if not webhook:
        err("webhook", "[webhook] section is required.",
            "[webhook]\nurl = \"https://your-webhook.example.com\"")
    elif not isinstance(webhook, dict):
        err("webhook", "[webhook] must be a TOML table.",
            "[webhook]\nurl = \"https://your-webhook.example.com\"")
    else:
        for key in webhook:
            if key not in _KNOWN_WEBHOOK:
                _unknown("webhook", key, _KNOWN_WEBHOOK)

        url = webhook.get("url", "")
        if not isinstance(url, str) or not url.strip():
            err("webhook.url", "webhook.url is required and must be a non-empty string.",
                'Fix: url = "https://your-webhook.example.com"')
        elif _has_crlf(url) or _has_ctrl(url):
            err("webhook.url",
                "webhook.url contains invalid control characters — CRLF in a URL would split the HTTP request line.")
        elif not re.match(r"^https?://", url):
            warn("webhook.url", f"webhook.url does not look like an HTTP(S) URL: {url!r}",
                 "Ensure the URL starts with https:// or http://")

        method = webhook.get("method", "POST")
        if not isinstance(method, str) or method.upper() not in VALID_METHODS:
            err("webhook.method",
                f"Invalid webhook.method: {method!r}",
                f"Valid values: {', '.join(sorted(VALID_METHODS))}")

        ignore_ssl = webhook.get("ignore_ssl_errors")
        if ignore_ssl is not None and not isinstance(ignore_ssl, bool):
            err("webhook.ignore_ssl_errors",
                f"webhook.ignore_ssl_errors must be true or false, got: {ignore_ssl!r}",
                "Fix: ignore_ssl_errors = false")

        headers = webhook.get("headers", [])
        if not isinstance(headers, list):
            err("webhook.headers", "webhook.headers must be a TOML array of tables.",
                "Fix: use [[webhook.headers]] blocks")
        else:
            for i, h in enumerate(headers):
                if not isinstance(h, dict):
                    err(f"webhook.headers[{i}]", "Each header must be a TOML table.",
                        "Fix:\n[[webhook.headers]]\nkey   = \"Content-Type\"\nvalue = \"application/json\"")
                    continue
                for key in h:
                    if key not in _KNOWN_HEADER:
                        err(f"webhook.headers[{i}].{key}",
                            f"Unknown key '{key}' in header entry.",
                            "Valid keys: key (or name), value")
                raw_key = h.get("key") or h.get("name") or ""
                if not isinstance(raw_key, str):
                    err(f"webhook.headers[{i}].key",
                        f"Header key must be a string, got {type(raw_key).__name__}: {raw_key!r}",
                        'Fix: key = "Content-Type"')
                    raw_key = ""
                    hkey = "x"
                else:
                    hkey = raw_key.strip()
                if not hkey:
                    err(f"webhook.headers[{i}]", "Header entry is missing a 'key' field.",
                        'Fix: key = "Content-Type"')
                elif any(c in hkey for c in " \t:\r\n") or _has_ctrl(hkey):
                    err(f"webhook.headers[{i}].key",
                        f"Invalid header name {hkey!r} — no whitespace, colons, or control characters.")
                if "value" not in h:
                    err(f"webhook.headers[{i}]", "Header entry is missing a 'value' field.",
                        'Fix: value = "application/json"')
                elif not isinstance(h.get("value"), str):
                    err(f"webhook.headers[{i}].value",
                        f"Header value must be a string, got {type(h['value']).__name__}: {h['value']!r}",
                        'Fix: value = "application/json"')
                else:
                    if _has_crlf(h["value"]) or _has_ctrl(h["value"]):
                        err(f"webhook.headers[{i}].value",
                            "Header value contains invalid control characters "
                            "(CRLF in a header value would split the HTTP response).")
                    for var in _ENV_VAR_RE.findall(h["value"]):
                        if not os.environ.get(var):
                            warn(f"webhook.headers[{i}].value",
                                 f"Header value references env var '{var}' but it is not set.",
                                 f"Export it before running:\n  export {var}=<value>")

        auth = webhook.get("auth", {})
        if auth and not isinstance(auth, dict):
            err("webhook.auth", "webhook.auth must be a TOML table.",
                "[webhook.auth]\ntype = \"NONE\"")
        elif isinstance(auth, dict):
            for key in auth:
                if key not in _KNOWN_AUTH:
                    _unknown("webhook.auth", key, _KNOWN_AUTH)
            auth_type = auth.get("type", "NONE")
            if not isinstance(auth_type, str) or auth_type.upper() not in VALID_AUTH_TYPES:
                err("webhook.auth.type",
                    f"Invalid webhook.auth.type: {auth_type!r}",
                    f"Valid values: {', '.join(sorted(VALID_AUTH_TYPES))}")
            if auth.get("password"):
                err("webhook.auth.password",
                    "Credentials must NOT be stored in the config file.",
                    "Remove 'password' from the config.\n"
                    "Instead: export CUSTOMER_WEBHOOK_PASSWORD=<password>")
            if auth.get("token"):
                err("webhook.auth.token",
                    "Credentials must NOT be stored in the config file.",
                    "Remove 'token' from the config.\n"
                    "Instead: export CUSTOMER_WEBHOOK_TOKEN=<token>")
            username = auth.get("username", "")
            if isinstance(username, str) and username and _has_crlf(username):
                err("webhook.auth.username",
                    "webhook.auth.username contains line breaks — "
                    "CRLF in a Basic auth username would inject headers into AppNeta's webhook calls.")

    # ── [include] / [exclude] ──────────────────────────────────────────────
    for section_name in ("include", "exclude"):
        section = config.get(section_name, {})
        if not isinstance(section, dict):
            err(section_name, f"[{section_name}] must be a TOML table.",
                f"Fix: [{section_name}]\nseverity = [...]")
            continue
        for key in section:
            if key not in _KNOWN_FILTER:
                if key in _KNOWN_TOP_LEVEL:
                    err(f"{section_name}.{key}",
                        f"'{key}' is a top-level key but was read as part of [{section_name}].",
                        f"In TOML, keys after a section header belong to that section.\n"
                        f"Move '{key}' to before the [{section_name}] line:\n"
                        f"  {key} = ...\n"
                        f"  [{section_name}]")
                else:
                    _unknown(section_name, key, _KNOWN_FILTER)

        sev = section.get("severity")
        if sev is not None:
            if not isinstance(sev, list):
                err(f"{section_name}.severity", "severity must be a list of strings.",
                    f'Fix: severity = ["CRITICAL", "MAJOR"]')
            else:
                invalid = [s for s in sev if not isinstance(s, str) or s.upper() not in VALID_SEVERITIES]
                if invalid:
                    err(f"{section_name}.severity",
                        f"Invalid severity value(s): {invalid}",
                        f"Valid values: {', '.join(sorted(VALID_SEVERITIES))}")

        nf = section.get("noise_filter")
        if nf is not None:
            if not isinstance(nf, list):
                err(f"{section_name}.noise_filter", "noise_filter must be a list of strings.",
                    'Fix: noise_filter = ["FREQUENT"]')
            else:
                invalid = [n for n in nf if not isinstance(n, str) or n.upper() not in VALID_NOISE_FILTERS]
                if invalid:
                    err(f"{section_name}.noise_filter",
                        f"Invalid noise_filter value(s): {invalid}",
                        f"Valid values: {', '.join(sorted(VALID_NOISE_FILTERS))}")

        for field in ("monitoring_point_names", "monitoring_point_types",
                      "monitoring_policy_names", "monitoring_policy_group_names", "targets"):
            val = section.get(field)
            if val is not None and not isinstance(val, list):
                err(f"{section_name}.{field}", f"{field} must be a list of strings.",
                    f"Fix: {field} = [\"value1\", \"value2\"]")
            elif val is not None:
                bad = [x for x in val if not isinstance(x, str)]
                if bad:
                    err(f"{section_name}.{field}",
                        f"{field} must contain strings only (quote them). Found: {bad}",
                        f"Fix: {field} = [\"value1\", \"value2\"]")

        mp_names = section.get("monitoring_point_names")
        if isinstance(mp_names, list):
            for mp_name in mp_names:
                if isinstance(mp_name, str) and any(c in mp_name for c in _GLOB_CHARS):
                    warn(f"{section_name}.monitoring_point_names",
                         f"'{mp_name}' contains glob characters (* ? ! +). "
                         "Glob patterns are passed to the AppNeta API as-is via monitoringPointName. "
                         "Exact names and numeric IDs are resolved to monitoringPointId. "
                         "Verify the pattern against your org's MP names.")

        ari = section.get("alarm_rule_ids")
        if ari is not None:
            if not isinstance(ari, list):
                err(f"{section_name}.alarm_rule_ids", "alarm_rule_ids must be a list of integers.",
                    "Fix: alarm_rule_ids = [105, 106]")
            else:
                non_int = [x for x in ari if isinstance(x, bool) or not isinstance(x, int)]
                if non_int:
                    err(f"{section_name}.alarm_rule_ids",
                        f"alarm_rule_ids must contain integers only. Found: {non_int}",
                        "Fix: alarm_rule_ids = [105]  # integers, no quotes")
                else:
                    non_positive = [x for x in ari if x <= 0]
                    if non_positive:
                        err(f"{section_name}.alarm_rule_ids",
                            f"alarm_rule_ids must be positive integers. Found: {non_positive}",
                            "Fix: alarm_rule_ids = [105]  # positive integers only")

        for toml_key, _out_key, _label in _NUMERIC_ID_FIELDS:
            raw = section.get(toml_key)
            if raw is None:
                continue
            # Accept a single bare integer as a convenience, same as a one-item list.
            values = [raw] if isinstance(raw, int) and not isinstance(raw, bool) else raw
            if not isinstance(values, list):
                err(f"{section_name}.{toml_key}",
                    f"{toml_key} must be an integer or a list of integers.",
                    f"Fix: {toml_key} = 2199023290142   # or: {toml_key} = [2199023290142, 2199023289104]")
                continue
            non_int = [x for x in values if isinstance(x, bool) or not isinstance(x, int)]
            if non_int:
                err(f"{section_name}.{toml_key}",
                    f"{toml_key} must contain integers only (no names, no quoted strings). Found: {non_int}",
                    f"Fix: {toml_key} = [2199023290142]  # integers, no quotes")
            else:
                non_positive = [x for x in values if x <= 0]
                if non_positive:
                    err(f"{section_name}.{toml_key}",
                        f"{toml_key} must be positive integers. Found: {non_positive}",
                        f"Fix: {toml_key} = [2199023290142]  # positive integers only")

        tags_list = section.get("tags")
        if tags_list is not None:
            if not isinstance(tags_list, list):
                err(f"{section_name}.tags", "tags must be an array of tables.",
                    f"Fix:\n[[{section_name}.tags]]\ncategory = \"Environment\"\nvalue = \"Production\"")
            else:
                for i, tag in enumerate(tags_list):
                    if not isinstance(tag, dict):
                        err(f"{section_name}.tags[{i}]",
                            "Each tag entry must be a TOML table with category and value.",
                            f"Fix:\n[[{section_name}.tags]]\ncategory = \"...\"\nvalue = \"...\"")
                        continue
                    for tf, ex in (("category", "Environment"), ("value", "Production")):
                        tv = tag.get(tf)
                        if not tv:
                            err(f"{section_name}.tags[{i}]", f"Tag entry is missing '{tf}'.",
                                f'Fix: {tf} = "{ex}"')
                        elif not isinstance(tv, str):
                            err(f"{section_name}.tags[{i}].{tf}",
                                f"Tag {tf} must be a string, got {type(tv).__name__}: {tv!r}",
                                f'Fix: {tf} = "{ex}"')

    # ── OR-logic warning for include block ────────────────────────────────────
    # Structural check only — no API calls. Fires when 2+ include filter fields
    # are non-empty in the TOML, which triggers AppNeta's OR evaluation.
    include_sec = config.get("include", {})
    if isinstance(include_sec, dict):
        _OR_FIELDS = [
            ("monitoring_point_names", "monitoring_point_names"),
            ("monitoring_point_types", "monitoring_point_types"),
            ("monitoring_policy_names", "monitoring_policy_names"),
            ("monitoring_policy_group_names", "monitoring_policy_group_names"),
            ("monitoring_point_ids", "monitoring_point_ids"),
            ("monitoring_policy_ids", "monitoring_policy_ids"),
            ("monitoring_policy_group_ids", "monitoring_policy_group_ids"),
            ("tags", "tags"),
            ("severity", "severity"),
            ("noise_filter", "noise_filter"),
            ("alarm_rule_ids", "alarm_rule_ids"),
            ("targets", "targets"),
        ]
        active = [label for field, label in _OR_FIELDS if include_sec.get(field)]
        if len(active) >= 2:
            warn("include",
                 f"Multiple include filter types are active ({', '.join(active)}). "
                 "AppNeta applies OR logic across filter types — an alarm is forwarded "
                 "if it matches ANY one criterion, not all of them.",
                 "If this is intentional, ignore this warning. "
                 "To require all criteria to match, that is not supported by the API.")

    return config, issues


def print_precheck_report(path: str, issues: list[dict]) -> None:
    """Print a formatted precheck report to stderr."""
    width = 70
    errors   = [i for i in issues if i["level"] == "ERROR"]
    warnings = [i for i in issues if i["level"] == "WARNING"]

    print("=" * width, file=sys.stderr)
    print(f"  TOML Config Check: {path}", file=sys.stderr)
    print("=" * width, file=sys.stderr)

    if not issues:
        print("  All checks passed — config looks good.", file=sys.stderr)
        print("=" * width, file=sys.stderr)
        return

    for issue in issues:
        tag = "[ERROR]  " if issue["level"] == "ERROR" else "[WARNING]"
        print(f"\n{tag} {issue['field']}", file=sys.stderr)
        print(f"         {issue['message']}", file=sys.stderr)
        if issue.get("fix"):
            for line in issue["fix"].split("\n"):
                print(f"         → {line}", file=sys.stderr)

    print(file=sys.stderr)
    print("─" * width, file=sys.stderr)
    parts: list[str] = []
    if errors:
        parts.append(f"{len(errors)} error(s)")
    if warnings:
        parts.append(f"{len(warnings)} warning(s)")
    outcome = "fix errors before running" if errors else "ready to run (review warnings)"
    print(f"  {', '.join(parts)} — {outcome}", file=sys.stderr)
    print("=" * width, file=sys.stderr)


# ---------------------------------------------------------------------------
# Org tree helpers
# ---------------------------------------------------------------------------

def _root_orgs(orgs: list[dict]) -> list[dict]:
    """Return orgs that are top-level within the result set (parentId not present in set)."""
    id_set = {o["id"] for o in orgs}
    return [o for o in orgs if o.get("parentId", 0) not in id_set]


def _find_org_root(org_id: int, org_by_id: dict[int, dict], id_set: set[int]) -> int:
    """Walk parentId chain to find the root org ID for a given org."""
    current = org_id
    visited: set[int] = set()
    while current not in visited:
        visited.add(current)
        org = org_by_id.get(current)
        if org is None:
            break
        pid = org.get("parentId", 0)
        if pid not in id_set:
            break
        current = pid
    return current


def _validate_same_hierarchy(selected: list[dict], all_orgs: list[dict]) -> tuple[bool, str]:
    """Verify all selected orgs share the same root in the full org list."""
    if len(selected) <= 1:
        return True, ""
    id_set = {o["id"] for o in all_orgs}
    org_by_id = {o["id"]: o for o in all_orgs}
    roots = {_find_org_root(o["id"], org_by_id, id_set) for o in selected}
    if len(roots) == 1:
        return True, ""
    root_names = []
    for rid in sorted(roots):
        rorg = org_by_id.get(rid)
        root_names.append(f"{rorg['name']} (id={rid})" if rorg else str(rid))
    return False, (
        f"Specified orgs span {len(roots)} unrelated customer hierarchies: "
        + ", ".join(root_names)
        + "\n  All org IDs must belong to the same customer hierarchy."
    )


def _build_org_tree(orgs: list[dict]) -> str:
    """Render the org list as an indented tree using parentId relationships."""
    id_set = {o["id"] for o in orgs}
    by_parent: dict[int, list[dict]] = {}
    for o in orgs:
        pid = o.get("parentId", 0)
        if pid not in id_set:
            pid = 0
        by_parent.setdefault(pid, []).append(o)

    lines: list[str] = []

    def _render(parent_id: int, indent: str) -> None:
        children = sorted(by_parent.get(parent_id, []), key=lambda o: o["id"])
        for i, org in enumerate(children):
            is_last = i == len(children) - 1
            branch = "└─ " if is_last else "├─ "
            lines.append(f"    {org['id']:>6}  {indent}{branch}{org['name']}")
            _render(org["id"], indent + ("   " if is_last else "│  "))

    for root in sorted(by_parent.get(0, []), key=lambda o: o["id"]):
        lines.append(f"    {root['id']:>6}  {root['name']}")
        _render(root["id"], "")

    return "\n".join(lines)


def _exit_multi_hierarchy_key(orgs: list[dict], roots: list[dict]) -> None:
    """Print a clear error message and exit when the key spans multiple unrelated hierarchies.

    This tool requires an org-scoped key.
    """
    tree = _build_org_tree(orgs)
    width = 70
    print("=" * width, file=sys.stderr)
    print("  Superadmin API Key Detected — Not Supported", file=sys.stderr)
    print("=" * width, file=sys.stderr)
    print(file=sys.stderr)
    print("  This tool does not support superadmin (account-wide) API keys.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  The key provided has access to {len(orgs)} organizations across", file=sys.stderr)
    print(f"  {len(roots)} unrelated customer hierarchies, which indicates an", file=sys.stderr)
    print("  account-wide key.", file=sys.stderr)
    print(file=sys.stderr)
    print("  Create a read-only API key scoped to the specific organization(s)", file=sys.stderr)
    print("  you want to configure:", file=sys.stderr)
    print(file=sys.stderr)
    print("    AppNeta TechDocs — API Access Tokens:", file=sys.stderr)
    print("    https://techdocs.broadcom.com/us/en/ca-enterprise-software/", file=sys.stderr)
    print("    it-operations-management/appneta/GA/integrate/api/", file=sys.stderr)
    print("    api-access-tokens.html", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Organizations visible to this key ({len(orgs)}):", file=sys.stderr)
    print(tree, file=sys.stderr)
    print(file=sys.stderr)
    print("=" * width, file=sys.stderr)
    sys.exit(1)


def _resolve_orgs(all_orgs: list[dict], specs: list) -> tuple[list[dict], list[str]]:
    """Resolve a list of org specs (int ID, numeric string, or name substring) to org objects.

    Each spec is resolved independently:
      - Integer or numeric string → exact org ID match
      - Non-numeric string → case-insensitive substring match on name (must be unambiguous)

    Returns (matched_orgs, error_messages). Duplicates are silently collapsed.
    """
    seen_ids: set[int] = set()
    matched: list[dict] = []
    errors: list[str] = []

    for spec in specs:
        spec_s = str(spec).strip()
        if isinstance(spec, int) or spec_s.lstrip("-").isdigit():
            target_id = int(spec_s)
            org = next((o for o in all_orgs if o["id"] == target_id), None)
            if org is None:
                errors.append(f"Org ID {target_id} not found in API response")
            elif target_id not in seen_ids:
                seen_ids.add(target_id)
                matched.append(org)
        else:
            needle = spec_s.lower()
            name_matches = [o for o in all_orgs if needle in o.get("name", "").lower()]
            if not name_matches:
                errors.append(f"No org matched name substring {spec_s!r}")
            elif len(name_matches) > 1:
                names = ", ".join(f"{o['name']} (id={o['id']})" for o in name_matches)
                errors.append(
                    f"Name substring {spec_s!r} is ambiguous — matched {len(name_matches)} orgs: {names}"
                )
            else:
                oid = name_matches[0]["id"]
                if oid not in seen_ids:
                    seen_ids.add(oid)
                    matched.append(name_matches[0])

    return matched, errors


# ---------------------------------------------------------------------------
# Config file loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    if not path.endswith(".toml"):
        sys.exit(f"ERROR: Config file must be a TOML file (.toml), got: {path!r}")
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# API client (stdlib urllib only) — one instance per organization
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: following one would forward the Authorization header (the
    API key) to whatever host the response names. AppNeta's API never redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urllib.request.install_opener(urllib.request.build_opener(_NoRedirect))


class AppNetaClient:
    def __init__(self, base_url: str, api_key: str, org_id: int, org_name: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.org_id = org_id
        self.org_name = org_name
        self._cache: dict = {}

    @classmethod
    def list_organizations(cls, base_url: str, api_key: str) -> list[dict]:
        """Discover all organizations accessible to this API key."""
        tmp = cls(base_url, api_key, 0, "")
        orgs = tmp._unwrap(tmp._get("/v4/organization?limit=1000"))
        if not orgs:
            sys.exit("ERROR: /api/v4/organization returned no organizations — check API key.")
        return orgs

    def _request(self, method: str, path: str, body: dict | None = None, allow_404: bool = False, allow_403: bool = False) -> Any:
        url = f"{self.base_url}/api{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers: dict[str, str] = {
            "Authorization": f"Token {self.api_key}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            if e.code == 401:
                sys.exit(
                    f"ERROR: 401 Unauthorized — check APPNETA_API_KEY.\n"
                    f"URL: {url}\n"
                    "Auth header used: Authorization: Token <key>"
                )
            if e.code == 403:
                if allow_403:
                    return None
                sys.exit(
                    f"ERROR: 403 Forbidden — API key does not have access to this endpoint.\n"
                    f"URL: {url}\n"
                    "  Check that APPNETA_API_KEY is scoped to the correct organization\n"
                    "  and has at least read access to the required resources."
                )
            err_body = e.read().decode(errors="replace")
            sys.exit(f"ERROR: HTTP {e.code} from {url}\n{err_body[:500]}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                sys.exit(f"ERROR: Request timed out after 30s: {url}")
            sys.exit(f"ERROR: Could not reach {url}\n{e.reason}")

    def _get(self, path: str, allow_404: bool = False, allow_403: bool = False) -> Any:
        return self._request("GET", path, allow_404=allow_404, allow_403=allow_403)

    def _unwrap(self, data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("data", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    def _fetch_all_pages(self, base_path: str, limit: int = 1000) -> list[dict]:
        """Fetch all items from a paginated AppNeta v4 endpoint.

        Requests pages of `limit` items using offset-based pagination until a page
        smaller than `limit` is returned. AppNeta defaults to 50 items per page;
        the maximum is 1000. A safety cap of 100 pages prevents infinite loops if
        the API behaves unexpectedly.
        """
        results: list[dict] = []
        sep = "&" if "?" in base_path else "?"
        for page_num in range(100):
            offset = page_num * limit
            page = self._unwrap(
                self._get(f"{base_path}{sep}limit={limit}&offset={offset}")
            )
            results.extend(page)
            if len(page) < limit:
                break
        return results

    def list_monitoring_points(self) -> list[dict]:
        if "points" not in self._cache:
            self._cache["points"] = self._fetch_all_pages(
                f"/v4/monitoringPoint?orgId={self.org_id}"
            )
        return self._cache["points"]

    def list_monitoring_policies(self) -> list[dict]:
        if "policies" not in self._cache:
            self._cache["policies"] = self._fetch_all_pages(
                f"/v4/monitoringPolicy?orgId={self.org_id}"
            )
        return self._cache["policies"]

    def list_tags(self) -> list[dict]:
        if "tags" not in self._cache:
            self._cache["tags"] = self._fetch_all_pages(
                f"/v3/tag?orgId={self.org_id}"
            )
        return self._cache["tags"]

    def list_monitoring_policy_groups(self) -> list[dict]:
        if "groups" not in self._cache:
            self._cache["groups"] = self._fetch_all_pages(
                f"/v4/monitoringPolicyGroup?orgId={self.org_id}"
            )
        return self._cache["groups"]

    def get_connector(self, connector_id: int) -> dict | None:
        """Fetch a specific connector by ID using the ?id= query parameter.

        The v4 API does not support GET /v4/connector/alarm/{id} (returns 405).
        Instead, the connector list endpoint accepts ?id=<id> to filter by connector ID.
        Returns None if the connector is not found or if the request is not permitted.
        """
        cache_key = f"connector_{connector_id}"
        if cache_key not in self._cache:
            result = self._get(
                f"/v4/connector/alarm?orgId={self.org_id}&id={connector_id}",
                allow_403=True,
            )
            connectors = self._unwrap(result) if result is not None else None
            if connectors is None:
                self._cache[cache_key] = None
            else:
                self._cache[cache_key] = next(
                    (c for c in connectors if c.get("id") == connector_id), None
                )
        return self._cache[cache_key]

    def group_ids_for_names(self, group_names: list[str], errors: list) -> list[int]:
        """Return int64 global IDs for the given policy group name(s).

        Uses the dedicated /v4/monitoringPolicyGroup endpoint which returns
        groups with their own int64 global IDs directly.
        """
        groups = self.list_monitoring_policy_groups()
        lower_to_orig = {g.strip().lower(): g.strip() for g in group_names}
        available_names = sorted(grp["name"] for grp in groups if grp.get("name"))
        name_to_id: dict[str, int] = {}
        for grp in groups:
            name = grp.get("name", "").strip()
            gid = grp.get("id")
            if name and gid and name.lower() in lower_to_orig:
                name_to_id[name.lower()] = int(gid)
        unmatched = set(lower_to_orig.keys()) - name_to_id.keys()
        for lower_name in unmatched:
            errors.append({
                "org": self.org_name,
                "type": "policy group",
                "value": lower_to_orig[lower_name],
                "available": available_names,
            })
        return sorted(name_to_id.values())



# ---------------------------------------------------------------------------
# Name → ID resolution helpers
# ---------------------------------------------------------------------------

def is_numeric(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _has_glob(value: str) -> bool:
    return any(c in value for c in _GLOB_CHARS)


def parse_tag_spec(raw: str) -> tuple[str | None, str | None]:
    """Parse a CLI tag spec into (category, value).

    "Location:Chicago IL"  → ("Location", "Chicago IL")  exact match
    "Location"            → ("Location", None)          category-only
    ":Chicago IL"         → (None, "Chicago IL")        value-only
    """
    raw = raw.strip()
    if ":" in raw:
        cat, _, val = raw.partition(":")
        return (cat.strip() or None, val.strip() or None)
    return (raw or None, None)


def _fetch_tag_catalog(client: "AppNetaClient") -> set[tuple[str, str]]:
    """Fetch all (category, value) tag pairs via /api/v3/tag.

    Handles both object form ({category, value} fields) and string form
    ("Category: Value" or "Category:Value"), since the API may return either.
    Spaces around the colon separator are stripped in both cases.
    """
    catalog: set[tuple[str, str]] = set()
    for t in client.list_tags():
        if isinstance(t, str):
            cat, _, val = t.partition(":")
            cat, val = cat.strip(), val.strip()
        else:
            cat = t.get("category", "").strip()
            val = t.get("value", "").strip()
        if cat and val:
            catalog.add((cat, val))
    return catalog


def resolve_tags(
    tag_specs: list[str],
    config_tags: list[dict],
    client: "AppNetaClient",
    errors: list,
) -> list[dict]:
    """Resolve and validate all tags against the org's tag catalog from /api/v3/tag.

    config_tags: [{category, value}] from the config file — validated as exact pairs.
    tag_specs: CLI specs — "Cat:Val" (exact), "Cat" (all values), ":Val" (all categories).

    Only full key:value pairs found in the catalog are included in the output.
    Unrecognized tags are written to the errors list.
    """
    if not tag_specs and not config_tags:
        return []

    catalog = _fetch_tag_catalog(client)
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(c: str, v: str) -> None:
        if (c, v) not in seen:
            seen.add((c, v))
            result.append({"category": c, "value": v})

    def _exact_error(cat: str, val: str) -> dict:
        cats_in_catalog = {c for c, _ in catalog}
        if cat.lower() in {c.lower() for c in cats_in_catalog}:
            return {
                "org": client.org_name, "type": "tag",
                "value": f"{cat}:{val}",
                "note": f"category '{cat}' exists but value '{val}' not found",
                "available": sorted(v for c, v in catalog if c.lower() == cat.lower()),
            }
        return {
            "org": client.org_name, "type": "tag",
            "value": f"{cat}:{val}",
            "note": f"category '{cat}' not found in tag catalog",
            "available": sorted(cats_in_catalog),
        }

    # Config-file tags: full {category, value} pairs — validate each exists
    for t in config_tags:
        cat = t.get("category", "").strip()
        val = t.get("value", "").strip()
        if not cat or not val:
            continue
        # Case-insensitive lookup; use canonical form from catalog
        match = next(((c, v) for c, v in catalog if c.lower() == cat.lower() and v.lower() == val.lower()), None)
        if match:
            _add(*match)
        else:
            errors.append(_exact_error(cat, val))

    # CLI tag specs — exact, category-only, or value-only
    for spec in tag_specs:
        cat, val = parse_tag_spec(spec)
        if cat and val:
            match = next(((c, v) for c, v in catalog if c.lower() == cat.lower() and v.lower() == val.lower()), None)
            if match:
                _add(*match)
            else:
                errors.append(_exact_error(cat, val))
        elif cat:
            matches = sorted((c, v) for c, v in catalog if c.lower() == cat.lower())
            if matches:
                for c, v in matches:
                    _add(c, v)
            else:
                errors.append({
                    "org": client.org_name, "type": "tag category", "value": cat,
                    "available": sorted({c for c, _ in catalog}),
                })
        elif val:
            matches = sorted((c, v) for c, v in catalog if v.lower() == val.lower())
            if matches:
                for c, v in matches:
                    _add(c, v)
            else:
                errors.append({
                    "org": client.org_name, "type": "tag value", "value": val,
                    "available": sorted({v for _, v in catalog}),
                })

    return result


def resolve_to_ids(
    raw_values: list[str],
    lookup: list[dict],
    name_field: str,
    id_field: str,
    label: str,
    errors: list,
    org_name: str,
) -> list[int]:
    """Mixed list of names/IDs → integer IDs. Appends to errors for unmatched values."""
    name_map = {item[name_field].strip().lower(): item[id_field] for item in lookup}
    id_set = {item[id_field] for item in lookup}
    available_names = sorted(name_map.keys())
    result: list[int] = []
    for raw in raw_values:
        raw = raw.strip()
        if is_numeric(raw):
            num_id = int(raw)
            if num_id in id_set:
                result.append(num_id)
            else:
                errors.append({"org": org_name, "type": label, "value": raw, "available": available_names[:30]})
        else:
            resolved = name_map.get(raw.lower())
            if resolved is not None:
                result.append(resolved)
            else:
                errors.append({"org": org_name, "type": label, "value": raw, "available": available_names[:30]})
    return result


def resolve_mp_types(raw_types: list[str], all_points: list[dict], errors: list, org_name: str) -> list[str]:
    """Resolve MP type specs to API values for monitoringPointType.

    Glob patterns (*, **, +, ?, !) are passed through as-is — the API interprets them.
    Plain strings are substring-matched case-insensitively against live type names
    (e.g. 'c50' → 'c50 Container').
    """
    available = sorted({p.get("type", "") for p in all_points if p.get("type")})
    result: list[str] = []
    for raw in raw_types:
        raw = raw.strip()
        if _has_glob(raw):
            result.append(raw)
        elif raw.lower() in _MP_TYPE_ALIASES:
            result.extend(_MP_TYPE_ALIASES[raw.lower()])
        else:
            matches = [t for t in available if raw.lower() in t.lower()]
            if matches:
                result.extend(matches)
            else:
                errors.append({"org": org_name, "type": "MP type", "value": raw, "available": available})
    return list(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# Connector fetch and diff (read-only GET — no write operations)
# ---------------------------------------------------------------------------

def _diff_field(path: str, current: Any, proposed: Any, lines: list[str]) -> None:
    """Recursively collect human-readable diff lines for changed fields."""
    if current == proposed:
        return
    if isinstance(current, str) and current and all(c == "*" for c in current):
        lines.append(f"  ? {path}: (masked in existing connector — cannot verify if value changed)")
        return
    if isinstance(current, dict) and isinstance(proposed, dict):
        for k in sorted(set(current) | set(proposed)):
            _diff_field(f"{path}.{k}", current.get(k), proposed.get(k), lines)
    elif isinstance(current, list) and isinstance(proposed, list):
        if all(isinstance(x, dict) for x in (current or []) + (proposed or [])):
            for i in range(max(len(current), len(proposed))):
                c = current[i] if i < len(current) else None
                p = proposed[i] if i < len(proposed) else None
                _diff_field(f"{path}[{i}]", c, p, lines)
        elif current != proposed:
            lines.append(f"  ~ {path}: {json.dumps(current)} → {json.dumps(proposed)}")
    else:
        curr_s = json.dumps(current)
        prop_s = json.dumps(proposed)
        if current is None:
            lines.append(f"  + {path}: {prop_s}")
        elif proposed is None:
            lines.append(f"  - {path}: {curr_s}")
        else:
            lines.append(f"  ~ {path}: {curr_s[:80]} → {prop_s[:80]}")


def diff_connector(current: dict, proposed: dict) -> list[str]:
    lines: list[str] = []
    for key in ("name", "description", "enabled", "include", "exclude", "notifyOn", "connectors"):
        _diff_field(key, current.get(key), proposed.get(key), lines)
    return lines


def resolve_connector_mode(
    client: "AppNetaClient", config: dict
) -> tuple[str, int | None, dict | None]:
    """Determine payload type (CREATE vs UPDATE) from connector.id in config.

    If connector.id is set, attempts a read-only GET of the existing connector
    for merge and diff. Returns POST mode if no id is set.

    No write operations are performed — this is a read-only lookup.
    """
    connector_cfg = config.get("connector", {})
    explicit_id = connector_cfg.get("id")
    if explicit_id is None:
        return "POST", None, None
    existing = client.get_connector(int(explicit_id))
    return "PUT", int(explicit_id), existing


def merge_with_existing(payload: dict, existing: dict, errors: list | None = None, org_name: str = "") -> dict:
    """Merge a generated payload with the existing connector for UPDATE (PUT) mode.

    Semantics: fields present in the TOML-generated payload override the existing
    connector. Fields absent from the TOML (empty lists, missing keys) are
    preserved from the existing connector unchanged.

    This means UPDATE is always a partial change — you only need to specify what
    you want to add or modify. To REMOVE a field entirely (e.g. clear all severity
    filters), you must DELETE the connector and re-create it with POST.

    Applied to:
    - include/exclude filter blocks — merged field-by-field
    - connectors list — merged by index; API-assigned fields (id, ignoreSSLErrors)
      preserved from the existing connector so PUT does not inadvertently drop them
    - description — preserved from existing when absent from TOML
    """
    merged = dict(payload)

    # Merge include/exclude filter blocks field-by-field
    for block_name in ("include", "exclude"):
        existing_block = existing.get(block_name, {})
        new_block = merged.get(block_name, {})
        if existing_block:
            merged_block = {**existing_block, **new_block}
            if merged_block:
                merged[block_name] = merged_block

    # Merge connector sub-objects by position — TOML fields override existing.
    # 'id' must be preserved: the API rejects a PUT whose sub-connector entry
    # has no 'id' (400 "Connector ID is required for update operations").
    existing_conns = existing.get("connectors", [])
    new_conns = merged.get("connectors", [])
    if existing_conns and new_conns:
        merged_conns = []
        for i, new_conn in enumerate(new_conns):
            if i < len(existing_conns):
                mc = {**existing_conns[i], **new_conn}
            else:
                mc = dict(new_conn)
            merged_conns.append(mc)
        merged["connectors"] = merged_conns

    # The field-by-field merge can pair an existing include value with a new exclude
    # value (or vice versa). Resolve exactly as build_payload does: exclude wins.
    if merged.get("include") and merged.get("exclude"):
        _remove_contradictions(merged["include"], merged["exclude"],
                               errors if errors is not None else [], org_name)
        for block_name in ("include", "exclude"):
            if not merged[block_name]:
                del merged[block_name]

    if "description" not in merged and existing.get("description"):
        merged["description"] = existing["description"]

    return merged


# ---------------------------------------------------------------------------
# JSON payload builder
# ---------------------------------------------------------------------------

def build_filter(
    section: dict,
    mp_name_overrides: list[str],
    mp_type_overrides: list[str],
    policy_overrides: list[str],
    group_overrides: list[str],
    tag_overrides: list[str],
    client: "AppNetaClient",
    errors: list,
) -> dict:
    out: dict[str, Any] = {}

    if section.get("severity"):
        out["severity"] = [s.upper() for s in section["severity"]]

    if section.get("noise_filter"):
        out["noiseFilter"] = [n.upper() for n in section["noise_filter"]]

    # Alarm rule IDs must be obtained from the AppNeta GUI — no API endpoint available.
    # Values are passed through as-is; non-numeric entries are skipped with a warning.
    alarm_rule_ids_raw = section.get("alarm_rule_ids", [])
    if alarm_rule_ids_raw:
        numeric_ids = [int(x) for x in alarm_rule_ids_raw if is_numeric(str(x))]
        skipped = [x for x in alarm_rule_ids_raw if not is_numeric(str(x))]
        if skipped:
            print(
                f"WARNING: alarm_rule_ids contains non-numeric value(s) {skipped} — "
                "alarm rule IDs must be obtained from the AppNeta GUI and entered as integers.",
                file=sys.stderr,
            )
        if numeric_ids:
            out["alarmRuleId"] = numeric_ids

    if section.get("targets"):
        out["target"] = list(section["targets"])

    all_policy_names = [str(x) for x in section.get("monitoring_policy_names", [])] + policy_overrides
    if all_policy_names:
        ids = resolve_to_ids(
            all_policy_names, client.list_monitoring_policies(), "name", "id", "policy", errors, client.org_name
        )
        if ids:
            out["monitoringPolicyId"] = ids

    all_group_names = [str(x) for x in section.get("monitoring_policy_group_names", [])] + group_overrides
    if all_group_names:
        numeric_group_ids = [int(x) for x in all_group_names if is_numeric(x)]
        name_strs = [x for x in all_group_names if not is_numeric(x)]
        resolved_ids = client.group_ids_for_names(name_strs, errors) if name_strs else []
        group_ids = sorted(set(numeric_group_ids + resolved_ids))
        if group_ids:
            out["monitoringPolicyGroupId"] = group_ids

    all_mp_names = [str(x) for x in section.get("monitoring_point_names", [])] + mp_name_overrides
    if all_mp_names:
        glob_names = [n.strip() for n in all_mp_names if _has_glob(n.strip())]
        plain_names = [n.strip() for n in all_mp_names if not _has_glob(n.strip())]
        if glob_names:
            out["monitoringPointName"] = glob_names
        if plain_names:
            ids = resolve_to_ids(
                plain_names, client.list_monitoring_points(), "name", "id", "monitoring point", errors, client.org_name
            )
            if ids:
                out["monitoringPointId"] = ids

    all_mp_types = list(section.get("monitoring_point_types", [])) + mp_type_overrides
    if all_mp_types:
        out["monitoringPointType"] = resolve_mp_types(all_mp_types, client.list_monitoring_points(), errors, client.org_name)

    tags = resolve_tags(tag_overrides, section.get("tags", []), client, errors)
    if tags:
        out["tag"] = tags

    # Dedicated numeric-only ID fields — direct passthrough, no API lookup at
    # all (unlike the *_names fields' numeric passthrough, which still checks
    # membership against the live catalog via resolve_to_ids). These exist so
    # a filter value's intent (an ID) is explicit in config, rather than
    # relying on a numeric-string heuristic under a "names" field. Merged
    # (union, sorted) with whatever the *_names fields already contributed to
    # the same output key, so using both mechanisms together composes safely.
    for toml_key, out_key, _label in _NUMERIC_ID_FIELDS:
        raw = section.get(toml_key)
        if raw is None:
            continue
        values = [raw] if isinstance(raw, int) and not isinstance(raw, bool) else list(raw)
        ids = sorted({int(x) for x in values if isinstance(x, int) and not isinstance(x, bool) and x > 0})
        if ids:
            out[out_key] = sorted(set(out.get(out_key, [])) | set(ids))

    return out


def _remove_contradictions(
    include_block: dict, exclude_block: dict, errors: list, org_name: str
) -> None:
    """Resolve values present in both include and exclude for the same field.

    Exclude wins: the contradicting value is removed from include and kept in
    exclude. This preserves the operator's explicit exclusion intent and is
    consistent with AppNeta's evaluation order (include first, then exclude).
    """
    scalar_fields = [
        ("severity",               "severity"),
        ("noiseFilter",            "noise filter"),
        ("monitoringPointId",      "monitoring point ID"),
        ("monitoringPointName",    "monitoring point name (glob)"),
        ("monitoringPointType",    "monitoring point type"),
        ("monitoringPolicyId",     "policy ID"),
        ("monitoringPolicyGroupId","policy group ID"),
        ("alarmRuleId",            "alarm rule ID"),
        ("target",                 "target"),
    ]
    for field, label in scalar_fields:
        inc = include_block.get(field, [])
        exc = exclude_block.get(field, [])
        if not inc or not exc:
            continue
        dupes = set(inc) & set(exc)
        if not dupes:
            continue
        for val in sorted(dupes, key=str):
            errors.append({
                "org": org_name, "type": "contradiction",
                "value": f"{label}: {val}",
                "note": "same value in both include and exclude — removed from include, kept in exclude",
            })
        include_block[field] = [v for v in inc if v not in dupes]
        if not include_block[field]:
            del include_block[field]

    # Tags are dicts — compare as (category, value) tuples.
    # For each contradicting pair, the side with MORE values for that key is the
    # "list"; the other side holds the exception. The contradicting entry is removed
    # from the list side and kept on the exception side. When counts are equal,
    # exclude wins as the conservative default.
    inc_tags = include_block.get("tag", [])
    exc_tags = exclude_block.get("tag", [])
    if inc_tags and exc_tags:
        inc_set = {(t["category"], t["value"]) for t in inc_tags}
        exc_set = {(t["category"], t["value"]) for t in exc_tags}
        dupes = inc_set & exc_set
        if dupes:
            remove_from_inc: set[tuple] = set()
            remove_from_exc: set[tuple] = set()
            for cat, val in sorted(dupes):
                inc_others = sum(1 for t in inc_tags if t["category"] == cat and t["value"] != val)
                exc_others = sum(1 for t in exc_tags if t["category"] == cat and t["value"] != val)
                if inc_others == exc_others:
                    # Equal count — cannot determine which side is the list.
                    # Reported as an unresolvable conflict; removed from include
                    # as a safe fallback but the operator must fix their config.
                    remove_from_inc.add((cat, val))
                    errors.append({
                        "org": org_name, "type": "conflict",
                        "value": f"tag: {cat}:{val}",
                        "note": f"'{cat}:{val}' appears in both include and exclude with no clear list/exception distinction — remove it from one side",
                    })
                elif exc_others > inc_others:
                    # Exclude is the list; include holds the exception — include wins
                    remove_from_exc.add((cat, val))
                    errors.append({
                        "org": org_name, "type": "contradiction",
                        "value": f"tag: {cat}:{val}",
                        "note": f"tag key '{cat}' has more values in exclude — '{val}' is the include exception; removed from exclude, kept in include",
                    })
                else:
                    # Include is the list — exclude holds the exception; exclude wins
                    remove_from_inc.add((cat, val))
                    errors.append({
                        "org": org_name, "type": "contradiction",
                        "value": f"tag: {cat}:{val}",
                        "note": f"tag key '{cat}' has more values in include — '{val}' is the exclude exception; removed from include, kept in exclude",
                    })
            include_block["tag"] = [t for t in inc_tags if (t["category"], t["value"]) not in remove_from_inc]
            exclude_block["tag"] = [t for t in exc_tags if (t["category"], t["value"]) not in remove_from_exc]
            if not include_block["tag"]:
                del include_block["tag"]
            if not exclude_block["tag"]:
                del exclude_block["tag"]


def build_payload(config: dict, args: argparse.Namespace, client: "AppNetaClient", errors: list) -> dict:
    include_section = config.get("include", {})
    exclude_section = dict(config.get("exclude", {}))

    include_block = build_filter(
        include_section,
        mp_name_overrides=split_csv(args.include_mp_names),
        mp_type_overrides=split_csv(args.include_mp_type),
        policy_overrides=split_csv(args.include_policy),
        group_overrides=split_csv(args.include_policy_group),
        tag_overrides=split_csv(args.include_tag),
        client=client,
        errors=errors,
    )
    exclude_block = build_filter(
        exclude_section,
        mp_name_overrides=split_csv(args.exclude_mp_names),
        mp_type_overrides=split_csv(args.exclude_mp_type),
        policy_overrides=split_csv(args.exclude_policy),
        group_overrides=split_csv(args.exclude_policy_group),
        tag_overrides=split_csv(args.exclude_tag),
        client=client,
        errors=errors,
    )

    _remove_contradictions(include_block, exclude_block, errors, client.org_name)

    # Warn when multiple include filter types are active simultaneously.
    # AppNeta applies OR logic across filter types — an alarm passes if it
    # matches ANY one criterion, not ALL of them. This surprises most users.
    _INC_GROUPS = [
        {"monitoringPointId", "monitoringPointName"},
        {"monitoringPointType"},
        {"monitoringPolicyId"},
        {"monitoringPolicyGroupId"},
        {"tag"},
        {"severity"},
        {"noiseFilter"},
        {"alarmRuleId"},
        {"target"},
    ]
    active_groups = sum(1 for g in _INC_GROUPS if any(k in include_block for k in g))
    if active_groups >= 2:
        fields = ", ".join(k for k in include_block)
        print(
            f"WARNING (org: {client.org_name}): Include block has {active_groups} filter "
            f"types active ({fields}). AppNeta applies OR logic — an alarm is forwarded "
            "if it matches ANY one criterion, not all of them.",
            file=sys.stderr,
        )

    webhook = config.get("webhook", {})
    connector_entry: dict[str, Any] = {
        "type": "WEBHOOK",
        "url": webhook["url"],
        "method": webhook.get("method", "POST").upper(),
    }
    if webhook.get("headers"):
        connector_entry["headers"] = [
            {"key": h.get("key") or h["name"], "value": interpolate_env(h["value"])}
            for h in webhook["headers"]
        ]
    auth = webhook.get("auth", {})
    auth_type = auth.get("type", "NONE").upper()
    if auth_type != "NONE":
        auth_block: dict[str, str] = {"type": auth_type}
        if auth_type == "BASIC":
            if auth.get("username"):
                auth_block["username"] = auth["username"]
            auth_block["password"] = os.environ["CUSTOMER_WEBHOOK_PASSWORD"]
        elif auth_type == "BEARER":
            auth_block["token"] = os.environ["CUSTOMER_WEBHOOK_TOKEN"]
        connector_entry["auth"] = auth_block
    if "ignore_ssl_errors" in webhook:
        # Emitted even when false: on UPDATE it is the only way to switch TLS verification back on.
        connector_entry["ignoreSSLErrors"] = bool(webhook["ignore_ssl_errors"])

    connector_cfg = config.get("connector", {})
    payload: dict[str, Any] = {
        "name": connector_cfg["name"],
        "enabled": connector_cfg.get("enabled", True),
    }
    if connector_cfg.get("description"):
        payload["description"] = connector_cfg["description"]
    if include_block:
        payload["include"] = include_block
    if exclude_block:
        payload["exclude"] = exclude_block

    payload["notifyOn"] = [n.upper() for n in config.get("notify_on", ["ALARM_RAISED", "ALARM_CLEARED"])]
    payload["connectors"] = [connector_entry]
    return payload


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def interpolate_env(value: str) -> str:
    """Replace ${VAR_NAME} in a string with the corresponding environment variable.

    Exits with a clear error if any referenced variable is unset.
    """
    def _sub(m: re.Match) -> str:
        var = m.group(1)
        val = os.environ.get(var)
        if val is None:
            sys.exit(
                f"ERROR: Header value references ${{{var}}} but that environment variable is not set.\n"
                f"  Export it before running:\n"
                f"    export {var}=<value>"
            )
        if _has_crlf(val) or _has_ctrl(val):
            sys.exit(
                f"ERROR: environment variable {var} contains line breaks or control characters — "
                "that would inject headers into AppNeta's webhook calls."
            )
        return val
    return _ENV_VAR_RE.sub(_sub, value)


def split_csv(value: "str | None") -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def safe_filename_part(name: str) -> str:
    sanitized = re.sub(r"[^\w-]", "_", name).strip("_")
    return sanitized or "unknown"


def write_file(path: str, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content + "\n")
    print(f"Written to {path}", file=sys.stderr)


def _write_backup(existing: dict, connector_id: int) -> str:
    """Save the pre-change connector JSON next to the output, owner-only."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"{ts}-connector-{connector_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    return path


def _require_existing(existing: dict | None, connector_id: int, org_name: str, lenient: bool) -> None:
    """An UPDATE we cannot read back would replace the connector blind; refuse unless --lenient."""
    if existing is None and not lenient:
        sys.exit(
            f"ERROR: connector.id={connector_id} in org {org_name!r} could not be read, so the update "
            "cannot be merged or diffed and would overwrite it blind.\n"
            "  Check the ID and that the API key can read this org's connectors, "
            "or use --lenient to PUT the generated payload as-is."
        )


def _orgs_with_emptied_include(results: list, errors: list[dict]) -> list[str]:
    """Orgs where a contradiction removed the last include criterion.

    exclude-wins can leave only an exclude block, which turns 'only X' into 'everything except X'.
    """
    hit = {e.get("org") for e in errors if e.get("type") == "contradiction"}
    return [c.org_name for c, p in results if c.org_name in hit and "include" not in p]


def write_errors(errors: list[dict], stem: str, source_desc: str) -> str:
    path = f"{stem}-connector-error.txt"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "AppNeta Connector — Resolution Errors",
        f"Config   : {source_desc}",
        f"Generated: {now}",
        "",
        "The following items were not found and were omitted from the payload.",
        "Correct the values in your config or CLI flags and rerun.",
        "",
    ]
    by_org: dict[str, list[dict]] = {}
    orgs_order: list[str] = []
    for e in errors:
        org = e["org"]
        if org not in by_org:
            by_org[org] = []
            orgs_order.append(org)
        by_org[org].append(e)
    for org in orgs_order:
        lines.append("─" * 70)
        lines.append(f"Org: {org}")
        lines.append("─" * 70)
        for e in by_org[org]:
            note = f"  ({e['note']})" if e.get("note") else ""
            action = "ignored" if e["type"] == "contradiction" else "not found"
            lines.append(f"  [{e['type']}] '{e['value']}' {action}{note}")
            avail = e.get("available", [])
            if avail:
                lines.append(f"    Available: {', '.join(str(a) for a in avail)}")
            else:
                lines.append("    Available: (none)")
        lines.append("")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Resolution errors written to {path}", file=sys.stderr)
    return path


# ---------------------------------------------------------------------------
# Config and environment validation
# ---------------------------------------------------------------------------

def _validate_base_url(url: str) -> None:
    """Ensure base_url is a well-formed HTTPS URL.

    IP-literal hosts are allowed deliberately: base_url is operator-supplied
    trusted config, not third-party input, and on-prem AppNeta deployments
    are commonly reached via a private IP (e.g. https://192.0.2.10) with no
    DNS entry at all.
    """
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError:
        port = 0
    if (parsed.scheme != "https" or not parsed.netloc or not parsed.hostname
            or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url)
            or "@" in parsed.netloc or parsed.path.rstrip("/") or parsed.query or parsed.fragment
            or "?" in url or "#" in url
            or (port is not None and not 1 <= port <= 65535)):
        sys.exit(
            f"ERROR: base_url must be a valid HTTPS URL.\n"
            f"  Got: {url!r}\n"
            f"  Scheme and host (optional port) only: no credentials, path, query or fragment.\n"
            f"  Example: https://app-01.pm.appneta.com or https://192.0.2.10"
        )


def validate_config(config: dict) -> None:
    """Check required fields and enforce that credentials are not stored in config."""
    missing = []
    if not config.get("connector", {}).get("name"):
        missing.append("connector.name")
    if not config.get("webhook", {}).get("url"):
        missing.append("webhook.url")
    if missing:
        sys.exit("ERROR: Missing required config field(s): " + ", ".join(missing))

    webhook_cfg = config.get("webhook", {})
    injectable = [("connector.name", config.get("connector", {}).get("name")),
                  ("connector.description", config.get("connector", {}).get("description")),
                  ("webhook.url", webhook_cfg.get("url")),
                  ("webhook.auth.username", (webhook_cfg.get("auth") or {}).get("username"))]
    for h in webhook_cfg.get("headers") or []:
        injectable += [("webhook.headers key", h.get("key") or h.get("name")), ("webhook.headers value", h.get("value"))]
    for label, val in injectable:
        if isinstance(val, str) and (_has_crlf(val) or _has_ctrl(val)):
            sys.exit(f"ERROR: {label} contains line breaks or control characters: {val!r}")

    connector_id = config.get("connector", {}).get("id")
    if connector_id is not None and (isinstance(connector_id, bool) or not isinstance(connector_id, int)):
        sys.exit(
            f"ERROR: connector.id must be an integer (int64 connector ID), "
            f"got {type(connector_id).__name__}: {connector_id!r}"
        )

    auth = config.get("webhook", {}).get("auth", {})
    auth_type = auth.get("type", "NONE").upper()
    if auth_type not in VALID_AUTH_TYPES:
        sys.exit(
            f"ERROR: Unsupported webhook.auth.type: {auth_type!r}\n"
            f"  Supported values: {', '.join(sorted(VALID_AUTH_TYPES - {'NONE'}))}"
        )

    method = config.get("webhook", {}).get("method", "POST").upper()
    if method not in VALID_METHODS:
        sys.exit(
            f"ERROR: Unsupported webhook.method: {method!r}\n"
            f"  Supported values: {', '.join(sorted(VALID_METHODS))}"
        )

    for section_name in ("include", "exclude"):
        section = config.get(section_name, {})
        invalid_sev = [s for s in section.get("severity", []) if s.upper() not in VALID_SEVERITIES]
        if invalid_sev:
            sys.exit(
                f"ERROR: Invalid {section_name}.severity value(s): {invalid_sev}\n"
                f"  Valid values: {sorted(VALID_SEVERITIES)}"
            )
        invalid_nf = [n for n in section.get("noise_filter", []) if n.upper() not in VALID_NOISE_FILTERS]
        if invalid_nf:
            sys.exit(
                f"ERROR: Invalid {section_name}.noise_filter value(s): {invalid_nf}\n"
                f"  Valid values: {sorted(VALID_NOISE_FILTERS)}"
            )

    notify_on = config.get("notify_on", [])
    invalid_no = [n for n in notify_on if n.upper() not in VALID_NOTIFY_ON]
    if invalid_no:
        sys.exit(
            f"ERROR: Invalid notify_on value(s): {invalid_no}\n"
            f"  Valid values: {sorted(VALID_NOTIFY_ON)}"
        )

    if auth.get("token"):
        sys.exit(
            "ERROR: webhook.auth.token must not be stored in the config file.\n"
            "  Export it as an environment variable instead:\n"
            "    export CUSTOMER_WEBHOOK_TOKEN=<token>"
        )
    if auth.get("password"):
        sys.exit(
            "ERROR: webhook.auth.password must not be stored in the config file.\n"
            "  Export it as an environment variable instead:\n"
            "    export CUSTOMER_WEBHOOK_PASSWORD=<password>"
        )

    if auth_type == "BEARER" and not os.environ.get("CUSTOMER_WEBHOOK_TOKEN"):
        sys.exit(
            "ERROR: webhook.auth.type is BEARER but CUSTOMER_WEBHOOK_TOKEN is not set.\n"
            "  Export it before running:\n"
            "    export CUSTOMER_WEBHOOK_TOKEN=<token>"
        )
    elif auth_type == "BEARER":
        token = os.environ.get("CUSTOMER_WEBHOOK_TOKEN", "")
        if "\r" in token or "\n" in token:
            sys.exit(
                "ERROR: CUSTOMER_WEBHOOK_TOKEN contains line breaks — "
                "CRLF in a Bearer token would inject headers into AppNeta's webhook calls."
            )
    if auth_type == "BASIC" and not os.environ.get("CUSTOMER_WEBHOOK_PASSWORD"):
        sys.exit(
            "ERROR: webhook.auth.type is BASIC but CUSTOMER_WEBHOOK_PASSWORD is not set.\n"
            "  Export it before running:\n"
            "    export CUSTOMER_WEBHOOK_PASSWORD=<password>"
        )
    elif auth_type == "BASIC":
        password = os.environ.get("CUSTOMER_WEBHOOK_PASSWORD", "")
        if "\r" in password or "\n" in password:
            sys.exit(
                "ERROR: CUSTOMER_WEBHOOK_PASSWORD contains line breaks — "
                "CRLF in a Basic auth password would inject headers into AppNeta's webhook calls."
            )

def validate_environment(args: argparse.Namespace, config: dict) -> bool:
    """Pre-flight check on environment variables and config file."""
    is_valid = True
    print("\n[+] Running pre-flight environment validation...", file=sys.stderr)

    if args.config is not None:
        if not os.path.exists(args.config):
            print(f"    -> Config file   : NOT FOUND ({args.config})", file=sys.stderr)
            is_valid = False
        else:
            print(f"    -> Config file   : {args.config}", file=sys.stderr)
    else:
        print("    -> Mode          : CLI-only (no TOML file)", file=sys.stderr)

    api_key = os.environ.get("APPNETA_API_KEY", "")
    if api_key:
        print("    -> API key       : Detected", file=sys.stderr)
    else:
        print("    -> API key       : NOT SET — set APPNETA_API_KEY", file=sys.stderr)
        is_valid = False

    if args.base_url:
        print(f"    -> Base URL      : {args.base_url}", file=sys.stderr)
    else:
        print("    -> Base URL      : NOT SET — required for API calls", file=sys.stderr)
        is_valid = False

    auth_type = config.get("webhook", {}).get("auth", {}).get("type", "NONE").upper()
    if auth_type == "BEARER":
        print("    -> Webhook token : Detected (CUSTOMER_WEBHOOK_TOKEN)", file=sys.stderr)
    elif auth_type == "BASIC":
        print("    -> Webhook pass  : Detected (CUSTOMER_WEBHOOK_PASSWORD)", file=sys.stderr)
    else:
        print("    -> Webhook auth  : None (auth type: NONE)", file=sys.stderr)

    print("[+] Pre-flight validation complete.\n", file=sys.stderr)
    return is_valid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _toml_str(s: str) -> str:
    """Format a Python string as a TOML basic string (double-quoted)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_filter_block(lines: list[str], section: str, args: argparse.Namespace) -> None:
    """Append a [include] or [exclude] TOML block to lines from CLI filter flags."""
    mp_names  = split_csv(getattr(args, f"{section}_mp_names",    None) or "")
    mp_types  = split_csv(getattr(args, f"{section}_mp_type",     None) or "")
    policies  = split_csv(getattr(args, f"{section}_policy",      None) or "")
    groups    = split_csv(getattr(args, f"{section}_policy_group", None) or "")
    tags_raw  = split_csv(getattr(args, f"{section}_tag",         None) or "")

    # Separate exact (Category:Value) from partial tag specs.
    exact_tags: list[tuple[str, str]] = []
    partial_tags: list[str] = []
    for t in tags_raw:
        t = t.strip()
        if not t:
            continue
        if ":" in t and not t.startswith(":"):
            cat, _, val = t.partition(":")
            exact_tags.append((cat.strip(), val.strip()))
        else:
            partial_tags.append(t)

    if not (mp_names or mp_types or policies or groups or exact_tags or partial_tags):
        return

    lines.append(f"[{section}]")
    if mp_names:
        vals = ", ".join(_toml_str(n) for n in mp_names)
        lines.append(f"monitoring_point_names        = [{vals}]")
    if mp_types:
        vals = ", ".join(_toml_str(t) for t in mp_types)
        lines.append(f"monitoring_point_types        = [{vals}]")
    if policies:
        vals = ", ".join(_toml_str(p) for p in policies)
        lines.append(f"monitoring_policy_names       = [{vals}]")
    if groups:
        vals = ", ".join(_toml_str(g) for g in groups)
        lines.append(f"monitoring_policy_group_names = [{vals}]")
    lines.append("")

    for cat, val in exact_tags:
        lines.append(f"[[{section}.tags]]")
        lines.append(f"category = {_toml_str(cat)}")
        lines.append(f"value    = {_toml_str(val)}")
        lines.append("")

    if partial_tags:
        lines.append(f"# Partial tag specs cannot be stored as-is in TOML.")
        lines.append(f"# Expand each to an explicit [[{section}.tags]] block:")
        for t in partial_tags:
            lines.append(f"#   --{section}-tag {t!r}")
        lines.append(f"# [[{section}.tags]]")
        lines.append(f'# category = "Location"   # replace with actual category')
        lines.append(f'# value    = "Seattle WA"  # replace with actual value')
        lines.append("")


def generate_toml_content(args: argparse.Namespace) -> str:
    """Serialize CLI-only mode flags to a TOML config string."""
    lines: list[str] = [
        "# Generated by generate_connector.py from CLI flags.",
        "# Review and modify before committing as a permanent configuration.",
        "# WARNING: Replace any literal credentials in webhook header values",
        "#          with ${ENV_VAR} references before storing this file.",
        "",
    ]

    base_url = args.base_url or os.environ.get("APPNETA_BASE_URL") or ""
    if base_url:
        lines.append(f"base_url  = {_toml_str(base_url)}")
    else:
        lines.append('# base_url = "https://your-appneta-instance.pm.appneta.com"')

    if args.notify_on:
        events = [e.strip() for e in args.notify_on.split(",")]
    else:
        events = ["ALARM_RAISED", "ALARM_CLEARED"]
    lines.append(f"notify_on = [{', '.join(_toml_str(e) for e in events)}]")
    lines.append("")

    lines.append("[connector]")
    lines.append(f"name        = {_toml_str(args.connector_name or '')}")
    if args.connector_description:
        lines.append(f"description = {_toml_str(args.connector_description)}")
    lines.append("enabled     = true")
    lines.append("")

    lines.append("[webhook]")
    lines.append(f"url               = {_toml_str(args.webhook_url or '')}")
    lines.append(f"method            = {_toml_str((args.webhook_method or 'POST').upper())}")
    lines.append(f"ignore_ssl_errors = {'true' if args.webhook_ignore_ssl else 'false'}")
    lines.append("")

    for raw in (args.webhook_headers or []):
        key, _, value = raw.partition(":")
        value = value.lstrip()
        lines.append("[[webhook.headers]]")
        lines.append(f"key   = {_toml_str(key)}")
        lines.append(f"value = {_toml_str(value)}")
        lines.append("")

    lines.append("[webhook.auth]")
    lines.append(f'type = {_toml_str((args.webhook_auth_type or "NONE").upper())}')
    lines.append("")

    _write_filter_block(lines, "include", args)
    _write_filter_block(lines, "exclude", args)

    return "\n".join(lines) + "\n"


def build_config_from_args(args: argparse.Namespace) -> dict:
    """Build a config dict from CLI flags for CLI-only mode (no TOML provided)."""
    missing = []
    if not args.connector_name:
        missing.append("--connector-name NAME")
    if not args.webhook_url:
        missing.append("--webhook-url URL")
    if missing:
        sys.exit(
            "ERROR: CLI-only mode (no TOML provided) requires:\n"
            + "".join(f"  {m}\n" for m in missing)
            + "\n  Pass a TOML file as the first argument, or supply these flags."
        )

    notify_on = (
        [e.strip() for e in args.notify_on.split(",")]
        if args.notify_on
        else ["ALARM_RAISED", "ALARM_CLEARED"]
    )

    headers: list[dict] = []
    for raw in (args.webhook_headers or []):
        key, _, value = raw.partition(":")
        value = value.lstrip()
        if not key or not value:
            sys.exit(f"ERROR: Malformed --webhook-header {raw!r} — expected 'Key: Value'")
        headers.append({"key": key, "value": value})

    return {
        "notify_on": notify_on,
        "connector": {
            "name": args.connector_name,
            "description": args.connector_description or "",
            "enabled": True,
        },
        "webhook": {
            "url": args.webhook_url,
            "method": args.webhook_method.upper(),
            "ignore_ssl_errors": args.webhook_ignore_ssl,
            "headers": headers,
            "auth": {"type": args.webhook_auth_type.upper()},
        },
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate AppNeta alarm connector JSON from a TOML config file or CLI flags.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
TWO MODES OF OPERATION:
  TOML mode (recommended — permanent record):
    Pass a .toml file as the first argument. All filter configuration
    ([include]/[exclude] blocks) must be in the TOML. CLI filter flags
    (--include-* / --exclude-*) are blocked in this mode — the TOML is the
    source of truth and must be reproducible on its own.

  CLI-only mode (no permanent record):
    Omit the TOML. Supply --connector-name, --webhook-url, and any filter
    flags on the command line. Nothing is written to a config file — this
    mode is for one-off or exploratory runs.

POST vs PUT (CREATE vs UPDATE):
  Default (no --connector-id, no connector.id in TOML): POST (CREATE).
  With --connector-id or connector.id in TOML: PUT (UPDATE).
  --connector-id is OPTIONAL — only set it when updating an existing connector.

IMPORTANT — API key scope:
  API keys should be scoped to specific organizations. A key with access to
  several organizations will generate a payload for each visible organization.
  Create a dedicated read-only key restricted to the target organization(s).

Base URL is required. Set it in the config file (base_url), via --base-url,
or via the APPNETA_BASE_URL environment variable.

Resolution errors (unmatched names or IDs) are written to <config-stem>-connector-error.txt.

Examples:
  # Default — write connector_<orgId>_<orgName>.json:
  python generate_connector.py config.toml

  # Custom filename prefix:
  python generate_connector.py config.toml -o myconnector
  # → writes myconnector_47291_Acme_Corp.json, etc.

  # Write to stdout:
  python generate_connector.py config.toml --stdout > connector.json

  # Restrict to one org (when the key covers multiple orgs in the same hierarchy):
  python generate_connector.py config.toml --org 18223

  # Dry run — review full output interactively before submitting:
  python generate_connector.py config.toml --dry-run

  # UPDATE an existing connector (PUT mode) — connector-id is optional:
  python generate_connector.py config.toml --connector-id 90521
  # Tip: set connector.id in the TOML for a permanent update target instead
  # of passing --connector-id on every run.

  # CLI-only mode (no TOML) — all config via flags, no permanent record:
  python generate_connector.py \\
      --connector-name "My Splunk Connector" \\
      --webhook-url "https://hec.example.com/services/collector/raw" \\
      --webhook-auth-type BEARER \\
      --webhook-header "Content-Type: application/json" \\
      --include-mp-type "m50" \\
      --include-tag "Cloud:GCP" \\
      --stdout
        """,
    )
    p.add_argument("config", nargs="?", default=None,
                   help="TOML config file path (.toml). If omitted, CLI-only mode is active "
                        "and all configuration must be supplied via flags. "
                        "When provided, CLI filter flags (--include-* / --exclude-*) are blocked.")

    inc = p.add_argument_group(
        "include filters — CLI-only mode only (no TOML); blocked when a TOML file is provided"
    )
    inc.add_argument("--include-mp-names", metavar="NAMES",
                     help="Comma-separated MP names, IDs, or glob patterns to include")
    inc.add_argument("--include-mp-type", metavar="TYPES",
                     help="Comma-separated MP type substrings to include (e.g. 'm50,Windows')")
    inc.add_argument("--include-policy", metavar="POLICIES",
                     help="Comma-separated policy names or IDs to include")
    inc.add_argument("--include-policy-group", metavar="GROUPS",
                     help="Comma-separated policy group names or IDs to include")
    inc.add_argument("--include-tag", metavar="TAGS",
                     help="Comma-separated tag specs to include. "
                          "Formats: 'Category:Value' (exact), 'Category' (all values for category), "
                          "':Value' (all categories with that value)")

    exc = p.add_argument_group(
        "exclude filters — CLI-only mode only (no TOML); blocked when a TOML file is provided"
    )
    exc.add_argument("--exclude-mp-names", metavar="NAMES",
                     help="Comma-separated MP names, IDs, or glob patterns to exclude")
    exc.add_argument("--exclude-mp-type", metavar="TYPES",
                     help="Comma-separated MP type substrings to exclude (e.g. 'c50,n10')")
    exc.add_argument("--exclude-policy", metavar="POLICIES",
                     help="Comma-separated policy names or IDs to exclude")
    exc.add_argument("--exclude-policy-group", metavar="GROUPS",
                     help="Comma-separated policy group names or IDs to exclude")
    exc.add_argument("--exclude-tag", metavar="TAGS",
                     help="Comma-separated tag specs to exclude. "
                          "Formats: 'Category:Value' (exact), 'Category' (all values for category), "
                          "':Value' (all categories with that value)")

    cli_id = p.add_argument_group(
        "CLI-only mode — connector identity (required when no TOML is provided)"
    )
    cli_id.add_argument("--connector-name", metavar="NAME", default=None,
                        help="Connector name (required in CLI-only mode)")
    cli_id.add_argument("--connector-description", metavar="TEXT", default=None,
                        help="Connector description (optional)")

    cli_wh = p.add_argument_group(
        "CLI-only mode — webhook and base URL (required when no TOML is provided; "
        "use --base-url from the general options for the AppNeta instance URL)"
    )
    cli_wh.add_argument("--webhook-url", metavar="URL", default=None,
                        help="Webhook endpoint URL (required in CLI-only mode)")
    cli_wh.add_argument("--webhook-method", metavar="METHOD", default="POST",
                        choices=sorted(VALID_METHODS),
                        help="HTTP method AppNeta uses to call the webhook endpoint "
                             f"({', '.join(sorted(VALID_METHODS))}). "
                             "Default: POST (correct for Splunk HEC and most targets).")
    cli_wh.add_argument("--webhook-auth-type", metavar="TYPE", default="NONE",
                        choices=["NONE", "BEARER", "BASIC"],
                        help="Webhook auth type: NONE, BEARER, or BASIC (default: NONE)")
    cli_wh.add_argument("--webhook-ignore-ssl", action="store_true", default=False,
                        help="Ignore SSL certificate errors for the webhook endpoint")
    cli_wh.add_argument("--webhook-header", metavar="'Key: Value'", action="append",
                        dest="webhook_headers", default=[],
                        help="Add a webhook header. Repeatable. Format: 'Key: Value'.")
    cli_wh.add_argument("--notify-on", metavar="EVENTS", default=None,
                        help="Comma-separated alarm event types to forward. "
                             "Default: ALARM_RAISED,ALARM_CLEARED")
    cli_wh.add_argument("--generate-toml", metavar="FILENAME", default=None,
                        help="Write the current CLI flags to a TOML config file (CLI-only mode). "
                             "Creates a starting point you can commit and reuse in TOML mode. "
                             "Exact tag specs (Category:Value) are written as [[include.tags]] blocks; "
                             "partial specs are written as commented stubs. "
                             "Replace any literal credentials in header values with ${ENV_VAR} references "
                             "before storing the file.")

    p.add_argument("--connector-id", metavar="ID", type=int, default=None,
                   help="[OPTIONAL — only needed when updating an existing connector] "
                        "Without this flag (and no connector.id in the TOML), every run generates a "
                        "POST (CREATE) payload for a new connector. "
                        "Set this only when updating an existing connector: PUT (UPDATE) mode — "
                        "fetches the existing connector by ID, merges your TOML changes on top, "
                        "and includes 'id' in the output JSON. "
                        "Overrides connector.id in the TOML when both are present and equal; "
                        "conflicting values (different IDs) are rejected.")
    p.add_argument("--org", metavar="ORG_ID_OR_NAME", default=None,
                   help="Restrict processing to specific org(s). "
                        "Accepts a numeric org ID, a comma-separated list of org IDs, "
                        "or a name substring (case-insensitive, single org). "
                        "Overrides org= in the config file. "
                        "Useful when the API key has access to multiple orgs in the same hierarchy.")
    p.add_argument("--output", "-o", metavar="PREFIX",
                   help="Filename prefix for output files (default: connector)")
    p.add_argument("--stdout", action="store_true",
                   help="Write output to stdout instead of files")
    p.add_argument("--base-url", default=None,
                   help="AppNeta base URL (overrides config and APPNETA_BASE_URL env var)")
    p.add_argument("--lenient", action="store_true", default=False,
                   help="Continue and write output even if filter values cannot be resolved. "
                        "Default behaviour is to exit non-zero when any resolution error occurs.")
    p.add_argument("--dry-run", action="store_true", default=False,
                   help="Print all output — status, warnings, and generated JSON — to stdout. "
                        "No files are written. Useful for reviewing the full run interactively "
                        "before committing to a POST. (--stdout produces clean JSON-only output "
                        "for piping; --dry-run is for human review.)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # --dry-run: merge all output onto stdout for interactive review.
    # sys.stderr reassignment propagates to every file=sys.stderr call below.
    if args.dry_run:
        sys.stderr = sys.stdout
        args.stdout = True  # imply --stdout so JSON goes to stdout too

    _CLI_FILTER_FLAGS = (
        "include_mp_names", "include_mp_type", "include_policy",
        "include_policy_group", "include_tag",
        "exclude_mp_names", "exclude_mp_type", "exclude_policy",
        "exclude_policy_group", "exclude_tag",
    )

    if args.config is not None:
        # TOML mode — TOML is the permanent record; CLI filter flags are blocked.
        config, issues = precheck_toml(args.config)
        if issues:
            print_precheck_report(args.config, issues)
        if any(i["level"] == "ERROR" for i in issues) or config is None:
            sys.exit(1)

        cli_flags_used = [
            f"--{f.replace('_', '-')}"
            for f in _CLI_FILTER_FLAGS
            if getattr(args, f, None)
        ]
        if cli_flags_used:
            flag_list = ", ".join(cli_flags_used)
            sys.exit(
                "ERROR: CLI filter flags cannot be used with a TOML file.\n\n"
                f"  CLI flags used : {flag_list}\n\n"
                "  The TOML file is the permanent record of the connector configuration.\n"
                "  CLI filter flags are not captured in the TOML, so any run using them\n"
                "  cannot be reproduced from the TOML alone.\n\n"
                "  Add the filters to the TOML file's [include] or [exclude] section\n"
                "  and remove the CLI flags."
            )

        error_stem = os.path.splitext(os.path.basename(args.config))[0]
        error_source = args.config

    else:
        # CLI-only mode — all config from flags; filter flags are allowed.
        config = build_config_from_args(args)
        safe_name = (args.connector_name or "connector").replace(" ", "_").replace("/", "_")
        error_stem = safe_name
        error_source = f"CLI-only (connector: {args.connector_name})"

        if args.generate_toml and args.dry_run:
            print(f"[dry-run] would generate TOML config: {args.generate_toml}", file=sys.stderr)
        elif args.generate_toml:
            if os.path.exists(args.generate_toml):
                print(f"[!] Overwriting existing file: {args.generate_toml}", file=sys.stderr)
            toml_str = generate_toml_content(args)
            with open(args.generate_toml, "w") as fh:
                fh.write(toml_str)
            print(f"[+] Generated TOML config: {args.generate_toml}", file=sys.stderr)
            print("    Review the file, replace any credential values with ${ENV_VAR}",
                  file=sys.stderr)
            print("    references, then add filter blocks if needed.", file=sys.stderr)

    if args.connector_id is not None:
        toml_connector_id = config.get("connector", {}).get("id")
        if toml_connector_id is not None and toml_connector_id != args.connector_id:
            sys.exit(
                f"ERROR: Conflicting connector IDs.\n\n"
                f"  TOML connector.id  : {toml_connector_id}\n"
                f"  --connector-id flag: {args.connector_id}\n\n"
                "  Remove connector.id from the TOML and use --connector-id only,\n"
                "  or remove --connector-id and let the TOML control the target."
            )
        config.setdefault("connector", {})["id"] = args.connector_id

    validate_config(config)

    # Validate base_url type before merging (TOML could provide a non-string value)
    config_base_url = config.get("base_url")
    if config_base_url is not None and not isinstance(config_base_url, str):
        sys.exit(
            f"ERROR: base_url in config must be a string, "
            f"got {type(config_base_url).__name__}: {config_base_url!r}"
        )

    # Resolve base_url: CLI flag > config file > environment variable
    args.base_url = (
        args.base_url
        or config_base_url
        or os.environ.get("APPNETA_BASE_URL")
    )
    if args.base_url:
        _validate_base_url(str(args.base_url))

    if not validate_environment(args, config):
        sys.exit(1)

    api_key = os.environ.get("APPNETA_API_KEY", "")
    base_url = str(args.base_url)  # non-None guaranteed by validate_environment

    orgs = AppNetaClient.list_organizations(base_url, api_key)

    # Multi-hierarchy key check — on the full org set, before any --org restriction,
    # so naming one org does not make such a key acceptable.
    roots = _root_orgs(orgs)
    if len(roots) > 1:
        _exit_multi_hierarchy_key(orgs, roots)

    # Org restriction: CLI --org takes precedence over TOML org=
    restrict_spec = args.org if args.org is not None else config.get("org")

    if restrict_spec is not None:
        # Parse specs: CLI is a comma-separated string; TOML org= can be int or list of ints
        if isinstance(restrict_spec, list):
            specs: list = restrict_spec
        else:
            specs = [s.strip() for s in str(restrict_spec).split(",") if s.strip()]

        matched, res_errors = _resolve_orgs(orgs, specs)

        if res_errors:
            tree = _build_org_tree(orgs)
            sys.exit(
                "ERROR: Could not resolve all org specifiers:\n"
                + "".join(f"  - {e}\n" for e in res_errors)
                + f"\n  Accessible organizations ({len(orgs)}):\n"
                + tree
            )

        ok, msg = _validate_same_hierarchy(matched, orgs)
        if not ok:
            sys.exit(f"ERROR: {msg}")

        orgs = matched
    # else: no restriction — proceed with all orgs returned for this key

    # Enforce single-org for UPDATE mode — connector IDs are org-scoped.
    connector_id_in_config = config.get("connector", {}).get("id")
    if connector_id_in_config is not None and len(orgs) > 1:
        org_list = "\n".join(f"    {o['name']} (id={o['id']})" for o in orgs)
        sys.exit(
            f"ERROR: connector.id = {connector_id_in_config} is set (UPDATE mode), "
            f"but the key resolves to {len(orgs)} organizations:\n"
            f"{org_list}\n\n"
            f"  A connector ID is org-specific — the same ID does not exist in every org.\n"
            f"  Use --org to target the one org that owns this connector.\n"
            f"  Example: --org {orgs[0]['id']}"
        )

    print(
        f"Found {len(orgs)} organization(s): "
        + ", ".join(f"{o['name']} (id={o['id']})" for o in orgs),
        file=sys.stderr,
    )
    clients = [
        AppNetaClient(base_url, api_key, org["id"], org["name"])
        for org in orgs
    ]

    prefix = re.sub(r"\.json$", "", args.output) if args.output else "connector"
    all_errors: list[dict] = []
    results: list[tuple[AppNetaClient, dict]] = []
    for client in clients:
        print(f"Processing org: {client.org_name} (id={client.org_id})", file=sys.stderr)
        payload = build_payload(config, args, client, all_errors)

        mode, connector_id, existing = resolve_connector_mode(client, config)
        if mode == "PUT":
            payload["id"] = connector_id
            print(f"  Mode   : UPDATE (connector.id={connector_id})", file=sys.stderr)
            _require_existing(existing, connector_id, client.org_name, args.lenient)
            if existing is None:
                print("  (existing connector not readable — merge and diff unavailable)", file=sys.stderr)
            else:
                if not args.dry_run:
                    backup_path = _write_backup(existing, connector_id)
                    print(f"  Backup : {backup_path} (credential header values are masked as ****** by AppNeta — restore them manually before reusing)", file=sys.stderr)
                payload = merge_with_existing(payload, existing, all_errors, client.org_name)
                diff = diff_connector(existing, payload)
                if diff:
                    print("  Changes vs. existing connector:", file=sys.stderr)
                    for line in diff:
                        print(f"    {line}", file=sys.stderr)
                else:
                    print("  No changes vs. existing connector.", file=sys.stderr)
            print(f"  PUT URL: {base_url}/api/v4/connector/alarm/{connector_id}?orgId={client.org_id}", file=sys.stderr)
        else:
            print(f"  Mode   : CREATE (no connector.id — POST to create new)", file=sys.stderr)
            print(f"  POST URL: {base_url}/api/v4/connector/alarm?orgId={client.org_id}", file=sys.stderr)

        results.append((client, payload))

    contradictions = [e for e in all_errors if e.get("type") == "contradiction"]
    resolution_errors = [e for e in all_errors if e.get("type") != "contradiction"]

    if contradictions:
        _yellow = "\033[33m" if sys.stderr.isatty() else ""
        _reset = "\033[0m" if sys.stderr.isatty() else ""
        print(
            f"\n{_yellow}NOTE: {len(contradictions)} filter value(s) appeared in both include and exclude "
            f"— exclude wins; removed from include.{_reset}",
            file=sys.stderr,
        )
        for e in contradictions:
            print(f"  {e.get('type', '')} · {e.get('value', '')} — {e.get('note', '')}", file=sys.stderr)

    if resolution_errors:
        n = len(resolution_errors)
        _red = "\033[31m" if sys.stderr.isatty() else ""
        _reset = "\033[0m" if sys.stderr.isatty() else ""
        print(
            f"\n{_red}WARNING: {n} filter value(s) could not be resolved and were omitted from the payload.{_reset}",
            file=sys.stderr,
        )
        if not args.dry_run:
            write_errors(resolution_errors, error_stem, error_source)
        if not args.lenient:
            print(
                f"{_red}Exiting without output — fix the resolution error(s) above and re-run.{_reset}\n"
                "Use --lenient to generate output despite unresolved filter values.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Only flag an unfiltered payload as an error when the user intended to set filters
    # (TOML had [include]/[exclude] blocks that all resolved to nothing).
    # A TOML with no filter blocks at all is a deliberate baseline connector — let it through.
    _user_intended_filters = (
        bool(config.get("include") or config.get("exclude"))
        or any(getattr(args, f, None) for f in _CLI_FILTER_FLAGS)
    )
    unfiltered = [c.org_name for c, p in results if "include" not in p and "exclude" not in p]
    if unfiltered and _user_intended_filters:
        _red = "\033[31m" if sys.stderr.isatty() else ""
        _reset = "\033[0m" if sys.stderr.isatty() else ""
        orgs = ", ".join(unfiltered)
        print(
            f"\n{_red}WARNING: Filter blocks present but produced no criteria — this "
            f"connector will forward ALL alarms ({orgs}).{_reset}",
            file=sys.stderr,
        )
        if not args.lenient:
            print(
                f"{_red}Exiting without output — check that your [include]/[exclude] "
                "values resolve against the live API.\n"
                f"Use --lenient to generate the unfiltered payload anyway.{_reset}",
                file=sys.stderr,
            )
            sys.exit(1)

    emptied = _orgs_with_emptied_include(results, all_errors)
    if emptied:
        _red = "\033[31m" if sys.stderr.isatty() else ""
        _reset = "\033[0m" if sys.stderr.isatty() else ""
        print(
            f"\n{_red}WARNING: every include criterion was also excluded, so the include block is empty — "
            f"for {', '.join(emptied)} this connector forwards everything EXCEPT the exclude list.{_reset}",
            file=sys.stderr,
        )
        if not args.lenient:
            print(f"{_red}Exiting without output — fix the overlap between include and exclude.\n"
                  f"Use --lenient to generate it anyway.{_reset}", file=sys.stderr)
            sys.exit(1)

    if args.stdout:
        for client, payload in results:
            print(json.dumps(payload, indent=2))
    else:
        for client, payload in results:
            filename = f"{prefix}_{client.org_id}_{safe_filename_part(client.org_name)}.json"
            write_file(filename, json.dumps(payload, indent=2))
            print(f"  Written: {filename}", file=sys.stderr)


if __name__ == "__main__":
    main()
