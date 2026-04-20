import json
import logging

from pyzabbix import ZabbixAPI, ZabbixAPIException

from . import config
from .tenant_discovery import TenantInfo

logger = logging.getLogger(__name__)

_zapi: ZabbixAPI | None = None


def _uses_authenticated_healthcheck(health_endpoint: str) -> bool:
    rel = config.HEALTHCHECK_REL_PATH.rstrip("/")
    return bool(rel) and health_endpoint.rstrip("/").endswith(rel)


def _step_extras(health_endpoint: str) -> tuple[str, list[dict[str, str]]]:
    if _uses_authenticated_healthcheck(health_endpoint):
        body = json.dumps({"password": config.HEALTHCHECK_API_KEY})
        return body, [{"name": "Content-Type", "value": "application/json"}]
    return "", []


def _headers_match(a: object, b: object) -> bool:
    def _norm(obj: object) -> list[tuple[str, str]]:
        if not isinstance(obj, list) or not obj:
            return []
        return sorted((str(e.get("name", "")), str(e.get("value", ""))) for e in obj if isinstance(e, dict))

    return _norm(a) == _norm(b)


def connect() -> None:
    global _zapi
    if _zapi is not None:
        return
    _zapi = ZabbixAPI(config.ZABBIX_API_URL)
    _zapi.login(config.ZABBIX_API_USER, config.ZABBIX_API_PASSWORD)
    logger.info("Connected to Zabbix %s", _zapi.api_version())


def ensure_host_group() -> str:
    groups = _zapi.hostgroup.get(filter={"name": [config.ZABBIX_HOST_GROUP]})
    if groups:
        return groups[0]["groupid"]
    result = _zapi.hostgroup.create(name=config.ZABBIX_HOST_GROUP)
    gid = result["groupids"][0]
    logger.info("Created host group '%s' (id=%s)", config.ZABBIX_HOST_GROUP, gid)
    return gid


def sync_tenant(tenant: TenantInfo, group_id: str) -> str | None:
    host_name = tenant.domain
    scenario_name = config.ZABBIX_WEB_SCENARIO_NAME_TEMPLATE.format(domain=tenant.domain)
    step_name = config.ZABBIX_WEB_STEP_NAME_TEMPLATE.format(domain=tenant.domain)

    existing = _zapi.host.get(filter={"host": host_name}, output=["hostid", "status"])
    if existing:
        host_id = existing[0]["hostid"]
        if existing[0].get("status") == "1":
            _zapi.host.update(hostid=host_id, status=0)
            logger.info("Re-enabled host %s", host_name)
    else:
        visible_name = tenant.tenant_name if tenant.tenant_name != tenant.domain else tenant.domain
        try:
            result = _zapi.host.create(
                host=host_name,
                name=visible_name,
                groups=[{"groupid": group_id}],
                tags=[
                    {"tag": "tenantId", "value": tenant.tenant_code},
                    {"tag": "licenseStatus", "value": tenant.license_status},
                    {"tag": "expiredDate", "value": tenant.expired_date},
                ],
            )
        except ZabbixAPIException as exc:
            if "same visible name" in str(exc) or "same technical name" in str(exc):
                logger.warning("Skipping host create for %s: %s", host_name, exc)
                return None
            raise
        host_id = result["hostids"][0]
        logger.info("Created host %s (id=%s)", host_name, host_id)

    _ensure_host_tags(host_id, tenant)
    _ensure_host_macros(host_id, tenant)
    _ensure_web_scenario(host_id, tenant, scenario_name=scenario_name, step_name=step_name)
    _ensure_health_response_item(host_id, tenant, scenario_name=scenario_name, step_name=step_name)
    _ensure_triggers(host_id, host_name, tenant, scenario_name=scenario_name, step_name=step_name)
    return host_id


def _ensure_host_tags(host_id: str, tenant: TenantInfo) -> None:
    desired = {
        "tenantId": str(tenant.tenant_code),
        "licenseStatus": tenant.license_status,
        "expiredDate": tenant.expired_date,
    }
    hosts = _zapi.host.get(hostids=host_id, selectTags=["tag", "value"], output=["hostid"])
    current_tags = hosts[0].get("tags", []) if hosts else []
    tag_map: dict[str, str] = {t["tag"]: t["value"] for t in current_tags}
    tag_map.update(desired)
    _zapi.host.update(hostid=host_id, tags=[{"tag": k, "value": v} for k, v in tag_map.items()])


