-- Dev only: Langfuse v3 DB on shared soothe-pgvector. Not used in production.
-- Soothe app DBs are auto-provisioned on daemon startup.

SELECT 'CREATE DATABASE langfuse'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'langfuse')\gexec
