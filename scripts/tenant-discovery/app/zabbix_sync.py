import json
import logging

from pyzabbix import ZabbixAPI, ZabbixAPIException

from . import config
from .tenant_discovery import TenantInfo

logger = logging.getLogger(__name__)

_zapi: ZabbixAPI | None = None


# ---------------------------------------------------------------------------
# Preprocessing scripts (JavaScript, Zabbix preprocessing type=21)
# ---------------------------------------------------------------------------

# Master item captures headers + body (retrieve_mode=2). Response body is
# everything after the first blank line that separates HTTP headers from body.
_JS_EXTRACT_BODY = (
    "var i = value.indexOf('\\r\\n\\r\\n');\n"
    "if (i < 0) i = value.indexOf('\\n\\n');\n"
    "if (i < 0) return value;\n"
    "return value.substring(i).replace(/^\\r?\\n\\r?\\n/, '');"
)

# Status line is the first line of the response: "HTTP/1.1 200 OK".
_JS_EXTRACT_STATUSCODE = (
    "var m = /^HTTP\\/\\S+\\s+(\\d+)/.exec(value);\n"
    "return m ? parseInt(m[1]) : 0;"
)


def _health_endpoint_uses_authenticated_healthcheck(health_endpoint: str) -> bool:
    """True when monitoring the real healthcheck path (POST+JSON); False for site-root fallback GET."""
    rel = config.HEALTHCHECK_REL_PATH.rstrip("/")
    return bool(rel) and health_endpoint.rstrip("/").endswith(rel)


def _monitor_json_body() -> str:
    return json.dumps({"password": config.HEALTHCHECK_API_KEY})


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


def _preprocessing_match(current: object, desired: list[dict[str, object]]) -> bool:
    def _norm(obj: object) -> list[tuple[str, str, str, str]]:
        if not isinstance(obj, list):
            return []
        rows: list[tuple[str, str, str, str]] = []
        for step in obj:
            if not isinstance(step, dict):
                continue
            rows.append((
                str(step.get("type", "")),
                str(step.get("params", "")),
                str(step.get("error_handler", "0")),
                str(step.get("error_handler_params", "")),
            ))
        return rows

    return _norm(current) == _norm(desired)


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


def sync_tenant(tenant: TenantInfo, group_id: str) -> str | None:
    """Create or update Zabbix host + health items + triggers for a tenant.

    Architecture: ONE upstream HTTP call per cycle via the master HTTP-agent
    item; dependent items derive statuscode and response body via preprocessing.
    """
    host_name = tenant.domain
    existing = _zapi.host.get(filter={"host": host_name}, output=["hostid", "status"])

    if existing:
        host_id = existing[0]["hostid"]
        # Host may have been disabled in a previous cycle; re-enable when tenant is active again.
        if existing[0].get("status") == "1":
            _zapi.host.update(hostid=host_id, status=0)
            logger.info("Re-enabled host %s (tenant active again)", host_name)
        _ensure_host_tags(host_id, tenant)
        _ensure_host_macros(host_id, tenant)
        master_itemid = _ensure_health_master_item(host_id, tenant)
        _ensure_health_statuscode_item(host_id, master_itemid, tenant)
        _ensure_health_response_item(host_id, master_itemid, tenant)
        _ensure_triggers(host_id, host_name, tenant)
        # Legacy cleanup AFTER new structure is in place (avoids monitoring gap)
        _cleanup_legacy_slow_trigger(host_id, tenant)
        _cleanup_legacy_web_scenario(host_id, tenant)
        return host_id

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
    master_itemid = _ensure_health_master_item(host_id, tenant)
    _ensure_health_statuscode_item(host_id, master_itemid, tenant)
    _ensure_health_response_item(host_id, master_itemid, tenant)
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


