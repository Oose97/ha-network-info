"""Constants for the Network Info integration."""

from __future__ import annotations

DOMAIN = "network_info"

URL_CARDS = "/network-info-cards"

STORAGE_VERSION = 1
SERVICE_FORGET_DEVICE = "forget_device"
SERVICE_IMPORT_IP_LOG = "import_ip_log"


def storage_key(entry_id: str) -> str:
    """Location of the per-entry seen-device memory in .storage."""
    return f"{DOMAIN}.{entry_id}"

CONF_IP_RANGE = "ip_range"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ROUTER_HOST = "router_host"
CONF_ROUTER_PASSWORD = "router_password"
CONF_EXTERNAL_IP = "external_ip"
CONF_EXTERNAL_IP_LOG = "external_ip_log"

# The integration's only internet call, and only when the user opted in.
EXTERNAL_IP_URL = "https://api.ipify.org/?format=json"
IP_LOG_MAX_ROWS = 500

DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_SCAN_INTERVAL_MINUTES = 1

# Connection-path labels shown to the user. The router provider decides which
# one applies to a client; the scanner alone can never know the path.
CONNECTION_ROUTER = "Router"
CONNECTION_LAN = "LAN"
CONNECTION_WIFI_24 = "2.4 GHz"
CONNECTION_WIFI_5 = "5 GHz"
CONNECTION_GUEST = "Guest"
CONNECTION_WIFI = "Wi-Fi"
CONNECTION_UNKNOWN = "Unknown"

# Slugs used for the per-path counters in sensor attributes.
CONNECTION_SLUGS = {
    CONNECTION_ROUTER: "router",
    CONNECTION_LAN: "lan",
    CONNECTION_WIFI_24: "wifi_2_4_ghz",
    CONNECTION_WIFI_5: "wifi_5_ghz",
    CONNECTION_GUEST: "guest",
    CONNECTION_WIFI: "wifi_other",
    CONNECTION_UNKNOWN: "unknown",
}

ATTR_DEVICES = "devices"
ATTR_COUNTS = "counts"
ATTR_HA_IP = "ha_ip"
ATTR_ROUTER_AVAILABLE = "router_available"
ATTR_ROUTER_MODEL = "router_model"
ATTR_LAST_SCAN = "last_scan"
ATTR_IP_LOG = "log"


def ip_log_storage_key(entry_id: str) -> str:
    """Location of the per-entry external IP log in .storage."""
    return f"{DOMAIN}.{entry_id}_ip_log"
