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
# Seconds before retrying a state read that failed or returned nothing usable.
STATE_RETRY_DELAY = 15
# Follow-up reads allowed after one command, for failed reads and for
# auto-lock devices that still report "unlocked" (late Bluetooth delivery).
MAX_FOLLOW_UP_READS = 3


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
        self._follow_up_reads = 0

    @property
    def device_info(self):
        """Link to the existing Tuya device if present, otherwise create our own."""
        return {
            "identifiers": {("tuya", self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tuya",
        }

    async def async_added_to_hass(self) -> None:
        """Retry the initial read later if it failed while Home Assistant started."""
        await super().async_added_to_hass()
        if not self._attr_available:
            self._follow_up_reads = 0
            self._schedule_verification(STATE_RETRY_DELAY)

    async def async_update(self) -> None:
        """Refresh the real lock state from Tuya without periodic polling."""
        await self._async_refresh()

    async def _async_refresh(self):
        """Apply a cloud reading to the entity; return it, or None when unusable."""
        reading = await self._async_get_real_state()
        if reading is None or not isinstance(reading.unlocked, bool):
            return None

        self._attr_is_locked = not reading.unlocked
        self._attr_available = True
        return reading

    async def _async_get_real_state(self):
        """Read cloud state while converting expected transport failures to unknown."""
        try:
            return await self._api.async_read_lock_state(self._device_id)
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
            self._follow_up_reads = 0
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
            self._follow_up_reads = 0
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
        """Refresh the lock state after a command, retrying a bounded number of times."""
        self._cancel_verify = None
        was_available = self._attr_available
        previous_state = self._attr_is_locked
        reading = await self._async_refresh()

        if reading is None:
            if self._follow_up_reads < MAX_FOLLOW_UP_READS:
                self._follow_up_reads += 1
                self._schedule_verification(STATE_RETRY_DELAY)
            else:
                # Never keep a stale optimistic state or leave the entity
                # unavailable for good: without polling nothing else would
                # correct it. Report an unknown state instead.
                self._attr_available = True
                self._attr_is_locked = None
        elif (
            reading.unlocked
            and reading.reported
            and reading.auto_lock
            and self._follow_up_reads < MAX_FOLLOW_UP_READS
        ):
            # The device itself still reports "unlocked" although auto-lock is
            # enabled: a Bluetooth lock may have received the command late, or
            # the door did not re-lock. Read again after another auto-lock window.
            self._follow_up_reads += 1
            self._schedule_verification(self._auto_lock_time + 1)

        if self._attr_available == was_available and self._attr_is_locked == previous_state:
            return
        self.async_write_ha_state()

