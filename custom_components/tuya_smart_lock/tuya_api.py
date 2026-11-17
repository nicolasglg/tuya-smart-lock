"""Tuya Cloud API client for Smart Lock operations."""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import aiohttp

from .const import (
    API_REGIONS,
    DOOR_OPERATE_ENDPOINT,
    LOCK_CATEGORIES,
    PASSWORD_FREE_OPEN_DOOR_ENDPOINT,
    REMOTE_UNLOCKS_ENDPOINT,
    SHADOW_PROPERTIES_ENDPOINT,
    STATUS_ENDPOINT,
    TICKET_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)

# Tuya error codes meaning the project cannot use an endpoint at all
# (permission deny, uri path invalid). Other failures are treated as transient.
_ENDPOINT_NOT_AUTHORIZED_CODES = {1106, 1108}


@dataclass(frozen=True)
class LockReading:
    """Lock state read from the Tuya cloud.

    ``unlocked`` is True/False, or None when unknown. ``reported`` is True when
    the device itself reported ``lock_motor_state`` after the last command sent
    by this integration, as opposed to a value inferred from ``automatic_lock``.
    """

    unlocked: bool | None
    reported: bool = False
    auto_lock: bool = False


def _reading_from_datapoints(
    values: dict,
    times: dict | None = None,
    command_at: float | None = None,
) -> LockReading:
    """Interpret Tuya datapoints as a lock state.

    Many Wi-Fi access-control locks (category ``mk``) never refresh
    ``lock_motor_state`` in the cloud after pairing: the datapoint stays
    frozen at its first reported value. ``lock_motor_state`` is therefore
    only trusted when the device reported it after our last command. Otherwise,
    when the device has automatic locking enabled, the bolt is assumed locked
    outside the auto-lock window; without auto-lock the last reported value is
    used as before.
    """
    motor = values.get("lock_motor_state")
    if not isinstance(motor, bool):
        motor = None
    auto_lock = values.get("automatic_lock") is True
    motor_time = (times or {}).get("lock_motor_state")

    if command_at is not None and motor is not None and motor_time is not None and motor_time >= command_at:
        return LockReading(unlocked=motor, reported=True, auto_lock=auto_lock)

    if auto_lock:
        return LockReading(unlocked=False, auto_lock=True)

    return LockReading(unlocked=motor)


