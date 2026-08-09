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

## Xiaomi MiWiFi

The shipped provider talks to the local Luci-style web API of Xiaomi/MiWiFi routers
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

## Adding a brand

1. Add a module in `router/` implementing `RouterProvider` for that brand's API.
2. Construct it in the coordinator instead of (or alongside) the Xiaomi one.

A config-flow brand selector is planned so this becomes a dropdown rather than a code
change; the interface is already shaped for it.
