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
| `connection` | `Router`, `Access point`, `2.4 GHz`, `5 GHz`, `6 GHz`, `LAN`, `Guest`, `Wi-Fi`, `Unknown` | router API; `Router` and `Access point` mark the configured infrastructure addresses themselves (no credentials needed); last known value is kept in scan-only mode |
| `signal` | `58` | router, wireless clients only |
| `online` | `true` | scan / router |
| `router_name` | `MyPhone` | router |
| `access_point` | `Garage AP` | the access point holding the association, or `Main router` |
| `ha_device` | `Living Room TV` | HA device registry — matched by MAC, falling back to IP (config-entry host / configuration URL) for devices whose integration never learns a MAC |
| `ha_area` | `Living Room` | HA area registry |
| `first_seen` | `2026-08-09T07:00:00+00:00` | integration memory |
| `last_seen` | `2026-08-09T09:15:00+00:00` | integration memory — last time seen **online** |
| `sources` | `["scan", "router"]` | who saw the device this cycle; also `access point`, and `["memory"]` = remembered, currently absent |

## Other attributes on the devices sensor

| Attribute | Meaning |
|---|---|
| `counts` | `total`, `online`, `offline`, and online devices per path: `router`, `access_point`, `lan`, `wifi_2_4_ghz`, `wifi_5_ghz`, `wifi_6_ghz`, `guest`, `wifi_other`, `unknown`. |
| `router_available` | `true`/`false` while a router is configured; `null` when running scan-only. The card uses this to gate path grouping. |
| `router_model` | Hardware id reported by the router, when available. |
| `access_points` | One entry per configured access point: `name`, `brand`, `managed` (false when declared without credentials) and `available`. |
| `ha_ip` | Same value as the diagnostic sensor. |
| `last_scan` | UTC timestamp of the last completed cycle. |

The `devices` list is bulky and changes every cycle, so it is excluded from the
recorder automatically — dashboards see it live, but it is never written to the
database.

## Services

| Service | Fields | What it does |
|---|---|---|
| `network_info.set_path` | `mac` (required), `path` (required) | Pins the device's connection path by hand — for devices nothing can observe, such as clients of an access point that cannot be polled. The pin lives in the device's memory record, so it survives restarts, outranks whatever scanning and router polling say, and disappears with the device on `forget_device`. `path: auto` clears the pin. For a device without a known MAC, pass `ip:<address>` as `mac`. The device table card offers the same from its Path column. |
| `network_info.set_name` | `mac` (required), `name` (optional) | Gives the device a name of your choosing, stored in the device memory. It outranks the automatic name chain (HA registry → router → DNS → vendor); an empty or omitted `name` clears it. For a device without a known MAC, pass `ip:<address>` as `mac`. The device table card offers the same from its Name column. |
| `network_info.forget_device` | `mac` (required) | Removes the device from the persistent memory. A device that is still on the network reappears on the next scan with fresh history; use this to prune stale offline rows. |
| `network_info.import_ip_log` | `path` (required) | Imports external IP history from a CSV file (one `date,ip` pair per line) into the change log. Rows merge by date and consecutive duplicate IPs collapse, so it is safe to run twice. The file must live inside the HA configuration directory; requires IP change logging to be enabled. Returns the resulting row count. |

## Events and triggers

Every scan cycle compares presence with the previous one and announces the
changes on the event bus. The first cycle after a start only sets the
baseline, so a restart does not announce every device as newly online.

| Event | When |
|---|---|
| `network_info_device_online` | A device is online this cycle and was not the cycle before. |
| `network_info_device_offline` | A device was online the cycle before and is not now. |
| `network_info_new_device` | A device was seen for the very first time (it comes online too). |

Event data: `entry_id`, `key` (MAC, or `ip:<address>` when no MAC is known),
`mac`, `ip`, `name`, `hostname`, `vendor`, `connection`, `access_point`,
`signal`, `first_seen`, `last_seen`.

The same three appear as **device triggers** on the Network Info device in the
automation editor — _Settings → Automations → Add trigger → Device → Network
Info_ — with optional match fields: a dropdown of every known device (offline
ones included), plus MAC, IP and name/hostname for matching devices without a
fixed identity, such as a phone using a private Wi-Fi address. Every field
given must match; none given means any device. A brand-new device is by
definition not in the list yet, so that trigger has no fields.

The classic case — a push notification when one particular device joins:

```yaml
triggers:
  - trigger: device
    domain: network_info
    device_id: <the Network Info device>     # picked in the editor
    type: device_online
    network_device: "aa:bb:cc:dd:ee:ff"     # picked from the dropdown
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "Device online"
      message: >-
        {{ trigger.event.data.name }} joined at {{ trigger.event.data.ip }}
        ({{ trigger.event.data.connection }})
```

The plain event form works everywhere, YAML included:

```yaml
triggers:
  - trigger: event
    event_type: network_info_new_device
```

A device that drops off Wi-Fi between two scans and returns reads as an
offline/online pair; the scan interval is the resolution of these events.
