-- Run once as administrator in a NEW dedicated CallOps database.
-- No passwords here: supply them separately through your secret management.
-- Existing roles/schema cause failure instead of being silently repurposed.
CREATE ROLE callops_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE callops_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
DO $$ BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO callops_migrator, callops_app', current_database());
END $$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA callops AUTHORIZATION callops_migrator;
GRANT USAGE ON SCHEMA callops TO callops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE callops_migrator IN SCHEMA callops
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO callops_app;
