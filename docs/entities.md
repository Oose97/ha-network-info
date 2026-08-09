# Entities & services

All entities live on one **Network Info** service device per config entry.

| Entity | Type | What it holds |
|---|---|---|
| `sensor.network_info_devices` | Sensor | State = number of **online** devices. Attributes carry the full device list and the counters below. |
| `sensor.network_info_home_assistant_ip` | Sensor (diagnostic) | Home Assistant's own local IP, detected via HA's network helper. |
| `button.network_info_scan_now` | Button | Runs a full cycle (scan + router poll) immediately, outside the regular interval. |
| `sensor.network_info_external_ip` | Sensor (opt-in) | The network's current public IP, fetched from `api.ipify.org` each cycle. Only created when external IP tracking (or logging) is enabled. |
| `sensor.network_info_external_ip_log` | Sensor (opt-in) | External IP change history. State is the **row count** — a state increase is a clean automation trigger for "the IP changed". Rows live in the `log` attribute as `{date, ip}` pairs (newest last, capped at 500, excluded from the recorder). |

## The `devices` attribute

One entry per device — everything the integration knows, remembered or live:

| Key | Example | Source |
|---|---|---|
| `ip` | `192.168.1.23` | scan / router |
| `mac` | `aa:bb:cc:dd:ee:ff` | scan / router |
| `name` | `Living Room TV` | best of: HA name → router name → hostname → vendor |
| `hostname` | `tv-livingroom.lan` | reverse DNS |
| `vendor` | `TP-Link Systems Inc.` | MAC OUI |
| `connection` | `Router`, `2.4 GHz`, `5 GHz`, `LAN`, `Guest`, `Wi-Fi`, `Unknown` | router API; `Router` marks the configured router address itself (works without a password); last known value is kept in scan-only mode |
| `signal` | `58` | router, wireless clients only |
| `online` | `true` | scan / router |
| `router_name` | `MyPhone` | router |
| `ha_device` | `Living Room TV` | HA device registry (matched by MAC) |
| `ha_area` | `Living Room` | HA area registry |
| `first_seen` | `2026-08-09T07:00:00+00:00` | integration memory |
| `last_seen` | `2026-08-09T09:15:00+00:00` | integration memory — last time seen **online** |
| `sources` | `["scan", "router"]` | who saw the device this cycle; `["memory"]` = remembered, currently absent |

## Other attributes on the devices sensor

| Attribute | Meaning |
|---|---|
| `counts` | `total`, `online`, `offline`, and online devices per path: `lan`, `wifi_2_4_ghz`, `wifi_5_ghz`, `guest`, `wifi_other`, `unknown`. |
| `router_available` | `true`/`false` while a router is configured; `null` when running scan-only. The card uses this to gate path grouping. |
| `router_model` | Hardware id reported by the router, when available. |
| `ha_ip` | Same value as the diagnostic sensor. |
| `last_scan` | UTC timestamp of the last completed cycle. |

The `devices` list is bulky and changes every cycle, so it is excluded from the
recorder automatically — dashboards see it live, but it is never written to the
database.

## Services

| Service | Fields | What it does |
|---|---|---|
| `network_info.forget_device` | `mac` (required) | Removes the device from the persistent memory. A device that is still on the network reappears on the next scan with fresh history; use this to prune stale offline rows. |
| `network_info.import_ip_log` | `path` (required) | Imports external IP history from a CSV file (one `date,ip` pair per line) into the change log. Rows merge by date and consecutive duplicate IPs collapse, so it is safe to run twice. The file must live inside the HA configuration directory; requires IP change logging to be enabled. Returns the resulting row count. |
