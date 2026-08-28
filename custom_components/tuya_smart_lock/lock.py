"""Lock entity for Tuya Smart Lock."""

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_NAME,
    DOMAIN,
    RECONCILE_INTERVAL_MINUTES,
    SIGNAL_PULSAR_MESSAGE,
    SIGNAL_PULSAR_STATUS,
    UNLOCK_EVENT_PREFIX,
    UNLOCK_EVENT_STALE_THRESHOLD_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_AUTO_LOCK_DELAY = 3

# Real-time state comes from Pulsar push; this is only a slow safety net for a
# dropped message. See RECONCILE_INTERVAL_MINUTES for the quota reasoning.
SCAN_INTERVAL = timedelta(minutes=RECONCILE_INTERVAL_MINUTES)


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
        [TuyaSmartLock(api, device_id, device_name, auto_lock_time, data.get("pulsar"))]
    )


class TuyaSmartLock(LockEntity):
    """Lock entity that controls a Tuya smart lock via Cloud API."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True

    def __init__(
        self,
        api,
        device_id: str,
        device_name: str,
        auto_lock_time: int,
        pulsar=None,
    ) -> None:
        self._api = api
        self._device_id = device_id
        self._auto_lock_time = auto_lock_time
        self._attr_unique_id = f"tuya_smart_lock_{device_id}"
        self._attr_is_locked = True
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        self._device_name = device_name
        self._relock_timer = None
        self._pulsar = pulsar
        self._last_unlock_method: str | None = None
        self._last_unlock_age_seconds: float | None = None
        self._last_unlock_was_stale: bool | None = None

    @property
    def extra_state_attributes(self):
        """Expose push-connection health.

        This box writes no home-assistant.log, so without these there's no way
        to tell a working push connection from a silently dead one.
        """
        if not self._pulsar:
            return None
        last = self._pulsar.last_message_at
        return {
            "push_connected": self._pulsar.connected,
            "push_frames_received": self._pulsar.frame_count,
            "push_messages_received": self._pulsar.message_count,
            "push_last_message": last.isoformat() if last else None,
            "push_last_error": self._pulsar.last_error,
            "push_last_decode_error": self._pulsar.last_decode_error,
            "push_last_frame": self._pulsar.last_frame_preview,
            "push_connect_attempts": self._pulsar.connect_attempts,
            "last_unlock_method": self._last_unlock_method,
            "last_unlock_age_seconds": self._last_unlock_age_seconds,
            "last_unlock_was_stale": self._last_unlock_was_stale,
        }

    @property
    def device_info(self):
        """Link to the existing Tuya device if present, otherwise create our own."""
        return {
            "identifiers": {("tuya", self._device_id)},
            "name": self._device_name,
            "manufacturer": "Tuya",
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to Tuya push messages."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_PULSAR_MESSAGE, self._handle_pulsar_message
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_PULSAR_STATUS, self._handle_pulsar_status
            )
        )

    @callback
    def _handle_pulsar_status(self) -> None:
        """Re-publish diagnostic attributes when the push link flaps."""
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending auto-relock reset."""
        self._cancel_relock()

    @callback
    def _handle_pulsar_message(self, message: dict) -> None:
        """Apply a decoded Tuya push message to this entity's state.

        This is what makes physical (fingerprint/keypad) and Smart Life app
        unlocks visible to HA at all -- the cloud API gives us no other way to
        learn about an unlock we didn't issue ourselves.
        """
        # Refresh diagnostics on EVERY message, including ones for other
        # devices on the account. `extra_state_attributes` is only re-rendered
        # when the entity writes state, so returning early here freezes the
        # push counters at whatever they were during the last write -- which
        # reads as "push is dead" when it is in fact working perfectly. That
        # false signal cost a long debugging detour; don't reintroduce it.
        if message.get("devId") != self._device_id:
            self.async_write_ha_state()
            return

        status = message.get("status") or []
        if not status:
            # bizCode messages (online/offline/nameUpdate) carry no datapoints.
            self.async_write_ha_state()
            return

        # Two signals can appear in the SAME message: `lock_motor_state` (the
        # motor's raw position) and an `unlock_*` datapoint (the fact that an
        # unlock just happened). The lock's own auto-relock can fire fast
        # enough that Tuya bundles both into one push -- fingerprint-unlock
        # immediately followed by the already-relocked motor position. A plain
        # last-write-wins loop over `status` let whichever datapoint happened
        # to be listed second silently decide the outcome, which meant a real
        # unlock could be reported and then erased within the same message,
        # producing NO visible state change at all. Root-caused 2026-08-24: an
        # unlock was confirmed reaching this handler (last_unlock_method was
        # set) while the lock's `state` never left "locked", which is exactly
        # this failure. It broke any automation that depends on this entity
        # transitioning to "unlocked" -- a transition that was silently never
        # happening.
        #
        # Fix: track both signals independently across the whole loop, then
        # apply UNLOCK_EVENT_PREFIX as authoritative over lock_motor_state
        # when both are present -- exactly the intent already documented on
        # UNLOCK_EVENT_PREFIX in const.py, which this loop did not actually
        # implement. The physical motor may already be relocked by the time
        # we're told about the unlock; that's fine, we still want anything
        # watching this entity to see that an unlock genuinely happened, even
        # if briefly.
        #
        # Second issue: many of these locks are battery-saving BLE peripherals
        # that disconnect when idle (see button.py's warm-link helper), so an
        # unlock_* datapoint can arrive long after it actually happened -- it
        # sat in the lock's local buffer until something forced a reconnect
        # and flushed it. Each datapoint carries its own `t` (epoch ms);
        # comparing that against wall-clock receipt time is how a live touch
        # is told apart from a replayed backlog event. Only a *fresh*
        # unlock_* is allowed to flip this entity to "unlocked" -- a stale one
        # still updates last_unlock_method for an honest history, but falls
        # back to trusting lock_motor_state (the device's actual current
        # position) instead of reporting a live unlock for something that's
        # already over. This matters most for any downstream automation that
        # triggers on this entity reaching "unlocked".
        unlock_method = None
        unlock_is_fresh = False
        unlock_age_seconds: float | None = None
        motor_unlocked = None
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for datapoint in status:
            code = datapoint.get("code", "")
            value = datapoint.get("value")

            if code == "lock_motor_state":
                # True == motor retracted == unlocked
                motor_unlocked = bool(value)
            elif code.startswith(UNLOCK_EVENT_PREFIX):
                # unlock_fingerprint / unlock_password / unlock_card / ...
                # The value identifies *who*; its presence is the event.
                _LOGGER.debug("Lock %s opened via %s", self._device_id, code)
                unlock_method = code
                event_t = datapoint.get("t")
                if event_t is None:
                    # No timestamp on this datapoint at all -- can't verify
                    # freshness, so fail open (preserve prior behavior) rather
                    # than silently dropping a real unlock we have no way to
                    # judge.
                    unlock_is_fresh = True
                else:
                    unlock_age_seconds = (now_ms - event_t) / 1000
                    unlock_is_fresh = (
                        unlock_age_seconds <= UNLOCK_EVENT_STALE_THRESHOLD_SECONDS
                    )
                    if not unlock_is_fresh:
                        _LOGGER.warning(
                            "Lock %s: %s is %.0fs old (>%ds threshold) -- "
                            "treating as stale, not reporting a live unlock",
                            self._device_id,
                            code,
                            unlock_age_seconds,
                            UNLOCK_EVENT_STALE_THRESHOLD_SECONDS,
                        )

        if unlock_method is not None:
            self._last_unlock_method = unlock_method
            self._last_unlock_age_seconds = unlock_age_seconds
            self._last_unlock_was_stale = not unlock_is_fresh
            if unlock_is_fresh:
                unlocked = True
            elif motor_unlocked is not None:
                unlocked = motor_unlocked
            else:
                self.async_write_ha_state()
                return
        elif motor_unlocked is not None:
            unlocked = motor_unlocked
        else:
            self.async_write_ha_state()
            return

        self._cancel_relock()
        self._attr_is_locking = False
        self._attr_is_unlocking = False
        self._attr_is_locked = not unlocked
        self.async_write_ha_state()

        if unlocked:
            self._schedule_relock()

    async def async_update(self) -> None:
        """Slow reconciliation poll in case a push message was missed."""
        if self._attr_is_locking or self._attr_is_unlocking:
            return  # don't fight an in-flight command with a stale poll
        is_unlocked = await self._api.async_get_lock_state(self._device_id)
        if is_unlocked is not None:
            self._attr_is_locked = not is_unlocked

    async def async_lock(self, **kwargs) -> None:
        """Lock the door."""
        self._attr_is_locking = True
        self.async_write_ha_state()

        success = await self._api.async_lock(self._device_id)

        self._attr_is_locking = False
        if success:
            self._cancel_relock()
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
            self._schedule_relock()

    def _schedule_relock(self) -> None:
        """Reset state to locked once the lock's own auto-lock has fired."""
        self._cancel_relock()
        delay = self._auto_lock_time + 1  # + buffer
        self._relock_timer = self.hass.loop.call_later(delay, self._set_locked)

    def _cancel_relock(self) -> None:
        if self._relock_timer is not None:
            self._relock_timer.cancel()
            self._relock_timer = None

    @callback
    def _set_locked(self) -> None:
        """Reset state to locked after auto-lock delay."""
        self._relock_timer = None
        self._attr_is_locked = True
        self.async_write_ha_state()
