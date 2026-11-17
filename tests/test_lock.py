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

        async def async_added_to_hass(self):
            self.base_added_called = True

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

    for name in ("const", "tuya_api", "lock"):
        spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", root / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.lock"]


_install_home_assistant_stubs()
lock_module = _load_lock_module()
tuya_api_module = sys.modules["custom_components.tuya_smart_lock.tuya_api"]
LockReading = tuya_api_module.LockReading


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

    async def async_read_lock_state(self, _device_id):
        self.state_calls += 1
        if isinstance(self.state, Exception):
            raise self.state
        if self.state is None or isinstance(self.state, LockReading):
            return self.state
        return LockReading(unlocked=self.state)


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

    async def test_failed_verification_retries_then_reports_unknown(self):
        entity = self.make_lock(None)
        await entity.async_unlock()
        self.assertFalse(entity._attr_is_locked)

        for attempt in range(lock_module.MAX_FOLLOW_UP_READS):
            await entity._async_verify_after_auto_lock(None)
            self.assertFalse(entity._attr_is_locked)
            self.assertEqual(entity.hass.scheduled[-1]["delay"], lock_module.STATE_RETRY_DELAY)

        scheduled_before = len(entity.hass.scheduled)
        await entity._async_verify_after_auto_lock(None)
        self.assertIsNone(entity._attr_is_locked)
        self.assertTrue(entity._attr_available)
        self.assertEqual(len(entity.hass.scheduled), scheduled_before)

    async def test_failed_lock_verification_does_not_stay_unavailable(self):
        entity = self.make_lock(None)
        entity._attr_available = True
        await entity.async_lock()
        self.assertFalse(entity._attr_available)

        for _ in range(lock_module.MAX_FOLLOW_UP_READS + 1):
            await entity._async_verify_after_auto_lock(None)

        self.assertTrue(entity._attr_available)
        self.assertIsNone(entity._attr_is_locked)

    async def test_reported_unlock_with_auto_lock_is_read_again(self):
        entity = self.make_lock(LockReading(unlocked=True, reported=True, auto_lock=True))
        await entity.async_unlock()

        await entity._async_verify_after_auto_lock(None)
        self.assertFalse(entity._attr_is_locked)
        self.assertEqual(entity.hass.scheduled[-1]["delay"], 4)

        entity._api.state = LockReading(unlocked=False, reported=True, auto_lock=True)
        scheduled_before = len(entity.hass.scheduled)
        await entity._async_verify_after_auto_lock(None)
        self.assertTrue(entity._attr_is_locked)
        self.assertEqual(len(entity.hass.scheduled), scheduled_before)

    async def test_reported_unlock_follow_up_reads_are_bounded(self):
        entity = self.make_lock(LockReading(unlocked=True, reported=True, auto_lock=True))
        await entity.async_unlock()

        for _ in range(lock_module.MAX_FOLLOW_UP_READS + 2):
            await entity._async_verify_after_auto_lock(None)

        # One verification from the unlock, then at most MAX_FOLLOW_UP_READS follow-ups.
        self.assertEqual(len(entity.hass.scheduled), 1 + lock_module.MAX_FOLLOW_UP_READS)
        self.assertFalse(entity._attr_is_locked)

    async def test_reported_unlock_without_auto_lock_is_not_read_again(self):
        entity = self.make_lock(LockReading(unlocked=True, reported=True, auto_lock=False))
        await entity.async_unlock()
        await entity._async_verify_after_auto_lock(None)
        self.assertEqual(len(entity.hass.scheduled), 1)
        self.assertFalse(entity._attr_is_locked)

    async def test_added_unavailable_entity_retries_initial_read(self):
        entity = self.make_lock(None)
        await entity.async_update()
        await entity.async_added_to_hass()
        self.assertTrue(entity.base_added_called)
        self.assertEqual(len(entity.hass.scheduled), 1)
        self.assertEqual(entity.hass.scheduled[0]["delay"], lock_module.STATE_RETRY_DELAY)

    async def test_added_available_entity_does_not_schedule_read(self):
        entity = self.make_lock(False)
        await entity.async_update()
        await entity.async_added_to_hass()
        self.assertEqual(entity.hass.scheduled, [])


class StatusApi(tuya_api_module.TuyaCloudApi):
    def __init__(self, response):
        tuya_api_module.TuyaCloudApi.__init__(self, "id", "secret", "eu")
        self.response = response
        self.paths = []

    async def _request(self, method, path, body=None):
        self.paths.append((method, path))
        return self.response


