import json
import logging

from pyzabbix import ZabbixAPI

from . import config
from .tenant_discovery import TenantInfo

logger = logging.getLogger(__name__)

_zapi: ZabbixAPI | None = None


def _health_endpoint_uses_authenticated_healthcheck(health_endpoint: str) -> bool:
    """True when monitoring the real healthcheck path (POST+JSON); False for site-root fallback GET."""
    rel = config.HEALTHCHECK_REL_PATH.rstrip("/")
    return bool(rel) and health_endpoint.rstrip("/").endswith(rel)


def _monitor_json_body() -> str:
    return json.dumps({"password": config.HEALTHCHECK_API_KEY})


def _web_scenario_step_extras(health_endpoint: str) -> tuple[str, list[dict[str, str]]]:
    if _health_endpoint_uses_authenticated_healthcheck(health_endpoint):
        return _monitor_json_body(), [
            {"name": "Content-Type", "value": "application/json"},
        ]
    return "", []


def _http_item_monitor_fields(health_endpoint: str) -> dict[str, object]:
    if _health_endpoint_uses_authenticated_healthcheck(health_endpoint):
        return {
            "request_method": 1,
            "post_type": 2,
            "posts": _monitor_json_body(),
            "headers": [],
        }
    return {
        "request_method": 0,
        "post_type": 0,
        "posts": "",
        "headers": [],
    }


def _headers_match(a: object, b: object) -> bool:
    def _norm(obj: object) -> list[tuple[str, str]]:
        if not isinstance(obj, list) or not obj:
            return []
        rows: list[tuple[str, str]] = []
        for entry in obj:
            if isinstance(entry, dict):
                rows.append((str(entry.get("name", "")), str(entry.get("value", ""))))
        return sorted(rows)

    return _norm(a) == _norm(b)


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
    scenario_name = config.ZABBIX_WEB_SCENARIO_NAME_TEMPLATE.format(domain=tenant.domain)
    step_name = config.ZABBIX_WEB_STEP_NAME_TEMPLATE.format(domain=tenant.domain)
    existing = _zapi.host.get(filter={"host": host_name}, output=["hostid", "status"])

    if existing:
        host_id = existing[0]["hostid"]
        # Host may have been disabled in a previous cycle; re-enable when tenant is active again.
        if existing[0].get("status") == "1":
            _zapi.host.update(hostid=host_id, status=0)
            logger.info("Re-enabled host %s (tenant active again)", host_name)
        _ensure_host_tags(host_id, tenant)
        _ensure_host_macros(host_id, tenant)
        _ensure_web_scenario(host_id, tenant, update=True, scenario_name=scenario_name, step_name=step_name)
        _ensure_health_response_item(host_id, tenant)
        _ensure_triggers(host_id, host_name, tenant, scenario_name=scenario_name, step_name=step_name)
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
    _ensure_host_macros(host_id, tenant)
    _ensure_web_scenario(host_id, tenant, update=False, scenario_name=scenario_name, step_name=step_name)
    _ensure_health_response_item(host_id, tenant)
    _ensure_triggers(host_id, host_name, tenant, scenario_name=scenario_name, step_name=step_name)
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


def _ensure_host_macros(host_id: str, tenant: TenantInfo) -> None:
    """Host macro {$TENANT.DOMAIN} for alert message templates (e.g. SITE DOWN - <domain>)."""
    macro_name = "{$TENANT.DOMAIN}"
    hosts = _zapi.host.get(
        hostids=host_id,
        selectMacros=["hostmacroid", "macro", "value"],
        output=["hostid"],
    )
    current = hosts[0].get("macros", []) if hosts else []
    found = next((m for m in current if m.get("macro") == macro_name), None)
    if found and found.get("value") == tenant.domain:
        return
    if found:
        _zapi.usermacro.update(hostmacroid=found["hostmacroid"], value=tenant.domain)
    else:
        _zapi.usermacro.create(hostid=host_id, macro=macro_name, value=tenant.domain)


