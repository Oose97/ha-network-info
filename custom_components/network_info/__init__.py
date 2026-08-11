"""The Network Info integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store

from .cards import async_register as async_register_cards
from .cards import async_unregister as async_unregister_cards
from .const import (
    DOMAIN,
    OVERRIDABLE_CONNECTIONS,
    PATH_AUTO,
    SERVICE_FORGET_DEVICE,
    SERVICE_IMPORT_IP_LOG,
    SERVICE_SET_NAME,
    SERVICE_SET_PATH,
    STORAGE_VERSION,
    ip_log_storage_key,
    storage_key,
)
from .coordinator import NetworkInfoCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

NetworkInfoConfigEntry = ConfigEntry[NetworkInfoCoordinator]

FORGET_DEVICE_SCHEMA = vol.Schema({vol.Required("mac"): cv.string})
IMPORT_IP_LOG_SCHEMA = vol.Schema({vol.Required("path"): cv.string})
SET_PATH_SCHEMA = vol.Schema(
    {
        vol.Required("mac"): cv.string,
        vol.Required("path"): vol.In((*OVERRIDABLE_CONNECTIONS, PATH_AUTO)),
    }
)
# An empty or omitted name clears the custom name, so the field is optional.
SET_NAME_SCHEMA = vol.Schema(
    {
        vol.Required("mac"): cv.string,
        vol.Optional("name", default=""): cv.string,
    }
)


def _device_key(value: str) -> str:
    """Normalize the service's device reference (MAC, or ip:<ip>)."""
    return value.strip().lower().replace("-", ":")


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
        mac = _device_key(str(call.data["mac"]))
        forgotten = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: NetworkInfoCoordinator = entry.runtime_data
            forgotten = await coordinator.async_forget_device(mac) or forgotten
        if not forgotten:
            raise ServiceValidationError(f"No remembered device with MAC {mac}")

    hass.services.async_register(
        DOMAIN, SERVICE_FORGET_DEVICE, _handle_forget, schema=FORGET_DEVICE_SCHEMA
    )

    async def _handle_set_path(call: ServiceCall) -> None:
        key = _device_key(str(call.data["mac"]))
        path = call.data["path"]
        connection = None if path == PATH_AUTO else path
        found = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: NetworkInfoCoordinator = entry.runtime_data
            found = await coordinator.async_set_path(key, connection) or found
        if not found:
            raise ServiceValidationError(f"No remembered device with MAC {key}")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_PATH, _handle_set_path, schema=SET_PATH_SCHEMA
    )

    async def _handle_set_name(call: ServiceCall) -> None:
        key = _device_key(str(call.data["mac"]))
        name = str(call.data["name"]).strip()
        found = False
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator: NetworkInfoCoordinator = entry.runtime_data
            found = await coordinator.async_set_name(key, name or None) or found
        if not found:
            raise ServiceValidationError(f"No remembered device with MAC {key}")

    hass.services.async_register(
        DOMAIN, SERVICE_SET_NAME, _handle_set_name, schema=SET_NAME_SCHEMA
    )

    async def _handle_import_ip_log(call: ServiceCall) -> ServiceResponse:
        path = str(call.data["path"]).strip()
        coordinators = [
            entry.runtime_data
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if entry.runtime_data.ip_log_enabled
        ]
        if not coordinators:
            raise ServiceValidationError(
                "Enable external IP change logging in the integration options first"
            )
        totals: dict[str, int] = {}
        for coordinator in coordinators:
            try:
                totals["rows"] = await coordinator.async_import_ip_log(path)
            except FileNotFoundError:
                raise ServiceValidationError(f"File not found: {path}") from None
            except ValueError as err:
                raise ServiceValidationError(str(err)) from None
        return totals

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_IP_LOG,
        _handle_import_ip_log,
        schema=IMPORT_IP_LOG_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
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
            hass.services.async_remove(DOMAIN, SERVICE_IMPORT_IP_LOG)
            hass.services.async_remove(DOMAIN, SERVICE_SET_PATH)
            hass.services.async_remove(DOMAIN, SERVICE_SET_NAME)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: NetworkInfoConfigEntry) -> None:
    """Delete the entry's stored data and clean up Lovelace resources."""
    for key in (storage_key(entry.entry_id), ip_log_storage_key(entry.entry_id)):
        store: Store = Store(hass, STORAGE_VERSION, key)
        await store.async_remove()
    if not hass.data.get(DOMAIN):
        await async_unregister_cards(hass)
