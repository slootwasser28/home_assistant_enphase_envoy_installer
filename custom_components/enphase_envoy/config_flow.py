"""Config flow for Enphase Envoy integration."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from .envoy_reader import EnvoyReader, EnlightenError, EnvoyError
import httpx
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import zeroconf
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.network import is_ipv4_address, is_ipv6_address

from .const import (
    DOMAIN,
    CONF_SERIAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_REALTIME_UPDATE_THROTTLE,
    ENABLE_ADDITIONAL_METRICS,
    DEFAULT_GETDATA_TIMEOUT,
)
from .envoy_endpoints import ENVOY_ENDPOINTS


_LOGGER = logging.getLogger(__name__)

ENVOY = "Envoy"


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> EnvoyReader:
    """Validate the user input allows us to connect."""
    envoy_reader = EnvoyReader(
        data[CONF_HOST],
        enlighten_user=data[CONF_USERNAME],
        enlighten_pass=data[CONF_PASSWORD],
        inverters=False,
        enlighten_serial_num=data[CONF_SERIAL],
    )

    try:
        await envoy_reader.get_data()
    except EnlightenError as err:
        raise InvalidAuth from err
    except (EnvoyError, httpx.ConnectError) as err:
        raise CannotConnect from err

    return envoy_reader


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enphase Envoy."""

    VERSION = 1

    def __init__(self):
        """Initialize an envoy flow."""
        self.ip_address = None
        self.username = None
        self._reauth_entry = None

    @callback
    def _async_generate_schema(self):
        """Generate schema."""
        schema = {}

        if self.ip_address:
            schema[vol.Required(CONF_HOST, default=self.ip_address)] = vol.In(
                [self.ip_address]
            )
        else:
            schema[vol.Required(CONF_HOST)] = str

        schema[vol.Required(CONF_SERIAL, default=self.unique_id)] = str
        schema[vol.Required(CONF_USERNAME, default=self.username)] = str
        schema[vol.Required(CONF_PASSWORD, default="")] = str

        return vol.Schema(schema)

    @callback
    def _async_current_hosts(self):
        """Return a set of hosts."""
        return {
            entry.data[CONF_HOST]
            for entry in self._async_current_entries(include_ignore=False)
            if CONF_HOST in entry.data
        }

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle a flow initialized by zeroconf discovery."""
        serial = discovery_info.properties["serialnum"]
        await self.async_set_unique_id(serial)
        self.ip_address = discovery_info.host

        for entry in self._async_current_entries(include_ignore=False):
            if entry.unique_id == self.unique_id:
                if entry.data[CONF_HOST] != self.ip_address:
                    """Update current host ip to new discovered one if same ip version"""
                    if (
                        is_ipv4_address(entry.data[CONF_HOST])
                        and is_ipv4_address(self.ip_address)
                    ) or (
                        is_ipv6_address(entry.data[CONF_HOST])
                        and is_ipv6_address(self.ip_address)
                    ):
                        self.hass.config_entries.async_update_entry(
                            entry, data={**entry.data, CONF_HOST: self.ip_address}
                        )
                        self.hass.async_create_task(
                            self.hass.config_entries.async_reload(entry.entry_id),
                            f"config entry reload {entry.title} {entry.domain} {entry.entry_id}",
                        )

                return self.async_abort(reason="already_configured")
            elif (
                entry.unique_id is None
                and CONF_HOST in entry.data
                and entry.data[CONF_HOST] == self.ip_address
            ):
                title = f"{ENVOY} {serial}" if entry.title == ENVOY else ENVOY
                self.hass.config_entries.async_update_entry(
                    entry, title=title, unique_id=serial
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entry.entry_id)
                )
                return self.async_abort(reason="already_configured")

        return await self.async_step_user()

    async def async_step_reauth(self, user_input):
        """Handle configuration by re-auth."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user()

    def _async_envoy_name(self) -> str:
        """Return the name of the envoy."""
        if self.unique_id:
            return f"{ENVOY} {self.unique_id}"
        return ENVOY

    async def _async_set_unique_id_from_envoy(self, envoy_reader: EnvoyReader) -> bool:
        """Set the unique id by fetching it from the envoy."""
        serial = None
        with contextlib.suppress(httpx.HTTPError):
            serial = await envoy_reader.get_full_serial_number()
        if serial:
            await self.async_set_unique_id(serial)
            return True
        return False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            if (
                not self._reauth_entry
                and user_input[CONF_HOST] in self._async_current_hosts()
            ):
                return self.async_abort(reason="already_configured")
            try:
                envoy_reader = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                data = user_input.copy()
                data[CONF_NAME] = self._async_envoy_name()

                if self._reauth_entry:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data=data,
                    )
                    return self.async_abort(reason="reauth_successful")

                if not self.unique_id and await self._async_set_unique_id_from_envoy(
                    envoy_reader
                ):
                    data[CONF_NAME] = self._async_envoy_name()

                if self.unique_id:
                    self._abort_if_unique_id_configured({CONF_HOST: data[CONF_HOST]})

                return self.async_create_entry(title=data[CONF_NAME], data=data)

        if self.unique_id:
            self.context["title_placeholders"] = {
                CONF_SERIAL: self.unique_id,
                CONF_HOST: self.ip_address,
            }
        return self.async_show_form(
            step_id="user",
            data_schema=self._async_generate_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        return EnvoyOptionsFlowHandler()


class EnvoyOptionsFlowHandler(config_entries.OptionsFlow):
    """Envoy config flow options handler."""

    async def async_step_init(self, _user_input=None):
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        optional_endpoints = {
            f"endpoint_{key}": key
            for key, endpoint in ENVOY_ENDPOINTS.items()
            if endpoint["optional"]
        }
        disabled_endpoints = [
            ep
            for ep in self.config_entry.options.get("disabled_endpoints", [])
            if ep in optional_endpoints.keys()
        ]

        schema = {
            vol.Optional(
                "time_between_update",
                default=self.config_entry.options.get(
                    "time_between_update", DEFAULT_SCAN_INTERVAL
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=5)),
            vol.Optional(
                "getdata_timeout",
                default=self.config_entry.options.get(
                    "getdata_timeout", DEFAULT_GETDATA_TIMEOUT
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=30)),
            vol.Optional(
                "disable_negative_production",
                default=self.config_entry.options.get(
                    "disable_negative_production", False
                ),
            ): bool,
            vol.Optional(
                "enable_realtime_updates",
                default=self.config_entry.options.get("enable_realtime_updates", False),
            ): bool,
            vol.Optional(
                "realtime_update_throttle",
                default=self.config_entry.options.get(
                    "realtime_update_throttle", DEFAULT_REALTIME_UPDATE_THROTTLE
                ),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),
            vol.Optional(
                ENABLE_ADDITIONAL_METRICS,
                default=self.config_entry.options.get(ENABLE_ADDITIONAL_METRICS, False),
            ): bool,
            vol.Optional(
                "enable_pcu_comm_check",
                default=self.config_entry.options.get("enable_pcu_comm_check", False),
            ): bool,
            vol.Optional(
                "devstatus_device_data",
                default=self.config_entry.options.get("devstatus_device_data", False),
            ): bool,
            vol.Optional(
                "lifetime_production_correction",
                default=self.config_entry.options.get(
                    "lifetime_production_correction", 0
                ),
            ): vol.All(vol.Coerce(int)),
            vol.Optional(
                "disabled_endpoints",
                description={"suggested_value": disabled_endpoints},
            ): cv.multi_select(optional_endpoints),
        }
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema))


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
