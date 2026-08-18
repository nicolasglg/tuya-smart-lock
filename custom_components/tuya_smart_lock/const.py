"""Constants for Tuya Smart Lock."""

DOMAIN = "tuya_smart_lock"

CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_API_REGION = "api_region"

API_REGIONS = {
    "eu": "openapi.tuyaeu.com",
    "us": "openapi.tuyaus.com",
    "cn": "openapi.tuyacn.com",
    "in": "openapi.tuyain.com",
}

# Tuya device categories that are locks / access control
LOCK_CATEGORIES = {
    "mk",        # Access control
    "ms",        # Smart lock
    "jtmsbh",    # Smart lock (legacy)
    "jtmspro",   # Smart lock pro
    "gyms",      # Gym locker
    "hotelms",   # Hotel lock
    "videolock", # Video lock
    "photolock", # Photo lock
}

TICKET_ENDPOINT = "/v1.0/devices/{device_id}/door-lock/password-ticket"
DOOR_OPERATE_ENDPOINT = "/v1.0/smart-lock/devices/{device_id}/password-free/door-operate"
STATUS_ENDPOINT = "/v1.0/iot-03/devices/{device_id}/status"
COMMANDS_ENDPOINT = "/v1.0/iot-03/devices/{device_id}/commands"
DEVICES_ENDPOINT = "/v1.0/users/{uid}/devices"
REMOTE_UNLOCKS_ENDPOINT = "/v1.0/devices/{device_id}/door-lock/remote-unlocks"

# --- Pulsar / Message Service push --------------------------------------

# Pulsar's WebSocket gateway (port 8285), not the native binary protocol on
# 7285 -- see pulsar.py for why.
PULSAR_WS_ENDPOINTS = {
    "eu": "wss://mqe.tuyaeu.com:8285/",
    "us": "wss://mqe.tuyaus.com:8285/",
    "cn": "wss://mqe.tuyacn.com:8285/",
    "in": "wss://mqe.tuyain.com:8285/",
}
PULSAR_TOPIC_ENV = "event"  # "event-test" is the sandbox topic
PULSAR_WS_QUERY = "?ackTimeoutMillis=30000&subscriptionType=Failover"

# Dispatcher signal carrying a decoded Tuya push message.
SIGNAL_PULSAR_MESSAGE = f"{DOMAIN}_pulsar_message"

# Dispatcher signal fired when the push connection goes up or down, so the
# entity can re-publish its diagnostic attributes.
SIGNAL_PULSAR_STATUS = f"{DOMAIN}_pulsar_status"

# The lock reports *how* it was opened as separate datapoints; any of them
# firing means the door just unlocked, regardless of lock_motor_state.
UNLOCK_EVENT_PREFIX = "unlock_"

# Reconciliation poll. Push is the real mechanism; this only catches a missed
# message. Tuya's free tier allows 1,000 requests/day across the whole cloud
# project, so keep this interval generous -- 30 min is ~48 requests/day.
RECONCILE_INTERVAL_MINUTES = 30

# Datapoint written (to its own current value) purely to force the gateway to
# open a BLE link to the lock. Must be writable and harmless -- see button.py.
WARM_LINK_DP = "beep_volume"
