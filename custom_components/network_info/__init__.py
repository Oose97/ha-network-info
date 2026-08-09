"""The Network Info integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .cards import async_register as async_register_cards
from .cards import async_unregister as async_unregister_cards
from .const import DOMAIN
from .coordinator import NetworkInfoCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

NetworkInfoConfigEntry = ConfigEntry[NetworkInfoCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> bool:
    """Set up Network Info from a config entry."""
    coordinator = NetworkInfoCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # The cards' resource-wait checks this to know the integration is loaded.
    hass.data.setdefault(DOMAIN, set()).add(entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await async_register_cards(hass)
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
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, set()).discard(entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> None:
    """Clean up the Lovelace resources when the last entry is removed."""
    if not hass.data.get(DOMAIN):
        await async_unregister_cards(hass)
