import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())


def _normalize_rel_path(path: str) -> str:
    p = (path or "").strip()
    return p if p.startswith("/") else "/" + p


TENANT_API_URL = os.getenv("TENANT_API_URL", "https://quyettam.cloud/get-tenant.php")
TENANT_API_KEY = os.getenv("TENANT_API_KEY", "")
TENANT_API_TIMEOUT = int(os.getenv("TENANT_API_TIMEOUT", "30"))

HEALTHCHECK_PROBE_TIMEOUT = int(os.getenv("HEALTHCHECK_PROBE_TIMEOUT", "10"))
HEALTHCHECK_REL_PATH = _normalize_rel_path(
    os.getenv("HEALTHCHECK_REL_PATH", "/webhooks/healthcheck.php"),
)
HEALTHCHECK_API_KEY = os.getenv("HEALTHCHECK_API_KEY", TENANT_API_KEY)
HEALTHCHECK_PROBE_USER_AGENT = os.getenv("HEALTHCHECK_PROBE_USER_AGENT", "tenant-discovery/1.0")
HEALTHCHECK_PROBE_ACCEPT = os.getenv("HEALTHCHECK_PROBE_ACCEPT", "*/*")
HEALTHCHECK_FALLBACK_HTTP_STATUS = int(os.getenv("HEALTHCHECK_FALLBACK_HTTP_STATUS", "404"))

ZABBIX_API_URL = os.getenv("ZABBIX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
ZABBIX_API_USER = os.getenv("ZABBIX_API_USER", "Admin")
ZABBIX_API_PASSWORD = os.getenv("ZABBIX_API_PASSWORD", "zabbix")
ZABBIX_HOST_GROUP = os.getenv("ZABBIX_HOST_GROUP", "Tenant Web Services")

# Web scenario + trigger tuning (Zabbix web.test.* checks)
ZABBIX_WEB_SCENARIO_NAME_TEMPLATE = os.getenv("ZABBIX_WEB_SCENARIO_NAME_TEMPLATE", "HTTP Check - {domain}")
ZABBIX_WEB_STEP_NAME_TEMPLATE = os.getenv("ZABBIX_WEB_STEP_NAME_TEMPLATE", "GET {domain}")
ZABBIX_WEB_CHECK_DELAY = os.getenv("ZABBIX_WEB_CHECK_DELAY", "120s")
ZABBIX_WEB_CHECK_RETRIES = int(os.getenv("ZABBIX_WEB_CHECK_RETRIES", "1"))
ZABBIX_WEB_CHECK_STATUS_CODES = os.getenv("ZABBIX_WEB_CHECK_STATUS_CODES", "200")
ZABBIX_WEB_CHECK_TIMEOUT = os.getenv("ZABBIX_WEB_CHECK_TIMEOUT", "10s")
ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS = int(os.getenv("ZABBIX_WEB_CHECK_FOLLOW_REDIRECTS", "1"))
ZABBIX_WEB_SLOW_SECONDS = int(os.getenv("ZABBIX_WEB_SLOW_SECONDS", "10"))
ZABBIX_TRIGGER_DOWN_RSP_CODES = os.getenv("ZABBIX_TRIGGER_DOWN_RSP_CODES", "500,502,503,504")
ZABBIX_HEALTH_RESPONSE_ITEM_KEY = os.getenv("ZABBIX_HEALTH_RESPONSE_ITEM_KEY", "healthcheck.response.raw")
ZABBIX_HEALTH_RESPONSE_ITEM_NAME = os.getenv("ZABBIX_HEALTH_RESPONSE_ITEM_NAME", "Healthcheck response body")
ZABBIX_HEALTH_RESPONSE_ITEM_DELAY = os.getenv("ZABBIX_HEALTH_RESPONSE_ITEM_DELAY", ZABBIX_WEB_CHECK_DELAY)

DISCOVERY_INTERVAL = int(os.getenv("DISCOVERY_INTERVAL", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
