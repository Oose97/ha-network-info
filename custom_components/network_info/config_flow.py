"""Config flow for the Network Info integration.

Two steps: the basics (scan range, interval, router brand, external IP
toggles), then — only when a router brand is selected — the router details,
prefilled from the brand catalog (router/routers.json) and showing only the
credential fields that brand requires. Brand "None" skips router polling
entirely.
"""

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
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AP_NAME,
    CONF_EXTERNAL_IP,
    CONF_EXTERNAL_IP_LOG,
    CONF_IP_RANGE,
    CONF_ROUTER_BRAND,
    CONF_ROUTER_HOST,
    CONF_ROUTER_PASSWORD,
    CONF_ROUTER_USE_HTTPS,
    CONF_ROUTER_USERNAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
    ROUTER_BRAND_NONE,
    SUBENTRY_TYPE_ACCESS_POINT,
)
from .router import RouterAuthError, RouterError, create_provider, load_catalog

_LOGGER = logging.getLogger(__name__)

FALLBACK_IP_RANGE = "192.168.1.0/24"


async def _async_default_ip_range(hass: HomeAssistant) -> str:
    """Suggest the scan range from HA's own IP."""
    try:
        source_ip = await async_get_source_ip(hass)
    except HomeAssistantError:
        source_ip = None
    if not source_ip or ":" in source_ip:  # no result or IPv6
        return FALLBACK_IP_RANGE
    return str(ip_interface(f"{source_ip}/24").network)


def _base_schema(
    defaults: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> vol.Schema:
    brand_options = [
        SelectOptionDict(value=ROUTER_BRAND_NONE, label="None (scanning only)")
    ] + [
        SelectOptionDict(value=brand_id, label=spec["name"])
        for brand_id, spec in catalog.items()
    ]
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
            vol.Required(
                CONF_ROUTER_BRAND,
                default=defaults.get(CONF_ROUTER_BRAND, ROUTER_BRAND_NONE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=brand_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(
                CONF_EXTERNAL_IP, default=bool(defaults.get(CONF_EXTERNAL_IP, False))
            ): BooleanSelector(),
            vol.Optional(
                CONF_EXTERNAL_IP_LOG,
                default=bool(defaults.get(CONF_EXTERNAL_IP_LOG, False)),
            ): BooleanSelector(),
        }
    )


def _router_schema(defaults: dict[str, Any], spec: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_ROUTER_HOST,
            default=defaults.get(CONF_ROUTER_HOST) or spec.get("default_gateway", ""),
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
    }
    if spec.get("requires_username"):
        fields[
            vol.Required(
                CONF_ROUTER_USERNAME,
                default=defaults.get(CONF_ROUTER_USERNAME)
                or spec.get("default_username", ""),
            )
        ] = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
    if spec.get("requires_password"):
        fields[
            vol.Required(
                CONF_ROUTER_PASSWORD, default=defaults.get(CONF_ROUTER_PASSWORD, "")
            )
        ] = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    return vol.Schema(fields)


def _ap_schema(
    defaults: dict[str, Any], catalog: dict[str, dict[str, Any]]
) -> vol.Schema:
    brand_options = [
        SelectOptionDict(
            value=ROUTER_BRAND_NONE, label="None (declare only, no polling)"
        )
    ] + [
        SelectOptionDict(value=brand_id, label=spec["name"])
        for brand_id, spec in catalog.items()
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_AP_NAME, default=defaults.get(CONF_AP_NAME, "")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT)),
            vol.Required(
                CONF_ROUTER_BRAND,
                default=defaults.get(CONF_ROUTER_BRAND, ROUTER_BRAND_NONE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=brand_options, mode=SelectSelectorMode.DROPDOWN
                )
            ),
        }
    )


