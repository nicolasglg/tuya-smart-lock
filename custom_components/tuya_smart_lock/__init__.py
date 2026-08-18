"""Tuya Smart Lock integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_API_REGION,
    DOMAIN,
    SIGNAL_PULSAR_MESSAGE,
    SIGNAL_PULSAR_STATUS,
)
from .pulsar import TuyaPulsarClient
from .tuya_api import TuyaCloudApi

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.LOCK, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tuya Smart Lock from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    api = TuyaCloudApi(
        access_id=entry.data[CONF_ACCESS_ID],
        access_secret=entry.data[CONF_ACCESS_SECRET],
        region=entry.data[CONF_API_REGION],
    )

    def _on_pulsar_message(message: dict) -> None:
        """Fan a decoded push message out to whichever entity owns the device."""
        async_dispatcher_send(hass, SIGNAL_PULSAR_MESSAGE, message)

    def _on_pulsar_status() -> None:
        """Push connection came up or went down."""
        async_dispatcher_send(hass, SIGNAL_PULSAR_STATUS)

    pulsar = TuyaPulsarClient(
        hass,
        access_id=entry.data[CONF_ACCESS_ID],
        access_secret=entry.data[CONF_ACCESS_SECRET],
        region=entry.data[CONF_API_REGION],
        on_message=_on_pulsar_message,
        on_status_change=_on_pulsar_status,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "entry_data": entry.data,
        "pulsar": pulsar,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Started after the platform so the entity is already listening.
    pulsar.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data and (pulsar := data.get("pulsar")):
        await pulsar.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
