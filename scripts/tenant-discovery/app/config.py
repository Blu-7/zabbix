import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

TENANT_API_URL = os.getenv("TENANT_API_URL", "https://quyettam.cloud/get-tenant")
TENANT_API_KEY = os.getenv("TENANT_API_KEY", "")
TENANT_API_TIMEOUT = int(os.getenv("TENANT_API_TIMEOUT", "30"))

ZABBIX_API_URL = os.getenv("ZABBIX_API_URL", "http://zabbix-web:8080/api_jsonrpc.php")
ZABBIX_API_USER = os.getenv("ZABBIX_API_USER", "Admin")
ZABBIX_API_PASSWORD = os.getenv("ZABBIX_API_PASSWORD", "zabbix")
ZABBIX_HOST_GROUP = os.getenv("ZABBIX_HOST_GROUP", "Tenant Web Services")

DISCOVERY_INTERVAL = int(os.getenv("DISCOVERY_INTERVAL", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