def _ensure_health_master_item(host_id: str, tenant: TenantInfo) -> str:
    """Single HTTP agent item that hits the healthcheck endpoint once per cycle.

    - retrieve_mode=2 captures full response (status line + headers + body)
      so dependent items can derive both the HTTP status code and the body
      without issuing a second request.
    - status_codes='100-599' accepts any HTTP response; the item never goes
      UNSUPPORTED on 4xx/5xx, so we can still trigger on them.
    - history='0' avoids persisting large raw dumps; dependent items keep
      their own, smaller history.

    Returns the itemid so dependents can link to it.
    """
    item_key = config.ZABBIX_HEALTH_MASTER_ITEM_KEY
    item_name = config.ZABBIX_HEALTH_MASTER_ITEM_NAME

    monitor = _http_item_monitor_fields(tenant.health_endpoint)

    existing = _zapi.item.get(
        hostids=host_id,
        filter={"key_": item_key},
        output=[
            "itemid", "name", "url", "delay", "timeout", "request_method",
            "post_type", "posts", "headers", "retrieve_mode", "status_codes",
            "history", "trends", "follow_redirects",
        ],
    )

    base_payload: dict[str, object] = {
        "name": item_name,
        "type": 19,  # HTTP agent
        "key_": item_key,
        "hostid": host_id,
        "value_type": 4,  # text
        "url": tenant.health_endpoint,
        "timeout": config.ZABBIX_WEB_CHECK_TIMEOUT,
        "delay": config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY,
        "history": "0",
        "trends": "0",
        "follow_redirects": config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS,
        "retrieve_mode": 2,
        "status_codes": "100-599",
        "allow_traps": 0,
        **monitor,
    }

    if existing:
        current = existing[0]
        itemid = current["itemid"]
        patch: dict[str, object] = {"itemid": itemid}
        if current.get("name") != item_name:
            patch["name"] = item_name
        if current.get("url") != tenant.health_endpoint:
            patch["url"] = tenant.health_endpoint
        if current.get("timeout") != config.ZABBIX_WEB_CHECK_TIMEOUT:
            patch["timeout"] = config.ZABBIX_WEB_CHECK_TIMEOUT
        if current.get("delay") != config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY:
            patch["delay"] = config.ZABBIX_HEALTH_RESPONSE_ITEM_DELAY
        if int(current.get("retrieve_mode", -1) or -1) != 2:
            patch["retrieve_mode"] = 2
        if current.get("status_codes") != "100-599":
            patch["status_codes"] = "100-599"
        if str(current.get("history", "")) != "0":
            patch["history"] = "0"
        if str(current.get("trends", "")) != "0":
            patch["trends"] = "0"
        if int(current.get("follow_redirects", -1) or -1) != int(config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS):
            patch["follow_redirects"] = config.ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS
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
            logger.info("Updated health master item for %s", tenant.domain)
        return itemid

    result = _zapi.item.create(**base_payload)
    itemid = result["itemids"][0]
    logger.info("Created health master item for %s -> %s", tenant.domain, tenant.health_endpoint)
    return itemid


def _ensure_health_statuscode_item(host_id: str, master_itemid: str, tenant: TenantInfo) -> None:
    """Dependent unsigned-int item: HTTP status code parsed from the master value."""
    item_key = config.ZABBIX_HEALTH_STATUSCODE_ITEM_KEY
    item_name = config.ZABBIX_HEALTH_STATUSCODE_ITEM_NAME

    preprocessing = [{
        "type": "21",  # JavaScript
        "params": _JS_EXTRACT_STATUSCODE,
        "error_handler": "0",
        "error_handler_params": "",
    }]

    existing = _zapi.item.get(
        hostids=host_id,
        filter={"key_": item_key},
        output=["itemid", "name", "type", "master_itemid", "value_type", "history", "trends"],
        selectPreprocessing="extend",
    )

    if existing and str(existing[0].get("type", "")) != "18":
        # Legacy standalone item using this key — delete so we can recreate as dependent.
        _zapi.item.delete(existing[0]["itemid"])
        logger.info("Deleted legacy standalone %s for %s (now dependent item)", item_key, tenant.domain)
        existing = []

    base_payload: dict[str, object] = {
        "name": item_name,
        "type": 18,  # Dependent item
        "key_": item_key,
        "hostid": host_id,
        "value_type": 3,  # unsigned int
        "master_itemid": master_itemid,
        "history": "7d",
        "trends": "0",
        "preprocessing": preprocessing,
    }

    if existing:
        current = existing[0]
        patch: dict[str, object] = {"itemid": current["itemid"]}
        if current.get("name") != item_name:
            patch["name"] = item_name
        if str(current.get("master_itemid", "")) != str(master_itemid):
            patch["master_itemid"] = master_itemid
        if int(current.get("value_type", -1) or -1) != 3:
            patch["value_type"] = 3
        if str(current.get("history", "")) != "7d":
            patch["history"] = "7d"
        if str(current.get("trends", "")) != "0":
            patch["trends"] = "0"
        if not _preprocessing_match(current.get("preprocessing", []), preprocessing):
            patch["preprocessing"] = preprocessing
        if len(patch) > 1:
            _zapi.item.update(**patch)
            logger.info("Updated health statuscode item for %s", tenant.domain)
    else:
        _zapi.item.create(**base_payload)
        logger.info("Created health statuscode item for %s", tenant.domain)


