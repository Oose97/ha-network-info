# Cards

The integration ships one card and registers it as a Lovelace resource by itself
(storage-mode dashboards). YAML-mode dashboards manage resources manually — the URL to
add is logged at startup. Card updates bust the browser cache via a `?v=<version>`
query on the resource.

## Network Info Table

![The device table with router access — paths, signal and history per device](images/network_devices_full_table_with_router_api.jpg)

Minimal config:

```yaml
type: custom:network-info-table
```

Full config:

```yaml
type: custom:network-info-table
entity: sensor.network_info_devices   # default
title: Network devices                # default
max_height: 70vh                      # table scroll bound, default
columns:                              # initial visible columns + order (optional)
  - name
  - ip
  - mac
  - connection
  - signal
  - ha_area
  - online
```

Available columns: `name`, `ip`, `mac`, `hostname`, `vendor`, `connection`, `signal`,
`online`, `ha_device`, `ha_area`, `router_name`, `first_seen`, `last_seen`, `sources`.

### Toolbar

- **Filter box** — one search across every field: name, IP, MAC, hostname, vendor,
  path, router name, HA name, area and sources.
- **↻ Scan now** — triggers an immediate scan; the icon spins until the fresh data
  arrives. The regular interval keeps running regardless.
- **⚙ Table settings** — opens the settings sheet.

In scan-only mode (no router brand configured) the same table works with the
path/signal columns empty:

![The device table in scan-only mode](images/network_devices_full_table_no_router_api.jpg)

### Settings sheet

![The settings sheet](images/network_devices_table_settings.jpg)

- **Group into 2.4 GHz / 5 GHz / LAN tables** — renders one section per connection
  path, each with a badge and device count. Only offered when the integration has
  router access; in scan-only mode the toggle is disabled with a hint, because there
  is no path information to group by.
- **Show offline devices** — remembered devices that are currently absent render
  dimmed; untick to hide them entirely.
- **Columns** — tick to show, arrows to reorder. The `columns:` in the YAML config
  only sets the initial state; changes here win afterwards.
- Everything on this sheet persists per browser (localStorage, keyed by entity).
  **Reset** restores the card's YAML/default state.

### Behaviour

- Click a column header to sort; click again to flip direction. IPs sort numerically,
  signal and the seen-timestamps sort by value.
- Offline rows are dimmed — devices the integration remembers but that are currently
  absent (`Seen by: memory`). `first seen` / `last seen` render in the browser's
  locale.

![An offline device kept from the integration's memory](images/network_devices_offline_device.jpg)
- The footer shows filtered/total counts, online count, router reachability (when a
  router is configured but unreachable) and the last scan time.

## Network Info IP Log

![The external IP log card](images/ip_log_masked.jpg)

Shows the external IP change history (requires **Log external IP changes** enabled in
the integration). Minimal config:

```yaml
type: custom:network-info-ip-log
```

Full config:

```yaml
type: custom:network-info-ip-log
entity: sensor.network_info_external_ip_log   # default
title: External IP log                        # default
page_size: 10                                 # initial page size: 10/25/50/100
sort: date_desc                               # initial sort: date_desc | date_asc | ip_asc | ip_desc
```

- **Filter box** — matches date and IP.
- **Click a header to sort** — dates chronologically, IPs numerically.
- **Current IP highlighted** — rows matching the IP in use right now get a green
  tint (including earlier stints on the same IP), and the newest row carries a
  "current" pill.
- **Pagination** — ‹ › controls with a row-range indicator in the footer.
- **⚙ settings sheet** — default page size and default sort, persisted per browser
  (localStorage). The YAML `page_size`/`sort` only set the initial state; **Reset**
  returns to it.

![The IP log settings sheet](images/ip_log_table_settings.jpg)

## Alternatives

The same data works with generic cards. With
[flex-table-card](https://github.com/custom-cards/flex-table-card):

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
  - name: Path
    data: devices
    modify: x.connection
  - name: Area
    data: devices
    modify: x.ha_area ?? ""
```

Or a core markdown card:

```yaml
type: markdown
content: >
  | Name | IP | Path | Area |
  |------|----|------|------|
  {% for d in state_attr('sensor.network_info_devices', 'devices') -%}
  | {{ d.name }} | {{ d.ip }} | {{ d.connection }} | {{ d.ha_area or '' }} |
  {% endfor %}
```
