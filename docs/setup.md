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

Setup is two steps. The first covers the basics, pre-filled from Home Assistant's own
network connection:

| Field | Meaning |
|---|---|
| IP range(s) to scan | Anything nmap accepts: CIDR (`192.168.1.0/24`), dash ranges (`192.168.1.1-254`), or several ranges separated by spaces. |
| Scan interval | Minutes between automatic scans. Default 15, allowed 1–1440. A `/24` ping sweep takes a few seconds, so short intervals are harmless. |
| Router brand | **None (scanning only)** by default. Pick your router's brand to get per-device connection paths and signal — the supported brands come from the [router catalog](routers.md#the-brand-catalog). |
| Track external IP | Fetches the network's public IP from `api.ipify.org` each scan cycle and publishes it as a sensor. This is the integration's **only** internet call, and only when enabled. |
| Log external IP changes | Keeps a persistent log of every external IP change (implies tracking). The log survives restarts and is capped at the newest 500 entries. |

Choosing a brand opens the second step, pre-filled from the catalog. Only the fields
that brand actually needs are shown:

| Field | Meaning |
|---|---|
| Router address | Pre-filled with the brand's usual gateway address; adjust if yours differs. |
| Router username | Only shown for brands that need one. |
| Router admin password | Only shown for brands that need one. |

The credentials are verified against the router before the entry is created:

| Error | Meaning |
|---|---|
| _The router rejected the credentials_ | Login reached the router and was refused. |
| _Could not reach the router at this address_ | Nothing answered the API there. |

Scan-only mode (brand None) still discovers, remembers and names devices — only the
path/signal columns stay empty until a router brand is configured.

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
