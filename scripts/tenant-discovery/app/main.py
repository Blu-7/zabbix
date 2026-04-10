import logging
import signal
import time
from pathlib import Path

from . import config
from .tenant_discovery import fetch_active_tenants
from . import zabbix_sync

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=config.LOG_LEVEL,
)
logger = logging.getLogger(__name__)

_running = True


def _shutdown(signum, _frame):
    global _running
    logger.info("Shutdown requested (signal %s)", signum)
    _running = False


signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


def run_cycle():
    """One cycle: fetch tenants -> sync into Zabbix -> disable removed."""
    try:
        logger.info("--- Sync cycle start ---")

        zabbix_sync.connect()
        group_id = zabbix_sync.ensure_host_group()

        zabbix_sync.migrate_legacy_hosts(group_id)

        tenants = fetch_active_tenants()
        if not tenants:
            logger.warning("No tenants found from API, reconciling by disabling existing hosts in group")

        active_domains: set[str] = set()
        for tenant in tenants:
            zabbix_sync.sync_tenant(tenant, group_id)
            active_domains.add(tenant.domain)

        zabbix_sync.disable_removed_tenants(active_domains, group_id)

        logger.info("--- Sync cycle done (%d tenants) ---", len(active_domains))
        Path("/tmp/healthcheck").touch()

    except Exception:
        logger.exception("Sync cycle failed")


def main():
    logger.info("Starting (interval=%ds, source=%s)", config.DISCOVERY_INTERVAL, config.TENANT_API_URL)

    run_cycle()

    while _running:
        time.sleep(config.DISCOVERY_INTERVAL)
        if _running:
            run_cycle()

    logger.info("Stopped")


if __name__ == "__main__":
    main()
