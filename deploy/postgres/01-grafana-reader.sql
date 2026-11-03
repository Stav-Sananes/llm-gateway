-- Grafana reads usage and audit rows with a read-only role, never the gateway's.
-- Runs once, when the Postgres volume is first created. The tables don't exist yet; the
-- grafana-grants service grants SELECT on the three the dashboard reads, after migrating.
CREATE ROLE grafana_reader LOGIN PASSWORD 'grafana_reader';
GRANT CONNECT ON DATABASE gateway TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
