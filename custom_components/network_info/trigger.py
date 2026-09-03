"""Integration triggers: a device coming online, going offline, or new.

These are what the automation editor lists under Network Info when picking a
trigger *by target* — the hub device is the target, and the optional MAC, IP
and name fields say which device to wait for (all given fields must match;
none means any device). The same three exist as device triggers in
device_trigger.py, reachable *by type → Device*, where the editor can build a
dropdown of known devices on demand — this framework's fields are static, so
here the device is named rather than picked.

Both listen for the presence events the coordinator fires each cycle.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_OPTIONS, CONF_TARGET
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.trigger import Trigger, TriggerActionRunner, TriggerConfig
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_DEVICE_OFFLINE, EVENT_DEVICE_ONLINE, EVENT_NEW_DEVICE
from .coordinator import device_key

CONF_MAC = "mac"
CONF_IP = "ip"
CONF_NAME = "name"

_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_MAC): cv.string,
        vol.Optional(CONF_IP): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)
_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_OPTIONS, default=dict): _OPTIONS_SCHEMA,
        vol.Optional(CONF_TARGET): cv.TARGET_FIELDS,
    }
)

_EVENT_FIELDS = (
    "key",
    "mac",
    "ip",
    "name",
    "hostname",
    "vendor",
    "connection",
    "access_point",
    "signal",
)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _entry_ids_for_target(
    hass: HomeAssistant, target: dict[str, Any] | None
) -> set[str] | None:
    """The config entries a target names, or None for "every entry".

    A target can name the hub device, one of its entities, or an area the hub
    sits in. A target that names nothing of ours resolves to an empty set —
    the trigger then never fires, which is more honest than firing for all.
    """
    if not target:
        return None
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    device_ids = set(_as_list(target.get("device_id")))
    for area_id in _as_list(target.get("area_id")):
        device_ids.update(
            device.id for device in dr.async_entries_for_area(device_reg, area_id)
        )
    for entity_id in _as_list(target.get("entity_id")):
        entry = entity_reg.async_get(entity_id)
        if entry is not None and entry.device_id:
            device_ids.add(entry.device_id)
    entry_ids: set[str] = set()
    for device_id in device_ids:
        device = device_reg.async_get(device_id)
        if device is None:
            continue
        for entry_id in device.config_entries:
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN:
                entry_ids.add(entry_id)
    return entry_ids


class _PresenceTrigger(Trigger):
    """One presence event, filtered by target and by the optional fields."""

    _event_type: str

    @classmethod
    async def async_validate_config(
        cls, hass: HomeAssistant, config: ConfigType
    ) -> ConfigType:
        """Validate the fields; the target keeps the shape the editor gave it."""
        return _CONFIG_SCHEMA(config)

    def __init__(self, hass: HomeAssistant, config: TriggerConfig) -> None:
        super().__init__(hass, config)
        options = config.options or {}
        self._want_mac = device_key(options[CONF_MAC]) if options.get(CONF_MAC) else None
        self._want_ip = (options.get(CONF_IP) or "").strip() or None
        self._want_name = (options.get(CONF_NAME) or "").strip().lower() or None
        self._entry_ids = _entry_ids_for_target(hass, config.target)

    def _matches(self, data: dict[str, Any]) -> bool:
        if self._entry_ids is not None and data.get("entry_id") not in self._entry_ids:
            return False
        if self._want_mac is not None and (data.get("mac") or "").lower() != self._want_mac:
            return False
        if self._want_ip is not None and data.get("ip") != self._want_ip:
            return False
        if self._want_name is not None and self._want_name not in (
            str(data.get("name") or "").strip().lower(),
            str(data.get("hostname") or "").strip().lower(),
        ):
            return False
        return True

    async def async_attach_runner(
        self, run_action: TriggerActionRunner, did_not_trigger: Any = None
    ) -> CALLBACK_TYPE:
        """Listen for the presence event and run the action on a match."""

        @callback
        def _handle_event(event: Event) -> None:
            data = event.data
            if not self._matches(data):
                return
            run_action(
                {"event": event, **{field: data.get(field) for field in _EVENT_FIELDS}},
                f"{self._event_type} ({data.get('name') or data.get('key')})",
                event.context,
            )

        return self._hass.bus.async_listen(self._event_type, _handle_event)


class DeviceOnlineTrigger(_PresenceTrigger):
    """A device came online."""

    _event_type = EVENT_DEVICE_ONLINE


class DeviceOfflineTrigger(_PresenceTrigger):
    """A device went offline."""

    _event_type = EVENT_DEVICE_OFFLINE


class NewDeviceTrigger(_PresenceTrigger):
    """A device was seen for the first time."""

    _event_type = EVENT_NEW_DEVICE


TRIGGERS: dict[str, type[Trigger]] = {
    "device_online": DeviceOnlineTrigger,
    "device_offline": DeviceOfflineTrigger,
    "new_device": NewDeviceTrigger,
}


async def async_get_triggers(hass: HomeAssistant) -> dict[str, type[Trigger]]:
    """Return the triggers this integration provides."""
    return TRIGGERS
