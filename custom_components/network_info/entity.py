"""Shared base entity for Network Info."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import NetworkInfoCoordinator


class NetworkInfoEntity(CoordinatorEntity[NetworkInfoCoordinator]):
    """Groups every entity under one service device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: NetworkInfoCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Network Info",
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="Network Info",
        )
