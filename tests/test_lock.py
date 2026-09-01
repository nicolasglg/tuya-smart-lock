"""Regression tests for Tuya Smart Lock state verification."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _install_home_assistant_stubs() -> None:
    aiohttp = types.ModuleType("aiohttp")

    class ClientError(Exception):
        pass

    setattr(aiohttp, "ClientError", ClientError)
    sys.modules["aiohttp"] = aiohttp

    modules = {
        name: types.ModuleType(name)
        for name in (
            "homeassistant",
            "homeassistant.components",
            "homeassistant.components.lock",
            "homeassistant.config_entries",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.entity_platform",
            "homeassistant.helpers.event",
        )
    }

    class LockEntity:
        def async_write_ha_state(self):
            self.state_writes = getattr(self, "state_writes", 0) + 1

        async def async_will_remove_from_hass(self):
            self.base_remove_called = True

    def async_call_later(hass, delay, callback):
        scheduled = {"delay": delay, "callback": callback, "cancelled": False}
        hass.scheduled.append(scheduled)

        def cancel():
            scheduled["cancelled"] = True

        return cancel

    setattr(modules["homeassistant.components.lock"], "LockEntity", LockEntity)
    setattr(modules["homeassistant.config_entries"], "ConfigEntry", object)
    setattr(modules["homeassistant.core"], "HomeAssistant", object)
    setattr(modules["homeassistant.helpers.entity_platform"], "AddEntitiesCallback", object)
    setattr(modules["homeassistant.helpers.event"], "async_call_later", async_call_later)
    sys.modules.update(modules)


def _load_lock_module():
    package_name = "custom_components.tuya_smart_lock"
    root = Path(__file__).parents[1] / "custom_components" / "tuya_smart_lock"
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package

    for name in ("const", "lock"):
        spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", root / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.lock"]


_install_home_assistant_stubs()
lock_module = _load_lock_module()


class FakeApi:
    def __init__(self, state):
        self.state = state
        self.state_calls = 0

    async def async_unlock(self, _device_id):
        return True

    async def async_lock(self, _device_id):
        return True

    async def async_get_auto_lock_time(self, _device_id):
        return 3

    async def async_get_lock_state(self, _device_id):
        self.state_calls += 1
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


class FakeHass:
    def __init__(self):
        self.scheduled = []


class TuyaSmartLockVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_requests_initial_cloud_refresh_before_add(self):
        api = FakeApi(False)
        entry = types.SimpleNamespace(entry_id="entry")
        hass = types.SimpleNamespace(
            data={
                lock_module.DOMAIN: {
                    "entry": {
                        "api": api,
                        "entry_data": {
                            lock_module.CONF_DEVICE_ID: "device",
                            lock_module.CONF_DEVICE_NAME: "Portillon",
                        },
                    }
                }
            }
        )
        added = []

        def add_entities(entities, update_before_add=False):
            added.append((entities, update_before_add))

        await lock_module.async_setup_entry(hass, entry, add_entities)

        self.assertEqual(len(added), 1)
        self.assertTrue(added[0][1])

    def make_lock(self, state):
        entity = lock_module.TuyaSmartLock(FakeApi(state), "device", "Portillon", 3)
        entity.hass = FakeHass()
        return entity

    async def test_initial_state_is_unavailable_until_cloud_refresh(self):
        entity = self.make_lock(False)

        self.assertFalse(entity._attr_available)
        self.assertIsNone(entity._attr_is_locked)

    async def test_cloud_refresh_makes_entity_available_with_real_state(self):
        entity = self.make_lock(False)

        await entity.async_update()

        self.assertTrue(entity._attr_available)
        self.assertTrue(entity._attr_is_locked)

    async def test_verification_uses_real_locked_state(self):
        entity = self.make_lock(False)
        entity._attr_is_locked = False
        await entity._async_verify_after_auto_lock(None)
        self.assertTrue(entity._attr_is_locked)
        self.assertEqual(entity._api.state_calls, 1)

    async def test_verification_preserves_state_when_cloud_read_fails(self):
        entity = self.make_lock(None)
        entity._attr_is_locked = False
        await entity._async_verify_after_auto_lock(None)
        self.assertFalse(entity._attr_is_locked)
        self.assertEqual(entity._api.state_calls, 1)

    async def test_verification_preserves_state_when_cloud_read_raises(self):
        entity = self.make_lock(ConnectionError("offline"))
        entity._attr_is_locked = False
        await entity._async_verify_after_auto_lock(None)
        self.assertFalse(entity._attr_is_locked)
        self.assertEqual(entity._api.state_calls, 1)

    async def test_lock_success_waits_for_real_state_verification(self):
        entity = self.make_lock(False)
        entity._attr_available = True
        entity._attr_is_locked = False

        await entity.async_lock()

        self.assertFalse(entity._attr_available)
        self.assertFalse(entity._attr_is_locked)
        self.assertEqual(len(entity.hass.scheduled), 1)
        self.assertEqual(entity.hass.scheduled[0]["delay"], 5)

    async def test_unlock_schedules_one_shot_verification_without_polling(self):
        entity = self.make_lock(False)
        await entity.async_unlock()
        self.assertFalse(entity._attr_should_poll)
        self.assertEqual(len(entity.hass.scheduled), 1)
        scheduled = entity.hass.scheduled[0]
        self.assertEqual(scheduled["delay"], 4)
        self.assertEqual(scheduled["callback"], entity._async_verify_after_auto_lock)

    async def test_repeated_unlock_replaces_pending_verification(self):
        entity = self.make_lock(False)
        await entity.async_unlock()
        first = entity.hass.scheduled[0]
        await entity.async_unlock()
        self.assertTrue(first["cancelled"])
        self.assertEqual(len(entity.hass.scheduled), 2)
        self.assertFalse(entity.hass.scheduled[1]["cancelled"])

    async def test_entity_removal_cancels_pending_verification(self):
        entity = self.make_lock(False)
        await entity.async_unlock()
        scheduled = entity.hass.scheduled[0]
        await entity.async_will_remove_from_hass()
        self.assertTrue(scheduled["cancelled"])
        self.assertTrue(entity.base_remove_called)


if __name__ == "__main__":
    unittest.main()
