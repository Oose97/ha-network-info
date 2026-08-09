# Setup

## Installing

**HACS** → three-dot menu → _Custom repositories_ → add
`https://github.com/Oose97/ha-network-info` as an **Integration**, download it, restart
Home Assistant.

**Manually**: copy `custom_components/network_info` into your
`config/custom_components/` directory and restart.

The `nmap` executable must be available — it is bundled with Home Assistant OS and
Container images; Core/venv installs may need it installed on the host.

## Adding the integration

_Settings → Devices & Services → Add Integration → Network Info._

<!-- screenshot: ![Config flow](images/config_flow.png) -->

The form comes pre-filled from Home Assistant's own network connection: the subnet
becomes the scan range and `.1` on that subnet is suggested as the router address.

| Field | Meaning |
|---|---|
| IP range(s) to scan | Anything nmap accepts: CIDR (`192.168.1.0/24`), dash ranges (`192.168.1.1-254`), or several ranges separated by spaces. |
| Scan interval | Minutes between automatic scans. Default 15, allowed 1–1440. A `/24` ping sweep takes a few seconds, so short intervals are harmless. |
| Router address (optional) | The router's IP. Only needed for connection-path and signal information. |
| Router admin password (optional) | The router's web-UI admin password. Leave empty for scan-only mode. |

When a password is entered it is verified against the router before the entry is
created:

| Error | Meaning |
|---|---|
| _Enter the router address to use the router password_ | Password given but no address. |
| _The router rejected the admin password_ | Login reached the router and was refused. |
| _Could not reach the router at this address_ | Nothing answered the API there. |

Scan-only mode (no password) still discovers, remembers and names devices — only the
path/signal columns stay empty until router access is added.

## Changing settings later

Open the integration's **Configure** dialog — the same form, editing the scan range,
interval and router credentials in place. Saving reloads the integration.

## Multiple networks

One config entry covers one scan-range string; adding a second entry with a different
range gives it its own sensors, memory and card data. The same range cannot be added
twice.

## Scanning on demand

The regular interval keeps running, but an immediate scan is available any time:

- press **Scan now** on the Network Info device (`button.network_info_scan_now`),
- press ↻ in the bundled table card,
- or call `homeassistant.update_entity` on the devices sensor from an automation.
