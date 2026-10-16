# generate_connector(1) — AppNeta Alarm Connector JSON Generator

## NAME

`generate_connector.py` — generate the JSON payload for an AppNeta alarm connector webhook

## SYNOPSIS

```
python generate_connector.py <config> [options]
python generate_connector.py <config> [options] -o <prefix>
python generate_connector.py <config> [options] --stdout
```


## BEFORE YOU BEGIN

The following environment variables **must be exported in your shell before running the script**. The script will exit immediately with an error if they are not set.

```bash
# Required — AppNeta API key for name-to-ID resolution:
export APPNETA_API_KEY=<your-appneta-api-token>

# Required only if your webhook uses BEARER authentication:
export CUSTOMER_WEBHOOK_TOKEN=<your-webhook-bearer-token>

# Required only if your webhook uses BASIC authentication:
export CUSTOMER_WEBHOOK_PASSWORD=<your-webhook-password>
```

These values must **never** be stored in the config file. See [ENVIRONMENT](#environment) for full details on each variable.

---

## DESCRIPTION

`generate_connector.py` reads a TOML config file describing an alarm connector, calls the AppNeta API to resolve human-readable names (monitoring points, policies, policy groups) to numeric IDs, and writes the JSON payload suitable for `POST /api/v4/connector/alarm` (see [Alarm connectors](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html)).

At startup the script validates the config file, checks required fields, validates the base URL format, and confirms the API key is set before making any API calls.

The script:

1. Calls `GET /api/v4/organization` to discover all organizations accessible to the key.
2. If `--org` or `org=` in the config is set, restricts processing to specific org(s) within that hierarchy.
3. For each organization, resolves any names in the config or `--include-*` / `--exclude-*` flags to IDs scoped to that org.
4. Writes one payload file per organization.

A key scoped to a single organization produces one output file with no additional flags required. A key with access to a parent org and its children processes all orgs in that hierarchy (optionally restricted with `--org`). **Account-wide (superadmin) keys are not supported** — the script exits with an error if one is detected.

If any monitoring point name/ID, policy name/ID, or policy group name/ID cannot be matched, the item is omitted from the payload and all such errors are written to `<config-stem>-connector-error.txt`. The error file lists exactly what was not found and what options are available, so you can correct your config or flags and rerun.

Policy groups are resolved via the dedicated `GET /v4/monitoringPolicyGroup` endpoint, which returns each group with its own int64 global ID. When a policy group name is specified in the config or via `--include-policy-group` / `--exclude-policy-group`, the script resolves it to the group's int64 global ID and emits that as `monitoringPolicyGroupId` in the output payload. Numeric IDs (int64 global) may also be supplied directly.

## INSTALLATION

`generate_connector.py` is a single file. Download or clone this repository, then run it with Python from that directory:

```bash
python3 generate_connector.py --help
```

## REQUIREMENTS

Python 3.11 or higher is required. `tomllib` (TOML support) and `urllib` (HTTP) are both part of the Python 3.11 standard library. No external packages are needed.

Python 3.11 has been available since October 2022. To check your version:

```bash
python3 --version
```

## API KEY SCOPE

The script uses the number of **root-level orgs** returned by `GET /api/v4/organization` — orgs whose `parentId` is not itself in the result set — to determine the key type and whether it is permitted to proceed.

| Key type | Root orgs | Behavior |
|---|---|---|
| **Org-level key** (scoped to one org) | 1 root, no children | Proceeds automatically — no `--org` needed |
| **Org-admin key** (parent + children) | 1 root + children | Proceeds automatically — all orgs in the hierarchy are processed |
| **Superadmin key** (account-wide) | Multiple roots | **Not supported — exits with an error** |

**Superadmin keys are not supported.** An account-wide key has access to all customer organizations on the AppNeta NIS. Using one in an automated script or sharing it with other tools is a security risk. The script detects this condition and exits immediately with a clear message.

Create a **read-only API key scoped to the specific organization(s)** you want to configure. See [API access tokens](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-access-tokens.html) for instructions.

`--org` is **exact**: you get precisely the org(s) you specify — no automatic subtree expansion. Multiple IDs are accepted as a comma-separated list on the CLI or an array in the TOML config, but all specified orgs must belong to the same customer hierarchy (share a common root); cross-hierarchy combinations are rejected.

```bash
# Restrict to a single org (useful for org-admin keys that cover multiple child orgs):
python generate_connector.py config.toml --org 4160

# Parent + specific child (must share a root):
python generate_connector.py config.toml --org 4160,4275

# Select by name substring (single org, unambiguous):
python generate_connector.py config.toml --org "Carousel"
```

```toml
# In the config file (before [connector]):
org = 4160              # single org ID
org = [4160, 4275]      # multiple org IDs (must share a root)
org = "Carousel"        # name substring (single org, unambiguous)
```

## UPDATE vs CREATE mode

When `connector.id` is set in the config (or `--connector-id` is passed on the CLI), the script operates in **UPDATE mode**:

1. **Enforces single-org** — if the key resolves to more than one organization, the script exits immediately with a clear error identifying each org and showing the correct `--org` flag to use. A connector ID is org-specific; the same ID does not exist in multiple orgs.
2. Fetches the existing connector from AppNeta via a read-only GET.
3. Merges your TOML changes on top — fields you specify in the TOML override the existing connector; fields absent from your TOML are preserved as-is from the existing connector.
4. Outputs the merged payload (with `"id"` included) suitable for `PUT /api/v4/connector/alarm`.
5. Shows a diff on stderr of exactly what would change.
6. Prints the PUT URL to stderr: `PUT {base_url}/api/v4/connector/alarm/{id}?orgId={org_id}`

This means **UPDATE is always a partial change**. You only need to specify what you want to add or modify. Example: if your TOML sets `include.severity` but the existing connector also has `include.tag` values, both will appear in the output.

> **To remove a filter field entirely** (e.g. clear all severity filters, or remove all include tags), you cannot use UPDATE mode. Instead: delete the connector via the AppNeta UI or API, then submit a fresh CREATE payload (POST) generated without `connector.id`.

When `connector.id` is absent, the script operates in **CREATE mode**: the payload is built entirely from the TOML and flags, with no existing connector fetched. The POST URL for each org is printed to stderr: `POST {base_url}/api/v4/connector/alarm?orgId={org_id}`.

---

## OPTIONS

| Flag | Description |
|---|---|
| `config` | Path to a TOML config file (`.toml`, required) |
| `--org IDS_OR_NAME` | Restrict processing to specific org(s) within the key's hierarchy. Accepts a single numeric org ID, a comma-separated list of org IDs, or a name substring (case-insensitive, single org only). **Exact** — no automatic subtree expansion. Multiple IDs must all share the same root hierarchy. Overrides `org=` in the config file. |
| `-o`, `--output PREFIX` | Filename prefix for output files. Default: `connector`. Output files are named `<prefix>_<orgId>_<orgName>.json`. A trailing `.json` on the prefix is stripped automatically. |
| `--stdout` | Write the generated JSON payload to stdout. All status, warnings, and diagnostics go to stderr. Safe to pipe directly to `curl` or another command — stderr never contaminates the JSON stream. |
| `--dry-run` | Print everything — pre-flight status, org info, warnings, and generated JSON — to stdout. No files are written. Use this to review the full run interactively before committing to a POST. (`--stdout` is for piping; `--dry-run` is for human review.) |
| `--lenient` | Continue and write output even if filter values (MP names, tags, policies) cannot be resolved. Default behaviour is to exit non-zero and produce no output when any resolution error occurs — use `--lenient` to override this when a partial payload is acceptable. |
| `--base-url URL` | AppNeta base URL. Overrides the config file and `APPNETA_BASE_URL`. Required if not set in the config or environment. |
| `--connector-id ID` | **Optional — only set this when updating an existing connector.** Without this flag (and no `connector.id` in the TOML), every run generates a POST (CREATE) payload. When set: PUT (UPDATE) mode — fetches the existing connector, merges TOML changes on top, includes `"id"` in the output JSON. Overrides `connector.id` in the TOML when both are present; conflicting values (different IDs) are rejected. See **UPDATE vs CREATE mode** above. |
| `--include-mp-names NAMES` | CLI-only mode: comma-separated monitoring point names, IDs or glob patterns to include. Companion flags exist for the other filters (`--include-mp-type`, `--include-policy`, `--include-policy-group`, `--include-tag`, and the matching `--exclude-*` flags). |
| `--webhook-ignore-ssl` | CLI-only mode: tell AppNeta to ignore SSL certificate errors for the webhook endpoint (`ignoreSSLErrors`). Leave it off when the endpoint presents a publicly trusted certificate. |
| `--generate-toml FILENAME` | CLI-only mode: write the current flags to a TOML config file you can review, commit and reuse in TOML mode. Replace any literal credentials in header values with `${ENV_VAR}` references before storing the file. |

## TOML mode vs CLI-only mode

The script operates in one of two modes depending on whether a TOML file is provided.

| | TOML mode | CLI-only mode |
|---|---|---|
| **How to invoke** | `generate_connector.py config.toml [flags]` | `generate_connector.py [flags]` (no positional arg) |
| **Connector identity** | `[connector]` block in TOML | `--connector-name`, `--connector-description` |
| **Webhook** | `[webhook]` block in TOML | `--webhook-url`, `--webhook-method`, `--webhook-auth-type`, `--webhook-header`, `--notify-on` |
| **Filters** | `[include]` / `[exclude]` blocks in TOML | `--include-*` / `--exclude-*` flags |
| **Partial tag specs** | Not supported in TOML — both `category` and `value` required | `--include-tag "Location"` or `--include-tag ":Seattle WA"` |
| **CLI filter flags** | **Blocked** — error if any `--include-*`/`--exclude-*` are used | Allowed |
| **Permanent record** | Yes — TOML captures the full configuration; repeatable | No — run cannot be reproduced from any single file |
| **PUT (UPDATE) mode** | `connector.id` in TOML, or `--connector-id` flag | `--connector-id` flag only |
| **When to use** | Production connectors, documented deployments, repeatable builds | Exploration, testing filter shapes, one-off runs |

### TOML mode example

```bash
export APPNETA_API_KEY=<key>
export SPLUNK_HEC_TOKEN=<token>
python generate_connector.py samples/01-critical-only.toml --stdout
```

Everything in `samples/01-critical-only.toml`: connector name, webhook URL, headers, include/exclude filters. Fully repeatable from the file alone.

### CLI-only mode example

```bash
export APPNETA_API_KEY=<key>
export CUSTOMER_WEBHOOK_TOKEN=<token>
python generate_connector.py \
    --connector-name "GCP Alarms — Test" \
    --webhook-url "https://hec.example.com/services/collector/raw" \
    --webhook-auth-type BEARER \
    --webhook-header "Content-Type: application/json" \
    --include-tag "Cloud:GCP" \
    --include-mp-type "m50" \
    --base-url "https://app-01.pm.appneta.com" \
    --dry-run
```

Nothing persisted — use `--dry-run` to inspect the output, then build a TOML from it for production use.

## ENVIRONMENT

| Variable | Description |
|---|---|
| `APPNETA_API_KEY` | AppNeta API token. Required — the script exits if not set. |
| `APPNETA_BASE_URL` | AppNeta base URL fallback. Used only if `base_url` is not set in the config file and `--base-url` is not passed on the CLI. |
| `CUSTOMER_WEBHOOK_TOKEN` | Bearer token for the customer's webhook endpoint. Required when `webhook.auth.type = BEARER`. Must not be stored in the config file. |
| `CUSTOMER_WEBHOOK_PASSWORD` | Password for the customer's webhook endpoint. Required when `webhook.auth.type = BASIC`. Must not be stored in the config file. |
| *(any name)* | Custom variables referenced in header values via `${VAR_NAME}`. Export before running — the script exits if a referenced variable is unset. |

## CONFIG FILE

The config file must be TOML (`.toml`). TOML supports inline comments, which makes configs easier to document and maintain, and its structure maps cleanly to the connector payload without being confused with the output JSON itself.

### Top-level keys

| Key | Type | Description |
|---|---|---|
| `base_url` | string | AppNeta base URL (e.g. `https://app-01.pm.appneta.com`). Must be an HTTPS URL — the script exits if it is not. Required unless set via `--base-url` or `APPNETA_BASE_URL`. |
| `org` | integer, list of integers, or string | Optional org restriction. Integer org ID, `[id1, id2]` list of org IDs, or a name substring string (case-insensitive, single org only). **Exact** — no automatic subtree expansion. Multiple IDs must share the same root hierarchy. Overridden by `--org` on the CLI. |
| `connector.id` | integer | Optional int64 connector ID. If set (or supplied via `--connector-id`), triggers UPDATE mode: the script fetches the existing connector via GET, merges your TOML changes on top, and includes `"id"` in the output JSON (valid for PUT). If absent, generates a full CREATE payload (valid for POST). See **UPDATE vs CREATE mode** below. |
| `connector.name` | string | Name for the alarm connector (required). Used to detect existing connectors when `connector.id` is not set. |
| `connector.description` | string | Optional description |
| `connector.enabled` | bool | Whether the connector is active. Default: `true` |
| `notify_on` | list | Events that trigger the webhook. Valid values: `ALARM_RAISED`, `ALARM_CLEARED`. Default: both. |
| `include` | object | Filter criteria. Only alarms matching **all** populated fields are forwarded. |
| `exclude` | object | Suppression criteria. Alarms matching **any** populated field are dropped. |
| `webhook` | object | Webhook destination settings. `webhook.url` is required — script exits if missing. |

### Filter fields (`include` and `exclude`)

Both `include` and `exclude` accept the same set of fields. Omitting a field or leaving its list empty means "no restriction on this field."

| Field | Type | Description |
|---|---|---|
| `severity` | list | `CLEARED`, `CRITICAL`, `INDETERMINATE`, `MAJOR`, `MINOR`, `WARNING`. Script exits on invalid values. |
| `noise_filter` | list | `FREQUENT` (only valid value per API spec). Script exits on invalid values. |
| `monitoring_point_names` | list | MP names or int64 global IDs. Plain names and IDs are resolved to int64 global IDs via API. Values containing any glob character (`*`, `**`, `+`, `?`, `!`) are passed through to the API as-is via the `monitoringPointName` field — the script does not interpret them. Example: `ACME-Lab-*`. |
| `monitoring_point_ids` | int or list | Dedicated numeric-only field for MP v4 global IDs — direct passthrough (no API lookup), emitted as `monitoringPointId`. Prefer this over an ID embedded in `monitoring_point_names` when you already have the ID — it's unambiguous, and avoids mixing an ID and a name for the same MP in one filter block (see the ID substitution defect noted below). Accepts a single integer or a list; merged with any IDs already contributed by `monitoring_point_names`. |
| `monitoring_point_types` | list | MP type substrings, built-in aliases, or glob patterns (`*`, `**`, `+`, `?`, `!`). Resolution order: (1) built-in alias expansion (see below), (2) case-insensitive substring match against live type names from `/v4/monitoringPoint` — matched full name emitted (e.g. `m50` → `m50 microAppliance`, `c50` → `c50 Container`), (3) glob values passed through to the API as-is. Substring expansion is limited to types present in the org's current MPs — use the exact full type name or a glob for types not currently deployed. |
| `monitoring_policy_names` | list | Policy names or int64 global IDs resolved via `/v4/monitoringPolicy`. |
| `monitoring_policy_ids` | int or list | Dedicated numeric-only field for policy v4 global IDs, same rationale as `monitoring_point_ids` above. Emitted as `monitoringPolicyId`. |
| `monitoring_policy_group_names` | list | Policy group names or int64 global IDs resolved via `/v4/monitoringPolicyGroup`. Emitted as `monitoringPolicyGroupId` in the payload. |
| `monitoring_policy_group_ids` | int or list | Dedicated numeric-only field for policy group v4 global IDs, same rationale as `monitoring_point_ids` above. Emitted as `monitoringPolicyGroupId`. |
| `alarm_rule_ids` | list | Alarm rule numeric IDs (e.g. `105`). **Must be obtained from the AppNeta GUI** (Alarms → Alarm Rules) — no API endpoint is available to list or resolve them. Non-numeric values are skipped with a warning. |
| `targets` | list | Target hostnames or URLs. Values are passed through to the API as-is — the script does not interpret them. The API accepts glob patterns (`*`, `**`, `+`, `?`, `!`). |
| `tags` | list | Tag objects with `category` and `value` fields. In TOML, use `[[include.tags]]` blocks. CLI equivalents: `--include-tag` / `--exclude-tag`. |

### Mixing an ID and a name for the same resource

If a filter block contains both `monitoringPointId` (a v4 global ID) and `monitoringPointName` for the *same* monitoring point, the API's response can show that resource's **v3 (org-scoped) ID** instead of the v4 global ID that was submitted. The filter itself is applied using the ID you submitted; only the echoed value differs.

**Practical guidance:** when using `monitoring_point_ids` / `monitoring_policy_ids` / `monitoring_policy_group_ids`, avoid also specifying the same resource by name in `monitoring_point_names` / `monitoring_policy_names` / `monitoring_policy_group_names` in the same filter block. The echoed value is harmless, but it is confusing when reviewing what was submitted.

### Monitoring point type aliases

The following short names are recognized as built-in aliases and expanded before any API lookup. They exist because the API does not use the hardware model name as the type string for software agents.

| Alias | Expands to | Notes |
|---|---|---|
| `n10` | `Windows`, `macOS` | AppNeta n10 software agent — the API represents it as two separate type strings based on host OS |

### Webhook fields

| Field | Type | Description |
|---|---|---|
| `url` | string | Webhook endpoint URL (required). Must be URL-encoded — unencoded spaces and reserved characters are rejected by the API. |
| `method` | string | HTTP method. Default: `POST`. Supported: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`. Script exits on any other value. |
| `headers` | list | List of `{key, value}` HTTP header objects. In TOML, use `[[webhook.headers]]` blocks. Header values support `${ENV_VAR}` substitution — see below. |
| `auth.type` | string | `NONE`, `BASIC`, or `BEARER`. The script exits on any other value. (`NONE` omits the auth block entirely.) |
| `auth.username` | string | Used with `BASIC` auth. Not a secret — may be stored in the config file. |
| `auth.password` | — | Must NOT be set in config. Set `CUSTOMER_WEBHOOK_PASSWORD` env var instead. |
| `auth.token` | — | Must NOT be set in config. Set `CUSTOMER_WEBHOOK_TOKEN` env var instead. |
| `ignore_ssl_errors` | bool | Skip TLS certificate verification. Default: `false` |

> **Security note:** Passwords and tokens must not be stored in config files — the script exits with an error if they are. Export `CUSTOMER_WEBHOOK_TOKEN` or `CUSTOMER_WEBHOOK_PASSWORD` before running, the same way `APPNETA_API_KEY` is handled.

#### Environment variable substitution in header values

Any header value may reference environment variables using `${VAR_NAME}` syntax. The script substitutes the variable at runtime and exits with a clear error if it is not set. The precheck warns if a referenced variable is not set when the script starts.

```toml
[[webhook.headers]]
key   = "Authorization"
value = "Splunk ${SPLUNK_HEC_TOKEN}"
```

**When to use headers instead of the `auth` block:** The built-in `auth` block supports `BASIC` (username + password) and `BEARER` (token with a standard `Authorization: Bearer <token>` header). Use a custom header with `${ENV_VAR}` substitution when the endpoint requires a non-standard Authorization format that the built-in types cannot express. Splunk HEC is the primary example — it requires `Authorization: Splunk <token>` rather than `Authorization: Bearer <token>`. In that case set `auth.type = "NONE"` and supply the Authorization header directly:

```toml
[[webhook.headers]]
key   = "Authorization"
value = "Splunk ${SPLUNK_HEC_TOKEN}"

[webhook.auth]
type = "NONE"
```

Export the token before running:

```bash
export SPLUNK_HEC_TOKEN=<your-hec-token>
```

## INCLUDE/EXCLUDE FILTER BEHAVIOR

AppNeta evaluates include and exclude filter blocks independently when deciding whether to forward an alarm to a connector. Understanding how they interact is essential for getting the expected behavior.

### Include is a strict whitelist

If **any** field in the `include` block is populated, AppNeta uses it as a whitelist: **only alarms from MPs matching the include criteria are forwarded.** Any MP that does not match is silently dropped — regardless of what the `exclude` block contains.

```toml
[include]
monitoring_point_types = ["m50"]
# Only m50 microAppliance alarms are delivered.
# All c50, macOS, Windows, and other types are dropped — even if exclude is empty.
```

An empty `include` block (all fields omitted or empty) means no restriction — all alarms pass through to the exclude stage.

### Exclude is an additive blacklist

Fields in the `exclude` block suppress alarms that match. An alarm matching **any** populated exclude field is dropped. Non-matching alarms pass through unchanged.

```toml
[exclude]
monitoring_point_types = ["macOS"]
# macOS MP alarms are dropped.
# m50, c50, Windows, and all other types pass through.
```

An empty `exclude` block suppresses nothing.

### Exclude overrides include

If an MP passes the include filter but also matches an exclude rule, the alarm is **dropped**. Exclude always wins.

This lets you select a broad class of MPs with include and carve out specific MPs with exclude:

```toml
[include]
monitoring_point_types = ["c50"]   # all c50 containers pass the include stage

[exclude]
monitoring_point_names = ["branch-office-c50"]  # this specific c50 is still dropped
```

Even though `branch-office-c50` is a c50 and would normally pass the type include, the ID-based exclude suppresses it.

### Combined include and exclude — evaluation order

AppNeta applies filters in sequence:

1. **Include filter** — MPs not matching the include criteria are dropped
2. **Exclude filter** — MPs matching any exclude rule are dropped from what remains
3. Alarms that survive both stages are forwarded to the webhook

### MP type filter values

MP type filter values use AppNeta's full display names, not short model codes. The built-in `n10` alias (expanded by this script before writing the payload) covers both macOS and Windows software agents, since AppNeta treats them as two separate types.

Type strings sourced from `GET /v4/monitoringPoint` across AppNeta platform hosts. Substring matching resolves against the org's live MP list — use the exact full type name or a glob for types not currently deployed.

| Config value / alias | Emitted in payload | Device |
|---|---|---|
| `m20` (substring) | `m20 microAppliance` | AppNeta m20 hardware appliance |
| `m22` (substring) | `m22 microAppliance` | AppNeta m22 hardware appliance |
| `m25` (substring) | `m25 microAppliance` | AppNeta m25 hardware appliance |
| `m30` (substring) | `m30 microAppliance` | AppNeta m30 hardware appliance |
| `m35` (substring) | `m35 microAppliance` | AppNeta m35 hardware appliance |
| `m50` (substring) | `m50 microAppliance` | AppNeta m50 hardware appliance |
| `m70` (substring) | `m70 microAppliance` | AppNeta m70 hardware appliance |
| `r40` (substring) | `r40 rackAppliance` | AppNeta r40 rack appliance |
| `r45` (substring) | `r45 rackAppliance` | AppNeta r45 rack appliance |
| `r90` (substring) | `r90 rackAppliance` | AppNeta r90 rack appliance |
| `r1000` (substring) | `r1000 rackAppliance` | AppNeta r1000 rack appliance |
| `v30` (substring) | `v30 virtualAppliance` | AppNeta v30 virtual appliance |
| `v35-KVM` (substring) | `v35-KVM virtualAppliance` | AppNeta v35 virtual appliance (KVM) |
| `v35-VMware` (substring) | `v35-VMware virtualAppliance` | AppNeta v35 virtual appliance (VMware) |
| `c50` (substring) | `c50 Container` | AppNeta c50 container agent |
| `cpe40` (substring) | `cpe40 Container` | AppNeta cpe40 container agent |
| `macOS` | `macOS` | AppNeta software agent on macOS |
| `Windows` | `Windows` | AppNeta software agent on Windows |
| `NetOps Data Collector` | `NetOps Data Collector` | AppNeta NetOps data collector |
| `Global Monitoring Point` | `Global Monitoring Point` | AppNeta global monitoring point |
| `n10` (alias) | `macOS`, `Windows` | Both software agent types (n10 hardware alias) |

Note: r-series appliances use the `rackAppliance` suffix, not `microAppliance`. The v35 virtual appliance is differentiated by hypervisor — `v35` as a substring matches both KVM and VMware variants if both are present in the org's MPs.

Substring matching is performed against the live MP list for the org. Use the exact full type name, the built-in alias, or a glob pattern for types not currently deployed.

**Important — filter type vs. alarm payload tag:** The `monitoringPointType` filter matches AppNeta's **agent classification**, not the `MP Type` tag delivered in alarm webhook payloads. An MP running a c50 agent on an m50 hardware box will match `"c50 Container"` in the filter, but its alarm payload will carry `"MP Type: m50"` in the tags array. Use the MP's agent type in the AppNeta UI to determine which filter value applies — do not infer it from alarm payload tags.

**Important — include filter logic across types is OR:** When multiple filter types are specified in an include block (e.g. `monitoring_point_types` and `tags`), AppNeta applies OR logic — an alarm passes if it matches any of the specified criteria. AND logic across filter types is not supported by the connector API.

---

## Glob Patterns

The `monitoring_point_names`, `monitoring_point_types`, and `targets` filter fields accept glob patterns. Values containing any glob character (`*`, `**`, `?`, `+`, `!`) are passed to the AppNeta API as-is — the script does not expand or interpret them.

| Character | Meaning | Example | Matches |
|---|---|---|---|
| `*` | Any sequence of characters | `ac*` | `acme-m50`, `ac-lab-01` |
| `**` | Same as `*` for flat strings | `ac**` | Same as `ac*` |
| `?` | Exactly one character | `?v-trident` | `sv-trident` |
| `!` | **No effect — does not exclude anything.** | `!acme-m50` | Nothing — passes through unchanged, as if `!acme-m50` weren't in the list at all |
| `+` | One or more characters (not reliably supported; prefer `*`) | `ac+` | Intended: names starting with ac |

Compound patterns combining multiple wildcards are supported: `a*-m?0` matches `acme-m50`.

**TOML examples:**

```toml
[include]
# Match all MPs whose names start with sw
monitoring_point_names = ["sw*"]

# Match all hardware appliances using type glob
monitoring_point_types = ["* microAppliance"]

# Match all container agents
monitoring_point_types = ["*Container"]

# Match targets on a specific domain
targets = ["*.example.com"]

# Compound: s + anything + -m + one char + 0
monitoring_point_names = ["a*-m?0"]
```

For `monitoring_point_types`, plain substrings (e.g. `m50`) are resolved against the org's live MP list first. If no match is found in the live list, the value is checked for glob characters — if none, an error is reported. Use glob syntax explicitly when targeting types not currently deployed in the org.

---

## SECURITY NOTES

- **No superadmin keys.** Account-wide (superadmin) API keys are not supported and will be rejected. Use a read-only key scoped to the specific organization(s) you are configuring. See [API access tokens](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-access-tokens.html).
- **HTTPS required.** `base_url` must be a valid HTTPS URL pointing to a hostname — the script exits if it is not. IP address literals are also rejected.
- **Output file permissions.** All output files (`.json` and `-connector-error.txt`) are written with mode `0o600` (owner read/write only). This protects payloads that may contain auth tokens or webhook credentials.
- **Credentials in config.** `webhook.auth.token` and `webhook.auth.password` must not be stored in the config file — the script exits with an error if they are. Set `CUSTOMER_WEBHOOK_TOKEN` (BEARER auth) or `CUSTOMER_WEBHOOK_PASSWORD` (BASIC auth) as environment variables, consistent with how `APPNETA_API_KEY` is handled.
- **API key scope.** Scope API keys to the minimum required organizations. A key with broad access exposes all org data to any process that can read `APPNETA_API_KEY`.
- **Request timeout.** All API requests time out after 30 seconds to prevent the script from hanging on an unresponsive server.

## OUTPUT

The script always writes named files — one per organization:

```
<prefix>_<orgId>_<orgName>.json
```

The prefix defaults to `connector`. Pass `-o myprefix` or `-o mydir/myprefix` to control the path and prefix. A trailing `.json` on the prefix is stripped automatically. All output files are written with permissions `0o600`.

Pass `--stdout` to write to stdout instead. When multiple organizations are resolved, each payload is printed to stdout in sequence.

Progress is written to stderr; only the JSON payload goes to stdout (when `--stdout` is used).

## ERROR FILE

If any item specified in a filter section (config or `--include-*` / `--exclude-*` flag) cannot be matched — whether by name or by numeric ID — the item is silently omitted from the payload and a `<config-stem>-connector-error.txt` file is written at the end of the run. The error file lists:

- The organization context
- The type of item that failed (monitoring point, policy, policy group, MP type)
- The value that was not found
- All available options for that type in the org

Example error file:

```
AppNeta Connector — Resolution Errors
Config   : config.toml
Generated: 2026-06-11 14:23:01

The following items were not found and were omitted from the payload.
Correct the values in your config or CLI flags and rerun.

──────────────────────────────────────────────────────────────────────
Org: Acme Corp (id=47291)
──────────────────────────────────────────────────────────────────────
  [monitoring point] 'ACME-Lab-Rocket' not found
    Available: ACME-MP-CanyonRoad, ACME-MP-MesaFlats, ACME-MP-PaintedDesert

  [policy group] 'Desert Labs' not found
    Available: Canyon Operations, Desert Laboratories, Southwest Desert
```

No error file is written when all items resolve successfully.

## EXAMPLES

### Typical run — write named file

```bash
export APPNETA_API_KEY=<token>
python generate_connector.py config.toml
# Writes: connector_47291_Acme_Corp.json
```

### Write to file with custom prefix

```bash
export APPNETA_API_KEY=<token>
python generate_connector.py config.toml -o output/prod
# Writes: output/prod_47291_Acme_Corp.json
```

### Write to stdout

```bash
export APPNETA_API_KEY=<token>
python generate_connector.py config.toml --stdout > connector.json
```

### Exclude monitoring points by name and type at runtime

```bash
python generate_connector.py config.toml \
    --exclude-mp-names "ACME-Lab-Rocket,ACME-Lab-Catapult" \
    --exclude-mp-type "c50,n10"
```

### Exclude an entire policy group

```bash
python generate_connector.py config.toml \
    --exclude-policy-group "Desert Labs"
```

The group name is resolved to its `monitoringPolicyGroupId` integer via the API. The output will contain `"monitoringPolicyGroupId": [366]` in the `exclude` block.

### Restrict to a single org (org-admin key covering multiple child orgs)

```bash
python generate_connector.py config.toml --org 4160
# Processes only: Carousel Industries (4160)
```

### Restrict to parent + specific child (must share a root)

```bash
python generate_connector.py config.toml --org 4160,4275
# Processes: Carousel Industries (4160) + Corporate (4275)
# Cross-hierarchy combinations are rejected
```

### Select by name substring (single org, unambiguous)

```bash
python generate_connector.py config.toml --org "Carousel"
# Matches "Carousel Industries" only — fails if substring matches multiple orgs
```

### Lock org restriction into the config file

```toml
org = 4160                   # single org only
# org = [4160, 4275]         # or multiple IDs (must share a root)
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "..."
```


---

## CONFIG FILE EXAMPLES

### Minimal TOML — severity filter and webhook only

```toml
base_url = "https://app-01.pm.appneta.com"

[connector]
name = "Roadrunner Alerts"

[include]
severity = ["CRITICAL", "MAJOR"]

[webhook]
url = "https://hooks.acme.corp/appneta"
method = "POST"

[[webhook.headers]]
key = "Content-Type"
value = "application/json"
```

### Full TOML — include and exclude filters, BEARER auth

```toml
base_url = "https://app-01.pm.appneta.com"

notify_on = ["ALARM_RAISED", "ALARM_CLEARED"]

[connector]
name = "Roadrunner Alert Feed"
description = "Forwards critical/major alarms, excluding desert lab probes"
enabled = true

[include]
severity = ["CRITICAL", "MAJOR"]
monitoring_policy_group_names = ["Southwest Desert", "Canyon Operations"]
monitoring_point_types = ["m50", "Windows"]

[[include.tags]]
category = "Region"
value = "Southwest Desert"

[exclude]
monitoring_policy_group_names = ["Desert Labs"]
monitoring_point_names = ["ACME-Lab-Rocket", "ACME-Lab-Catapult"]
noise_filter = ["FREQUENT"]

[webhook]
url = "https://hooks.acme.corp/roadrunner-alerts"
method = "POST"
ignore_ssl_errors = false

[[webhook.headers]]
key = "Content-Type"
value = "application/json"

[[webhook.headers]]
key = "X-Source"
value = "appneta"

[webhook.auth]
type = "BEARER"
# export CUSTOMER_WEBHOOK_TOKEN=<token>
```

### Splunk HEC — custom Authorization header with env var substitution

Splunk's HTTP Event Collector requires `Authorization: Splunk <token>`, which is not compatible with the built-in `BEARER` auth type. Use a custom header with `${ENV_VAR}` substitution and set `auth.type = "NONE"`:

```toml
base_url = "https://app-01.pm.appneta.com"

notify_on = ["ALARM_RAISED", "ALARM_CLEARED"]

[connector]
name = "Splunk HEC Feed"

[include]
severity = ["CRITICAL", "MAJOR"]

[webhook]
url    = "https://splunk.example.com/services/collector/raw?sourcetype=appneta%3Aalarm"
method = "POST"

[[webhook.headers]]
key   = "Content-Type"
value = "application/json"

[[webhook.headers]]
key   = "Authorization"
value = "Splunk ${SPLUNK_HEC_TOKEN}"

[webhook.auth]
type = "NONE"
```

```bash
export APPNETA_API_KEY=<appneta-token>
export SPLUNK_HEC_TOKEN=<hec-token>
python generate_connector.py splunk_hec_connector.toml
```

---

## WEBHOOK TARGETS

The connector delivers alarm payloads to any HTTPS endpoint that accepts `POST`. The table below covers platforms commonly deployed in enterprise environments. Sample TOML files are in `samples/`.

| Target | Sample file | Auth header | Notes |
|---|---|---|---|
| **Splunk HEC** | `07-splunk-hec.toml` | `Authorization: Splunk <token>` | `/services/collector/raw` for raw JSON |
| **ServiceNow** | `08-servicenow.toml` | `Authorization: Basic <base64>` | URL is customer-defined (Scripted REST API) |
| **OpsGenie** | `09-opsgenie.toml` | `Authorization: GenieKey <key>` | EU: `api.eu.opsgenie.com` |
| **Datadog Logs** | `10-datadog.toml` | `DD-API-KEY: <key>` | Region-specific ingest URL |
| **Microsoft Teams** | `11-teams.toml` | None (auth in URL `sig=`) | Power Automate workflow; legacy webhooks retired March 2026 |
| **PagerDuty** | — | None | `routing_key` must be in the JSON body — AppNeta's format doesn't support body injection directly; a middleware shim is required |

**PagerDuty note:** PagerDuty Events API v2 authenticates via a `routing_key` field in the JSON body, not a header. AppNeta connectors cannot inject fields into the alarm payload body. A middleware that wraps the AppNeta alarm JSON and adds `routing_key` before forwarding to `https://events.pagerduty.com/v2/enqueue` is required. Customers already running MuleSoft, Azure Logic Apps, Workato, Boomi, or Tray.io can use their existing integration platform as the intermediary — most have native PagerDuty connectors and require no custom code. For cloud-native environments, an AWS Lambda or Azure Function behind an API Gateway URL is a lightweight alternative.

---

## SUPPORT TOOLS

### support/json_to_toml.py — Convert connector JSON to TOML

Accepts any AppNeta v4 alarm connector JSON and produces a TOML config file compatible with `generate_connector.py`. Useful for:

- Seeding a TOML from an existing connector (export once, version-control it)
- Converting a connector shared as JSON into the team's standard TOML format

```bash
# Convert a connector JSON to TOML (output: connector.toml)
python support/json_to_toml.py connector.json

# Specify output file
python support/json_to_toml.py connector.json -o my-connector.toml

# Print to stdout
python support/json_to_toml.py connector.json --stdout
```

**Masked credential values** (`******`) are detected automatically. The script exits with an error listing every field that must be restored before conversion proceeds. AppNeta masks credential header values (e.g. `Authorization`) in GET responses — fill these in manually after conversion.

**ID-based filters** (`monitoringPointId`, `monitoringPolicyId`, `monitoringPolicyGroupId`) cannot be reverse-resolved to names without an API call. They are emitted as TOML comments with the raw IDs listed; replace each with the corresponding `monitoring_point_names` / `monitoring_policy_names` entry.

#### Recovering from a bad PUT

`generate_connector.py` writes a timestamped backup JSON (e.g. `20260716-093045-connector-12345.json`) before every PUT merge. If you applied changes to the wrong connector:

1. Open the backup JSON and restore any masked (`******`) header values
2. Convert it to TOML:
   ```bash
   python support/json_to_toml.py 20260716-093045-connector-12345.json -o revert.toml
   ```
3. Set `base_url` and `connector.id` in `revert.toml` (both are noted as needed)
4. Run `generate_connector.py revert.toml` to produce the revert payload
5. Submit via `PUT /api/v4/connector/alarm/<id>?orgId=<org>`

---

## SAMPLE OUTPUT

The following JSON is ready to `POST` to `https://<host>/api/v4/connector/alarm`.

```json
{
  "name": "Roadrunner Alert Feed",
  "enabled": true,
  "description": "Forwards critical/major alarms, excluding desert lab probes",
  "include": {
    "severity": [
      "CRITICAL",
      "MAJOR"
    ],
    "monitoringPolicyGroupId": [
      214,
      389
    ],
    "monitoringPointType": [
      "m50 microAppliance",
      "Windows PathView Agent"
    ],
    "tag": [
      {
        "category": "Region",
        "value": "Southwest Desert"
      }
    ]
  },
  "exclude": {
    "monitoringPolicyGroupId": [
      366
    ],
    "monitoringPointId": [
      29912,
      30047
    ],
    "noiseFilter": [
      "FREQUENT"
    ]
  },
  "notifyOn": [
    "ALARM_RAISED",
    "ALARM_CLEARED"
  ],
  "connectors": [
    {
      "type": "WEBHOOK",
      "url": "https://hooks.acme.corp/roadrunner-alerts",
      "method": "POST",
      "headers": [
        {
          "key": "Content-Type",
          "value": "application/json"
        },
        {
          "key": "X-Source",
          "value": "appneta"
        }
      ],
      "auth": {
        "type": "BEARER",
        "token": "your-token-here"
      }
    }
  ]
}
```

---

## API ENDPOINTS USED

All endpoints are under `https://<host>/api/`. Full interactive documentation is available at `https://<host>/swagger-ui.html` when authenticated via a browser session. See also the [v3 reference](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-v3.html) and [v4 reference](https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-v4.html) on Broadcom TechDocs.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v4/organization` | Discover accessible organizations — returns `id`, `name` |
| `GET` | `/v4/monitoringPoint?orgId=<id>` | List monitoring points — returns `id` (int64 global), `name`, `type` |
| `GET` | `/v4/monitoringPolicy?orgId=<id>` | List monitoring policies — returns `id` (int64 global), `name`, `monitoringPolicyGroup.id` (int64 global) |
| `GET` | `/v4/monitoringPolicyGroup?orgId=<id>` | List policy groups — returns `id` (int64 global), `name` directly on each group object |
| `GET` | `/v4/connector/alarm?orgId=<id>` | List all connectors. Called in UPDATE mode to fetch the existing connector for merge and diff. Degrades gracefully if the key lacks access. |
| `GET` | `/v4/connector/alarm?orgId=<id>&id=<connector_id>` | Fetch a specific connector by ID via query parameter. The v4 API does not support path-based access (`GET /alarm/{id}` returns 405). |
| `POST` | `/v4/connector/alarm?orgId=<id>` | Create a new alarm connector. **Not called by this script** — submit the generated JSON manually. Returns the connector `id`; save it in `connector.id` in your config for future updates. |
| `PUT` | `/v4/connector/alarm?orgId=<id>` | Update an existing connector. **Not called by this script** — the connector `id` is in the request body, not the URL path. The v4 API does not support path-based PUT (`/alarm/{id}` returns 405). |
| — | `/v4/alarmRule` | **Not available via API** — alarm rule IDs must be obtained from the AppNeta GUI (Alarms → Alarm Rules). |

All IDs returned by the v4 endpoints are int64 global IDs, consistent with what the alarm connector filter payload requires. The v3 endpoints returned smaller org-scoped int32 IDs which are **not** compatible with the filter payload.

**Note on policy counts:** `/v4/monitoringPolicy` may return fewer policies than `/v3/monitoringPolicy` for the same org — v4 appears to exclude archived or inactive policies. Verify against the Swagger UI if you expect a specific count.

## SUBMITTING THE OUTPUT (POST / PUT)

This script only ever performs `GET` requests and only ever writes JSON locally (file or stdout) — see [SECURITY NOTES](#security-notes). It never submits a connector to AppNeta. That submission step is deliberately a separate, explicit action you run yourself, using whatever tool fits your workflow (`curl`, a one-line shell wrapper, or a short Python script). The examples below use the generic demo NIS `demo.pm.appneta.com` — substitute your own NIS hostname and org.

### curl — CREATE (POST)

```bash
python3 generate_connector.py -c config.toml --stdout | \
  curl -sS -X POST "https://demo.pm.appneta.com/api/v4/connector/alarm?orgId=47291" \
    -H "Authorization: Token ${APPNETA_API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    --data-binary @-
```

`POST` returns the new connector's `id` in the response body. Save it into `connector.id` in your TOML config (or pass `--connector-id`) so subsequent runs generate an UPDATE (`PUT`) payload instead of a duplicate CREATE.

### curl — UPDATE (PUT)

```bash
python3 generate_connector.py -c config.toml --connector-id 90521 --stdout | \
  curl -sS -X PUT "https://demo.pm.appneta.com/api/v4/connector/alarm?orgId=47291" \
    -H "Authorization: Token ${APPNETA_API_KEY}" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    --data-binary @-
```

The connector `id` goes in the request body (generated automatically when `connector.id` / `--connector-id` is set) — the v4 API rejects path-based access (`/alarm/{id}` returns `405`), so the URL is identical to the `POST` case.

### bash wrapper — verb selectable, with basic error handling

```bash
#!/usr/bin/env bash
# submit_connector.sh — pipe generate_connector.py output to this script.
# Usage: generate_connector.py -c config.toml --stdout | ./submit_connector.sh POST 47291
set -euo pipefail

METHOD="${1:?usage: submit_connector.sh <POST|PUT> <orgId>}"
ORG_ID="${2:?usage: submit_connector.sh <POST|PUT> <orgId>}"
BASE_URL="${APPNETA_BASE_URL:-https://demo.pm.appneta.com}"

: "${APPNETA_API_KEY:?APPNETA_API_KEY must be set}"

http_code=$(curl -sS -o /tmp/connector_response.json -w '%{http_code}' \
  -X "${METHOD}" "${BASE_URL}/api/v4/connector/alarm?orgId=${ORG_ID}" \
  -H "Authorization: Token ${APPNETA_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  --data-binary @-)

cat /tmp/connector_response.json
echo

if [[ "${http_code}" -lt 200 || "${http_code}" -ge 300 ]]; then
  echo "ERROR: ${METHOD} failed with HTTP ${http_code}" >&2
  exit 1
fi
```

### Python wrapper — stdlib only, consistent with this project's dependency policy

```python
#!/usr/bin/env python3
"""submit_connector.py — pipe generate_connector.py output to this script.
Usage: generate_connector.py -c config.toml --stdout | python3 submit_connector.py POST 47291
"""
import json
import os
import sys
import urllib.error
import urllib.request

def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("POST", "PUT"):
        sys.exit(f"usage: {sys.argv[0]} <POST|PUT> <orgId>")
    method, org_id = sys.argv[1], sys.argv[2]

    api_key = os.environ.get("APPNETA_API_KEY")
    if not api_key:
        sys.exit("ERROR: APPNETA_API_KEY must be set")
    base_url = os.environ.get("APPNETA_BASE_URL", "https://demo.pm.appneta.com")

    payload = sys.stdin.read().encode("utf-8")
    url = f"{base_url}/api/v4/connector/alarm?orgId={org_id}"
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2))
    except urllib.error.HTTPError as e:
        print(f"ERROR: HTTP {e.code} from {url}", file=sys.stderr)
        print(e.read().decode(errors="replace"), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
```

These wrappers are documentation examples, not part of the shipped CLI — save one locally if you want to use it, and keep `APPNETA_API_KEY` out of shell history (`export` it in your session rather than passing it inline).

## DEPENDENCIES

None. The script uses Python's standard library exclusively:

| Module | Purpose |
|---|---|
| `tomllib` | TOML config parsing (stdlib as of Python 3.11) |
| `urllib` | HTTP requests to the AppNeta API |
| `json` | JSON output serialization |
| `argparse` | CLI argument parsing |

## FILES

| File | Purpose |
|---|---|
| `generate_connector.py` | Main script — invoke directly with `python3` |
| `README.md` | This document |
| `config_sample.toml` | Annotated TOML config template (recommended starting point) |
| `config_minimal.toml` | Minimal TOML template — connector identity + webhook only |
| `splunk_hec_connector.toml` | Ready-to-use Splunk HEC config template (uses `${SPLUNK_HEC_TOKEN}` header auth) |
| `samples/` | Platform-specific sample TOMLs (Splunk, ServiceNow, OpsGenie, Datadog, Teams) |
| `support/json_to_toml.py` | Convert any AppNeta alarm connector JSON to TOML; see SUPPORT TOOLS |
| `test/` | Unit tests — run with `python3 -m unittest discover -s test` |

## SEE ALSO

| Resource | URL |
|---|---|
| Alarm connectors overview | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html |
| Webhook connector payload reference | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html#title-alarm-connectors_webhook-connector-payload |
| Required API parameters for alarm connectors | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-connectors.html#title-alarm-connectors_required-api-parameters |
| Alarm rules | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms-rules.html |
| Alarms overview | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/alarms-set-up/alarms.html |
| API overview | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api.html |
| API access tokens | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-access-tokens.html |
| REST API v3 reference | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-v3.html |
| REST API v4 reference | https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/appneta/GA/integrate/api/api-v4.html |
| Interactive API docs (requires browser login) | `https://<host>/swagger-ui.html` |
| Python 3.11 downloads | https://www.python.org/downloads/ |
