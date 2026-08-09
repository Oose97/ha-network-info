"""Coordinator merging network scan results with router client info."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import Any

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
    CONF_EXTERNAL_IP,
    CONF_EXTERNAL_IP_LOG,
    CONF_IP_RANGE,
    CONF_ROUTER_HOST,
    CONF_ROUTER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONNECTION_ROUTER,
    CONNECTION_SLUGS,
    CONNECTION_UNKNOWN,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    EXTERNAL_IP_URL,
    IP_LOG_MAX_ROWS,
    STORAGE_VERSION,
    ip_log_storage_key,
    storage_key,
)
from .router import RouterAuthError, RouterClient, RouterError, RouterProvider
from .router.xiaomi_miwifi import XiaomiMiWiFiProvider
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
    last_scan: datetime | None = None
    external_ip: str | None = None  # None = tracking disabled or not yet known
    ip_log: list[dict[str, str]] | None = None  # None = logging disabled


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
        self._provider: RouterProvider | None = None
        router_host = (config.get(CONF_ROUTER_HOST) or "").strip()
        router_password = config.get(CONF_ROUTER_PASSWORD) or ""
        # The device at this address IS the router — known from config even
        # without a password, so it can be labeled regardless of API access.
        self._router_ip = router_host.split("://")[-1].split("/")[0].split(":")[0]
        if router_host and router_password:
            self._provider = XiaomiMiWiFiProvider(
                router_host, router_password, async_get_clientsession(hass)
            )
        self._router_warned = False
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

        router_clients: dict[str, RouterClient] = {}
        router_available: bool | None = None
        if self._provider is not None:
            try:
                router_clients = await self._provider.async_get_clients()
                router_available = True
                if self._router_warned:
                    self._router_warned = False
                    _LOGGER.info("Router connection recovered")
            except RouterAuthError as err:
                router_available = False
                if not self._router_warned:
                    self._router_warned = True
                    _LOGGER.warning(
                        "Router rejected credentials, connection paths unavailable: %s", err
                    )
            except RouterError as err:
                router_available = False
                if not self._router_warned:
                    self._router_warned = True
                    _LOGGER.warning(
                        "Router unreachable, connection paths unavailable: %s", err
                    )

        devices = self._merge(scanned, router_clients)
        if self._router_ip:
            for device in devices:
                if device["ip"] == self._router_ip:
                    device["connection"] = CONNECTION_ROUTER
                    break
        self._apply_memory(devices)
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
            router_available=router_available,
            router_model=self._provider.model if self._provider else None,
            last_scan=dt_util.utcnow(),
            external_ip=self._external_ip if self._track_external_ip else None,
            ip_log=list(self._ip_log or []) if self._log_external_ip else None,
        )

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

        return list(merged.values())

    def _apply_memory(self, devices: list[dict[str, Any]]) -> None:
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
            for field in ("ip", "mac", "hostname", "vendor", "router_name"):
                if dev.get(field):
                    rec[field] = dev[field]
            if dev["connection"] != CONNECTION_UNKNOWN:
                rec["connection"] = dev["connection"]
            elif rec.get("connection"):
                # Scan-only cycle: keep showing the last path the router knew.
                dev["connection"] = rec["connection"]
            if dev["online"]:
                rec["last_seen"] = now_iso
            dev["first_seen"] = rec["first_seen"]
            dev["last_seen"] = rec.get("last_seen")

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
            if rec.get("connection"):
                dev["connection"] = rec["connection"]
            dev["first_seen"] = rec.get("first_seen")
            dev["last_seen"] = rec.get("last_seen")
            devices.append(dev)

        self._store.async_delay_save(lambda: memory, 30)

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
        """Attach Home Assistant device names and areas by MAC."""
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)

        ha_by_mac: dict[str, dr.DeviceEntry] = {}
        for device in dev_reg.devices.values():
            for conn_type, conn_id in device.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    ha_by_mac[dr.format_mac(conn_id)] = device

        for entry in devices:
            mac = entry.get("mac")
            ha_device = ha_by_mac.get(dr.format_mac(mac)) if mac else None
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
        "ha_device": None,
        "ha_area": None,
        "sources": sources,
    }


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
