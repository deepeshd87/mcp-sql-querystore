-- Provision a dedicated least-privilege login for mcp-sql-querystore.
-- This login's permissions ARE the read-only guarantee. Run once per instance,
-- then run the per-database section against each DB you want to inspect.
--
-- Adjust names, and prefer a strong password from your secret store or use a
-- contained/AAD login on Azure SQL MI.

-------------------------------------------------------------------------------
-- 1. Server-level login (SQL auth shown; swap for Windows/AAD as appropriate)
-------------------------------------------------------------------------------
USE [master];
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'mcp_readonly')
BEGIN
    CREATE LOGIN [mcp_readonly] WITH PASSWORD = N'<set-from-secret-store>',
        CHECK_POLICY = ON;
END
GO

-- Needed for server-scoped DMVs (sys.dm_exec_*). VIEW SERVER STATE is broad;
-- if you only use DB-scoped Query Store views you can rely on VIEW DATABASE
-- STATE per database instead and skip this grant.
GRANT VIEW SERVER STATE TO [mcp_readonly];
GO

-------------------------------------------------------------------------------
-- 2. Per-database: run this block in EACH database to be inspected
-------------------------------------------------------------------------------
-- USE [YourDatabase];
-- GO
-- CREATE USER [mcp_readonly] FOR LOGIN [mcp_readonly];
-- GO
-- GRANT VIEW DATABASE STATE TO [mcp_readonly];   -- Query Store + DB DMVs
-- GO
--
-- Deliberately NOT granted: db_datareader, SELECT on user tables, any write role.
-- The server only reads sys.query_store_* and sys.dm_* which VIEW DATABASE STATE
-- (plus VIEW SERVER STATE for server DMVs) covers.
