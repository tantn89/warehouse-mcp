-- Roles, users, warehouse and cost guards for the warehouse-mcp Snowflake backend. Run once,
-- by hand, as ACCOUNTADMIN in a Snowsight worksheet (resource monitors need ACCOUNTADMIN).
-- Tables are created later by scripts/load_snapshot.py --target snowflake, connected as
-- WAREHOUSE_OWNER_SVC.
--
-- Two roles, each with one service user:
--   WAREHOUSE_OWNER  owns schema WAREHOUSE_MCP.WIKIPEDIA and loads it. Never given to the MCP
--                    server.
--   MCP_READER       what the MCP server connects as: USAGE on one warehouse, database and
--                    schema, and SELECT on the schema's tables. No CREATE privilege anywhere,
--                    so writes to the warehouse's tables and new permanent tables or stages
--                    fail on grants even if the SQL guard were bypassed.
--
-- What grants cannot close, and what closes it instead:
--   - Temporary tables need no CREATE TABLE privilege, and every user has a user stage (@~)
--     that no grant controls. Both are private to the session or user and are refused by
--     the SQL guard (CREATE, PUT, COPY, stage reads). Unloading data to any internal stage,
--     @~ included, and to inline URLs is switched off on the reader user below.
--   - Every role inherits PUBLIC. Many accounts, trials included, grant PUBLIC imported
--     privileges on SNOWFLAKE_SAMPLE_DATA. SHOW GRANTS TO ROLE PUBLIC at the end lists what
--     the reader inherits; the SQL guard reads declared datasets only, whatever PUBLIC holds.
--
-- Cost, three layers:
--   MCP_WH           an XS warehouse that starts suspended and suspends after 60 s idle, with
--                    a 60 s statement and queue timeout.
--   MCP_MONITOR      a resource monitor that suspends MCP_WH at a small credit quota.
--   the reader user  its own STATEMENT_TIMEOUT_IN_SECONDS; the backend also sets one per
--                    session and per statement. Snowflake enforces the lowest.
--
-- Users are TYPE = SERVICE: key-pair auth only, no password, no browser or MFA login. Paste
-- each PUBLIC key (the base64 body between the PEM header and footer) where the placeholders
-- are. Private keys never go into this file, the repository or a chat.

USE ROLE ACCOUNTADMIN;

-- Roles. Both roll up to SYSADMIN, so administrators keep sight of what they own.
CREATE ROLE IF NOT EXISTS WAREHOUSE_OWNER COMMENT = 'warehouse-mcp: owns and loads the data';
CREATE ROLE IF NOT EXISTS MCP_READER COMMENT = 'warehouse-mcp: SELECT-only, used by the server';
GRANT ROLE WAREHOUSE_OWNER TO ROLE SYSADMIN;
GRANT ROLE MCP_READER TO ROLE SYSADMIN;

-- Compute. INITIALLY_SUSPENDED: creating it costs nothing. AUTO_SUSPEND = 60: an idle demo
-- bills at most one minute after the last query.
CREATE WAREHOUSE IF NOT EXISTS MCP_WH WITH
  WAREHOUSE_SIZE = XSMALL
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  INITIALLY_SUSPENDED = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 60
  STATEMENT_QUEUED_TIMEOUT_IN_SECONDS = 60
  COMMENT = 'warehouse-mcp demo';

-- Spend cap: notify at 80 percent, suspend at the quota (running queries finish), and
-- suspend immediately at 110 percent (running queries are cancelled).
CREATE OR REPLACE RESOURCE MONITOR MCP_MONITOR WITH
  CREDIT_QUOTA = 5
  FREQUENCY = MONTHLY
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS ON 80 PERCENT DO NOTIFY
           ON 100 PERCENT DO SUSPEND
           ON 110 PERCENT DO SUSPEND_IMMEDIATE;
ALTER WAREHOUSE MCP_WH SET RESOURCE_MONITOR = MCP_MONITOR;

GRANT USAGE ON WAREHOUSE MCP_WH TO ROLE WAREHOUSE_OWNER;
GRANT USAGE ON WAREHOUSE MCP_WH TO ROLE MCP_READER;

