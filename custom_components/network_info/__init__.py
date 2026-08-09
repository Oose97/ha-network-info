"""The Network Info integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import NetworkInfoCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

NetworkInfoConfigEntry = ConfigEntry[NetworkInfoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> bool:
    """Set up Network Info from a config entry."""
    coordinator = NetworkInfoCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: NetworkInfoConfigEntry
) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: NetworkInfoConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