def _ensure_web_scenario(
    host_id: str,
    tenant: TenantInfo,
    update: bool,
    *,
    scenario_name: str,
    step_name: str,
) -> None:
    """Create or update a Web Scenario (Zabbix HTTP check)."""
    desired_posts, desired_headers = _web_scenario_step_extras(tenant.health_endpoint)

    if update:
        scenarios = _zapi.httptest.get(
            hostids=host_id,
            selectSteps=["httpstepid", "url", "name", "posts", "headers"],
            output=["httptestid", "name"],
        )
        if scenarios:
            by_name = next((s for s in scenarios if s.get("name") == scenario_name), None)
            chosen = by_name or scenarios[0]
            step = chosen["steps"][0]
            payload: dict[str, object] = {"httptestid": chosen["httptestid"]}
            if chosen.get("name") != scenario_name:
                payload["name"] = scenario_name
            step_patch: dict[str, object] = {"httpstepid": step["httpstepid"]}
            if step.get("url") != tenant.health_endpoint:
                step_patch["url"] = tenant.health_endpoint
            if step.get("name") != step_name:
                step_patch["name"] = step_name
            cur_posts = step.get("posts") if step.get("posts") is not None else ""
            if cur_posts != desired_posts:
                step_patch["posts"] = desired_posts
            if not _headers_match(step.get("headers"), desired_headers):
                step_patch["headers"] = desired_headers
            if len(step_patch) > 1:
                payload["steps"] = [step_patch]
            if len(payload) > 1:
                _zapi.httptest.update(**payload)
                logger.info("Updated web scenario for %s", tenant.tenant_code)
            extra_ids = [
                s["httptestid"] for s in scenarios if s["httptestid"] != chosen["httptestid"]
            ]
            if extra_ids:
                _zapi.httptest.delete(*extra_ids)
                logger.warning(
                    "Removed %d duplicate web scenario(s) for hostid=%s",
                    len(extra_ids),
                    host_id,
                )
            return

    step_create: dict[str, object] = {
        "name": step_name,
        "url": tenant.health_endpoint,
        "status_codes": config.ZABBIX_WEB_CHECK_STATUS_CODES,
        "no": 1,
        "timeout": config.ZABBIX_WEB_CHECK_TIMEOUT,
        "follow_redirects": config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS,
        "posts": desired_posts,
        "headers": desired_headers,
    }
    _zapi.httptest.create(
        name=scenario_name,
        hostid=host_id,
        delay=config.ZABBIX_WEB_CHECK_DELAY,
        retries=config.ZABBIX_WEB_CHECK_RETRIES,
        status=0,
        steps=[step_create],
    )
    logger.info("Created web scenario for %s -> %s", tenant.tenant_code, tenant.health_endpoint)