-- Storage. The owner role owns the schema, so it can create and replace the tables. No grant
-- is made to PUBLIC here; see the check at the end for what the account already gives it.
CREATE DATABASE IF NOT EXISTS WAREHOUSE_MCP COMMENT = 'warehouse-mcp demo snapshot';
CREATE SCHEMA IF NOT EXISTS WAREHOUSE_MCP.WIKIPEDIA;
GRANT OWNERSHIP ON SCHEMA WAREHOUSE_MCP.WIKIPEDIA TO ROLE WAREHOUSE_OWNER COPY CURRENT GRANTS;
GRANT USAGE ON DATABASE WAREHOUSE_MCP TO ROLE WAREHOUSE_OWNER;

-- The reader: USAGE to resolve names, SELECT to read. Each reload replaces the tables, so the
-- SELECT grant is also made on future tables; it is the only future grant, and SELECT is the
-- only privilege the reader gets on them. No grant on views, stages, functions, procedures,
-- sequences or the SNOWFLAKE database.
GRANT USAGE ON DATABASE WAREHOUSE_MCP TO ROLE MCP_READER;
GRANT USAGE ON SCHEMA WAREHOUSE_MCP.WIKIPEDIA TO ROLE MCP_READER;
GRANT SELECT ON ALL TABLES IN SCHEMA WAREHOUSE_MCP.WIKIPEDIA TO ROLE MCP_READER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA WAREHOUSE_MCP.WIKIPEDIA TO ROLE MCP_READER;

-- Service users, key-pair auth. DEFAULT_SECONDARY_ROLES = () keeps every session to the one
-- granted role: no secondary roles can widen what a session may do.
CREATE USER IF NOT EXISTS WAREHOUSE_OWNER_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = WAREHOUSE_OWNER
  DEFAULT_WAREHOUSE = MCP_WH
  DEFAULT_SECONDARY_ROLES = ()
  RSA_PUBLIC_KEY = '<paste the owner public key here>'
  COMMENT = 'warehouse-mcp: loads the snapshot';
GRANT ROLE WAREHOUSE_OWNER TO USER WAREHOUSE_OWNER_SVC;

CREATE USER IF NOT EXISTS MCP_READER_SVC
  TYPE = SERVICE
  DEFAULT_ROLE = MCP_READER
  DEFAULT_WAREHOUSE = MCP_WH
  DEFAULT_SECONDARY_ROLES = ()
  RSA_PUBLIC_KEY = '<paste the reader public key here>'
  COMMENT = 'warehouse-mcp: the MCP server, read-only';
ALTER USER MCP_READER_SVC SET STATEMENT_TIMEOUT_IN_SECONDS = 60;
-- User-level parameters: no COPY INTO @~ (or any internal stage) and no COPY INTO 's3://...'.
ALTER USER MCP_READER_SVC SET PREVENT_UNLOAD_TO_INTERNAL_STAGES = TRUE;
ALTER USER MCP_READER_SVC SET PREVENT_UNLOAD_TO_INLINE_URL = TRUE;
GRANT ROLE MCP_READER TO USER MCP_READER_SVC;

-- Check: the reader holds USAGE x3 and SELECT, plus whatever PUBLIC holds (not listed by
-- SHOW GRANTS TO ROLE MCP_READER), and the unload parameters are set.
SHOW GRANTS TO ROLE MCP_READER;
SHOW GRANTS TO ROLE PUBLIC;
SHOW PARAMETERS LIKE 'PREVENT_UNLOAD%' IN USER MCP_READER_SVC;
SHOW FUTURE GRANTS IN SCHEMA WAREHOUSE_MCP.WIKIPEDIA;

-- Teardown after the trial run (as ACCOUNTADMIN):
--   DROP USER IF EXISTS MCP_READER_SVC;  DROP USER IF EXISTS WAREHOUSE_OWNER_SVC;
--   DROP DATABASE IF EXISTS WAREHOUSE_MCP;  DROP WAREHOUSE IF EXISTS MCP_WH;
--   DROP RESOURCE MONITOR IF EXISTS MCP_MONITOR;
--   DROP ROLE IF EXISTS MCP_READER;  DROP ROLE IF EXISTS WAREHOUSE_OWNER;
