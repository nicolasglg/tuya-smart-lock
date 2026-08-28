"""Tuya Pulsar (Message Service) push client.

Tuya's Message Service exposes a Pulsar topic per Cloud Project. Rather than
pulling in the native `pulsar-client` binary dependency (a C++ binding with no
musl wheels, which would have to compile inside the HA container), this speaks
Pulsar's **WebSocket** API on port 8285 -- the same transport Tuya's own
`tuya-connector-python` SDK uses:

    wss://mqe.tuya<region>.com:8285/ws/v2/consumer/persistent/{access_id}/out/{env}/{access_id}-sub

Auth is a pair of custom headers (not HTTP Basic): `username` = Access ID,
`password` = md5(access_id + md5(access_secret))[8:24].

Message envelope is JSON: {"messageId": ..., "payload": <base64>, "properties": {...}}.
The decoded payload is itself JSON with an AES-encrypted `data` field. The
`em` property says which mode ("aes_gcm" on this project; older projects use
ECB) and the key is always access_secret[8:24].

Every message must be acked back over the same socket or Tuya redelivers it
after ackTimeoutMillis.

**Why `websockets` and not `aiohttp`** (which HA already ships): this began on
aiohttp and was switched after an aiohttp client appeared to connect and then
receive nothing. That observation is *not* trustworthy -- it was made while a
separate bug was freezing the diagnostic counters that were being used to judge
it, so "no frames" may have been a reporting artifact rather than real silence.
The `websockets` build is the one that has since been verified end-to-end in
production, so it is what ships here.

Dropping this dependency is therefore an open question, not a settled one. The
change is confined to `_consume()`. If you attempt it, verify against a real
device event with a consumer that actually holds the Failover slot -- an
external probe alongside a running integration is a silent standby and proves
nothing. If the failure is real, it looks exactly like "no events happened".

Note on subscription semantics: the topic is consumed with
subscriptionType=Failover, so only one consumer is active at a time.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from homeassistant.core import HomeAssistant

from .const import PULSAR_TOPIC_ENV, PULSAR_WS_ENDPOINTS, PULSAR_WS_QUERY

_LOGGER = logging.getLogger(__name__)

RECONNECT_MIN_SECONDS = 5
# Was 300s. This is a front door, not a background sync job -- five minutes of
# silent downtime after a disconnect is too slow. See IDLE_TIMEOUT_SECONDS
# below for why disconnects can now also be detected (and recovered from)
# much sooner than they used to be.
RECONNECT_MAX_SECONDS = 60
PING_INTERVAL_SECONDS = 30
PING_TIMEOUT_SECONDS = 10
OPEN_TIMEOUT_SECONDS = 20
# `websockets`' own ping/pong (PING_INTERVAL/TIMEOUT_SECONDS above) only
# proves the TRANSPORT is alive; it says nothing about whether Tuya's
# application is actually delivering anything over it. Observed 2026-08-24:
# this connection sat reporting `connected=True` while genuinely delivering
# zero application frames for 30+ minutes -- ping/pong never caught it,
# because pings were still succeeding at the protocol level the whole time.
# This is a second, independent watchdog: if no application frame (not just
# no pong) arrives within this window, force a reconnect rather than trusting
# a connection that has gone this quiet. Generous enough that a genuinely
# idle Tuya account (no device on the account did anything) shouldn't trip
# it under normal use, per traffic observed on a real account (roughly one
# frame every 10-60s whenever anything was actually happening).
IDLE_TIMEOUT_SECONDS = 300


def _md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


class TuyaPulsarClient:
    """Long-lived consumer of a Tuya Message Service topic."""

    def __init__(
        self,
        hass: HomeAssistant,
        access_id: str,
        access_secret: str,
        region: str,
        on_message,
        on_status_change=None,
    ) -> None:
        self._hass = hass
        self._access_id = access_id
        self._access_secret = access_secret
        self._region = region
        self._on_message = on_message
        self._on_status_change = on_status_change

        self._task: asyncio.Task | None = None
        self._stopping = False

        # Diagnostics, surfaced as entity attributes -- this box writes no
        # home-assistant.log, so the entity is the only practical way to see
        # whether push is actually alive.
        self.connected = False
        self.last_message_at: datetime | None = None
        self.last_error: str | None = None
        self.message_count = 0
        # Counted separately from message_count: a frame that arrives but fails
        # to decode must not look identical to no frame arriving at all.
        self.frame_count = 0
        self.last_decode_error: str | None = None
        self.last_frame_preview: str | None = None
        self.connect_attempts = 0

    def _set_connected(self, connected: bool) -> None:
        if self.connected == connected:
            return
        self.connected = connected
        if self._on_status_change:
            self._on_status_change()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background consumer task."""
        self._stopping = False
        self._task = self._hass.async_create_background_task(
            self._run(), "tuya_smart_lock_pulsar"
        )

    async def async_stop(self) -> None:
        """Stop the consumer and wait for it to unwind."""
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ------------------------------------------------------------------
    # connection handling
    # ------------------------------------------------------------------

    @property
    def topic_url(self) -> str:
        endpoint = PULSAR_WS_ENDPOINTS.get(self._region, PULSAR_WS_ENDPOINTS["us"])
        return (
            f"{endpoint}ws/v2/consumer/persistent/{self._access_id}"
            f"/out/{PULSAR_TOPIC_ENV}/{self._access_id}-sub{PULSAR_WS_QUERY}"
        )

    def _password(self) -> str:
        return _md5_hex(self._access_id + _md5_hex(self._access_secret))[8:24]

    async def _run(self) -> None:
        """Connect, consume, and reconnect forever with backoff."""
        delay = RECONNECT_MIN_SECONDS
        while not self._stopping:
            try:
                await self._consume()
                delay = RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                self._set_connected(False)
                raise
            except Exception as err:  # noqa: BLE001 - must never kill the task
                self.last_error = str(err) or err.__class__.__name__
                _LOGGER.warning(
                    "Tuya Pulsar connection lost (%s); reconnecting in %ss",
                    err,
                    delay,
                )
            finally:
                self._set_connected(False)

            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)

    async def _consume(self) -> None:
        """One connection's lifetime: consume until the socket closes."""
        url = self.topic_url
        headers = {"username": self._access_id, "password": self._password()}

        _LOGGER.debug("Connecting to Tuya Pulsar: %s", url)
        self.connect_attempts += 1

        kwargs = {
            "max_size": 20_000_000,
            "open_timeout": OPEN_TIMEOUT_SECONDS,
            "ping_interval": PING_INTERVAL_SECONDS,
            "ping_timeout": PING_TIMEOUT_SECONDS,
        }
        try:
            connector = websockets.connect(url, additional_headers=headers, **kwargs)
        except TypeError:
            # websockets < 14 spells it differently.
            connector = websockets.connect(url, extra_headers=headers, **kwargs)

        async with connector as socket:
            _LOGGER.info("Tuya Pulsar connected (region=%s)", self._region)
            self.last_error = None
            self._set_connected(True)

            while True:
                try:
                    raw = await asyncio.wait_for(
                        socket.recv(), timeout=IDLE_TIMEOUT_SECONDS
                    )
                # asyncio.TimeoutError and the builtin TimeoutError are only
                # the same class from Python 3.11 on; catch the asyncio one
                # explicitly so this doesn't depend on which Python HA
                # happens to be running.
                except asyncio.TimeoutError as err:
                    # See IDLE_TIMEOUT_SECONDS: ping/pong alone doesn't catch
                    # this. Force the same reconnect path as any other drop.
                    raise ConnectionError(
                        f"no Tuya Pulsar frame in {IDLE_TIMEOUT_SECONDS}s "
                        "(idle timeout -- connection was alive but silent)"
                    ) from err

                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "replace")
                message_id = self._handle_frame(raw)
                if message_id:
                    await socket.send(json.dumps({"messageId": message_id}))

        raise ConnectionError("Tuya Pulsar socket closed")

    def _handle_frame(self, raw: str) -> str | None:
        """Decode and dispatch one frame; return its messageId for acking."""
        self.frame_count += 1
        self.last_frame_preview = raw[:300]

        try:
            envelope = json.loads(raw)
        except ValueError:
            self.last_decode_error = f"non-JSON frame: {raw[:120]}"
            _LOGGER.warning("Ignoring non-JSON Pulsar frame: %s", raw[:200])
            return None

        message_id = envelope.get("messageId")
        payload_b64 = envelope.get("payload")
        if not payload_b64:
            # Pulsar sends ack-responses and *errors* on this same socket; an
            # error here is the difference between "subscribed and idle" and
            # "silently not subscribed", so don't bury it at debug level.
            self.last_decode_error = f"frame without payload: {raw[:160]}"
            _LOGGER.warning("Pulsar frame with no payload: %s", raw[:200])
            return message_id

        try:
            payload = base64.b64decode(payload_b64).decode("utf-8")
            outer = json.loads(payload)
            mode = (envelope.get("properties") or {}).get("em")
            decrypted = self._decrypt(outer["data"], mode)
            message = json.loads(decrypted)
        except Exception as err:  # noqa: BLE001 - still needs acking
            self.last_decode_error = f"{err.__class__.__name__}: {err}"
            _LOGGER.error("Failed to decode Pulsar message: %s", err)
        else:
            _LOGGER.debug("Tuya Pulsar message: %s", message)
            self.last_message_at = datetime.now(timezone.utc)
            self.message_count += 1
            self.last_decode_error = None
            try:
                self._on_message(message)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Error in Pulsar message handler: %s", err)

        return message_id

    # ------------------------------------------------------------------
    # crypto
    # ------------------------------------------------------------------

    def _decrypt(self, data: str, mode: str | None) -> str:
        raw = base64.b64decode(data)
        key = self._access_secret[8:24].encode("utf-8")

        if mode == "aes_gcm":
            return self._decrypt_gcm(raw, key)

        # Older projects don't set `em` at all and use ECB. Be tolerant either
        # way rather than trusting the property: try one, fall back to the other.
        try:
            return self._decrypt_ecb(raw, key)
        except (ValueError, UnicodeDecodeError):
            return self._decrypt_gcm(raw, key)

    @staticmethod
    def _decrypt_gcm(raw: bytes, key: bytes) -> str:
        nonce, ciphertext, tag = raw[:12], raw[12:-16], raw[-16:]
        return AESGCM(key).decrypt(nonce, ciphertext + tag, None).decode("utf-8")

    @staticmethod
    def _decrypt_ecb(raw: bytes, key: bytes) -> str:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(raw) + decryptor.finalize()
        text = plain.decode("utf-8")
        return text[: -ord(text[-1])]
