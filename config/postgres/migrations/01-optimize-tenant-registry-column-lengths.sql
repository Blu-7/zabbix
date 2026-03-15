-------------------------------------------------------------------------------
-- Migration: align tenant_registry column lengths with init script.
-- Keep target types in sync with init-scripts/01-create-uptime-tables.sql.
-- Safe for existing data: VARCHAR increase is metadata-only; decrease fails if
-- any row is longer (no data loss). Run: make db-migrate
-------------------------------------------------------------------------------

ALTER TABLE tenant_registry
    ALTER COLUMN tenant_code     TYPE VARCHAR(20),
    ALTER COLUMN tenant_name     TYPE VARCHAR(50),
    ALTER COLUMN domain          TYPE VARCHAR(50),
    ALTER COLUMN health_endpoint TYPE VARCHAR(100);

ALTER table tenant_registry ADD column license_status VARCHAR(50) NOT NULL;
ALTER table tenant_registry ADD column expired_date DATE;
