"""Lock entity for Tuya Smart Lock."""

import asyncio
import logging

import aiohttp

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import CONF_DEVICE_ID, CONF_DEVICE_NAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUTO_LOCK_DELAY = 3


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

    async_add_entities(
        [TuyaSmartLock(api, device_id, device_name, auto_lock_time)],
        True,
    )


class TuyaSmartLock(LockEntity):
    """Lock entity that controls a Tuya smart lock via Cloud API."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, api, device_id: str, device_name: str, auto_lock_time: int) -> None:
        self._api = api
        self._device_id = device_id
        self._auto_lock_time = auto_lock_time
        self._attr_unique_id = f"tuya_smart_lock_{device_id}"
        self._attr_available = False
        self._attr_is_locked = None
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        self._device_name = device_name
        self._cancel_verify = None

    @property
    def device_info(self):
        """Link to the existing Tuya device if present, otherwise create our own."""
        return {
            "identifiers": {("tuya", self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tuya",
        }

    async def async_update(self) -> None:
        """Refresh the real lock state from Tuya without periodic polling."""
        state = await self._async_get_real_state()
        if state is None:
            return
        if not isinstance(state, bool):
            _LOGGER.warning(
                "Ignoring invalid lock state type for %s: %s",
                self._device_id,
                type(state).__name__,
            )
            return

        self._attr_is_locked = not state
        self._attr_available = True

    async def _async_get_real_state(self):
        """Read cloud state while converting expected transport failures to unknown."""
        try:
            return await self._api.async_get_lock_state(self._device_id)
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ConnectionError,
            KeyError,
            TypeError,
            ValueError,
        ) as err:
            _LOGGER.warning(
                "Could not read lock state for %s: %s",
                self._device_id,
                type(err).__name__,
            )
            return None

    async def async_lock(self, **kwargs) -> None:
        """Lock the door."""
        self._attr_is_locking = True
        self.async_write_ha_state()

        success = await self._api.async_lock(self._device_id)

        self._attr_is_locking = False
        if success:
            # A successful cloud response only confirms command acceptance, not
            # that the physical bolt moved. Keep the prior state unavailable
            # until a targeted verification completes.
            self._attr_available = False
            self._schedule_verification(5)
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        """Unlock the door."""
        self._attr_is_unlocking = True
        self.async_write_ha_state()

        success = await self._api.async_unlock(self._device_id)

        self._attr_is_unlocking = False
        if success:
            self._attr_is_locked = False
            self._attr_available = True
        self.async_write_ha_state()

        if success:
            # Verify the actual state after the device's auto-lock window instead
            # of assuming that the physical lock completed the operation.
            self._schedule_verification(self._auto_lock_time + 1)

    def _schedule_verification(self, delay: int) -> None:
        """Replace any pending verification with one delayed cloud refresh."""
        self._cancel_pending_verification()
        self._cancel_verify = async_call_later(
            self.hass, delay, self._async_verify_after_auto_lock
        )

    def _cancel_pending_verification(self) -> None:
        """Cancel a pending post-command state verification."""
        if self._cancel_verify is not None:
            self._cancel_verify()
            self._cancel_verify = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel delayed work when Home Assistant removes the entity."""
        self._cancel_pending_verification()
        await super().async_will_remove_from_hass()

    async def _async_verify_after_auto_lock(self, _now) -> None:
        """Refresh the lock state after the configured auto-lock window."""
        self._cancel_verify = None
        was_available = self._attr_available
        previous_state = self._attr_is_locked
        await self.async_update()
        if self._attr_available == was_available and self._attr_is_locked == previous_state:
            return
        self.async_write_ha_state()