class TuyaCloudApi:
    """Tuya Cloud API client for lock operations."""

    def __init__(self, access_id: str, access_secret: str, region: str = "eu") -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._base_url = f"https://{API_REGIONS[region]}"
        self._token: str | None = None
        self._token_expiry: float = 0
        self._uid: str | None = None
        # Tuya server time (seconds) of the last accepted lock/unlock command, per device.
        self._last_command_at: dict[str, float] = {}
        self._shadow_unsupported = False

    async def _ensure_token(self) -> None:
        """Get or refresh the access token."""
        if self._token and time.time() < self._token_expiry:
            return

        url = f"{self._base_url}/v1.0/token?grant_type=1"
        t = str(int(time.time() * 1000))

        string_to_sign = (
            "GET\n"
            + hashlib.sha256(b"").hexdigest()
            + "\n\n"
            + "/v1.0/token?grant_type=1"
        )
        sign_str = self._access_id + t + string_to_sign
        sign = hmac.new(
            self._access_secret.encode(),
            sign_str.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

        headers = {
            "client_id": self._access_id,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "secret": self._access_secret,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                data = await resp.json()

        if not data.get("success"):
            _LOGGER.error("Failed to get Tuya token: %s", data.get("msg"))
            raise ConnectionError(f"Tuya token error: {data.get('msg')}")

        result = data["result"]
        self._token = result["access_token"]
        self._token_expiry = time.time() + result["expire_time"] - 60
        self._uid = result.get("uid")

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Build signed headers for a Tuya API request."""
        t = str(int(time.time() * 1000))
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        string_to_sign = f"{method}\n{content_hash}\n\n{path}"
        sign_str = self._access_id + self._token + t + string_to_sign
        sign = hmac.new(
            self._access_secret.encode(),
            sign_str.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

        return {
            "client_id": self._access_id,
            "access_token": self._token,
            "sign": sign,
            "t": t,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        """Make a signed request to the Tuya API."""
        await self._ensure_token()
        url = f"{self._base_url}{path}"
        body_str = json.dumps(body) if body else ""
        headers = self._sign_request(method, path, body_str)

        async with aiohttp.ClientSession() as session:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    return await resp.json()
            else:
                async with session.post(url, headers=headers, data=body_str) as resp:
                    return await resp.json()

    async def async_test_credentials(self) -> bool:
        """Test if the credentials are valid."""
        try:
            self._token = None
            self._token_expiry = 0
            await self._ensure_token()
            return True
        except ConnectionError:
            return False

    async def async_discover_devices(self) -> list[dict]:
        """Discover lock devices linked to this account."""
        await self._ensure_token()

        # Use the associated-users endpoint which lists all devices linked via the app.
        # It returns 20 devices per page by default, so on accounts with more devices
        # the locks can fall outside the first page and never be discovered. Ask for
        # the largest page and follow last_row_key until the account is exhausted.
        # Query params have to stay alphabetically sorted or the signature is rejected.
        all_devices = []
        last_row_key = None

        while True:
            path = "/v1.0/iot-01/associated-users/devices?"
            if last_row_key:
                path += f"last_row_key={last_row_key}&"
            path += "size=100"

            resp = await self._request("GET", path)

            if not resp.get("success"):
                _LOGGER.error("Failed to list devices: %s", resp.get("msg"))
                return []

            # Response structure: result.devices (list)
            result = resp.get("result", {})
            if not isinstance(result, dict):
                all_devices.extend(result)
                break

            page = result.get("devices") or []
            all_devices.extend(page)
            last_row_key = result.get("last_row_key")

            if not result.get("has_more") or not page or not last_row_key:
                break

        devices = []
        for device in all_devices:
            category = device.get("category", "")
            if category in LOCK_CATEGORIES:
                devices.append({
                    "id": device["id"],
                    "name": device.get("name", device["id"]),
                    "category": category,
                    "model": device.get("model", ""),
                    "product_name": device.get("product_name", ""),
                })

        _LOGGER.debug(
            "Discovery: %d device(s) linked to the cloud project, %d lock(s) matched; categories seen: %s",
            len(all_devices),
            len(devices),
            ", ".join(sorted({str(d.get("category") or "?") for d in all_devices if isinstance(d, dict)})) or "none",
        )
        return devices

    async def async_check_remote_unlock(self, device_id: str) -> bool:
        """Check if remote unlock without password is enabled."""
        path = REMOTE_UNLOCKS_ENDPOINT.format(device_id=device_id)
        resp = await self._request("GET", path)

        if not resp.get("success"):
            _LOGGER.warning("Could not check remote unlock status: %s", resp.get("msg"))
            return True  # Assume enabled if we can't check

        for unlock_type in resp.get("result", []):
            if unlock_type.get("remote_unlock_type") == "remoteUnlockWithoutPwd":
                return unlock_type.get("open", False)

        return False

    async def async_get_auto_lock_time(self, device_id: str) -> int | None:
        """Get the auto-lock delay in seconds from device status."""
        path = STATUS_ENDPOINT.format(device_id=device_id)
        resp = await self._request("GET", path)

        if not resp.get("success"):
            return None

        for dp in resp.get("result", []):
            if dp["code"] == "auto_lock_time":
                return dp["value"]

        return None

    async def _async_get_ticket(self, device_id: str) -> str | None:
        """Request a temporary password-free ticket for a lock command."""
        path = TICKET_ENDPOINT.format(device_id=device_id)
        ticket_resp = await self._request("POST", path)

        if not ticket_resp.get("success"):
            _LOGGER.error("Failed to get ticket: %s", ticket_resp.get("msg"))
            return None

        return ticket_resp["result"]["ticket_id"]

    def _record_command(self, device_id: str, resp: dict) -> None:
        """Remember when Tuya accepted a command, using the server clock when available."""
        server_ms = resp.get("t")
        if isinstance(server_ms, (int, float)) and not isinstance(server_ms, bool):
            self._last_command_at[device_id] = server_ms / 1000
        else:
            self._last_command_at[device_id] = time.time()

    async def async_unlock(self, device_id: str) -> bool:
        """Unlock the door via ticket flow."""
        ticket_id = await self._async_get_ticket(device_id)
        if ticket_id is None:
            return False

        path = DOOR_OPERATE_ENDPOINT.format(device_id=device_id)
        unlock_resp = await self._request("POST", path, {"ticket_id": ticket_id, "open": True})

        if unlock_resp.get("success"):
            self._record_command(device_id, unlock_resp)
            _LOGGER.info("Door %s unlocked successfully", device_id)
            return True

        # Some locks reject door-operate (e.g. "not support this device") but
        # accept Tuya's password-free open-door endpoint, which documents
        # support for Bluetooth and video locks. Retry once there with a fresh
        # ticket. The first command was refused, so this cannot unlock twice.
        _LOGGER.warning(
            "door-operate refused to unlock %s (%s); retrying with password-free open-door",
            device_id,
            unlock_resp.get("msg"),
        )
        ticket_id = await self._async_get_ticket(device_id)
        if ticket_id is None:
            return False

        path = PASSWORD_FREE_OPEN_DOOR_ENDPOINT.format(device_id=device_id)
        open_resp = await self._request("POST", path, {"ticket_id": ticket_id})

        if open_resp.get("success") and open_resp.get("result") is not False:
            self._record_command(device_id, open_resp)
            _LOGGER.info("Door %s unlocked successfully with password-free open-door", device_id)
            return True

        _LOGGER.error(
            "Failed to unlock: %s (door-operate: %s)",
            open_resp.get("msg"),
            unlock_resp.get("msg"),
        )
        return False

    async def async_lock(self, device_id: str) -> bool:
        """Lock the door via ticket flow."""
        ticket_id = await self._async_get_ticket(device_id)
        if ticket_id is None:
            return False

        path = DOOR_OPERATE_ENDPOINT.format(device_id=device_id)
        lock_resp = await self._request("POST", path, {"ticket_id": ticket_id, "open": False})

        if not lock_resp.get("success"):
            _LOGGER.error("Failed to lock: %s", lock_resp.get("msg"))
            return False

        self._record_command(device_id, lock_resp)
        _LOGGER.info("Door %s locked successfully", device_id)
        return True

    async def async_get_lock_state(self, device_id: str) -> bool | None:
        """Return True if unlocked, False if locked, None when unknown."""
        reading = await self.async_read_lock_state(device_id)
        return reading.unlocked if reading is not None else None

    async def async_read_lock_state(self, device_id: str) -> LockReading | None:
        """Read the lock state, or None when the cloud could not be read.

        After a command, timestamped shadow properties tell whether the device
        reported ``lock_motor_state`` since then. Without a command (startup),
        or when the project cannot read shadow properties, the plain device
        status is used.
        """
        command_at = self._last_command_at.get(device_id)
        if command_at is not None and not self._shadow_unsupported:
            reading = await self._async_read_shadow_state(device_id, command_at)
            if reading is not None:
                return reading

        path = STATUS_ENDPOINT.format(device_id=device_id)
        resp = await self._request("GET", path)

        if not resp.get("success"):
            _LOGGER.error("Failed to get status: %s", resp.get("msg"))
            return None

        values = {
            dp.get("code"): dp.get("value")
            for dp in resp.get("result", [])
            if isinstance(dp, dict)
        }
        return _reading_from_datapoints(values)

    async def _async_read_shadow_state(self, device_id: str, command_at: float) -> LockReading | None:
        """Read timestamped datapoints; None means fall back to the device status."""
        path = SHADOW_PROPERTIES_ENDPOINT.format(device_id=device_id)
        resp = await self._request("GET", path)

        if not resp.get("success"):
            if resp.get("code") in _ENDPOINT_NOT_AUTHORIZED_CODES:
                self._shadow_unsupported = True
            _LOGGER.debug("Shadow properties unavailable for %s: %s", device_id, resp.get("msg"))
            return None

        result = resp.get("result")
        properties = result.get("properties") if isinstance(result, dict) else None
        if not isinstance(properties, list):
            return None

        values: dict = {}
        times: dict = {}
        for prop in properties:
            if not isinstance(prop, dict) or not prop.get("code"):
                continue
            values[prop["code"]] = prop.get("value")
            report_ms = prop.get("time")
            if isinstance(report_ms, (int, float)) and not isinstance(report_ms, bool):
                times[prop["code"]] = report_ms / 1000

        return _reading_from_datapoints(values, times, command_at)