def _ensure_health_response_item(host_id: str, master_itemid: str, tenant: TenantInfo) -> None:
    """Dependent text item: response body extracted from master (headers stripped)."""
    item_key = config.ZABBIX_HEALTH_RESPONSE_ITEM_KEY
    item_name = config.ZABBIX_HEALTH_RESPONSE_ITEM_NAME

    preprocessing = [{
        "type": "21",  # JavaScript
        "params": _JS_EXTRACT_BODY,
        "error_handler": "0",
        "error_handler_params": "",
    }]

    existing = _zapi.item.get(
        hostids=host_id,
        filter={"key_": item_key},
        output=["itemid", "name", "type", "master_itemid", "value_type", "history", "trends"],
        selectPreprocessing="extend",
    )

    if existing and str(existing[0].get("type", "")) != "18":
        # Legacy standalone HTTP agent item — delete so we can recreate as dependent.
        _zapi.item.delete(existing[0]["itemid"])
        logger.info("Deleted legacy standalone %s for %s (now dependent item)", item_key, tenant.domain)
        existing = []

    base_payload: dict[str, object] = {
        "name": item_name,
        "type": 18,  # Dependent item
        "key_": item_key,
        "hostid": host_id,
        "value_type": 4,  # text
        "master_itemid": master_itemid,
        "history": "7d",
        "trends": "0",
        "preprocessing": preprocessing,
    }

    if existing:
        current = existing[0]
        patch: dict[str, object] = {"itemid": current["itemid"]}
        if current.get("name") != item_name:
            patch["name"] = item_name
        if str(current.get("master_itemid", "")) != str(master_itemid):
            patch["master_itemid"] = master_itemid
        if int(current.get("value_type", -1) or -1) != 4:
            patch["value_type"] = 4
        if str(current.get("history", "")) != "7d":
            patch["history"] = "7d"
        if str(current.get("trends", "")) != "0":
            patch["trends"] = "0"
        if not _preprocessing_match(current.get("preprocessing", []), preprocessing):
            patch["preprocessing"] = preprocessing
        if len(patch) > 1:
            _zapi.item.update(**patch)
            logger.info("Updated health response item for %s", tenant.domain)
    else:
        _zapi.item.create(**base_payload)
        logger.info("Created health response item for %s", tenant.domain)


