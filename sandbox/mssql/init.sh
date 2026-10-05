#!/usr/bin/env bash
# Waits for SQL Server, then applies seed.sql. Used by the mssql-init service.
set -euo pipefail

SQLCMD=/opt/mssql-tools18/bin/sqlcmd
HOST="${MSSQL_HOST:-mssql}"

: "${MSSQL_SA_PASSWORD:?MSSQL_SA_PASSWORD must be set}"
: "${MC_READER_PASSWORD:?MC_READER_PASSWORD must be set}"

for attempt in $(seq 1 60); do
  if "$SQLCMD" -S "$HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -l 5 -Q "SELECT 1" > /dev/null 2>&1; then
    echo "SQL Server is ready (attempt $attempt)"
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "SQL Server did not become ready" >&2
    exit 1
  fi
  sleep 2
done

"$SQLCMD" -S "$HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -b \
  -v READER_PASSWORD="$MC_READER_PASSWORD" \
  -i /seed/seed.sql

echo "Seed complete"
