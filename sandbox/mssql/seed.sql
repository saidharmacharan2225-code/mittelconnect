-- =============================================================================
-- Sandbox seed for the mock legacy ERP (SQL Server).
-- Executed by the mssql-init service with:
--   sqlcmd -v READER_PASSWORD="..." -i seed.sql
-- Idempotent: safe to run on every `docker compose up`.
-- =============================================================================
SET NOCOUNT ON;
GO

IF DB_ID(N'PRODUKTION') IS NULL
BEGIN
    CREATE DATABASE PRODUKTION COLLATE Latin1_General_CI_AS;
END
GO

USE PRODUKTION;
GO

-- Typical late-1990s table: VARCHAR in Windows-1252 collation, padded CHAR codes,
-- German column names, DECIMAL quantities.
IF OBJECT_ID(N'dbo.Lagerbestand', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.Lagerbestand (
        LfdNr              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Lagerbestand PRIMARY KEY,
        ArtikelNr          VARCHAR(40)    NOT NULL,
        Bezeichnung        VARCHAR(80)    NULL,
        Werk               CHAR(4)        NOT NULL,
        Lagerort           CHAR(4)        NULL,
        Bestand            DECIMAL(15,3)  NOT NULL,
        Mengeneinheit      CHAR(3)        NOT NULL,
        LetzterBearbeiter  VARCHAR(60)    NULL,
        LastChanged        DATETIME2(3)   NOT NULL CONSTRAINT DF_Lagerbestand_LastChanged DEFAULT SYSDATETIME()
    );
    CREATE INDEX IX_Lagerbestand_LastChanged ON dbo.Lagerbestand (LastChanged);
END
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Lagerbestand)
BEGIN
    ;WITH n AS (
        SELECT TOP (500) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
        FROM sys.all_objects a CROSS JOIN sys.all_objects b
    )
    INSERT INTO dbo.Lagerbestand
        (ArtikelNr, Bezeichnung, Werk, Lagerort, Bestand, Mengeneinheit, LetzterBearbeiter, LastChanged)
    SELECT
        CASE WHEN i % 97 = 0 THEN CONCAT('INVALID-', i) ELSE CONCAT('mat-', RIGHT(CONCAT('000000', i), 6)) END,
        CONCAT(CASE i % 4 WHEN 0 THEN 'Flanschdichtung ' WHEN 1 THEN 'Kugellager ' WHEN 2 THEN 'Getriebewelle ' ELSE 'Schraube M8 ' END, i),
        CASE WHEN i % 3 = 0 THEN '1000' ELSE '2000' END,
        CASE WHEN i % 2 = 0 THEN '0001' ELSE '0002' END,
        CAST((i * 13) % 5000 AS DECIMAL(15,3)) + 0.125,
        CASE i % 3 WHEN 0 THEN 'STK' WHEN 1 THEN 'KG ' ELSE 'M  ' END,
        CASE i % 5 WHEN 0 THEN 'Jürgen Müller' WHEN 1 THEN 'Anna Schäfer' WHEN 2 THEN 'Klaus Weiß'
                   WHEN 3 THEN 'Petra Groß' ELSE 'Özlem Yılmaz' END,
        DATEADD(SECOND, i, CAST('2024-01-01T06:00:00' AS DATETIME2(3)))
    FROM n;
END
GO

-- Least-privilege, read-only login for the middleware.
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'mc_reader')
BEGIN
    DECLARE @create NVARCHAR(400) =
        N'CREATE LOGIN mc_reader WITH PASSWORD = N''$(READER_PASSWORD)'', CHECK_POLICY = ON, DEFAULT_DATABASE = PRODUKTION';
    EXEC (@create);
END
ELSE
BEGIN
    DECLARE @alter NVARCHAR(400) = N'ALTER LOGIN mc_reader WITH PASSWORD = N''$(READER_PASSWORD)''';
    EXEC (@alter);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'mc_reader')
BEGIN
    CREATE USER mc_reader FOR LOGIN mc_reader;
END
GO

GRANT SELECT ON dbo.Lagerbestand TO mc_reader;
DENY INSERT, UPDATE, DELETE ON dbo.Lagerbestand TO mc_reader;
GO

SELECT COUNT(*) AS seeded_rows FROM dbo.Lagerbestand;
GO
