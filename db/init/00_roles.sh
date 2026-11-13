#!/usr/bin/env bash
# Creates the least-privilege application role. The owner/superuser role
# (POSTGRES_USER) is used only for schema management; the API and the worker
# connect as `jellyrent_app`, whose privileges are granted in 06_grants.sql.
set -euo pipefail

: "${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     --set app_password="$APP_DB_PASSWORD" <<'EOSQL'
SELECT format('CREATE ROLE jellyrent_app LOGIN PASSWORD %L', :'app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jellyrent_app')
\gexec
EOSQL
