"""Coordinator merging network scan results with router client info."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AP_NAME,
    CONF_EXTERNAL_IP,
    CONF_EXTERNAL_IP_LOG,
    CONF_IP_RANGE,
    CONF_ROUTER_BRAND,
    CONF_ROUTER_HOST,
    CONF_ROUTER_PASSWORD,
    CONF_ROUTER_USE_HTTPS,
    CONF_ROUTER_USERNAME,
    CONF_SCAN_INTERVAL,
    CONNECTION_ACCESS_POINT,
    CONNECTION_ROUTER,
    CONNECTION_SLUGS,
    CONNECTION_UNKNOWN,
    CONNECTION_LAN,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    EXTERNAL_IP_URL,
    IP_LOG_MAX_ROWS,
    LABEL_MAIN_ROUTER,
    ROLE_ACCESS_POINT,
    ROLE_GATEWAY,
    ROUTER_BRAND_NONE,
    STORAGE_VERSION,
    SUBENTRY_TYPE_ACCESS_POINT,
    WIFI_CONNECTIONS,
    ip_log_storage_key,
    storage_key,
)
from .router import (
    RouterAuthError,
    RouterClient,
    RouterError,
    RouterProvider,
    create_provider,
)
from .scanner import ScannedDevice, ScannerError, scan_network

_LOGGER = logging.getLogger(__name__)


@dataclass
class NetworkData:
    """Merged result of one update cycle."""

    devices: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    ha_ip: str | None = None
    router_available: bool | None = None  # None = no router configured
    router_model: str | None = None
    access_points: list[dict[str, Any]] = field(default_factory=list)
    last_scan: datetime | None = None
    external_ip: str | None = None  # None = tracking disabled or not yet known
    ip_log: list[dict[str, str]] | None = None  # None = logging disabled


@dataclass
class _Source:
    """One router this integration polls: the gateway, or an access point."""

    name: str
    brand: str
    role: str
    provider: RouterProvider | None  # None = declared but not polled
    ip: str | None = None  # its own address on the network, when known
    polled: bool = False

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "brand": self.brand,
            "managed": self.provider is not None,
            "available": self.polled if self.provider is not None else None,
        }


@dataclass
class _Station:
    """A wireless association claimed by one access point."""

    band: str
    signal: int | None
    ap_name: str


class NetworkInfoCoordinator(DataUpdateCoordinator[NetworkData]):
    """Runs the scan + router poll and merges everything by MAC."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        config = {**entry.data, **entry.options}
        minutes = int(config.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES))
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self._ip_range: str = config[CONF_IP_RANGE]
        session = async_get_clientsession(hass)
        router_host = (config.get(CONF_ROUTER_HOST) or "").strip()
        router_password = config.get(CONF_ROUTER_PASSWORD) or ""
        brand = config.get(CONF_ROUTER_BRAND)
        if brand is None:
            # Entries created before the brand catalog carried host+password
            # only; those were always Xiaomi MiWiFi.
            brand = "xiaomi_miwifi" if router_host and router_password else ROUTER_BRAND_NONE
        # The device at this address IS the router — known from config even
        # without API access, so it can be labeled regardless.
        self._router_ip = _host_ip(router_host)

        gateway_provider: RouterProvider | None = None
        if brand != ROUTER_BRAND_NONE and router_host:
            gateway_provider = create_provider(
                brand,
                router_host,
                (config.get(CONF_ROUTER_USERNAME) or "").strip(),
                router_password,
                session,
                bool(config.get(CONF_ROUTER_USE_HTTPS)),
            )
            if gateway_provider is None:
                _LOGGER.warning("Unknown router brand %r — running scan-only", brand)
        self._gateway = _Source(
            name=LABEL_MAIN_ROUTER,
            brand=brand,
            role=ROLE_GATEWAY,
            provider=gateway_provider,
        )

        # One subentry per downstream access point. A subentry whose brand is
        # "none" declares that an AP exists without giving credentials for it —
        # enough to know the gateway's wired/wireless view is incomplete.
        self._access_points: list[_Source] = []
        for sub in entry.subentries.values():
            if sub.subentry_type != SUBENTRY_TYPE_ACCESS_POINT:
                continue
            data = dict(sub.data)
            ap_brand = data.get(CONF_ROUTER_BRAND, ROUTER_BRAND_NONE)
            ap_host = (data.get(CONF_ROUTER_HOST) or "").strip()
            provider: RouterProvider | None = None
            if ap_brand != ROUTER_BRAND_NONE and ap_host:
                provider = create_provider(
                    ap_brand,
                    ap_host,
                    (data.get(CONF_ROUTER_USERNAME) or "").strip(),
                    data.get(CONF_ROUTER_PASSWORD) or "",
                    session,
                    bool(data.get(CONF_ROUTER_USE_HTTPS)),
                )
            self._access_points.append(
                _Source(
                    name=data.get(CONF_AP_NAME) or sub.title,
                    brand=ap_brand,
                    role=ROLE_ACCESS_POINT,
                    provider=provider,
                    ip=_host_ip(ap_host),
                )
            )
        self._warned: set[str] = set()
        # Every device ever seen, keyed by MAC (ip:<ip> before the MAC is
        # known). This is the integration's own memory — offline devices stay
        # listed even in scan-only mode; the router only enriches it.
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._memory: dict[str, dict[str, Any]] | None = None
        # External IP tracking (opt-in). Logging implies tracking.
        self._log_external_ip = bool(config.get(CONF_EXTERNAL_IP_LOG))
        self._track_external_ip = (
            bool(config.get(CONF_EXTERNAL_IP)) or self._log_external_ip
        )
        self._ip_log_store: Store[list[dict[str, str]]] = Store(
            hass, STORAGE_VERSION, ip_log_storage_key(entry.entry_id)
        )
        self._ip_log: list[dict[str, str]] | None = None
        self._external_ip: str | None = None
        self._ip_warned = False

    async def _async_update_data(self) -> NetworkData:
        if self._memory is None:
            self._memory = await self._store.async_load() or {}

        try:
            scanned = await self.hass.async_add_executor_job(
                scan_network, self._ip_range
            )
        except ScannerError as err:
            raise UpdateFailed(str(err)) from err

        # Every router is polled concurrently and independently: one that is
        # down must not delay or fail the others, nor the scan.
        results = await asyncio.gather(
            *(
                self._async_poll(source)
                for source in (self._gateway, *self._access_points)
            )
        )
        router_clients = results[0]
        ap_stations = self._collect_stations(results[1:])

        # The gateway sees a device behind an access point as wired, because
        # that is how it arrives. Its "LAN" verdict is therefore only
        # trustworthy once every declared access point has been asked and none
        # of them claims the device.
        lan_trustworthy = all(ap.polled for ap in self._access_points)
        devices = self._merge(scanned, router_clients, ap_stations, lan_trustworthy)
        # The network's own infrastructure: these devices are what everything
        # else connects *through*, so a path of their own says more than
        # whichever port they happen to sit on.
        infrastructure = {
            ap.ip: CONNECTION_ACCESS_POINT for ap in self._access_points if ap.ip
        }
        if self._router_ip:
            infrastructure[self._router_ip] = CONNECTION_ROUTER
        for device in devices:
            label = infrastructure.get(device["ip"])
            if label:
                device["connection"] = label
        # A remembered path may only stand in when nothing could observe one
        # this cycle; otherwise an old value would outlive the truth.
        paths_observed = self._gateway.polled or any(
            ap.polled for ap in self._access_points
        )
        self._apply_memory(devices, paths_observed)
        self._enrich_from_registries(devices)
        devices.sort(key=_ip_sort_key)

        counts = _compute_counts(devices)

        try:
            ha_ip = await async_get_source_ip(self.hass)
        except HomeAssistantError:
            ha_ip = None

        if self._track_external_ip:
            await self._async_update_external_ip()

        return NetworkData(
            devices=devices,
            counts=counts,
            ha_ip=ha_ip,
            router_available=(
                self._gateway.polled if self._gateway.provider is not None else None
            ),
            router_model=(
                self._gateway.provider.model if self._gateway.provider else None
            ),
            access_points=[ap.as_dict for ap in self._access_points],
            last_scan=dt_util.utcnow(),
            external_ip=self._external_ip if self._track_external_ip else None,
            ip_log=list(self._ip_log or []) if self._log_external_ip else None,
        )

    async def _async_poll(self, source: _Source) -> dict[str, RouterClient]:
        """Poll one router. Never raises — a failure is just no data from it."""
        source.polled = False
        if source.provider is None:
            return {}
        try:
            clients = await source.provider.async_get_clients()
        except RouterAuthError as err:
            self._warn_once(source, f"rejected the credentials: {err}")
            return {}
        except RouterError as err:
            self._warn_once(source, f"is unreachable: {err}")
            return {}
        except Exception:  # noqa: BLE001 - one bad provider must not stop the rest
            _LOGGER.exception("Unexpected error polling %s", source.name)
            return {}
        source.polled = True
        if source.name in self._warned:
            self._warned.discard(source.name)
            _LOGGER.info("%s is reachable again", source.name)
        return clients

    def _warn_once(self, source: _Source, what: str) -> None:
        if source.name not in self._warned:
            self._warned.add(source.name)
            _LOGGER.warning(
                "%s %s — connection paths from it are unavailable", source.name, what
            )

    def _collect_stations(
        self, results: list[dict[str, RouterClient]]
    ) -> dict[str, _Station]:
        """Union of the access points' wireless associations, keyed by MAC.

        Only wireless claims are taken: an access point calling a device wired
        says nothing useful, since the gateway already reports that. A device
        that shows up on two access points (a stale association after roaming)
        is attributed to the one hearing it strongest.
        """
        stations: dict[str, _Station] = {}
        for source, clients in zip(self._access_points, results, strict=True):
            if not source.polled:
                continue
            for mac, client in clients.items():
                if not client.connection or client.connection not in WIFI_CONNECTIONS:
                    continue
                new = _Station(client.connection, client.signal, source.name)
                current = stations.get(mac)
                if current is None or _stronger(new.signal, current.signal):
                    stations[mac] = new
        return stations

    async def _async_update_external_ip(self) -> None:
        """Fetch the public IP and append to the change log when it moved."""
        if self._ip_log is None:
            self._ip_log = await self._ip_log_store.async_load() or []

        try:
            resp = await async_get_clientsession(self.hass).get(
                EXTERNAL_IP_URL, timeout=ClientTimeout(total=15)
            )
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
            external_ip = str(payload.get("ip") or "").strip() or None
        except (ClientError, TimeoutError, ValueError) as err:
            if not self._ip_warned:
                self._ip_warned = True
                _LOGGER.warning("External IP lookup failed: %s", err)
            return  # keep the last known value
        if self._ip_warned:
            self._ip_warned = False
            _LOGGER.info("External IP lookup recovered")
        if external_ip is None:
            return

        self._external_ip = external_ip
        if not self._log_external_ip:
            return
        last = self._ip_log[-1]["ip"] if self._ip_log else None
        if external_ip != last:
            self._ip_log.append(
                {
                    "date": dt_util.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ip": external_ip,
                }
            )
            del self._ip_log[:-IP_LOG_MAX_ROWS]
            self._ip_log_store.async_delay_save(lambda: self._ip_log, 5)

    def _merge(
        self,
        scanned: list[ScannedDevice],
        router_clients: dict[str, RouterClient],
        ap_stations: dict[str, _Station],
        lan_trustworthy: bool,
    ) -> list[dict[str, Any]]:
        """Union of scan results and router clients, keyed by MAC (IP as fallback)."""
        merged: dict[str, dict[str, Any]] = {}

        for item in scanned:
            key = item.mac or f"ip:{item.ip}"
            merged[key] = _new_device(
                ip=item.ip,
                mac=item.mac,
                hostname=item.hostname,
                vendor=item.vendor,
                online=True,
                sources=["scan"],
            )

        for mac, client in router_clients.items():
            entry = merged.get(mac)
            if entry is None and client.ip:
                # Scan saw the IP but no MAC (e.g. the HA host itself).
                ip_key = f"ip:{client.ip}"
                if ip_key in merged:
                    entry = merged.pop(ip_key)
                    entry["mac"] = mac
                    merged[mac] = entry
            if entry is None:
                entry = _new_device(
                    ip=client.ip,
                    mac=mac,
                    hostname=None,
                    vendor=None,
                    online=client.online,
                    sources=[],
                )
                merged[mac] = entry
            entry["sources"].append("router")
            if client.connection:
                entry["connection"] = client.connection
            entry["signal"] = client.signal
            entry["router_name"] = client.name
            entry["online"] = entry["online"] or client.online
            if not entry["ip"] and client.ip:
                entry["ip"] = client.ip

        # Devices only an access point knows about — associated to it but not
        # (yet) leased or answering the sweep.
        for mac, station in ap_stations.items():
            if mac not in merged:
                merged[mac] = _new_device(
                    ip=None,
                    mac=mac,
                    hostname=None,
                    vendor=None,
                    online=True,
                    sources=[],
                )

        for mac, entry in merged.items():
            station = ap_stations.get(mac)
            if station is not None:
                # An access point hearing a device outranks the gateway, which
                # can only see it arriving over a wire.
                entry["connection"] = station.band
                entry["signal"] = station.signal
                entry["access_point"] = station.ap_name
                entry["online"] = True
                if "access point" not in entry["sources"]:
                    entry["sources"].append("access point")
            elif entry["connection"] == CONNECTION_LAN and not lan_trustworthy:
                entry["connection"] = CONNECTION_UNKNOWN
            elif entry["connection"] in WIFI_CONNECTIONS:
                entry["access_point"] = LABEL_MAIN_ROUTER

        return list(merged.values())

    def _apply_memory(
        self, devices: list[dict[str, Any]], paths_observed: bool = False
    ) -> None:
        """Fold this cycle into the persistent memory, and the memory into it.

        Live devices update their memory record (first/last seen, last known
        facts); remembered devices absent from this cycle are appended as
        offline rows so they never silently vanish — regardless of whether a
        router is configured.
        """
        assert self._memory is not None
        memory = self._memory
        now_iso = dt_util.utcnow().isoformat()
        live_keys: set[str] = set()

        for dev in devices:
            key = dev["mac"] or f"ip:{dev['ip']}"
            live_keys.add(key)
            rec = memory.get(key)
            if rec is None and dev["mac"] and dev["ip"]:
                # Stored before its MAC was known — migrate the IP-keyed record.
                rec = memory.pop(f"ip:{dev['ip']}", None)
                if rec is not None:
                    memory[key] = rec
            if rec is None:
                rec = {"first_seen": now_iso}
                memory[key] = rec
            for field in (
                "ip", "mac", "hostname", "vendor", "router_name", "access_point"
            ):
                if dev.get(field):
                    rec[field] = dev[field]
            if (
                dev["connection"] == CONNECTION_LAN
                and "scan" not in dev["sources"]
                and rec.get("connection") in WIFI_CONNECTIONS
            ):
                # Router-claimed "online, wired" on a device the scan cannot
                # see, right after it was on Wi-Fi: the router's device list
                # keeps a stale online flag for a while after a wireless
                # client disassociates. A genuinely wired device always
                # answers the ARP sweep, so this is the flag, not a
                # re-cabling — keep the last known band.
                dev["connection"] = rec["connection"]
            if dev["connection"] != CONNECTION_UNKNOWN:
                rec["connection"] = dev["connection"]
            elif rec.get("connection") and not (paths_observed and dev["online"]):
                # Keep showing the last known path — but only where nothing
                # could observe one this cycle. A router that answered and did
                # not place an online device must be allowed to say "unknown",
                # otherwise a stale label outlives the truth.
                dev["connection"] = rec["connection"]
                dev["access_point"] = dev["access_point"] or rec.get("access_point")
            if dev["online"]:
                rec["last_seen"] = now_iso
            dev["first_seen"] = rec["first_seen"]
            dev["last_seen"] = rec.get("last_seen")
            # A path set by hand outranks everything observed — that is its
            # point: it covers exactly the devices nothing can observe. The
            # observed path was still recorded above, so clearing the
            # override falls straight back to the latest truth.
            if rec.get("path_override"):
                dev["connection"] = rec["path_override"]
                dev["path_override"] = True

        for key, rec in memory.items():
            if key in live_keys:
                continue
            dev = _new_device(
                ip=rec.get("ip"),
                mac=rec.get("mac"),
                hostname=rec.get("hostname"),
                vendor=rec.get("vendor"),
                online=False,
                sources=["memory"],
            )
            dev["router_name"] = rec.get("router_name")
            dev["access_point"] = rec.get("access_point")
            if rec.get("connection"):
                dev["connection"] = rec["connection"]
            if rec.get("path_override"):
                dev["connection"] = rec["path_override"]
                dev["path_override"] = True
            dev["first_seen"] = rec.get("first_seen")
            dev["last_seen"] = rec.get("last_seen")
            devices.append(dev)

        self._store.async_delay_save(lambda: memory, 30)

    @property
    def ip_log_enabled(self) -> bool:
        return self._log_external_ip

    async def async_import_ip_log(self, path: str) -> int:
        """Merge "date,ip" rows from a CSV file into the change log.

        Rows are merged with the existing log by date and consecutive
        duplicate IPs collapse, so importing is idempotent. Returns the
        resulting log length.
        """
        if self._ip_log is None:
            self._ip_log = await self._ip_log_store.async_load() or []
        rows = await self.hass.async_add_executor_job(
            _read_ip_log_csv, path, self.hass.config.path()
        )
        merged = sorted(rows + self._ip_log, key=lambda r: r["date"])
        deduped: list[dict[str, str]] = []
        for row in merged:
            if deduped and deduped[-1]["ip"] == row["ip"]:
                continue
            deduped.append(row)
        self._ip_log[:] = deduped[-IP_LOG_MAX_ROWS:]
        await self._ip_log_store.async_save(self._ip_log)
        if self.data is not None:
            self.async_set_updated_data(
                replace(self.data, ip_log=list(self._ip_log))
            )
        return len(self._ip_log)

    async def async_set_path(self, key: str, connection: str | None) -> bool:
        """Pin a device's connection path by hand, or clear the pin.

        The override lives in the device's memory record, so it survives
        restarts, works in scan-only mode, and disappears with the device on
        `forget_device`. `None` clears it, falling back to whatever the next
        cycle observes (the last observed path immediately).
        """
        if self._memory is None:
            self._memory = await self._store.async_load() or {}
        rec = self._memory.get(key)
        if rec is None:
            return False
        if connection:
            rec["path_override"] = connection
        elif rec.pop("path_override", None) is None:
            return True  # clearing a pin that was never set: nothing to do
        await self._store.async_save(self._memory)
        if self.data is not None:
            devices = [dict(d) for d in self.data.devices]
            for dev in devices:
                if (dev.get("mac") or f"ip:{dev.get('ip')}") != key:
                    continue
                dev["connection"] = (
                    connection or rec.get("connection") or CONNECTION_UNKNOWN
                )
                dev["path_override"] = bool(connection)
            self.async_set_updated_data(
                replace(self.data, devices=devices, counts=_compute_counts(devices))
            )
        return True

    async def async_forget_device(self, mac: str) -> bool:
        """Drop a device from memory. An online device reappears next scan."""
        if self._memory is None:
            return False
        key = mac.strip().lower().replace("-", ":")
        if self._memory.pop(key, None) is None:
            return False
        await self._store.async_save(self._memory)
        if self.data is not None:
            devices = [
                d
                for d in self.data.devices
                if (d.get("mac") or f"ip:{d.get('ip')}") != key
            ]
            self.async_set_updated_data(
                replace(self.data, devices=devices, counts=_compute_counts(devices))
            )
        return True

    def _enrich_from_registries(self, devices: list[dict[str, Any]]) -> None:
        """Attach Home Assistant device names and areas.

        MAC is the primary key. Devices whose integrations never learn a MAC
        (IPP printers and other UUID-identified gear) fall back to an IP
        match, built from config-entry host values and configuration URLs.
        """
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        ha_by_mac: dict[str, dr.DeviceEntry] = {}
        for device in dev_reg.devices.values():
            for conn_type, conn_id in device.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    ha_by_mac[dr.format_mac(conn_id)] = device

        ha_by_ip = self._ha_devices_by_ip(dev_reg)

        for entry in devices:
            mac = entry.get("mac")
            ha_device = ha_by_mac.get(dr.format_mac(mac)) if mac else None
            if ha_device is None and entry.get("ip"):
                ha_device = ha_by_ip.get(entry["ip"])
            if ha_device is not None:
                entry["ha_device"] = ha_device.name_by_user or ha_device.name
                area = (
                    area_reg.async_get_area(ha_device.area_id)
                    if ha_device.area_id
                    else None
                )
                entry["ha_area"] = area.name if area else None
            entry["name"] = (
                entry.get("ha_device")
                or entry.get("router_name")
                or entry.get("hostname")
                or entry.get("vendor")
                or "Unknown"
            )

    def _ha_devices_by_ip(
        self, dev_reg: dr.DeviceRegistry
    ) -> dict[str, dr.DeviceEntry]:
        """Map IPs to HA devices via config-entry hosts and config URLs."""
        # Config entries first — the host an integration polls is the most
        # deliberate statement of "this device lives at this IP".
        entry_ip: dict[str, str] = {}
        for config_entry in self.hass.config_entries.async_entries():
            for key in ("host", "ip_address"):
                ip = _as_ipv4(config_entry.data.get(key))
                if ip:
                    entry_ip[config_entry.entry_id] = ip
                    break

        by_entry: dict[str, list[dr.DeviceEntry]] = {}
        for device in dev_reg.devices.values():
            for entry_id in device.config_entries:
                if entry_id in entry_ip:
                    by_entry.setdefault(entry_id, []).append(device)

        ha_by_ip: dict[str, dr.DeviceEntry] = {}
        for entry_id, dev_list in by_entry.items():
            # A hub-style entry lists the hub and its children; the entry's
            # host belongs to the hub — the device without a via_device.
            root = next((d for d in dev_list if d.via_device_id is None), dev_list[0])
            ha_by_ip.setdefault(entry_ip[entry_id], root)

        for device in dev_reg.devices.values():
            if not device.configuration_url:
                continue
            try:
                ip = _as_ipv4(urlsplit(device.configuration_url).hostname)
            except ValueError:
                continue
            if ip:
                ha_by_ip.setdefault(ip, device)

        return ha_by_ip


