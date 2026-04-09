import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from . import config

logger = logging.getLogger(__name__)


@dataclass
class TenantInfo:
    tenant_code: str
    tenant_name: str
    domain: str
    license_status: str
    expired_date: str
    health_endpoint: str


def fetch_active_tenants() -> list[TenantInfo]:
    """POST to tenant API and return list of TenantInfo."""
    resp = requests.post(
        config.TENANT_API_URL,
        json={"password": config.TENANT_API_KEY},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=config.TENANT_API_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("success") != 1:
        logger.error("API returned success=%s: %s", data.get("success"), data.get("message"))
        return []

    raw = data.get("tenants", "[]")
    items = json.loads(raw) if isinstance(raw, str) else (raw or [])

    tenants: list[TenantInfo] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in items:
        url = item["primary_site_url"]
        parsed = urlparse(url)
        domain = parsed.netloc or url.replace("https://", "").replace("http://", "")
        # health_endpoint = url.rstrip("/") + "/webhooks/healthcheck"
        health_endpoint = url.rstrip("/") # Remove trailing slash if exists
        key = (domain, health_endpoint)

        if key in seen_keys:
            logger.warning(
                "Duplicate tenant for domain=%s health=%s, skipping (tenant_id=%s)",
                domain,
                health_endpoint,
                item.get("id"),
            )
            continue

        seen_keys.add(key)

        tenant_code = str(item["primary_site_url"])
        tenants.append(TenantInfo(
            tenant_code=tenant_code,
            tenant_name=domain,
            domain=domain,
            license_status=item["license_status"],
            expired_date=item["expired_date"],
            health_endpoint=health_endpoint,
        ))

    logger.info("Fetched %d tenants from %s (after de-duplication)", len(tenants), config.TENANT_API_URL)
    logger.debug("Active tenant codes from API: %s", [tenant.tenant_code for tenant in tenants])
    return tenants
