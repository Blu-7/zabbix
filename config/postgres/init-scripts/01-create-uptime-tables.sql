-------------------------------------------------------------------------------
-- uptime reporting.
--
-- LƯU Ý: Zabbix Server auto create history tables when starting.
-- This file only create VIEW to query uptime report
-- from data that Zabbix has already written.
--
-- Ref: https://www.zabbix.com/documentation/7.0/en/manual/appendix/install/db_scripts#postgresql
-------------------------------------------------------------------------------

-- Table mapping tenant code ↔ Zabbix host (for report)
CREATE TABLE IF NOT EXISTS tenant_registry (
    id              SERIAL PRIMARY KEY,
    tenant_code     VARCHAR(20) UNIQUE NOT NULL,
    tenant_name     VARCHAR(50) NOT NULL,
    domain          VARCHAR(50) NOT NULL,
    health_endpoint VARCHAR(100) NOT NULL,
    zabbix_host_id  BIGINT,
    license_status  VARCHAR(50) NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenant_registry_code
    ON tenant_registry(tenant_code);
CREATE INDEX IF NOT EXISTS idx_tenant_registry_active
    ON tenant_registry(is_active);

-- ============================================================================
-- VIEW: Uptime summary from Zabbix history tables
--
-- Zabbix save web check result into history_uint table (items with value_type=3)
-- Item key "web.test.fail[*]" → value 0 = OK, ≥1 = FAIL
--
-- Ref: https://www.zabbix.com/documentation/7.0/en/manual/config/items/itemtypes/http_checks
-- ============================================================================
CREATE OR REPLACE VIEW v_web_check_daily AS
SELECT
    h.host                                                  AS zabbix_host,
    REPLACE(h.host, 'tenant-', '')                          AS tenant_code,
    DATE(TO_TIMESTAMP(hu.clock) AT TIME ZONE 'Asia/Ho_Chi_Minh') AS check_date,
    COUNT(*)                                                AS total_checks,
    SUM(CASE WHEN hu.value = 0 THEN 1 ELSE 0 END)         AS ok_count,
    SUM(CASE WHEN hu.value > 0 THEN 1 ELSE 0 END)         AS fail_count,
    ROUND(
        100.0 * SUM(CASE WHEN hu.value = 0 THEN 1 ELSE 0 END)
        / NULLIF(COUNT(*), 0), 4
    )                                                       AS uptime_pct
FROM history_uint hu
JOIN items i ON i.itemid = hu.itemid
JOIN hosts h ON h.hostid = i.hostid
WHERE i.key_ LIKE 'web.test.fail[%]'
GROUP BY h.host, check_date
ORDER BY check_date DESC, h.host;
