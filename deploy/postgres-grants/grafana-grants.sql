-- Only what the dashboard reads. Not api_keys: least privilege, even for hashes.
GRANT SELECT ON tenants, usage_events, audit_events TO grafana_reader;
