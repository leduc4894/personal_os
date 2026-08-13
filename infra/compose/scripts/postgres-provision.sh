#!/bin/sh
set -eu

admin_password_file=/run/secrets/postgres_admin_password
application_password_file=/run/secrets/postgres_application_password
temporal_password_file=/run/secrets/postgres_temporal_password

if [ ! -r "$admin_password_file" ] || [ ! -r "$application_password_file" ] || [ ! -r "$temporal_password_file" ]; then
    printf '%s\n' secret_unavailable >&2
    exit 1
fi

admin_password=$(cat "$admin_password_file")
application_password=$(cat "$application_password_file")
temporal_password=$(cat "$temporal_password_file")
if [ -z "$admin_password" ] || [ -z "$application_password" ] || [ -z "$temporal_password" ]; then
    printf '%s\n' secret_unavailable >&2
    exit 1
fi

PGPASSWORD=$admin_password
KNOWLEDGE_APPLICATION_PASSWORD=$application_password
TEMPORAL_SERVICE_PASSWORD=$temporal_password
export PGPASSWORD KNOWLEDGE_APPLICATION_PASSWORD TEMPORAL_SERVICE_PASSWORD
unset admin_password application_password temporal_password

psql -XAtq -v ON_ERROR_STOP=1 \
    --host postgresql \
    --port 5432 \
    --username stack_admin \
    --dbname postgres <<'SQL'
\getenv knowledge_application_password KNOWLEDGE_APPLICATION_PASSWORD
\getenv temporal_service_password TEMPORAL_SERVICE_PASSWORD

SELECT format(
    'CREATE ROLE %s LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %s',
    format('%I', 'knowledge_app'),
    format('%L', :'knowledge_application_password')
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'knowledge_app')
\gexec

SELECT format(
    'ALTER ROLE %s WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s',
    format('%I', 'knowledge_app'),
    format('%L', :'knowledge_application_password')
)
\gexec

SELECT format(
    'CREATE ROLE %s LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %s',
    format('%I', 'temporal_service'),
    format('%L', :'temporal_service_password')
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'temporal_service')
\gexec

SELECT format(
    'ALTER ROLE %s WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %s',
    format('%I', 'temporal_service'),
    format('%L', :'temporal_service_password')
)
\gexec

SELECT format(
    'REVOKE %s FROM %s',
    format('%I', granted_role.rolname),
    format('%I', member_role.rolname)
)
FROM pg_auth_members AS memberships
JOIN pg_roles AS granted_role ON granted_role.oid = memberships.roleid
JOIN pg_roles AS member_role ON member_role.oid = memberships.member
WHERE granted_role.rolname IN ('knowledge_app', 'temporal_service')
   OR member_role.rolname IN ('knowledge_app', 'temporal_service')
\gexec

SELECT format(
    'CREATE DATABASE %s OWNER %s',
    format('%I', database_name),
    format('%I', owner_name)
)
FROM (
    VALUES
        ('knowledge', 'knowledge_app'),
        ('temporal', 'temporal_service'),
        ('temporal_visibility', 'temporal_service')
) AS expected_databases(database_name, owner_name)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_database WHERE datname = expected_databases.database_name
)
\gexec

SELECT format(
    'ALTER DATABASE %s OWNER TO %s',
    format('%I', database_name),
    format('%I', owner_name)
)
FROM (
    VALUES
        ('knowledge', 'knowledge_app'),
        ('temporal', 'temporal_service'),
        ('temporal_visibility', 'temporal_service')
) AS expected_databases(database_name, owner_name)
\gexec

SELECT format(
    'REVOKE CONNECT, CREATE ON DATABASE %s FROM PUBLIC',
    format('%I', database_name)
)
FROM (
    VALUES ('postgres'), ('knowledge'), ('temporal'), ('temporal_visibility')
) AS expected_databases(database_name)
\gexec

SELECT format(
    'REVOKE ALL PRIVILEGES ON DATABASE %s FROM %s',
    format('%I', database_name),
    format('%I', role_name)
)
FROM (
    VALUES
        ('postgres', 'knowledge_app'),
        ('postgres', 'temporal_service'),
        ('knowledge', 'temporal_service'),
        ('temporal', 'knowledge_app'),
        ('temporal_visibility', 'knowledge_app')
) AS denied_connections(database_name, role_name)
\gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %s TO %s',
    format('%I', database_name),
    format('%I', role_name)
)
FROM (
    VALUES
        ('knowledge', 'knowledge_app'),
        ('temporal', 'temporal_service'),
        ('temporal_visibility', 'temporal_service')
) AS allowed_connections(database_name, role_name)
\gexec
SQL

unset PGPASSWORD KNOWLEDGE_APPLICATION_PASSWORD TEMPORAL_SERVICE_PASSWORD