def _ensure_host_macros(host_id: str, tenant: TenantInfo) -> None:
    macro_name = "{$TENANT.DOMAIN}"
    hosts = _zapi.host.get(hostids=host_id, selectMacros=["hostmacroid", "macro", "value"], output=["hostid"])
    current = hosts[0].get("macros", []) if hosts else []
    found = next((m for m in current if m.get("macro") == macro_name), None)
    if found and found.get("value") == tenant.domain:
        return
    if found:
        _zapi.usermacro.update(hostmacroid=found["hostmacroid"], value=tenant.domain)
    else:
        _zapi.usermacro.create(hostid=host_id, macro=macro_name, value=tenant.domain)


def _ensure_web_scenario(host_id: str, tenant: TenantInfo, *, scenario_name: str, step_name: str) -> None:
    desired_posts, desired_headers = _step_extras(tenant.health_endpoint)
    scenarios = _zapi.httptest.get(
        hostids=host_id,
        selectSteps=["httpstepid", "url", "name", "posts", "headers"],
        output=["httptestid", "name"],
    )

    if scenarios:
        # Keep only the scenario matching our managed name; delete everything else (including
        # legacy "Healthcheck response body" scenarios that made duplicate HTTP requests).
        chosen = next((s for s in scenarios if s.get("name") == scenario_name), None) or scenarios[0]

        extra_ids = [s["httptestid"] for s in scenarios if s["httptestid"] != chosen["httptestid"]]
        if extra_ids:
            _zapi.httptest.delete(*extra_ids)
            logger.info(
                "Deleted %d extra web scenario(s) for hostid=%s: %s",
                len(extra_ids),
                host_id,
                [s["name"] for s in scenarios if s["httptestid"] != chosen["httptestid"]],
            )

        step = chosen["steps"][0]
        payload: dict[str, object] = {"httptestid": chosen["httptestid"]}
        if chosen.get("name") != scenario_name:
            payload["name"] = scenario_name
        step_patch: dict[str, object] = {"httpstepid": step["httpstepid"]}
        if step.get("url") != tenant.health_endpoint:
            step_patch["url"] = tenant.health_endpoint
        if step.get("name") != step_name:
            step_patch["name"] = step_name
        if (step.get("posts") or "") != desired_posts:
            step_patch["posts"] = desired_posts
        if not _headers_match(step.get("headers"), desired_headers):
            step_patch["headers"] = desired_headers
        if len(step_patch) > 1:
            payload["steps"] = [step_patch]
        if len(payload) > 1:
            _zapi.httptest.update(**payload)
            logger.info("Updated web scenario for %s", tenant.domain)
        return

    _zapi.httptest.create(
        name=scenario_name,
        hostid=host_id,
        delay=config.ZABBIX_WEB_CHECK_DELAY,
        retries=config.ZABBIX_WEB_CHECK_RETRIES,
        status=0,
        steps=[{
            "name": step_name,
            "url": tenant.health_endpoint,
            "status_codes": config.ZABBIX_WEB_CHECK_STATUS_CODES,
            "no": 1,
            "timeout": config.ZABBIX_WEB_CHECK_TIMEOUT,
            "follow_redirects": config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS,
            "posts": desired_posts,
            "headers": desired_headers,
        }],
    )
    logger.info("Created web scenario for %s -> %s", tenant.domain, tenant.health_endpoint)


def _ensure_health_response_item(host_id: str, tenant: TenantInfo, scenario_name: str, step_name: str) -> None:
    """Dependent Item (type=18) that reads the web scenario response body — no extra HTTP request.

    Zabbix feeds the value from web.test.body[scenario,step] on each check cycle.
    Reference in Telegram alert templates: {?last(/{HOST.HOST}/healthcheck.response.raw)}
    """
    item_key = config.ZABBIX_HEALTH_RESPONSE_ITEM_KEY
    item_name = config.ZABBIX_HEALTH_RESPONSE_ITEM_NAME

    master_key = f"web.test.body[{scenario_name},{step_name}]"
    master_items = _zapi.item.get(hostids=host_id, filter={"key_": master_key}, output=["itemid"])
    if not master_items:
        logger.warning("Master item '%s' not found for %s — skipping dependent item", master_key, tenant.domain)
        return
    master_itemid = master_items[0]["itemid"]

    existing = _zapi.item.get(
        hostids=host_id,
        filter={"key_": item_key},
        output=["itemid", "name", "master_itemid", "type"],
    )

    if existing:
        current = existing[0]
        if int(current.get("type", -1) or -1) != 18:
            # Legacy HTTP Agent item (type=19) or any other non-dependent type — delete and recreate.
            _zapi.item.delete(current["itemid"])
            logger.info("Deleted legacy item (type=%s) for %s, recreating as Dependent", current.get("type"), tenant.domain)
            existing = []
        else:
            patch: dict[str, object] = {"itemid": current["itemid"]}
            if current.get("name") != item_name:
                patch["name"] = item_name
            if str(current.get("master_itemid", "")) != str(master_itemid):
                patch["master_itemid"] = master_itemid
            if len(patch) > 1:
                _zapi.item.update(**patch)
                logger.info("Updated health response item for %s", tenant.domain)
            return

    _zapi.item.create(
        name=item_name,
        type=18,
        key_=item_key,
        hostid=host_id,
        value_type=4,
        master_itemid=master_itemid,
        history="7d",
        trends="0",
    )
    logger.info("Created health response item for %s (master: %s)", tenant.domain, master_key)