def _new_device(
    *,
    ip: str | None,
    mac: str | None,
    hostname: str | None,
    vendor: str | None,
    online: bool,
    sources: list[str],
) -> dict[str, Any]:
    return {
        "ip": ip,
        "mac": mac,
        "name": None,
        "hostname": hostname,
        "vendor": vendor,
        "connection": CONNECTION_UNKNOWN,
        "signal": None,
        "online": online,
        "router_name": None,
        "access_point": None,
        "ha_device": None,
        "ha_area": None,
        "sources": sources,
        "path_override": False,
    }


def _host_ip(host: str) -> str:
    """The bare address out of a configured host (scheme and port stripped)."""
    return host.split("://")[-1].split("/")[0].split(":")[0]


def _stronger(new: int | None, current: int | None) -> bool:
    """Whether `new` is a better signal. Units are per-brand; higher is better
    in both conventions used (less-negative dBm, larger percentage)."""
    if new is None:
        return False
    if current is None:
        return True
    return new > current


def _as_ipv4(value: Any) -> str | None:
    """The value as a dotted IPv4 string, or None (hostnames are skipped)."""
    if not value:
        return None
    try:
        return str(IPv4Address(str(value).strip()))
    except ValueError:
        return None


def _read_ip_log_csv(path: str, config_dir: str) -> list[dict[str, str]]:
    """Parse "date,ip" lines. Blocking — run in an executor."""
    target = Path(path).resolve()
    try:
        target.relative_to(Path(config_dir).resolve())
    except ValueError:
        raise ValueError(
            "Path must be inside the Home Assistant configuration directory"
        ) from None
    if not target.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, str]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        date, _, ip = line.strip().partition(",")
        date, ip = date.strip(), ip.strip()
        if date and ip:
            rows.append({"date": date, "ip": ip})
    if not rows:
        raise ValueError("No date,ip rows found in the file")
    return rows


def _compute_counts(devices: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"total": len(devices), "online": 0, "offline": 0}
    for slug in CONNECTION_SLUGS.values():
        counts[slug] = 0
    for device in devices:
        if device["online"]:
            counts["online"] += 1
            slug = CONNECTION_SLUGS.get(device["connection"], "unknown")
            counts[slug] += 1
        else:
            counts["offline"] += 1
    return counts


def _ip_sort_key(device: dict[str, Any]) -> tuple[int, int]:
    ip = device.get("ip")
    if ip:
        try:
            return (0, int(IPv4Address(ip)))
        except ValueError:
            pass
    return (1, 0)
