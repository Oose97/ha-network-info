"""Buttons for Network Info."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NetworkInfoConfigEntry
from .entity import NetworkInfoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetworkInfoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Network Info buttons."""
    async_add_entities([ScanNowButton(entry.runtime_data, entry)])


class ScanNowButton(NetworkInfoEntity, ButtonEntity):
    """Trigger an immediate scan, outside the regular interval."""

    _attr_name = "Scan now"
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator, entry: NetworkInfoConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_scan_now"

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()
