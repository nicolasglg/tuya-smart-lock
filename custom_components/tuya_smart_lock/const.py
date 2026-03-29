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
DEVICES_ENDPOINT = "/v1.0/users/{uid}/devices"
REMOTE_UNLOCKS_ENDPOINT = "/v1.0/devices/{device_id}/door-lock/remote-unlocks"
