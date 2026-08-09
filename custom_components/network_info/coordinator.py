"""Coordinator merging network scan results with router client info."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import Any

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_IP_RANGE,
    CONF_ROUTER_HOST,
    CONF_ROUTER_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONNECTION_SLUGS,
    CONNECTION_UNKNOWN,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
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
        if router_host and router_password:
            self._provider = XiaomiMiWiFiProvider(
                router_host, router_password, async_get_clientsession(hass)
            )
        self._router_warned = False

    async def _async_update_data(self) -> NetworkData:
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
        self._enrich_from_registries(devices)
        devices.sort(key=_ip_sort_key)

        counts = {"total": len(devices), "online": 0}
        for slug in CONNECTION_SLUGS.values():
            counts[slug] = 0
        for device in devices:
            if device["online"]:
                counts["online"] += 1
                slug = CONNECTION_SLUGS.get(device["connection"], "unknown")
                counts[slug] += 1

        try:
            ha_ip = await async_get_source_ip(self.hass)
        except HomeAssistantError:
            ha_ip = None

        return NetworkData(
            devices=devices,
            counts=counts,
            ha_ip=ha_ip,
            router_available=router_available,
            router_model=self._provider.model if self._provider else None,
            last_scan=dt_util.utcnow(),
        )

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


def _ip_sort_key(device: dict[str, Any]) -> tuple[int, int]:
    ip = device.get("ip")
    if ip:
        try:
            return (0, int(IPv4Address(ip)))
        except ValueError:
            pass
    return (1, 0)
