# Scanning & memory

## One update cycle

Every interval (and on every manual refresh) the coordinator runs the same sequence:

1. **nmap ping sweep** of the configured range(s) — every responding host with its IP,
   MAC, OUI vendor and reverse-DNS hostname.
2. **Router poll** (when configured) — the router's full client list: wired and
   wireless, online and offline, with connection path, signal and the router's device
   names. Any configured access points are polled at the same time, each one
   independently: a router that is down delays nothing and fails nothing else.
3. **Merge by MAC** — scan and router views of the same device become one record;
   a record seen by only one source keeps what that source knew. Where they
   disagree about the path, the access point holding the association outranks
   the gateway, which only sees the traffic arrive over a wire.
4. **Memory fold** — the merged cycle updates the persistent memory, and remembered
   devices missing from the cycle are appended as offline rows.
5. **Registry enrichment** — every MAC is looked up in Home Assistant's device
   registry; matches get their HA name and area. Devices whose integration never
   records a MAC (IPP printers and other UUID-identified gear) match by IP
   instead, taken from config-entry host values and configuration URLs — MAC
   always wins when both could apply.

## The scanner

The scan is `nmap -sn`: ping/ARP discovery only, no port probing. MAC addresses come
from the ARP table, which requires the scan to run as root — the normal situation
inside the Home Assistant container. Hostnames are whatever reverse DNS answers, which
on most home networks means the router's DHCP names.

The scanner is completely brand-independent and is always on; everything else layers
on top of it.

## The router's contribution

The router is the only component that knows a client's **path** — which band it is
associated to, or that it is wired. The provider (see [Router providers](routers.md))
contributes per client: connection path, signal, the router's own name for the device,
and its online flag. Because the router also lists devices that are currently
disconnected, it enriches the memory with devices the scanner may never have seen.

## Device memory

The integration owns its device list — the router does not have to remember anything
for the table to be complete.

- Every device ever seen gets a record in HA storage
  (`.storage/network_info.<entry_id>`), keyed by MAC (by IP until a MAC is first
  learned, migrating automatically).
- A record stores `first_seen`, `last_seen` and the last known facts: IP, hostname,
  vendor, router name, connection path.
- `last_seen` only advances when the device is actually **online**; a router merely
  remembering a device does not count as seeing it.
- Devices absent from the current cycle are served from memory as offline rows with
  `sources: ["memory"]` — in scan-only mode just the same as with a router.
- Offline rows keep the last path and access point the device was known on.
- A remembered path only stands in while nothing could observe one — scan-only
  cycles, or a router that could not be reached. Once a router answers and does
  not place an online device, that device reads Unknown: a stale label is not
  allowed to outlive the truth.
- `network_info.forget_device` deletes a record; removing the config entry deletes the
  store.

## Name resolution

Each device shows the best name available, first match wins:

1. Home Assistant device name (registry match by MAC — user-given name preferred)
2. The router's name for the device
3. Reverse-DNS hostname
4. MAC vendor
5. "Unknown"

## What a wired device looks like

A device genuinely plugged into the main router reports **LAN** — the router
says it arrived over a wired port, and no access point claims it as a wireless
station.

That verdict is only trusted when every declared access point actually
answered. If one is declared without credentials, or could not be reached, the
gateway's wired verdict cannot be distinguished from "wireless behind that
access point", so those devices read **Unknown** instead. It is a deliberate
trade: a genuinely wired device loses its label rather than a wireless one
gaining a wrong one. Making every access point pollable is what resolves it —
or pinning the path by hand: click the device's path badge in the table card,
or call `network_info.set_path`. A pinned path outranks every observation and
sticks until set back to automatic (observations keep being recorded
underneath, so clearing the pin falls straight back to the latest truth).

## Limitations

- A scan alone can never determine 2.4 GHz vs 5 GHz vs LAN — that information only
  exists inside the access point.
- Devices connected behind another access point or router segment that the configured
  router cannot see keep path `Unknown`.
- Devices that block ping **and** are unknown to the router are invisible until they
  are seen once; from then on the memory keeps them listed.