def _ensure_triggers(
    host_id: str,
    host_name: str,
    tenant: TenantInfo,
    *,
    scenario_name: str,
    step_name: str,
) -> None:
    """Create or update DOWN + slow-response triggers for a tenant host."""
    down_desc = f"[{tenant.tenant_name}] SITE DOWN - {tenant.domain}"
    slow_desc = f"[{tenant.tenant_name}] Slow response - {tenant.domain}"

    codes = [c.strip() for c in config.ZABBIX_TRIGGER_DOWN_RSP_CODES.split(",") if c.strip()]
    if not codes:
        codes = ["500", "502", "503", "504"]
    down_expr_core = " or ".join(
        f"last(/{host_name}/web.test.rspcode[{scenario_name},{step_name}])={c}"
        for c in codes
    )
    # Include response item in expression so {ITEM.LASTVALUE2} is available in opdata/message macros.
    down_expr = (
        f"({down_expr_core}) and "
        f"strlen(last(/{host_name}/{config.ZABBIX_HEALTH_RESPONSE_ITEM_KEY}))>=0"
    )
    down_opdata = "Status code: {ITEM.LASTVALUE1}\nResponse: {ITEM.LASTVALUE2}"
    slow_expr = (
        f"last(/{host_name}/web.test.time[{scenario_name},{step_name},resp])>"
        f"{config.ZABBIX_WEB_SLOW_SECONDS}"
    )

    # Create downtime trigger if it doesn't exist
    existing_down = _zapi.trigger.get(
        hostids=host_id,
        filter={"description": down_desc},
        output=["triggerid", "expression", "opdata"],
    )
    if existing_down:
        trig = existing_down[0]
        payload: dict[str, object] = {"triggerid": trig["triggerid"]}
        if trig["expression"] != down_expr:
            payload["expression"] = down_expr
        if trig.get("opdata", "") != down_opdata:
            payload["opdata"] = down_opdata
        if len(payload) > 1:
            _zapi.trigger.update(**payload)
    else:
        _zapi.trigger.create(
            description=down_desc,
            expression=down_expr,
            opdata=down_opdata,
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

    logger.info("Ensured triggers for %s", tenant.domain)


def _ensure_health_response_item(host_id: str, tenant: TenantInfo) -> None:
    """Create or update an HTTP agent item to expose raw healthcheck response in alerts."""
    item_key = config.ZABBIX_HEALTH_RESPONSE_ITEM_KEY
    item_name = config.ZABBIX_HEALTH_RESPONSE_ITEM_NAME

    monitor = _http_item_monitor_fields(tenant.health_endpoint)

    existing = _zapi.item.get(
        hostids=host_id,
        filter={"key_": item_key},
        output=[
            "itemid", "name", "url", "delay", "timeout",
            "request_method", "post_type", "posts", "headers",
        ],
    )

    payload: dict[str, object] = {
        "name": item_name,
        "type": 19,  # HTTP agent
        "key_": item_key,
        "hostid": host_id,
        "value_type": 4,  # text
        "url": tenant.health_endpoint,
        "timeout": config.ZABBIX_WEB_CHECK_TIMEOUT,
        "delay": config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY,
        "history": "7d",
        "trends": "0",
        "follow_redirects": config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS,
        **monitor,
    }

    if existing:
        current = existing[0]
        patch: dict[str, object] = {"itemid": current["itemid"]}
        if current.get("name") != item_name:
            patch["name"] = item_name
        if current.get("url") != tenant.health_endpoint:
            patch["url"] = tenant.health_endpoint
        if current.get("timeout") != config.ZABBIX_WEB_CHECK_TIMEOUT:
            patch["timeout"] = config.ZABBIX_WEB_CHECK_TIMEOUT
        if current.get("delay") != config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY:
            patch["delay"] = config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY
        if int(current.get("request_method", -1) or -1) != int(monitor["request_method"]):
            patch["request_method"] = monitor["request_method"]
        if int(current.get("post_type", -1) or -1) != int(monitor["post_type"]):
            patch["post_type"] = monitor["post_type"]
        cur_posts = current.get("posts") if current.get("posts") is not None else ""
        if cur_posts != str(monitor["posts"]):
            patch["posts"] = monitor["posts"]
        if not _headers_match(current.get("headers"), monitor["headers"]):
            patch["headers"] = monitor["headers"]
        if len(patch) > 1:
            _zapi.item.update(**patch)
    else:
        _zapi.item.create(**payload)


def disable_removed_tenants(active_codes: set[str], group_id: str) -> None:
    """Disable hosts whose tenant is no longer in the active list."""
    hosts = _zapi.host.get(groupids=group_id, output=["hostid", "host", "status"])
    normalized_active_codes = {str(code) for code in active_codes}
    for host in hosts:
        code = str(host["host"]).replace("tenant-", "")
        if code not in normalized_active_codes and host["status"] == "0":
            _zapi.host.update(hostid=host["hostid"], status=1)
            logger.warning("Disabled host %s (tenant removed)", host["host"])