def _ensure_triggers(host_id: str, host_name: str, tenant: TenantInfo) -> None:
    """Create or update the DOWN trigger using statuscode + response items."""
    down_desc = f"[{tenant.tenant_name}] SITE DOWN - {tenant.domain}"

    codes = [c.strip() for c in config.ZABBIX_TRIGGER_DOWN_RSP_CODES.split(",") if c.strip()]
    if not codes:
        codes = ["500", "502", "503", "504"]

    statuscode_key = config.ZABBIX_HEALTH_STATUSCODE_ITEM_KEY
    response_key = config.ZABBIX_HEALTH_RESPONSE_ITEM_KEY

    rspcode_parts = [
        f"last(/{host_name}/{statuscode_key})={c}"
        for c in codes
    ]
    # Reference the response item so {ITEM.VALUE2} resolves in opdata.
    # length() is always >=0, so <0 is never true — clause never fires.
    response_ref = f"length(last(/{host_name}/{response_key}))<0"
    down_expr = " or ".join(rspcode_parts) + " or " + response_ref
    down_opdata = (
        f"Status code: {{ITEM.VALUE1}}\n"
        f"Response: {{ITEM.VALUE2}}"
    )

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
            logger.info("Updated down trigger for %s", tenant.domain)
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
        logger.info("Created down trigger for %s", tenant.domain)

    logger.info("Ensured triggers for %s", tenant.domain)


def _cleanup_legacy_web_scenario(host_id: str, tenant: TenantInfo) -> None:
    """Remove legacy Web Scenario(s): replaced by the HTTP agent master item.

    Runs every sync cycle — it's a no-op when nothing left to clean.
    """
    scenarios = _zapi.httptest.get(hostids=host_id, output=["httptestid", "name"])
    if not scenarios:
        return
    ids = [s["httptestid"] for s in scenarios]
    _zapi.httptest.delete(*ids)
    logger.info(
        "Deleted %d legacy web scenario(s) for %s (double-hit eliminated)",
        len(ids),
        tenant.domain,
    )


def _cleanup_legacy_slow_trigger(host_id: str, tenant: TenantInfo) -> None:
    """Remove legacy slow-response trigger — it referenced web.test.time which no longer exists."""
    slow_desc = f"[{tenant.tenant_name}] Slow response - {tenant.domain}"
    existing = _zapi.trigger.get(
        hostids=host_id,
        filter={"description": slow_desc},
        output=["triggerid"],
    )
    if not existing:
        return
    ids = [t["triggerid"] for t in existing]
    _zapi.trigger.delete(*ids)
    logger.info("Deleted legacy slow-response trigger for %s", tenant.domain)


def migrate_legacy_hosts(group_id: str) -> None:
    """One-time migration: rename hosts still using the old 'tenant-<uuid>' scheme to domain.

    Looks up the {$TENANT.DOMAIN} macro on each old-style host and renames the
    technical host name (host field) to the domain value. Safe to call every
    cycle — hosts already using a domain name are ignored.
    """
    hosts = _zapi.host.get(
        groupids=group_id,
        output=["hostid", "host"],
        selectMacros=["hostmacroid", "macro", "value"],
    )
    for host in hosts:
        name: str = host["host"]
        # Old pattern: "tenant-" followed by a UUID (36 chars) or any non-domain string
        if not name.startswith("tenant-"):
            continue
        macros: list[dict] = host.get("macros", [])
        domain = next(
            (m["value"] for m in macros if m.get("macro") == "{$TENANT.DOMAIN}"),
            None,
        )
        if not domain:
            logger.info(
                "Legacy host %s has no {$TENANT.DOMAIN} macro; "
                "will be disabled by disable_removed_tenants after domain-named host is synced",
                name,
            )
            continue
        # Check no host with the target domain name already exists
        conflict = _zapi.host.get(filter={"host": domain}, output=["hostid"])
        if conflict:
            logger.warning(
                "Cannot rename %s -> %s: target host already exists (hostid=%s)",
                name,
                domain,
                conflict[0]["hostid"],
            )
            continue
        _zapi.host.update(hostid=host["hostid"], host=domain)
        logger.info("Migrated host %s -> %s", name, domain)


def disable_removed_tenants(active_domains: set[str], group_id: str) -> None:
    """Disable hosts whose domain is no longer in the active list."""
    hosts = _zapi.host.get(groupids=group_id, output=["hostid", "host", "status"])
    normalized_active = {str(d) for d in active_domains}
    for host in hosts:
        if host["host"] not in normalized_active and host["status"] == "0":
            _zapi.host.update(hostid=host["hostid"], status=1)
            logger.warning("Disabled host %s (tenant removed)", host["host"])