def _normalize_base(user_input: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    data = {
        CONF_IP_RANGE: user_input[CONF_IP_RANGE].strip(),
        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
        CONF_ROUTER_BRAND: user_input.get(CONF_ROUTER_BRAND, ROUTER_BRAND_NONE),
        CONF_EXTERNAL_IP: bool(user_input.get(CONF_EXTERNAL_IP)),
        CONF_EXTERNAL_IP_LOG: bool(user_input.get(CONF_EXTERNAL_IP_LOG)),
        # Cleared here; the router step fills them when a brand is selected.
        CONF_ROUTER_HOST: "",
        CONF_ROUTER_USERNAME: "",
        CONF_ROUTER_PASSWORD: "",
        CONF_ROUTER_USE_HTTPS: False,
    }
    if not data[CONF_IP_RANGE]:
        errors[CONF_IP_RANGE] = "invalid_ip_range"
    if data[CONF_EXTERNAL_IP_LOG] and not data[CONF_EXTERNAL_IP]:
        errors[CONF_EXTERNAL_IP_LOG] = "log_requires_tracking"
    return data, errors


async def _check_router(
    hass: HomeAssistant,
    base: dict[str, Any],
    spec: dict[str, Any],
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the router step and merge its fields into the entry data."""
    errors: dict[str, str] = {}
    host = (user_input.get(CONF_ROUTER_HOST) or "").strip()
    username = (user_input.get(CONF_ROUTER_USERNAME) or "").strip()
    password = user_input.get(CONF_ROUTER_PASSWORD) or ""
    use_https = bool(spec.get("default_https"))

    if not host:
        errors[CONF_ROUTER_HOST] = "invalid_host"
    elif spec.get("requires_username") and not username:
        errors[CONF_ROUTER_USERNAME] = "username_required"
    elif spec.get("requires_password") and not password:
        errors[CONF_ROUTER_PASSWORD] = "password_required"
    else:
        provider = create_provider(
            base[CONF_ROUTER_BRAND],
            host,
            username,
            password,
            async_get_clientsession(hass),
            use_https,
        )
        if provider is None:
            errors["base"] = "unknown_brand"
        else:
            try:
                await provider.async_login()
            except RouterAuthError as err:
                _LOGGER.warning("Router rejected the credentials: %s", err)
                errors[CONF_ROUTER_PASSWORD] = "invalid_auth"
            except RouterError as err:
                _LOGGER.warning("Router validation failed: %s", err)
                errors[CONF_ROUTER_HOST] = "cannot_connect"

    data = {
        **base,
        CONF_ROUTER_HOST: host,
        CONF_ROUTER_USERNAME: username,
        CONF_ROUTER_PASSWORD: password,
        CONF_ROUTER_USE_HTTPS: use_https,
    }
    return data, errors


async def _load_catalog(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    return await hass.async_add_executor_job(load_catalog)


class NetworkInfoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._base: dict[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        catalog = await _load_catalog(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _normalize_base(user_input)
            if not errors:
                self._async_abort_entries_match({CONF_IP_RANGE: data[CONF_IP_RANGE]})
                self._base = data
                if data[CONF_ROUTER_BRAND] == ROUTER_BRAND_NONE:
                    return self._create(data)
                return await self.async_step_router()
            defaults = user_input
        else:
            defaults = {CONF_IP_RANGE: await _async_default_ip_range(self.hass)}

        return self.async_show_form(
            step_id="user", data_schema=_base_schema(defaults, catalog), errors=errors
        )

    async def async_step_router(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._base is not None
        catalog = await _load_catalog(self.hass)
        spec = catalog[self._base[CONF_ROUTER_BRAND]]
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _check_router(self.hass, self._base, spec, user_input)
            if not errors:
                return self._create(data)
            defaults = user_input
        else:
            defaults = {}

        return self.async_show_form(
            step_id="router",
            data_schema=_router_schema(defaults, spec),
            errors=errors,
            description_placeholders={"brand": spec["name"]},
        )

    def _create(self, data: dict[str, Any]) -> config_entries.ConfigFlowResult:
        return self.async_create_entry(
            title=f"Network Info ({data[CONF_IP_RANGE]})", data=data
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> NetworkInfoOptionsFlow:
        return NetworkInfoOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Access points are children of the entry, added one at a time."""
        return {SUBENTRY_TYPE_ACCESS_POINT: AccessPointSubentryFlow}


class AccessPointSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or reconfigure one downstream access point."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        return await self._async_step_name(user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        return await self._async_step_name(user_input, reconfigure=True)

    async def _async_step_name(
        self, user_input: dict[str, Any] | None, reconfigure: bool = False
    ) -> config_entries.SubentryFlowResult:
        catalog = await _load_catalog(self.hass)
        errors: dict[str, str] = {}
        step = "reconfigure" if reconfigure else "user"
        if user_input is not None:
            name = (user_input.get(CONF_AP_NAME) or "").strip()
            brand = user_input.get(CONF_ROUTER_BRAND, ROUTER_BRAND_NONE)
            if not name:
                errors[CONF_AP_NAME] = "name_required"
            if not errors:
                self._data = {CONF_AP_NAME: name, CONF_ROUTER_BRAND: brand}
                if brand == ROUTER_BRAND_NONE:
                    # Declared but not polled: enough to know the gateway's
                    # view of what is wired cannot be complete. Its address is
                    # still worth having, to label the access point itself.
                    return await self._async_step_address(None, reconfigure)
                return await self._async_step_credentials(None, reconfigure)
            defaults = user_input
        else:
            defaults = dict(self._entry_data()) if reconfigure else {}

        return self.async_show_form(
            step_id=step,
            data_schema=_ap_schema(defaults, catalog),
            errors=errors,
        )

    async def async_step_address(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        return await self._async_step_address(user_input, False)

    async def _async_step_address(
        self, user_input: dict[str, Any] | None, reconfigure: bool
    ) -> config_entries.SubentryFlowResult:
        """Address only, for an access point that is declared but not polled."""
        if user_input is not None:
            self._data[CONF_ROUTER_HOST] = (
                user_input.get(CONF_ROUTER_HOST) or ""
            ).strip()
            return self._finish(reconfigure)

        current = self._entry_data() if reconfigure else {}
        return self.async_show_form(
            step_id="address",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ROUTER_HOST,
                        default=current.get(CONF_ROUTER_HOST, ""),
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
                }
            ),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        return await self._async_step_credentials(user_input, False)

    async def _async_step_credentials(
        self, user_input: dict[str, Any] | None, reconfigure: bool
    ) -> config_entries.SubentryFlowResult:
        catalog = await _load_catalog(self.hass)
        spec = catalog[self._data[CONF_ROUTER_BRAND]]
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _check_router(
                self.hass, self._data, spec, user_input
            )
            if not errors:
                self._data = data
                return self._finish(reconfigure)
            defaults = user_input
        else:
            current = self._entry_data() if reconfigure else {}
            defaults = (
                current
                if current.get(CONF_ROUTER_BRAND) == self._data[CONF_ROUTER_BRAND]
                else {}
            )

        return self.async_show_form(
            step_id="credentials",
            data_schema=_router_schema(defaults, spec),
            errors=errors,
            description_placeholders={"brand": spec["name"]},
        )

    def _entry_data(self) -> dict[str, Any]:
        subentry = self._get_reconfigure_subentry()
        return dict(subentry.data) if subentry else {}

    def _finish(self, reconfigure: bool) -> config_entries.SubentryFlowResult:
        title = self._data[CONF_AP_NAME]
        if reconfigure:
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=self._data,
                title=title,
            )
        return self.async_create_entry(title=title, data=self._data)


class NetworkInfoOptionsFlow(config_entries.OptionsFlow):
    """Allow changing everything after setup, same two steps."""

    def __init__(self) -> None:
        self._base: dict[str, Any] | None = None

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        catalog = await _load_catalog(self.hass)
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _normalize_base(user_input)
            if not errors:
                self._base = data
                if data[CONF_ROUTER_BRAND] == ROUTER_BRAND_NONE:
                    return self.async_create_entry(data=data)
                return await self.async_step_router()
            defaults = user_input
        else:
            defaults = self._current()

        return self.async_show_form(
            step_id="init", data_schema=_base_schema(defaults, catalog), errors=errors
        )

    async def async_step_router(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._base is not None
        catalog = await _load_catalog(self.hass)
        spec = catalog[self._base[CONF_ROUTER_BRAND]]
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _check_router(self.hass, self._base, spec, user_input)
            if not errors:
                return self.async_create_entry(data=data)
            defaults = user_input
        else:
            current = self._current()
            # Keep the stored details only while the brand stays the same;
            # a brand switch starts from that brand's catalog defaults.
            same_brand = (
                current.get(CONF_ROUTER_BRAND) == self._base[CONF_ROUTER_BRAND]
                or (
                    current.get(CONF_ROUTER_BRAND) is None
                    and self._base[CONF_ROUTER_BRAND] == "xiaomi_miwifi"
                )
            )
            defaults = current if same_brand else {}

        return self.async_show_form(
            step_id="router",
            data_schema=_router_schema(defaults, spec),
            errors=errors,
            description_placeholders={"brand": spec["name"]},
        )
