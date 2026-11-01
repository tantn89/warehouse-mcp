-- Schema and roles for the warehouse-mcp Postgres backend. Run once as a superuser in the
-- target database (docker-compose mounts it into /docker-entrypoint-initdb.d; CI runs it with
-- psql). Tables are created later by scripts/load_snapshot.py, connected as warehouse_owner.
--
-- Two roles:
--   warehouse_owner  owns schema `wikipedia` and loads it. Never given to the MCP server.
--   mcp_reader       what the MCP server connects as: USAGE on the schema and SELECT on its
--                    tables, nothing else. Writes fail on grants even if every other layer
--                    (SQL guard, read-only transaction) were bypassed.
--
-- PASSWORDS ARE LOCAL-ONLY PLACEHOLDERS for a loopback-bound demo container. Never reuse them.

\set ON_ERROR_STOP on

CREATE ROLE warehouse_owner LOGIN PASSWORD 'local-only-owner-password';   -- placeholder
CREATE ROLE mcp_reader LOGIN PASSWORD 'local-only-reader-password';       -- placeholder

-- Session defaults for the reader, a further layer under the backend's own settings: every
-- transaction is read-only and no statement runs unbounded, whichever client connects.
ALTER ROLE mcp_reader SET default_transaction_read_only = on;
ALTER ROLE mcp_reader SET statement_timeout = '60s';
ALTER ROLE mcp_reader SET idle_in_transaction_session_timeout = '60s';

-- Database: connect only. PUBLIC loses TEMPORARY, so the reader cannot create temp tables.
DO $$
BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO warehouse_owner, mcp_reader',
                 current_database());
END
$$;

-- No one but the superuser creates objects in `public` (already the default since PG 15;
-- stated so that older servers and restored dumps end up the same).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA wikipedia AUTHORIZATION warehouse_owner;
REVOKE ALL ON SCHEMA wikipedia FROM PUBLIC;
GRANT USAGE ON SCHEMA wikipedia TO mcp_reader;

-- Tables the owner creates later (each reload drops and recreates them) are readable at once,
-- and only readable: SELECT is the sole privilege the reader gets on them.
ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_owner IN SCHEMA wikipedia
  GRANT SELECT ON TABLES TO mcp_reader;
