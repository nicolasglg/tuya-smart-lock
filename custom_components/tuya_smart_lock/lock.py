"""Lock entity for Tuya Smart Lock."""

import logging
from datetime import timedelta

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUTO_LOCK_DELAY = 3
SCAN_INTERVAL = timedelta(seconds=20)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up lock entity from config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    entry_data = data["entry_data"]
    device_id = entry_data[CONF_DEVICE_ID]
    device_name = entry_data[CONF_DEVICE_NAME]

    # Read auto_lock_time from device
    auto_lock_time = await api.async_get_auto_lock_time(device_id)
    if auto_lock_time is None:
        auto_lock_time = DEFAULT_AUTO_LOCK_DELAY

    async_add_entities([TuyaSmartLock(api, device_id, device_name, auto_lock_time)])


class TuyaSmartLock(LockEntity):
    """Lock entity that controls a Tuya smart lock via Cloud API."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True

    def __init__(self, api, device_id: str, device_name: str, auto_lock_time: int) -> None:
        self._api = api
        self._device_id = device_id
        self._auto_lock_time = auto_lock_time
        self._attr_unique_id = f"tuya_smart_lock_{device_id}"
        self._attr_is_locked = True
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        self._device_name = device_name

    @property
    def device_info(self):
        """Link to the existing Tuya device if present, otherwise create our own."""
        return {
            "identifiers": {("tuya", self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tuya",
        }

    async def async_update(self) -> None:
        """Refresh the real lock state from the device."""
        state = await self._api.async_get_lock_state(self._device_id)
        if state is None:
            _LOGGER.warning(
                "Could not refresh lock state for %s; keeping last known state",
                self._device_id,
            )
            return
        self._attr_is_locked = not state

    async def async_lock(self, **kwargs) -> None:
        """Lock the door."""
        self._attr_is_locking = True
        self.async_write_ha_state()

        success = await self._api.async_lock(self._device_id)

        self._attr_is_locking = False
        if success:
            self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        """Unlock the door."""
        self._attr_is_unlocking = True
        self.async_write_ha_state()

        success = await self._api.async_unlock(self._device_id)

        self._attr_is_unlocking = False
        if success:
            self._attr_is_locked = False
        self.async_write_ha_state()

        if success:
            # Verify the real state after the device's auto-lock window instead
            # of assuming it relocked itself.
            delay = self._auto_lock_time + 1
            async_call_later(self.hass, delay, self._async_verify_after_auto_lock)

    async def _async_verify_after_auto_lock(self, _now) -> None:
        """Check the actual device state after the auto-lock window elapses."""
        await self.async_update()
        self.async_write_ha_state()

