#!/usr/bin/env bash
# Factory simulator: inserts one changed stock row every SIMULATOR_INTERVAL seconds
# so the middleware has a continuous stream of new data to sync.
set -euo pipefail

SQLCMD=/opt/mssql-tools18/bin/sqlcmd
HOST="${MSSQL_HOST:-mssql}"
INTERVAL="${SIMULATOR_INTERVAL:-15}"

: "${MSSQL_SA_PASSWORD:?MSSQL_SA_PASSWORD must be set}"

until "$SQLCMD" -S "$HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -l 5 -d PRODUKTION \
      -Q "SELECT 1 FROM dbo.Lagerbestand WHERE 1 = 0" > /dev/null 2>&1; do
  echo "Waiting for seeded database"
  sleep 3
done

counter=0
while true; do
  counter=$((counter + 1))
  "$SQLCMD" -S "$HOST" -U sa -P "$MSSQL_SA_PASSWORD" -C -b -d PRODUKTION -Q "
    SET NOCOUNT ON;
    INSERT INTO dbo.Lagerbestand
      (ArtikelNr, Bezeichnung, Werk, Lagerort, Bestand, Mengeneinheit, LetzterBearbeiter, LastChanged)
    VALUES
      (CONCAT('sim-', RIGHT(CONCAT('000000', ${counter}), 6)), 'Simulierte Buchung', '1000', '0001',
       CAST(ABS(CHECKSUM(NEWID())) % 1000 AS DECIMAL(15,3)), 'STK', 'Schichtleiter Nord', SYSDATETIME());" \
    && echo "Inserted simulated row ${counter}" \
    || echo "Insert ${counter} failed; retrying next interval" >&2
  sleep "$INTERVAL"
done
