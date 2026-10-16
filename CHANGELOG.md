# Changelog

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
