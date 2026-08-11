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
  router's device name, connection path (`LAN` / `2.4 GHz` / `5 GHz` / `6 GHz` / `Guest`),
  signal, and whether it is currently online.

Errors during a cycle degrade gracefully: the scan results still publish, the sensor's
`router_available` attribute turns `false`, and one warning is logged until the router
recovers.

## Confirmed to work with

Every provider has been exercised against real hardware:

| Brand | Confirmed hardware |
|---|---|
| Xiaomi / MiWiFi | Mesh System AX3000 (RA82), as the main router |
| Technicolor (Homeware) | TG789vac v2, as the main router |
| OpenWrt / Cudy (ubus) | Cudy AP1300, as a downstream access point |
| ASUS (ASUSWRT) | RT-AC65P, as a downstream access point |
| TP-Link Archer | Archer C6 on firmware 1.3 and newer, as a downstream access point — firmware 1.1 is confirmed **not** to work (see the TP-Link section) |

Other models of the same families are expected to work — each provider targets the
API family, not one model — but the hardware above is what they were actually
validated against. Reports for further models are welcome.

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

## The brand catalog

`router/routers.json` describes every brand the config flow offers. One entry per
brand:

| Key | Meaning |
|---|---|
| `id` | Internal brand id; maps to the provider class. |
| `name` | Label shown in the config flow dropdown. |
| `default_gateway` | Pre-fills the router address field. |
| `default_username` | The username that applies unless overridden. |
| `requires_username` | Whether the effective username must be non-empty for this brand. |
| `requires_password` | Whether the flow requires a password. |
| `default_https` | Whether to talk HTTPS unless overridden (self-signed certs accepted). Providers additionally fall back to HTTP when the router has no TLS listener, and upgrade to HTTPS when the router redirects. |
| `api_endpoint` | The API base shape, for reference and for providers that build URLs from it. |

The config flow reads the catalog for its dropdown (with **None (scanning only)** as
the default choice). The router step then asks only for what varies per household —
address and password — while the brand's username and HTTP(S) transport apply
silently from the catalog. The **Override the username / HTTPS defaults** toggle
opens one more step showing both for editing; an override is kept and shown on later
visits, and a cleared field stays cleared.

## Technicolor (Homeware)

For ISP-supplied Technicolor gateways running Homeware firmware — the
OpenWRT-based build with the `.lp` web UI used by many DGA/TG "ac" models.
Authentication is the gateway's **SRP-6** handshake (fixed multiplier, SHA-256
over the RFC 5054 2048-bit group), implemented in `router/srp6.py` with no
extra dependency. It needs a username (usually `admin`) and the admin
password — on ISP units the password is often the "Access key" printed on the
device label. The session cookie and the `X-Requested-With` / `Referer`
headers its AJAX endpoints expect are handled explicitly.

Two modals are read, mirroring the two-endpoint pattern:

- `device-modal.lp` — every device the gateway knows, with hostname, IPv4 and
  MAC (`ipv6devices-modal.lp` is the fallback when it is empty). This list
  includes devices that are **not currently connected**, so a row counts as
  online only when an explicit state column says so, or — when the build has
  no such column — when the device holds a current lease. A row with no IP is
  a remembered device, not a connected one. Builds that state the connection
  in this modal itself (type/port columns) get their band read directly from
  it, which also covers builds whose wireless modal is unusable.
- `wireless-modal.lp` — the currently associated stations per radio, which is
  where the band (2.4 GHz / 5 GHz / guest) and signal come from. Layouts vary
  a lot between builds, so each station's band is taken from the nearest
  preceding radio heading rather than an assumed column.

The wireless page is only trusted when at least one MAC on it belongs to a
device the gateway listed. Some builds serve radio *configuration* there — the
access points' own BSSIDs — which looks like a station list but would mark
every real client wired; that case is detected and discarded, leaving paths
unknown rather than wrong.

An online device absent from the wireless list is reported as **LAN**, because
that is how the gateway sees it. Note this covers devices behind a downstream
access point or switch too: they reach the gateway over a wired port, so the
gateway cannot tell they are wireless further out. On a network with extra
access points, expect their clients to read LAN. When no wireless list can be
read at all, paths stay unknown rather than claiming everything is wired.

Per-device signal is only available for stations the gateway itself serves.

Because Homeware builds are heavily ISP-customized, modal layouts differ. The
parser handles the table layout (columns matched by header) and falls back to
scanning for MAC addresses with their surrounding context, so an unseen build
still yields devices even when the path cannot be determined. Technicolor's
DOCSIS cable gateways (CGA/CGM series) run a different UI and are not covered.

## OpenWrt / Cudy (ubus)

For OpenWrt and the vendor firmwares derived from it, including Cudy hardware
such as the AP1300. Everything goes over the `/ubus` JSON-RPC endpoint: a
session login, then `iwinfo` for the radios and their associated stations.

This is the cleanest source available. `iwinfo assoclist` returns the stations
actually associated to each radio with their signal, and the radio's own
frequency states the band — no scraping, no heuristics. DHCP leases add IPs
and hostnames for wired clients; an access point normally has none, which is
expected and harmless.

Requires ubus to be reachable over HTTP (`uhttpd-mod-ubus`, shipped with
LuCI). Vendor builds that strip it cannot be polled this way. The username is
usually `root`.

## ASUS (ASUSWRT)

For ASUS routers on ASUSWRT or Merlin firmware (RT series, e.g. the RT-AC65P).
Login posts a base64 `user:password` to `login.cgi`; a single
`appGet.cgi?hook=get_clientlist()` call then returns every client the router
knows.

That one call is unusually complete: each client states its band outright
(wired, 2.4 GHz, or one of the 5 GHz radios), its RSSI, and whether it is
online — so nothing has to be inferred.

## TP-Link Archer

For the consumer Archer line (C6, C7, A7 and relatives). These have no
username field — a password alone logs you in — but the way that password is
transmitted changed across firmware generations: a base64 password (plain),
an RSA-encrypted password (rsa), or an AES-encrypted payload with an RSA
signature (signed, the newest). Which one a firmware wants is read from the
key material the login page hands out unauthenticated, so the choice is made
*before* logging in and exactly one login attempt is ever spent — these
routers lock the account after a handful of failures, and probing by trial
would burn that budget.

The signed variant implements the full scheme the router's own web UI speaks:
the password RSA-encrypted (PKCS#1 v1.5) inside an AES-128-CBC envelope, a
chunked RSA signature carrying the session key and sequence, replies decrypted
with the same session key, and the `sysauth` session cookie replayed on every
authenticated request — the client list included, which travels in the same
envelope.

Clients come from `admin/status?form=client_status`, whose entries carry a
`wire_type` of `wired`, `2.4G` or `5G` — the band, stated directly — with the
wireless statistics page as a fallback.

Confirmed with the Archer C6 on **firmware 1.3 and newer**. Firmware **1.1 is
confirmed not to work** — it refuses the login without an error code — so
update the router's firmware before adding it.

The AES half is taken from `cryptography` (a Home Assistant dependency, so
present in every install); if it were missing the signed variant is skipped
rather than the provider failing.

## Adding a brand

1. Add a module in `router/` implementing `RouterProvider` for that brand's API.
2. Add its entry to `router/routers.json`.
3. Map the id to the class in `create_provider()` (`router/__init__.py`).

The brand then appears in the config flow dropdown with its defaults — no flow or
coordinator changes needed.
