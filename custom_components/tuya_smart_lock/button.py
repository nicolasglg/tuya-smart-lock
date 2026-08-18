"""Warm-link button for Tuya Smart Lock.

The lock is a BLE peripheral that deliberately disconnects when idle to save
battery, so it cannot push an unlock event unless a connection already exists.
The gateway *can* open one on demand -- that is why cloud remote-unlock works
even with the phone's Bluetooth off.

Pressing this button writes `beep_volume` back to the value it already has: a
no-op on the device, but a real command, which forces the gateway to connect.
Measured behaviour: command at T+0, device acks at ~T+3s, lock reports ONLINE
with a full datapoint sync at ~T+6s. Fire this on an approach/arrival trigger
and the link is already up by the time someone touches the lock, so the unlock
is delivered in ~1s instead of being queued until the next reconnect.

Each press wakes the lock's radio, so drive it from arrival events rather than
a fixed timer -- battery, not API quota, is the limiting factor.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN, WARM_LINK_DP

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the warm-link button."""
    data = hass.data[DOMAIN][entry.entry_id]
    entry_data = data["entry_data"]
    async_add_entities(
        [
            TuyaSmartLockWarmLinkButton(
                data["api"],
                entry_data[CONF_DEVICE_ID],
                entry_data[CONF_DEVICE_NAME],
            )
        ]
    )


class TuyaSmartLockWarmLinkButton(ButtonEntity):
    """Forces the gateway to open a BLE connection to the lock."""

    _attr_has_entity_name = True
    _attr_name = "Warm BLE link"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_entity_registry_enabled_default = True

    def __init__(self, api, device_id: str, device_name: str) -> None:
        self._api = api
        self._device_id = device_id
        self._device_name = device_name
        self._attr_unique_id = f"tuya_smart_lock_{device_id}_warm_link"

    @property
    def device_info(self):
        return {
            "identifiers": {("tuya", self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tuya",
        }

    async def async_press(self) -> None:
        """Send a no-op command so the gateway connects to the lock."""
        # Read the current value first and write that same value back, so this
        # can never actually change the user's beep setting.
        current = await self._api.async_get_dp(self._device_id, WARM_LINK_DP)
        if current is None:
            _LOGGER.warning(
                "Could not read %s; skipping warm-up rather than guessing a value",
                WARM_LINK_DP,
            )
            return

        ok = await self._api.async_send_command(self._device_id, WARM_LINK_DP, current)
        _LOGGER.debug(
            "Warm-link command %s=%s -> %s", WARM_LINK_DP, current, "ok" if ok else "failed"
        )
