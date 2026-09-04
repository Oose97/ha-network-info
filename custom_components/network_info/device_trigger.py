"""Device triggers: a network device coming online, going offline, or new.

They hang off the integration's hub device, so the automation editor offers
them under Network Info. The picked device comes from a dropdown built from
the current device list — the memory, so offline devices are offered too —
while MAC, IP and name fields allow matching a device that has no fixed
identity, such as a phone with a private (randomised) MAC address. Every
given field must match; none given means any device.

Underneath, each trigger listens for the integration's presence events, so
the same conditions are available to a plain event trigger in YAML.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, Event, HassJob, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_DEVICE_OFFLINE, EVENT_DEVICE_ONLINE, EVENT_NEW_DEVICE
from .coordinator import NetworkInfoCoordinator, device_key

TRIGGER_DEVICE_ONLINE = "device_online"
TRIGGER_DEVICE_OFFLINE = "device_offline"
TRIGGER_NEW_DEVICE = "new_device"

EVENT_FOR_TYPE = {
    TRIGGER_DEVICE_ONLINE: EVENT_DEVICE_ONLINE,
    TRIGGER_DEVICE_OFFLINE: EVENT_DEVICE_OFFLINE,
    TRIGGER_NEW_DEVICE: EVENT_NEW_DEVICE,
}

CONF_NETWORK_DEVICE = "network_device"
CONF_MAC = "mac"
CONF_IP = "ip"
CONF_NAME = "name"

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(EVENT_FOR_TYPE),
        vol.Optional(CONF_NETWORK_DEVICE): cv.string,
        vol.Optional(CONF_MAC): cv.string,
        vol.Optional(CONF_IP): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)


def _entry_for_device(hass: HomeAssistant, device_id: str):
    """The loaded config entry behind one of this integration's hub devices."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    for entry_id in device.config_entries:
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is not None
            and entry.domain == DOMAIN
            and entry.state is ConfigEntryState.LOADED
        ):
            return entry
    return None


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List the triggers the hub device offers."""
    if _entry_for_device(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in EVENT_FOR_TYPE
    ]


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """The optional match fields, with the device list as a dropdown.

    A brand-new device is by definition not in the list yet, so that trigger
    offers no fields — it fires for any newcomer.
    """
    if config[CONF_TYPE] == TRIGGER_NEW_DEVICE:
        return {}
    options: list[SelectOptionDict] = []
    entry = _entry_for_device(hass, config[CONF_DEVICE_ID])
    coordinator: NetworkInfoCoordinator | None = (
        entry.runtime_data if entry is not None else None
    )
    if coordinator is not None and coordinator.data is not None:
        for dev in sorted(
            coordinator.data.devices, key=lambda d: str(d.get("name") or "").lower()
        ):
            key = dev.get("mac") or f"ip:{dev.get('ip')}"
            label = " — ".join(
                part
                for part in (dev.get("name"), dev.get("ip"), dev.get("mac"))
                if part
            )
            options.append(SelectOptionDict(value=key, label=label or key))
    text = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
    return {
        "extra_fields": vol.Schema(
            {
                vol.Optional(CONF_NETWORK_DEVICE): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                vol.Optional(CONF_MAC): text,
                vol.Optional(CONF_IP): text,
                vol.Optional(CONF_NAME): text,
            }
        )
    }


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Listen for the matching presence event."""
    event_type = EVENT_FOR_TYPE[config[CONF_TYPE]]
    entry = _entry_for_device(hass, config[CONF_DEVICE_ID])
    entry_id = entry.entry_id if entry is not None else None
    want_key = device_key(config[CONF_NETWORK_DEVICE]) if config.get(CONF_NETWORK_DEVICE) else None
    want_mac = device_key(config[CONF_MAC]) if config.get(CONF_MAC) else None
    want_ip = (config.get(CONF_IP) or "").strip() or None
    want_name = (config.get(CONF_NAME) or "").strip().lower() or None
    trigger_data = trigger_info["trigger_data"]
    job = HassJob(action)

    @callback
    def _handle_event(event: Event) -> None:
        data = event.data
        if entry_id is not None and data.get("entry_id") != entry_id:
            return
        if want_key is not None and data.get("key") != want_key:
            return
        if want_mac is not None and (data.get("mac") or "").lower() != want_mac:
            return
        if want_ip is not None and data.get("ip") != want_ip:
            return
        if want_name is not None and want_name not in (
            str(data.get("name") or "").strip().lower(),
            str(data.get("hostname") or "").strip().lower(),
        ):
            return
        hass.async_run_hass_job(
            job,
            {
                "trigger": {
                    **trigger_data,
                    **config,
                    "event": event,
                    "description": f"{event_type} ({data.get('name') or data.get('key')})",
                }
            },
            event.context,
        )

    return hass.bus.async_listen(event_type, _handle_event)
