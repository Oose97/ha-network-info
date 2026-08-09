"""Config flow for the Network Info integration."""

from __future__ import annotations

import logging
from ipaddress import ip_interface
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.network import async_get_source_ip
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_IP_RANGE,
    CONF_ROUTER_HOST,
    CONF_ROUTER_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)
from .router import RouterAuthError, RouterError
from .router.xiaomi_miwifi import XiaomiMiWiFiProvider

_LOGGER = logging.getLogger(__name__)

FALLBACK_IP_RANGE = "192.168.1.0/24"
FALLBACK_ROUTER_HOST = "192.168.1.1"


async def _async_network_defaults(hass: HomeAssistant) -> tuple[str, str]:
    """Suggest scan range and router host from HA's own IP."""
    try:
        source_ip = await async_get_source_ip(hass)
    except HomeAssistantError:
        source_ip = None
    if not source_ip or ":" in source_ip:  # no result or IPv6
        return FALLBACK_IP_RANGE, FALLBACK_ROUTER_HOST
    network = ip_interface(f"{source_ip}/24").network
    return str(network), str(network.network_address + 1)


async def _async_validate_router(
    hass: HomeAssistant, host: str, password: str
) -> None:
    provider = XiaomiMiWiFiProvider(host, password, async_get_clientsession(hass))
    await provider.async_login()


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_IP_RANGE, default=defaults.get(CONF_IP_RANGE, FALLBACK_IP_RANGE)
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_SCAN_INTERVAL_MINUTES,
                    max=1440,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Optional(
                CONF_ROUTER_HOST, default=defaults.get(CONF_ROUTER_HOST, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Optional(
                CONF_ROUTER_PASSWORD, default=defaults.get(CONF_ROUTER_PASSWORD, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        }
    )


async def _async_check_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalize input and validate the optional router credentials."""
    errors: dict[str, str] = {}
    data = {
        CONF_IP_RANGE: user_input[CONF_IP_RANGE].strip(),
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_ROUTER_HOST: (user_input.get(CONF_ROUTER_HOST) or "").strip(),
        CONF_ROUTER_PASSWORD: user_input.get(CONF_ROUTER_PASSWORD) or "",
    }
    if not data[CONF_IP_RANGE]:
        errors[CONF_IP_RANGE] = "invalid_ip_range"
    if data[CONF_ROUTER_PASSWORD] and not data[CONF_ROUTER_HOST]:
        errors[CONF_ROUTER_HOST] = "password_without_host"
    elif data[CONF_ROUTER_PASSWORD]:
        try:
            await _async_validate_router(
                hass, data[CONF_ROUTER_HOST], data[CONF_ROUTER_PASSWORD]
            )
        except RouterAuthError:
            errors[CONF_ROUTER_PASSWORD] = "invalid_auth"
        except RouterError:
            errors[CONF_ROUTER_HOST] = "cannot_connect"
    return data, errors


class NetworkInfoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_check_input(self.hass, user_input)
            if not errors:
                self._async_abort_entries_match({CONF_IP_RANGE: data[CONF_IP_RANGE]})
                return self.async_create_entry(
                    title=f"Network Info ({data[CONF_IP_RANGE]})", data=data
                )
            defaults = user_input
        else:
            ip_range, router_host = await _async_network_defaults(self.hass)
            defaults = {CONF_IP_RANGE: ip_range, CONF_ROUTER_HOST: router_host}

        return self.async_show_form(
            step_id="user", data_schema=_schema(defaults), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> NetworkInfoOptionsFlow:
        return NetworkInfoOptionsFlow()


class NetworkInfoOptionsFlow(config_entries.OptionsFlow):
    """Allow changing everything after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_check_input(self.hass, user_input)
            if not errors:
                return self.async_create_entry(data=data)
            defaults = user_input
        else:
            defaults = {
                **self.config_entry.data,
                **self.config_entry.options,
            }

        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults), errors=errors
        )
