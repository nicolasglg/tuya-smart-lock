"""Config flow for Tuya Smart Lock."""

import logging

import voluptuous as vol

from homeassistant import config_entries

from .const import CONF_ACCESS_ID, CONF_ACCESS_SECRET, CONF_API_REGION, CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN
from .tuya_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

REGIONS = {
    "eu": "Europe",
    "us": "Americas",
    "cn": "China",
    "in": "India",
}


class TuyaSmartLockConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya Smart Lock."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: TuyaCloudApi | None = None
        self._credentials: dict = {}
        self._discovered_devices: list[dict] = []

    async def async_step_user(self, user_input: dict | None = None):
        """Step 1: Collect Tuya Cloud credentials."""
        errors = {}

        if user_input is not None:
            api = TuyaCloudApi(
                access_id=user_input[CONF_ACCESS_ID],
                access_secret=user_input[CONF_ACCESS_SECRET],
                region=user_input[CONF_API_REGION],
            )

            if await api.async_test_credentials():
                self._api = api
                self._credentials = user_input
                return await self.async_step_select_device()

            errors["base"] = "invalid_auth"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCESS_ID): str,
                    vol.Required(CONF_ACCESS_SECRET): str,
                    vol.Required(CONF_API_REGION, default="eu"): vol.In(REGIONS),
                }
            ),
            errors=errors,
        )

    async def async_step_select_device(self, user_input: dict | None = None):
        """Step 2: Discover and select a lock device."""
        errors = {}

        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]

            # Find device name from discovered list
            device_name = device_id
            for device in self._discovered_devices:
                if device["id"] == device_id:
                    device_name = device["name"]
                    break

            # Check remote unlock is enabled
            remote_ok = await self._api.async_check_remote_unlock(device_id)
            if not remote_ok:
                errors["base"] = "remote_unlock_disabled"
            else:
                # Set unique ID and check not already configured
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=device_name,
                    data={
                        CONF_ACCESS_ID: self._credentials[CONF_ACCESS_ID],
                        CONF_ACCESS_SECRET: self._credentials[CONF_ACCESS_SECRET],
                        CONF_API_REGION: self._credentials[CONF_API_REGION],
                        CONF_DEVICE_ID: device_id,
                        CONF_DEVICE_NAME: device_name,
                    },
                )

        # Discover devices
        if not self._discovered_devices:
            self._discovered_devices = await self._api.async_discover_devices()

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        # Build device selection list
        device_options = {
            device["id"]: f"{device['name']} ({device['category']})"
            for device in self._discovered_devices
        }

        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_ID): vol.In(device_options),
                }
            ),
            errors=errors,
        )
