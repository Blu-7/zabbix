import logging

from pyzabbix import ZabbixAPI

from . import config
from .tenant_discovery import TenantInfo

logger = logging.getLogger(__name__)

_zapi: ZabbixAPI | None = None


def connect() -> None:
    global _zapi
    if _zapi is not None:
        return
    _zapi = ZabbixAPI(config.ZABBIX_API_URL)
    _zapi.login(config.ZABBIX_API_USER, config.ZABBIX_API_PASSWORD)
    logger.info("Connected to Zabbix %s", _zapi.api_version())


def ensure_host_group() -> str:
    """Create host group if it doesn't exist, return group ID."""
    groups = _zapi.hostgroup.get(filter={"name": [config.ZABBIX_HOST_GROUP]})
    if groups:
        return groups[0]["groupid"]

    result = _zapi.hostgroup.create(name=config.ZABBIX_HOST_GROUP)
    gid = result["groupids"][0]
    logger.info("Created host group '%s' (id=%s)", config.ZABBIX_HOST_GROUP, gid)
    return gid


def sync_tenant(tenant: TenantInfo, group_id: str) -> str:
    """Create or update Zabbix host + web scenario + triggers for a tenant."""
    host_name = f"tenant-{tenant.tenant_code}"
    existing = _zapi.host.get(filter={"host": host_name}, output=["hostid", "status"])

    if existing:
        host_id = existing[0]["hostid"]
        # Host may have been disabled in a previous cycle; re-enable when tenant is active again.
        if existing[0].get("status") == "1":
            _zapi.host.update(hostid=host_id, status=0)
            logger.info("Re-enabled host %s (tenant active again)", host_name)
        _ensure_host_tags(host_id, tenant)
        _ensure_web_scenario(host_id, tenant, update=True)
        _ensure_triggers(host_id, host_name, tenant)
        return host_id

    result = _zapi.host.create(
        host=host_name,
        name=f"{tenant.tenant_name} ({tenant.domain})",
        groups=[{"groupid": group_id}],
        tags=[
            {"tag": "tenantId", "value": tenant.tenant_code},
            {"tag": "licenseStatus", "value": tenant.license_status},
            {"tag": "expiredDate", "value": tenant.expired_date},
        ],
    )
    host_id = result["hostids"][0]
    logger.info("Created host %s (id=%s)", host_name, host_id)

    _ensure_host_tags(host_id, tenant)
    _ensure_web_scenario(host_id, tenant, update=False)
    _ensure_triggers(host_id, host_name, tenant)
    return host_id


def _ensure_host_tags(host_id: str, tenant: TenantInfo) -> None:
    """Ensure host has the expected tenant-related tags, preserving any others."""
    desired = {
        "tenantId": str(tenant.tenant_code),
        "licenseStatus": tenant.license_status,
        "expiredDate": tenant.expired_date,
    }

    hosts = _zapi.host.get(
        hostids=host_id,
        selectTags=["tag", "value"],
        output=["hostid"],
    )
    current_tags = hosts[0].get("tags", []) if hosts else []

    # Build map tag -> value so we can merge
    tag_map: dict[str, str] = {t["tag"]: t["value"] for t in current_tags}
    tag_map.update(desired)

    new_tags = [{"tag": k, "value": v} for k, v in tag_map.items()]
    _zapi.host.update(hostid=host_id, tags=new_tags)


def _ensure_web_scenario(host_id: str, tenant: TenantInfo, update: bool) -> None:
    """Create or update a Web Scenario (Zabbix HTTP check every 60s)."""
    scenario_name = f"HTTP Check - {tenant.health_endpoint}"

    if update:
        scenarios = _zapi.httptest.get(
            hostids=host_id,
            filter={"name": scenario_name},
            selectSteps=["httpstepid", "url"],
        )
        if scenarios:
            step = scenarios[0]["steps"][0]
            if step["url"] != tenant.health_endpoint:
                _zapi.httptest.update(
                    httptestid=scenarios[0]["httptestid"],
                    steps=[{"httpstepid": step["httpstepid"], "url": tenant.health_endpoint}],
                )
                logger.info("Updated web scenario for %s", tenant.tenant_code)
            return

    _zapi.httptest.create(
        name=scenario_name,
        hostid=host_id,
        delay="120s",
        retries=2,
        status=0,
        steps=[{
            "name": f"GET {tenant.domain}",
            "url": tenant.health_endpoint,
            "status_codes": "200",
            "no": 1,
            "timeout": "7s",
            "follow_redirects": 1,
        }],
    )
    logger.info("Created web scenario for %s -> %s", tenant.tenant_code, tenant.health_endpoint)


def _ensure_triggers(host_id: str, host_name: str, tenant: TenantInfo) -> None:
    """Create or update DOWN + slow-response triggers for a tenant host."""
    scenario_name = f"HTTP Check - {tenant.health_endpoint}"
    step_name = f"GET {tenant.domain}"
    down_desc = f"[{tenant.tenant_name}] SITE DOWN - {tenant.domain}"
    slow_desc = f"[{tenant.tenant_name}] Slow response - {tenant.domain}"

    # Downtime expression: check if the site is down (status code 404, 500, 503)
    down_expr = (
        f"last(/{host_name}/web.test.rspcode[{scenario_name},{step_name}])=404"
        f" or last(/{host_name}/web.test.rspcode[{scenario_name},{step_name}])=500"
        f" or last(/{host_name}/web.test.rspcode[{scenario_name},{step_name}])=503"
    )
    # Slow response expression: check if the response time is greater than 7 seconds
    slow_expr = f"last(/{host_name}/web.test.time[{scenario_name},{step_name},resp])>7"

    # Create downtime trigger if it doesn't exist
    existing_down = _zapi.trigger.get(
        hostids=host_id,
        filter={"description": down_desc},
        output=["triggerid", "expression"],
    )
    if existing_down:
        trig = existing_down[0]
        if trig["expression"] != down_expr:
            _zapi.trigger.update(triggerid=trig["triggerid"], expression=down_expr)
    else:
        _zapi.trigger.create(
            description=down_desc,
            expression=down_expr,
            priority=4,
            tags=[
                {"tag": "scope", "value": "availability"},
                {"tag": "tenant", "value": tenant.tenant_code},
            ],
        )

    # Create slow response trigger if it doesn't exist
    existing_slow = _zapi.trigger.get(
        hostids=host_id,
        filter={"description": slow_desc},
        output=["triggerid", "expression"],
    )
    if existing_slow:
        trig = existing_slow[0]
        if trig["expression"] != slow_expr:
            _zapi.trigger.update(triggerid=trig["triggerid"], expression=slow_expr)
    else:
        _zapi.trigger.create(
            description=slow_desc,
            expression=slow_expr,
            priority=2,
            tags=[
                {"tag": "scope", "value": "performance"},
                {"tag": "tenant", "value": tenant.tenant_code},
            ],
        )

    logger.info("Ensured triggers for %s", tenant.tenant_code)


def disable_removed_tenants(active_codes: set[str], group_id: str) -> None:
    """Disable hosts whose tenant is no longer in the active list."""
    hosts = _zapi.host.get(groupids=group_id, output=["hostid", "host", "status"])
    normalized_active_codes = {str(code) for code in active_codes}
    for host in hosts:
        code = str(host["host"]).replace("tenant-", "")
        if code not in normalized_active_codes and host["status"] == "0":
            _zapi.host.update(hostid=host["hostid"], status=1)
            logger.warning("Disabled host %s (tenant removed)", host["host"])
