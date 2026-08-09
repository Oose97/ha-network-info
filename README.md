# Network Info for Home Assistant

A Home Assistant custom integration that discovers **every device on your network** and enriches each one with as much detail as possible — including **which network path it uses: LAN, 2.4 GHz or 5 GHz Wi-Fi**.

Inspired by [network_scanner](https://github.com/parvez/network_scanner), rebuilt around three ideas:

1. **Brand-independent discovery.** An nmap ping sweep finds every responding device (IP, MAC, vendor, hostname) — no router required.
2. **Pluggable router providers.** Connection path (LAN / 2.4 GHz / 5 GHz / guest) and signal strength only exist inside your router, so a small brand-specific provider fetches them. The provider is an isolated module behind a common interface — currently Xiaomi MiWiFi is implemented; other brands can be added without touching the core.
3. **Home Assistant awareness.** Discovered MACs are matched against HA's device registry, so devices HA already knows show up with their HA name and area. The integration also detects Home Assistant's own local IP automatically and uses it to pre-fill the scan range.

## Features

- Periodic nmap scan of one or more IP ranges (CIDR, dash ranges, space-separated)
- Union of scan results and the router's client list — devices that block ping are still found via the router, and wired-but-silent devices via the scan
- Per device: IP, MAC, best-known name, hostname, vendor, connection path, Wi-Fi signal, online state, HA device name and area
- Per-path counters (LAN / 2.4 GHz / 5 GHz / guest / unknown)
- `sensor.network_info_devices` — online device count with the full device list as attributes (excluded from the recorder to keep the database small)
- `sensor.network_info_home_assistant_ip` — HA's own local IP (diagnostic)
- Optional router polling: add your router's address and admin password in the config flow; leave empty for scan-only mode
- All settings changeable later via the integration's **Configure** dialog

## Requirements

- The `nmap` executable (bundled with Home Assistant OS / Container images)
- For connection-path info: a Xiaomi/MiWiFi router (tested with AX series) and its admin password

## Installation

### HACS (custom repository)

1. HACS → three-dot menu → **Custom repositories**
2. Add this repository URL, category **Integration**
3. Install **Network Info**, restart Home Assistant

### Manual

1. Copy `custom_components/network_info` into your HA `custom_components` directory
2. Restart Home Assistant

## Configuration

Settings → Devices & Services → **Add Integration** → **Network Info**

| Field | Meaning |
|---|---|
| IP range(s) to scan | Pre-filled from HA's own network, e.g. `192.168.1.0/24`. Multiple ranges separated by spaces are allowed. |
| Scan interval | Minutes between scans (default 15). |
| Router address (optional) | Your Xiaomi/MiWiFi router IP, e.g. `192.168.1.1`. |
| Router admin password (optional) | The router web UI password. Leave empty to skip router polling — everything works except connection path and signal. |

## Device attributes

Each entry in the `devices` attribute of `sensor.network_info_devices`:

| Key | Example | Source |
|---|---|---|
| `ip` | `192.168.1.23` | scan / router |
| `mac` | `aa:bb:cc:dd:ee:ff` | scan / router |
| `name` | `Living Room TV` | best of: HA name → router name → hostname → vendor |
| `hostname` | `tv-livingroom.lan` | reverse DNS |
| `vendor` | `TP-Link Systems Inc.` | MAC OUI |
| `connection` | `2.4 GHz`, `5 GHz`, `LAN`, `Guest`, `Unknown` | router |
| `signal` | `58` | router |
| `online` | `true` | scan / router |
| `router_name` | `MyPhone` | router |
| `ha_device` | `Living Room TV` | HA device registry |
| `ha_area` | `Living Room` | HA area registry |
| `sources` | `["scan", "router"]` | which sources saw the device |

## Dashboard example

Using [flex-table-card](https://github.com/custom-cards/flex-table-card):

```yaml
type: custom:flex-table-card
title: Network Devices
entities:
  include: sensor.network_info_devices
columns:
  - name: Name
    data: devices
    modify: x.name
  - name: IP
    data: devices
    modify: x.ip
  - name: MAC
    data: devices
    modify: x.mac
  - name: Path
    data: devices
    modify: x.connection
  - name: Signal
    data: devices
    modify: x.signal ?? ""
  - name: Area
    data: devices
    modify: x.ha_area ?? ""
```

Or with a core markdown card:

```yaml
type: markdown
content: >
  | Name | IP | Path | Area |
  |------|----|------|------|
  {% for d in state_attr('sensor.network_info_devices', 'devices') -%}
  | {{ d.name }} | {{ d.ip }} | {{ d.connection }} | {{ d.ha_area or '' }} |
  {% endfor %}
```

## How connection-path detection works

Which band a device is associated to only exists inside the access point — no scan can see it. The Xiaomi provider logs into the router's local web API and reads its client list (`devicelist` + `wifi_connect_devices`), which reports per client whether it is wired or on the 2.4 GHz / 5 GHz / guest network, plus signal level. Devices connected to a different AP or switch segment appear with path `Unknown`.

## Roadmap

- Router brand selection in the config flow (the provider interface already supports it)
- Per-device tracker entities / presence detection
- Deeper enrichment (mDNS, UPnP, NetBIOS names)

## License

MIT — see [LICENSE](LICENSE).
