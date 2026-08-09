"""Constants for the Network Info integration."""

from __future__ import annotations

DOMAIN = "network_info"

CONF_IP_RANGE = "ip_range"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ROUTER_HOST = "router_host"
CONF_ROUTER_PASSWORD = "router_password"

DEFAULT_SCAN_INTERVAL_MINUTES = 15
MIN_SCAN_INTERVAL_MINUTES = 1

# Connection-path labels shown to the user. The router provider decides which
# one applies to a client; the scanner alone can never know the path.
CONNECTION_LAN = "LAN"
CONNECTION_WIFI_24 = "2.4 GHz"
CONNECTION_WIFI_5 = "5 GHz"
CONNECTION_GUEST = "Guest"
CONNECTION_WIFI = "Wi-Fi"
CONNECTION_UNKNOWN = "Unknown"

# Slugs used for the per-path counters in sensor attributes.
CONNECTION_SLUGS = {
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
