"""Regression tests for Tuya Smart Lock device discovery."""

import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path


def _install_aiohttp_stub() -> None:
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    setattr(aiohttp, "ClientError", ClientError)
    sys.modules["aiohttp"] = aiohttp


def _load_api_module():
    package_name = "custom_components.tuya_smart_lock"
    root = Path(__file__).parents[1] / "custom_components" / "tuya_smart_lock"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package

    for name in ("const", "tuya_api"):
        spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", root / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.tuya_api"]


_install_aiohttp_stub()
api_module = _load_api_module()

DEVICES_PATH = "/v1.0/iot-01/associated-users/devices"


def _filler(count, start=0):
    """Non-lock devices, the kind that fills the first page on busy accounts."""
    return [
        {"id": f"switch{i}", "name": f"Switch {i}", "category": "kg"}
        for i in range(start, start + count)
    ]


LOCK = {
    "id": "ebtestlockid",
    "name": "Front Door",
    "category": "ms",
    "model": "M1",
    "product_name": "Smart Lock",
}


class FakeApi:
    """TuyaCloudApi with a pre-seeded token and canned HTTP responses."""

    def __init__(self, responses):
        self.api = api_module.TuyaCloudApi("access-id", "access-secret", "us")
        # Skip the token round trip; discovery is what these tests exercise.
        self.api._token = "token"
        self.api._token_expiry = time.time() + 3600
        self.responses = list(responses)
        self.paths = []
        self.api._request = self._request

    async def _request(self, method, path, body=None):
        self.paths.append(path)
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {path}")
        return self.responses.pop(0)


def _page(devices, last_row_key=None, has_more=False):
    result = {"devices": devices, "has_more": has_more}
    if last_row_key is not None:
        result["last_row_key"] = last_row_key
    return {"success": True, "result": result}


class DeviceDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_page_asks_for_the_largest_page_size(self):
        fake = FakeApi([_page([LOCK])])

        await fake.api.async_discover_devices()

        self.assertEqual(fake.paths, [f"{DEVICES_PATH}?size=100"])

    async def test_finds_lock_beyond_the_first_page(self):
        fake = FakeApi(
            [
                _page(_filler(100), last_row_key="KEY1", has_more=True),
                _page(_filler(20, start=100) + [LOCK]),
            ]
        )

        devices = await fake.api.async_discover_devices()

        self.assertEqual([d["id"] for d in devices], [LOCK["id"]])
        self.assertEqual(devices[0]["category"], "ms")
        self.assertEqual(devices[0]["name"], "Front Door")

    async def test_pagination_keeps_query_params_alphabetically_sorted(self):
        # Tuya rejects the request signature when the query string is not sorted.
        fake = FakeApi(
            [
                _page(_filler(100), last_row_key="KEY1", has_more=True),
                _page([LOCK]),
            ]
        )

        await fake.api.async_discover_devices()

        self.assertEqual(fake.paths[1], f"{DEVICES_PATH}?last_row_key=KEY1&size=100")

    async def test_stops_when_the_account_has_a_single_page(self):
        fake = FakeApi([_page(_filler(5) + [LOCK])])

        devices = await fake.api.async_discover_devices()

        self.assertEqual(len(fake.paths), 1)
        self.assertEqual(len(devices), 1)

    async def test_stops_when_has_more_is_set_without_a_row_key(self):
        fake = FakeApi([_page(_filler(3) + [LOCK], has_more=True)])

        devices = await fake.api.async_discover_devices()

        self.assertEqual(len(fake.paths), 1)
        self.assertEqual(len(devices), 1)

    async def test_returns_empty_list_when_a_page_fails(self):
        fake = FakeApi(
            [
                _page(_filler(100), last_row_key="KEY1", has_more=True),
                {"success": False, "msg": "sign invalid"},
            ]
        )

        devices = await fake.api.async_discover_devices()

        self.assertEqual(devices, [])
        self.assertEqual(len(fake.paths), 2)

    async def test_accepts_a_bare_list_result(self):
        fake = FakeApi([{"success": True, "result": _filler(2) + [LOCK]}])

        devices = await fake.api.async_discover_devices()

        self.assertEqual([d["id"] for d in devices], [LOCK["id"]])


if __name__ == "__main__":
    unittest.main()
