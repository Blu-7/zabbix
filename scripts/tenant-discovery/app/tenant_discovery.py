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


def _fetch_tenant_api_json() -> dict:
    resp = requests.post(
        config.TENANT_API_URL,
        json={"password": config.TENANT_API_KEY},
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=config.TENANT_API_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _parse_tenant_rows(data: dict) -> list:
    if data.get("success") != 1:
        logger.error("API returned success=%s: %s", data.get("success"), data.get("message"))
        return []
    raw = data.get("tenants", "[]")
    return json.loads(raw) if isinstance(raw, str) else (raw or [])


def _domain_and_base(primary_site_url: str) -> tuple[str, str]:
    parsed = urlparse(primary_site_url)
    domain = parsed.netloc or primary_site_url.replace("https://", "").replace("http://", "")
    base = str(primary_site_url).strip().rstrip("/") or str(primary_site_url).strip()
    return domain, base


def _probe_health_route_status(health_url: str) -> int | None:
    """Lightweight existence check: no auth body (expect 401/403 if route exists)."""
    headers = {
        "User-Agent": config.HEALTHCHECK_PROBE_USER_AGENT,
        "Accept": config.HEALTHCHECK_PROBE_ACCEPT,
    }
    try:
        resp = requests.get(
            health_url,
            timeout=config.HEALTHCHECK_PROBE_TIMEOUT,
            allow_redirects=True,
            headers=headers,
        )
        return resp.status_code
    except requests.RequestException as exc:
        logger.warning("Healthcheck probe GET %s failed: %s", health_url, exc)
        return None


def _pick_health_endpoint(base: str, health_url: str, probe_status: int | None) -> str:
    if probe_status is None:
        logger.warning(
            "Healthcheck probe gave no status for %s (timeout/connection error); falling back to site base %s",
            health_url,
            base,
        )
        return base
    if probe_status == config.HEALTHCHECK_FALLBACK_HTTP_STATUS:
        logger.info(
            "Healthcheck returned HTTP %s for %s; falling back to site base %s",
            probe_status,
            health_url,
            base,
        )
        return base
    logger.info(
        "Healthcheck probe %s returned HTTP %s; route exists, monitoring healthcheck endpoint",
        health_url,
        probe_status,
    )
    return health_url


def fetch_active_tenants() -> list[TenantInfo]:
    """POST to tenant API and return list of TenantInfo."""
    data = _fetch_tenant_api_json()
    items = _parse_tenant_rows(data)

    tenants: list[TenantInfo] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in items:
        domain, base = _domain_and_base(item["primary_site_url"])
        health_url = f"{base.rstrip('/')}{config.HEALTHCHECK_REL_PATH}"

        probe_status = _probe_health_route_status(health_url)
        health_endpoint = _pick_health_endpoint(base, health_url, probe_status)
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
        tenant_code = str(item["id"])
        tenant_name = str(item.get("name") or item.get("tenant_name") or domain)
        tenants.append(TenantInfo(
            tenant_code=tenant_code,
            tenant_name=tenant_name,
            domain=domain,
            license_status=item["license_status"],
            expired_date=item["expired_date"],
            health_endpoint=health_endpoint,
        ))

    logger.info("Fetched %d tenants from %s (after de-duplication)", len(tenants), config.TENANT_API_URL)
    logger.debug("Active tenant domains from API: %s", [tenant.domain for tenant in tenants])
    return tenants
