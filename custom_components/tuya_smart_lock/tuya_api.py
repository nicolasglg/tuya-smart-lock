"""Tuya Cloud API client for Smart Lock operations."""

import hashlib
import hmac
import json
import logging
import time

import aiohttp

from .const import (
    API_REGIONS,
    DOOR_OPERATE_ENDPOINT,
    LOCK_CATEGORIES,
    REMOTE_UNLOCKS_ENDPOINT,
    STATUS_ENDPOINT,
    TICKET_ENDPOINT,
)

_LOGGER = logging.getLogger(__name__)


class TuyaCloudApi:
    """Tuya Cloud API client for lock operations."""

    def __init__(self, access_id: str, access_secret: str, region: str = "eu") -> None:
        self._access_id = access_id
        self._access_secret = access_secret
        self._base_url = f"https://{API_REGIONS[region]}"
        self._token: str | None = None
        self._token_expiry: float = 0
        self._uid: str | None = None

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

        # Use the associated-users endpoint which lists all devices linked via the app
        resp = await self._request("GET", "/v1.0/iot-01/associated-users/devices")

        if not resp.get("success"):
            _LOGGER.error("Failed to list devices: %s", resp.get("msg"))
            return []

        # Response structure: result.devices (list)
        result = resp.get("result", {})
        all_devices = result.get("devices", result) if isinstance(result, dict) else result

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

    async def async_unlock(self, device_id: str) -> bool:
        """Unlock the door via ticket flow."""
        path = TICKET_ENDPOINT.format(device_id=device_id)
        ticket_resp = await self._request("POST", path)

        if not ticket_resp.get("success"):
            _LOGGER.error("Failed to get ticket: %s", ticket_resp.get("msg"))
            return False

        ticket_id = ticket_resp["result"]["ticket_id"]

        path = DOOR_OPERATE_ENDPOINT.format(device_id=device_id)
        unlock_resp = await self._request("POST", path, {"ticket_id": ticket_id, "open": True})

        if not unlock_resp.get("success"):
            _LOGGER.error("Failed to unlock: %s", unlock_resp.get("msg"))
            return False

        _LOGGER.info("Door %s unlocked successfully", device_id)
        return True

    async def async_lock(self, device_id: str) -> bool:
        """Lock the door via ticket flow."""
        path = TICKET_ENDPOINT.format(device_id=device_id)
        ticket_resp = await self._request("POST", path)

        if not ticket_resp.get("success"):
            _LOGGER.error("Failed to get ticket: %s", ticket_resp.get("msg"))
            return False

        ticket_id = ticket_resp["result"]["ticket_id"]

        path = DOOR_OPERATE_ENDPOINT.format(device_id=device_id)
        lock_resp = await self._request("POST", path, {"ticket_id": ticket_id, "open": False})

        if not lock_resp.get("success"):
            _LOGGER.error("Failed to lock: %s", lock_resp.get("msg"))
            return False

        _LOGGER.info("Door %s locked successfully", device_id)
        return True

    async def async_get_lock_state(self, device_id: str) -> bool | None:
        """Get lock_motor_state. Returns True if unlocked, False if locked, None on error."""
        path = STATUS_ENDPOINT.format(device_id=device_id)
        resp = await self._request("GET", path)

        if not resp.get("success"):
            _LOGGER.error("Failed to get status: %s", resp.get("msg"))
            return None

        for dp in resp.get("result", []):
            if dp["code"] == "lock_motor_state":
                return dp["value"]

        return None
