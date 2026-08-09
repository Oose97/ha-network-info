"""Serve and register the bundled Lovelace card.

`frontend.add_extra_js_url` looks simpler, but a card registered that way is
invisible in the UI and gives no feedback at all when it fails to load — the
dashboard just reports "Custom element doesn't exist". Registering real
Lovelace resources is what other integrations shipping cards do, it survives
restarts, and the resource is visible and removable under
Settings → Dashboards → Resources.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.loader import async_get_integration

from .const import DOMAIN, URL_CARDS

_LOGGER = logging.getLogger(__name__)

CARD_FILES = ("network-info-table.js", "network-info-ip-log.js")

_PATHS_DONE = f"{DOMAIN}_static_paths"
RETRY_SECONDS = 5
# Lovelace resources load within seconds on a healthy system; two minutes of
# retries means something else is wrong, and looping forever would not fix it.
MAX_RESOURCE_RETRIES = 24


def _lovelace(hass: HomeAssistant) -> Any | None:
    return hass.data.get("lovelace")


def _resource_mode(hass: HomeAssistant) -> str | None:
    """Storage vs YAML mode, across the versions that spell it differently."""
    data = _lovelace(hass)
    if data is None:
        return None
    for attr in ("resource_mode", "mode"):
        mode = getattr(data, attr, None)
        if mode is not None:
            return mode
    if isinstance(data, dict):
        return data.get("mode")
    return None


def _resources(hass: HomeAssistant) -> Any | None:
    data = _lovelace(hass)
    if data is None:
        return None
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    return resources


async def async_register(hass: HomeAssistant) -> None:
    """Serve the card files and make sure Lovelace knows about them."""
    await _async_register_paths(hass)

    if _resource_mode(hass) != "storage":
        # YAML-mode dashboards manage their own resource list; the files are
        # served either way, so the user only has to add the URLs once.
        _LOGGER.info(
            "Lovelace is in YAML mode — add these as module resources yourself: %s",
            ", ".join(f"{URL_CARDS}/{name}" for name in CARD_FILES),
        )
        return

    await _async_wait_for_resources(hass)


async def _async_register_paths(hass: HomeAssistant) -> None:
    if hass.data.get(_PATHS_DONE):
        return

    cards_dir = pathlib.Path(__file__).parent / "frontend"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_CARDS, str(cards_dir), False)]
        )
        _LOGGER.debug("Serving cards from %s at %s", cards_dir, URL_CARDS)
    except RuntimeError:
        # Already registered by a previous setup of this same integration.
        _LOGGER.debug("Static paths already registered")

    hass.data[_PATHS_DONE] = True


async def _async_wait_for_resources(hass: HomeAssistant) -> None:
    """Lovelace resources load asynchronously; register once they are ready."""
    attempts = 0

    async def _check(_now: Any = None) -> None:
        nonlocal attempts
        if not hass.data.get(DOMAIN):
            # Every entry was unloaded while we waited; a retry landing now
            # would register resources for an integration that is gone.
            return
        resources = _resources(hass)
        if resources is not None and getattr(resources, "loaded", False):
            await _async_register_resources(hass, resources)
            return
        attempts += 1
        if attempts >= MAX_RESOURCE_RETRIES:
            _LOGGER.warning(
                "Lovelace resources never became available — add these as "
                "module resources yourself: %s",
                ", ".join(f"{URL_CARDS}/{name}" for name in CARD_FILES),
            )
            return
        _LOGGER.debug("Lovelace resources not loaded yet; retrying in %ss", RETRY_SECONDS)
        async_call_later(hass, RETRY_SECONDS, _check)

    await _check()


async def _async_register_resources(hass: HomeAssistant, resources: Any) -> None:
    """Add a module resource per card, or bump the version on an existing one."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version or "0")

    existing = {
        str(item["url"]).split("?")[0]: item
        for item in resources.async_items()
        if str(item.get("url", "")).startswith(URL_CARDS)
    }

    for filename in CARD_FILES:
        url = f"{URL_CARDS}/{filename}"
        versioned = f"{url}?v={version}"
        current = existing.get(url)

        if current is None:
            _LOGGER.info("Registering Lovelace resource %s", versioned)
            await resources.async_create_item({"res_type": "module", "url": versioned})
        elif str(current.get("url")) != versioned:
            # Version moved on: update in place so the browser refetches
            # instead of serving a cached copy of the old card.
            _LOGGER.info("Updating Lovelace resource to %s", versioned)
            await resources.async_update_item(
                current.get("id"), {"res_type": "module", "url": versioned}
            )


async def async_unregister(hass: HomeAssistant) -> None:
    """Drop the resources. Only on entry removal, never on a reload."""
    resources = _resources(hass)
    if resources is None or _resource_mode(hass) != "storage":
        return

    for item in list(resources.async_items()):
        if str(item.get("url", "")).startswith(URL_CARDS):
            _LOGGER.info("Removing Lovelace resource %s", item.get("url"))
            await resources.async_delete_item(item.get("id"))
