"""Sensors exposing the merged network device list."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetworkInfoConfigEntry
from .const import (
    ATTR_COUNTS,
    ATTR_DEVICES,
    ATTR_HA_IP,
    ATTR_IP_LOG,
    ATTR_LAST_SCAN,
    ATTR_ROUTER_AVAILABLE,
    ATTR_ROUTER_MODEL,
    CONF_EXTERNAL_IP,
    CONF_EXTERNAL_IP_LOG,
)
from .coordinator import NetworkInfoCoordinator
from .entity import NetworkInfoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetworkInfoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Network Info sensors."""
    coordinator = entry.runtime_data
    config = {**entry.data, **entry.options}
    entities: list[SensorEntity] = [
        NetworkDevicesSensor(coordinator, entry),
        HomeAssistantIpSensor(coordinator, entry),
    ]
    if config.get(CONF_EXTERNAL_IP) or config.get(CONF_EXTERNAL_IP_LOG):
        entities.append(ExternalIpSensor(coordinator, entry))
    if config.get(CONF_EXTERNAL_IP_LOG):
        entities.append(ExternalIpLogSensor(coordinator, entry))
    async_add_entities(entities)


class NetworkDevicesSensor(NetworkInfoEntity, SensorEntity):
    """Number of online devices, with the full device list as attributes."""

    _attr_name = "Devices"
    _attr_icon = "mdi:lan"
    _attr_native_unit_of_measurement = "devices"
    # The devices list is large and changes constantly — keep it out of the
    # recorder database. Live dashboards still see it.
    _unrecorded_attributes = frozenset({ATTR_DEVICES})

    def __init__(
        self, coordinator: NetworkInfoCoordinator, entry: NetworkInfoConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_devices"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.counts.get("online")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        return {
            ATTR_DEVICES: data.devices,
            ATTR_COUNTS: data.counts,
            ATTR_ROUTER_AVAILABLE: data.router_available,
            ATTR_ROUTER_MODEL: data.router_model,
            ATTR_HA_IP: data.ha_ip,
            ATTR_LAST_SCAN: data.last_scan.isoformat() if data.last_scan else None,
        }


class ExternalIpSensor(NetworkInfoEntity, SensorEntity):
    """The network's current public IP (opt-in)."""

    _attr_name = "External IP"
    _attr_icon = "mdi:wan"

    def __init__(
        self, coordinator: NetworkInfoCoordinator, entry: NetworkInfoConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_external_ip"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.external_ip


class ExternalIpLogSensor(NetworkInfoEntity, SensorEntity):
    """Change log of the public IP. State is the row count, so a state
    increase is a clean automation trigger for "the IP changed"."""

    _attr_name = "External IP log"
    _attr_icon = "mdi:ip-network-outline"
    _unrecorded_attributes = frozenset({ATTR_IP_LOG})

    def __init__(
        self, coordinator: NetworkInfoCoordinator, entry: NetworkInfoConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_external_ip_log"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.ip_log or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        # The current IP rides along so the log card can mark it standalone.
        return {ATTR_IP_LOG: data.ip_log or [], "external_ip": data.external_ip}


class HomeAssistantIpSensor(NetworkInfoEntity, SensorEntity):
    """Home Assistant's own local IP address."""

    _attr_name = "Home Assistant IP"
    _attr_icon = "mdi:ip-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: NetworkInfoCoordinator, entry: NetworkInfoConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_ha_ip"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.ha_ip