class ScriptedApi(tuya_api_module.TuyaCloudApi):
    """Answers each request from a per-path queue of canned responses."""

    def __init__(self, responses):
        tuya_api_module.TuyaCloudApi.__init__(self, "id", "secret", "eu")
        self.responses = {path: list(answers) for path, answers in responses.items()}
        self.calls = []

    async def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        # Ignore the query string so discovery tests survive pagination parameters.
        answers = self.responses.get(path.split("?", 1)[0])
        if not answers:
            raise AssertionError(f"unexpected request {method} {path}")
        return answers.pop(0)


TICKET = "/v1.0/devices/device/door-lock/password-ticket"
OPERATE = "/v1.0/smart-lock/devices/device/password-free/door-operate"
OPEN_DOOR = "/v1.0/devices/device/door-lock/password-free/open-door"
STATUS = "/v1.0/iot-03/devices/device/status"
SHADOW = "/v2.0/cloud/thing/device/shadow/properties"


def _ticket(ticket_id="T1"):
    return {"success": True, "result": {"ticket_id": ticket_id}, "t": 1_000_000}


def _shadow(**datapoints):
    """Datapoints as (value, report time in ms)."""
    return {
        "success": True,
        "result": {
            "properties": [
                {"code": code, "value": value, "time": report_ms}
                for code, (value, report_ms) in datapoints.items()
            ]
        },
    }


def _status(**datapoints):
    return {
        "success": True,
        "result": [{"code": code, "value": value} for code, value in datapoints.items()],
    }


class TuyaCloudApiLockStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_lock_enabled_reports_locked_despite_frozen_motor_state(self):
        api = StatusApi(_status(automatic_lock=True, lock_motor_state=True))
        self.assertFalse(await api.async_get_lock_state("device"))
        self.assertEqual(api.paths, [("GET", "/v1.0/iot-03/devices/device/status")])

    async def test_auto_lock_disabled_uses_motor_state(self):
        api = StatusApi(_status(automatic_lock=False, lock_motor_state=True))
        self.assertTrue(await api.async_get_lock_state("device"))

        api = StatusApi(_status(automatic_lock=False, lock_motor_state=False))
        self.assertFalse(await api.async_get_lock_state("device"))

    async def test_missing_auto_lock_uses_motor_state(self):
        api = StatusApi(_status(lock_motor_state=True))
        self.assertTrue(await api.async_get_lock_state("device"))

    async def test_no_state_datapoints_is_unknown(self):
        api = StatusApi(_status(battery_state="high"))
        self.assertIsNone(await api.async_get_lock_state("device"))

    async def test_failed_status_is_unknown(self):
        api = StatusApi({"success": False, "msg": "permission deny"})
        self.assertIsNone(await api.async_get_lock_state("device"))

    async def test_unlock_then_verification_returns_to_locked_with_auto_lock(self):
        api = StatusApi(_status(automatic_lock=True, lock_motor_state=True))
        api.async_unlock = FakeApi(False).async_unlock
        entity = lock_module.TuyaSmartLock(api, "device", "Portillon", 5)
        entity.hass = FakeHass()

        await entity.async_unlock()
        self.assertFalse(entity._attr_is_locked)
        self.assertEqual(entity.hass.scheduled[0]["delay"], 6)

        await entity._async_verify_after_auto_lock(None)
        self.assertTrue(entity._attr_is_locked)


class TuyaCloudApiCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlock_uses_door_operate_and_records_server_time(self):
        api = ScriptedApi({TICKET: [_ticket()], OPERATE: [{"success": True, "t": 1_700_000_000_000}]})
        self.assertTrue(await api.async_unlock("device"))
        self.assertEqual([c[1] for c in api.calls], [TICKET, OPERATE])
        self.assertEqual(api.calls[1][2], {"ticket_id": "T1", "open": True})
        self.assertEqual(api._last_command_at["device"], 1_700_000_000)

    async def test_unlock_falls_back_to_password_free_open_door(self):
        api = ScriptedApi({
            TICKET: [_ticket("T1"), _ticket("T2")],
            OPERATE: [{"success": False, "msg": "not support this device"}],
            OPEN_DOOR: [{"success": True, "result": True, "t": 1_700_000_000_000}],
        })
        self.assertTrue(await api.async_unlock("device"))
        self.assertEqual([c[1] for c in api.calls], [TICKET, OPERATE, TICKET, OPEN_DOOR])
        self.assertEqual(api.calls[3][2], {"ticket_id": "T2"})
        self.assertIn("device", api._last_command_at)

    async def test_unlock_fails_when_both_endpoints_refuse(self):
        api = ScriptedApi({
            TICKET: [_ticket("T1"), _ticket("T2")],
            OPERATE: [{"success": False, "msg": "not support this device"}],
            OPEN_DOOR: [{"success": False, "msg": "not support this device"}],
        })
        self.assertFalse(await api.async_unlock("device"))
        self.assertNotIn("device", api._last_command_at)

    async def test_unlock_fails_without_ticket(self):
        api = ScriptedApi({TICKET: [{"success": False, "msg": "permission deny"}]})
        self.assertFalse(await api.async_unlock("device"))
        self.assertEqual(len(api.calls), 1)

    async def test_lock_does_not_use_unlock_fallback(self):
        api = ScriptedApi({TICKET: [_ticket()], OPERATE: [{"success": False, "msg": "not support"}]})
        self.assertFalse(await api.async_lock("device"))
        self.assertEqual([c[1] for c in api.calls], [TICKET, OPERATE])
        self.assertEqual(api.calls[1][2], {"ticket_id": "T1", "open": False})


class TuyaCloudApiShadowStateTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self, shadow_responses, status_responses=()):
        api = ScriptedApi({SHADOW: list(shadow_responses), STATUS: list(status_responses)})
        api._last_command_at["device"] = 1_000.0
        return api

    async def test_motor_state_reported_after_command_is_trusted(self):
        api = self.make_api([_shadow(automatic_lock=(True, 0), lock_motor_state=(True, 1_002_000))])
        reading = await api.async_read_lock_state("device")
        self.assertEqual(reading, LockReading(unlocked=True, reported=True, auto_lock=True))

    async def test_frozen_motor_state_falls_back_to_auto_lock(self):
        # Real-world case: lock_motor_state stuck at "unlocked" for months.
        api = self.make_api([_shadow(automatic_lock=(True, 0), lock_motor_state=(True, 5_000))])
        reading = await api.async_read_lock_state("device")
        self.assertEqual(reading, LockReading(unlocked=False, reported=False, auto_lock=True))

    async def test_stale_motor_state_without_auto_lock_keeps_previous_rule(self):
        api = self.make_api([_shadow(automatic_lock=(False, 0), lock_motor_state=(True, 5_000))])
        reading = await api.async_read_lock_state("device")
        self.assertEqual(reading, LockReading(unlocked=True))

    async def test_unauthorized_shadow_endpoint_falls_back_and_is_not_retried(self):
        api = self.make_api(
            [{"success": False, "code": 1106, "msg": "permission deny"}],
            [_status(automatic_lock=True, lock_motor_state=True), _status(automatic_lock=True)],
        )
        self.assertFalse((await api.async_read_lock_state("device")).unlocked)
        self.assertTrue(api._shadow_unsupported)
        await api.async_read_lock_state("device")
        self.assertEqual([c[1] for c in api.calls], [SHADOW, STATUS, STATUS])

    async def test_transient_shadow_failure_falls_back_for_this_read_only(self):
        api = self.make_api(
            [{"success": False, "code": 500, "msg": "system error"}],
            [_status(automatic_lock=False, lock_motor_state=False)],
        )
        self.assertFalse((await api.async_read_lock_state("device")).unlocked)
        self.assertFalse(api._shadow_unsupported)

    async def test_without_command_status_endpoint_is_used(self):
        api = ScriptedApi({STATUS: [_status(automatic_lock=True, lock_motor_state=True)]})
        reading = await api.async_read_lock_state("device")
        self.assertEqual(reading, LockReading(unlocked=False, auto_lock=True))
        self.assertEqual([c[1] for c in api.calls], [STATUS])


class TuyaCloudApiDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_logs_categories_seen(self):
        api = ScriptedApi({
            "/v1.0/iot-01/associated-users/devices": [{
                "success": True,
                "result": {"devices": [
                    {"id": "a", "name": "Plug", "category": "cz"},
                    {"id": "b", "name": "Gate", "category": "jtmspro"},
                ]},
            }],
        })
        api._token = "token"
        api._token_expiry = 10**12
        with self.assertLogs(tuya_api_module._LOGGER, level="DEBUG") as logs:
            devices = await api.async_discover_devices()
        self.assertEqual([d["id"] for d in devices], ["b"])
        self.assertIn("2 device(s)", logs.output[-1])
        self.assertIn("cz, jtmspro", logs.output[-1])


class RegionTests(unittest.TestCase):
    def test_every_config_flow_region_has_an_endpoint(self):
        const = sys.modules["custom_components.tuya_smart_lock.const"]
        source = (Path(__file__).parents[1] / "custom_components" / "tuya_smart_lock" / "config_flow.py").read_text()
        for region in const.API_REGIONS:
            self.assertIn(f'"{region}":', source)
        self.assertEqual(const.API_REGIONS["sg"], "openapi-sg.iotbing.com")


if __name__ == "__main__":
    unittest.main()