def _ensure_triggers(
    host_id: str,
    host_name: str,
    tenant: TenantInfo,
    *,
    scenario_name: str,
    step_name: str,
) -> None:
    down_desc = f"[{tenant.tenant_name}] SITE DOWN - {tenant.domain}"
    slow_desc = f"[{tenant.tenant_name}] Slow response - {tenant.domain}"

    codes = [c.strip() for c in config.ZABBIX_TRIGGER_DOWN_RSP_CODES.split(",") if c.strip()] or ["500", "502", "503", "504"]
    down_expr = " or ".join(
        f"last(/{host_name}/web.test.rspcode[{scenario_name},{step_name}])={c}" for c in codes
    )
    down_opdata = "Status code: {ITEM.VALUE}"
    slow_expr = (
        f"last(/{host_name}/web.test.time[{scenario_name},{step_name},resp])>{config.ZABBIX_WEB_SLOW_SECONDS}"
    )

    managed_descs = {down_desc, slow_desc}

    # DOWN trigger
    existing_down = _zapi.trigger.get(
        hostids=host_id, filter={"description": down_desc}, output=["triggerid", "expression", "opdata"],
    )
    if existing_down:
        trig = existing_down[0]
        patch: dict[str, object] = {"triggerid": trig["triggerid"]}
        if trig["expression"] != down_expr:
            patch["expression"] = down_expr
        if trig.get("opdata", "") != down_opdata:
            patch["opdata"] = down_opdata
        if len(patch) > 1:
            _zapi.trigger.update(**patch)
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

    # Slow response trigger
    existing_slow = _zapi.trigger.get(
        hostids=host_id, filter={"description": slow_desc}, output=["triggerid", "expression"],
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

    # Remove all unmanaged triggers — includes Zabbix built-in "Last error message of scenario..."
    # (flags=0 or flags=2/discovered) and legacy triggers from old code versions.
    # Skip only flags=4 (template-inherited) which Zabbix API refuses to delete.
    all_triggers = _zapi.trigger.get(hostids=host_id, output=["triggerid", "description", "flags"])
    for t in all_triggers:
        if t.get("description") in managed_descs:
            continue
        if int(t.get("flags", 0) or 0) == 4:
            continue
        try:
            _zapi.trigger.delete(t["triggerid"])
            logger.info("Removed unmanaged trigger '%s' (id=%s, flags=%s)", t["description"], t["triggerid"], t.get("flags"))
        except ZabbixAPIException as exc:
            logger.warning("Could not remove trigger '%s' (id=%s): %s", t["description"], t["triggerid"], exc)


def migrate_legacy_hosts(group_id: str) -> None:
    """Rename hosts still using the old 'tenant-<uuid>' scheme to their domain name."""
    hosts = _zapi.host.get(
        groupids=group_id,
        output=["hostid", "host"],
        selectMacros=["hostmacroid", "macro", "value"],
    )
    for host in hosts:
        name: str = host["host"]
        if not name.startswith("tenant-"):
            continue
        domain = next(
            (m["value"] for m in host.get("macros", []) if m.get("macro") == "{$TENANT.DOMAIN}"),
            None,
        )
        if not domain:
            logger.info("Legacy host %s has no {$TENANT.DOMAIN} macro — skipping migration", name)
            continue
        conflict = _zapi.host.get(filter={"host": domain}, output=["hostid"])
        if conflict:
            logger.warning("Cannot rename %s -> %s: target host already exists (id=%s)", name, domain, conflict[0]["hostid"])
            continue
        _zapi.host.update(hostid=host["hostid"], host=domain)
        logger.info("Migrated host %s -> %s", name, domain)


def disable_removed_tenants(active_domains: set[str], group_id: str) -> None:
    """Disable hosts whose domain is no longer in the active tenant list."""
    hosts = _zapi.host.get(groupids=group_id, output=["hostid", "host", "status"])
    normalized_active = {str(d) for d in active_domains}
    for host in hosts:
        if host["host"] not in normalized_active and host["status"] == "0":
            _zapi.host.update(hostid=host["hostid"], status=1)
            logger.warning("Disabled host %s (tenant removed)", host["host"])
