# Changelog

## v0.1.1

Security hardening and UPDATE-mode fixes.

- The script no longer follows redirects, and `base_url` must be a plain `https://host[:port]`
  (no credentials, query or fragment), so the API key is only ever sent to the host you configured.
- UPDATE mode removes include/exclude conflicts created by the merge, can turn TLS verification
  back on, and shows a diff of what is actually sent. It now stops if the existing connector cannot
  be read, or if exclude rules leave an empty include block (which would forward everything), unless
  you pass `--lenient`.
- CR/LF and control characters are rejected in connector names, URLs, header names and values, and
  in values expanded from `${ENV_VAR}`.
- Malformed TOML types (for example a number where a string is expected) are reported as errors
  instead of crashing; `true`/`false` are no longer accepted as IDs.
- Backup files are created owner-only (`0600`); `--dry-run` no longer writes backup, error or
  generated-TOML files.
- `support/json_to_toml.py` now reads the `ignoreSSLErrors` and `tag` fields, keeps severity, noise
  filter, alarm rule and target filters, writes ID filters as real keys, never writes credentials,
  escapes control characters, and writes its output file owner-only.

## v0.1.0

Initial release.

- `generate_connector.py`: builds the JSON payload for an AppNeta alarm connector (webhook) from a
  TOML config, resolving monitoring point, policy and policy group names to IDs through the AppNeta
  API.
- CREATE mode (POST payload) and UPDATE mode (merges your changes onto an existing connector and
  shows a diff).
- Include and exclude filters for severity, monitoring point type, tags, names and policies, with
  glob patterns.
- Webhook authentication (`NONE`, `BASIC`, `BEARER`, or custom headers with `${ENV_VAR}`
  substitution); secrets are read from the environment, never from the config file.
- `support/json_to_toml.py`: converts an existing connector JSON back into a TOML config.
- Sample configs for common destinations (Splunk HEC, ServiceNow, Opsgenie, Datadog, Teams).
- Unit tests in `test/` (Python standard library only, no network access required).
