# Router providers

## Why providers exist

Which band a wireless client is associated to — and whether a client is wired at all —
only exists inside the access point. There is no scan, packet trick or protocol that
reveals it from the outside, and there is no vendor-neutral API for it either. So the
integration keeps a small brand-specific module per router family, behind one
interface, and everything else stays brand-agnostic.

No provider configured (no router password) means scan-only mode: discovery, memory
and naming all work; path and signal stay unknown.

## The interface

A provider lives in `custom_components/network_info/router/` and implements
`RouterProvider`:

- `async_login()` — authenticate; raises `RouterAuthError` (bad credentials) or
  `RouterConnectionError` (unreachable/unexpected response).
- `async_get_clients()` — returns `{mac: RouterClient}` with, per client: IP, the
  router's device name, connection path (`LAN` / `2.4 GHz` / `5 GHz` / `Guest`),
  signal, and whether it is currently online.

Errors during a cycle degrade gracefully: the scan results still publish, the sensor's
`router_available` attribute turns `false`, and one warning is logged until the router
recovers.

## Xiaomi / MiWiFi

This provider talks to the local Luci-style web API of Xiaomi/MiWiFi routers
(AX series and older):

- `init_info` (unauthenticated) supplies the model and which password-hash scheme the
  firmware wants — both the older sha1 and newer sha256 challenge logins are
  implemented, so old and new firmwares work.
- Login yields a session token; a stale token is re-negotiated automatically once.
- `devicelist` lists every client the router knows (wired and wireless, online and
  offline, with names and IPs); `wifi_connect_devices` lists currently associated
  wireless clients with band index and signal. A client in the wireless list gets its
  band; an online client absent from it is wired.

Only the router **admin password** is needed — the username is always `admin` on
MiWiFi.

## Technicolor (Homeware)

Supports Technicolor gateways running Homeware firmware — the OpenWRT-based build with
the `.lp` web UI, used by many ISP-supplied DGA/TG models.

- Authentication is the gateway's **SRP-6** handshake (fixed multiplier, SHA-256 over
  the RFC 5054 2048-bit group), implemented in `router/srp6.py` with no extra
  dependency. It needs both a username (usually `admin`) and the admin password —
  on ISP units the password is often the "Access key" printed on the device label.
- Clients come from `modals/device-modal.lp`, falling back to
  `modals/ipv6devices-modal.lp` when the first is empty. Hostname, IPv4 and MAC are
  read reliably; the **connection path is parsed best-effort** from whatever the build
  labels its interfaces, and stays Unknown when the modal does not expose it. Per-client
  signal is not published by these modals, so it stays empty.
- Because Homeware builds are heavily ISP-customized, modal layouts differ. The parser
  handles the table layout (columns matched by header) and falls back to scanning for
  MAC addresses with their surrounding context, so an unseen build still yields
  devices even when the path cannot be determined.
- Technicolor's DOCSIS cable gateways (CGA/CGM series) run a different UI and are not
  covered.

## The brand catalog

`router/routers.json` describes every brand the config flow offers. One entry per
brand:

| Key | Meaning |
|---|---|
| `id` | Internal brand id; maps to the provider class. |
| `name` | Label shown in the config flow dropdown. |
| `default_gateway` | Pre-fills the router address field. |
| `requires_username` | Whether the flow shows a username field. |
| `requires_password` | Whether the flow shows a password field. |
| `default_https` | Start talking HTTPS (self-signed certs accepted). Providers fall back to HTTP when the router has no TLS listener, and upgrade to HTTPS when the router redirects — either way both firmware generations work. |
| `api_endpoint` | The API base shape, for reference and for providers that build URLs from it. |

The config flow reads the catalog for its dropdown (with **None (scanning only)** as
the default choice) and pre-fills the router step from the selected entry; only the
credential fields the brand requires are shown.

## Adding a brand

1. Add a module in `router/` implementing `RouterProvider` for that brand's API.
2. Add its entry to `router/routers.json`.
3. Map the id to the class in `create_provider()` (`router/__init__.py`).

The brand then appears in the config flow dropdown with its defaults — no flow or
coordinator changes needed.
