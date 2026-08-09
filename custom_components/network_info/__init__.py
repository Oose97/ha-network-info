"""The Network Info integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .cards import async_register as async_register_cards
from .cards import async_unregister as async_unregister_cards
from .const import DOMAIN, SERVICE_FORGET_DEVICE, STORAGE_VERSION, storage_key
from .coordinator import NetworkInfoCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

NetworkInfoConfigEntry = ConfigEntry[NetworkInfoCoordinator]

FORGET_DEVICE_SCHEMA = vol.Schema({vol.Required("mac"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> bool:
    """Set up Network Info from a config entry."""
    coordinator = NetworkInfoCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # The cards' resource-wait checks this to know the integration is loaded.
    hass.data.setdefault(DOMAIN, set()).add(entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    await async_register_cards(hass)
    return True


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_FORGET_DEVICE):
        return

    async def _handle_forget(call: ServiceCall) -> None:
        mac = str(call.data["mac"]).strip().lower().replace("-", ":")
        forgotten = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: NetworkInfoCoordinator = entry.runtime_data
            forgotten = await coordinator.async_forget_device(mac) or forgotten
        if not forgotten:
            raise ServiceValidationError(f"No remembered device with MAC {mac}")

    hass.services.async_register(
        DOMAIN, SERVICE_FORGET_DEVICE, _handle_forget, schema=FORGET_DEVICE_SCHEMA
    )


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
        remaining = [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        if not remaining:
            hass.services.async_remove(DOMAIN, SERVICE_FORGET_DEVICE)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> None:
    """Delete the entry's device memory and clean up Lovelace resources."""
    store: Store = Store(hass, STORAGE_VERSION, storage_key(entry.entry_id))
    await store.async_remove()
    if not hass.data.get(DOMAIN):
        await async_unregister_cards(hass)
