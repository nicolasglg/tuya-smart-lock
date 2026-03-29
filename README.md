# Tuya Smart Lock

[![HACS](https://img.shields.io/badge/HACS-Custom-orange?style=flat-square)](https://hacs.xyz/)
[![GitHub Release](https://img.shields.io/github/v/release/nicolasglg/tuya-smart-lock?style=flat-square)](https://github.com/nicolasglg/tuya-smart-lock/releases)
[![License](https://img.shields.io/github/license/nicolasglg/tuya-smart-lock?style=flat-square)](LICENSE)
[![Buy Me A Beer](https://img.shields.io/badge/Buy%20Me%20A%20Beer-support-yellow?style=flat-square&logo=buy-me-a-coffee)](https://buymeacoffee.com/nicolasglg)

**Control your Tuya smart lock directly from Home Assistant.**

The official Tuya integration doesn't support lock/unlock — it only exposes a binary sensor (open/closed state). This integration fills the gap by using the Tuya Smart Lock Cloud API to add a proper `lock` entity with lock/unlock control.

## Why this integration?

The official Home Assistant Tuya integration uses the `tuya-device-sharing-sdk` which does not implement the Smart Lock API. Lock devices (categories `mk`, `ms`, `jtmsbh`, etc.) only get a `binary_sensor` with no control capability.

Tuya Smart Lock uses the Cloud API ticket-based flow to send lock/unlock commands — the same mechanism the Tuya and Smart Life mobile apps use.

## What you get

| Entity | Type | What it does |
|--------|------|--------------|
| Lock | `lock` | Lock and unlock your door via Tuya Cloud API |

The lock entity is linked to your existing Tuya device in Home Assistant. It appears alongside the `binary_sensor` from the official Tuya integration, all grouped under the same device.

## Prerequisites

Before installing, you need to set up a few things on the Tuya IoT Platform. This takes about 10 minutes.

### 1. Create a Tuya IoT project

1. Go to [iot.tuya.com](https://iot.tuya.com) and create an account (or log in)
2. Go to **Cloud** > **Development** > **Create Cloud Project**
3. Give it a name (e.g. "Home Assistant")
4. Select the **Data Center** that matches your region (Western Europe, US East, etc.)
5. For **Development Method**, select **Smart Home**
6. Click **Create**

### 2. Link your Tuya / Smart Life app account

1. In your project, go to **Devices** > **Link Tuya App Account**
2. Click **Add App Account**
3. Open the Tuya or Smart Life app on your phone
4. Go to **Me** > tap the scan icon in the top right
5. Scan the QR code displayed on iot.tuya.com
6. Confirm the linking in the app
7. Your devices should now appear in the **All Devices** tab

### 3. Subscribe to required API services

1. In your project, go to **Service API**
2. Click **Go to Authorize** (or find the service list)
3. Search for and subscribe to **IoT Core** (Free Trial)
4. Search for and subscribe to **Smart Lock Open Service** (Free Trial)

Both services are free for personal use. They may require periodic renewal (every ~6 months) — you'll get an email when it's time.

### 4. Enable remote unlock on your device

1. Open the **Tuya** or **Smart Life** app on your phone
2. Go to your lock device
3. Open the device **Settings**
4. Enable **Remote Unlock** (sometimes called "Remote Unlock Without Password")

### 5. Note your credentials

1. Go back to [iot.tuya.com](https://iot.tuya.com) > **Cloud** > your project > **Overview**
2. Copy your **Access ID** and **Access Secret** — you'll need them during setup

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Click the 3 dots menu > **Custom repositories**
3. Add `nicolasglg/tuya-smart-lock` as **Integration**
4. Search for and install **Tuya Smart Lock**
5. Restart Home Assistant
6. Go to **Settings** > **Integrations** > **Add Integration** > search for **Tuya Smart Lock**
7. Want to make my day? Buy me a beer :) [![Buy Me A Beer](https://img.shields.io/badge/Buy%20Me%20A%20Beer-support-yellow?style=flat&logo=buy-me-a-coffee)](https://buymeacoffee.com/nicolasglg)

### Manual

1. Copy the `custom_components/tuya_smart_lock` folder to your Home Assistant `custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings** > **Integrations** > **Add Integration** > search for **Tuya Smart Lock**

## Configuration

The integration uses a UI-based config flow — no YAML needed.

### Step 1: Enter your Tuya credentials

- **Access ID**: from your Tuya IoT project overview
- **Access Secret**: from your Tuya IoT project overview
- **API Region**: select the region matching your Tuya data center

### Step 2: Select your device

The integration will automatically discover lock devices linked to your Tuya account. Select the device you want to control.

If you have multiple locks, add the integration once per device.

## Supported devices

**Tested:**
- Tuya Access Control (category `mk`, model WIFI_A)

**Should work with any Tuya lock/access control device that supports the Smart Lock Cloud API, including categories:**
- `mk` — Access control
- `ms` — Smart lock
- `jtmsbh` — Smart lock (legacy)
- `jtmspro` — Smart lock pro
- `gyms` — Gym locker
- `hotelms` — Hotel lock
- `videolock` — Video lock
- `photolock` — Photo lock

If your lock device uses the Tuya ticket-based unlock flow, it should work. If it doesn't, please [open an issue](https://github.com/nicolasglg/tuya-smart-lock/issues) with your device model and category.

## Limitations

- **Cloud-only**: Tuya locks do not support local control. Commands go through the Tuya Cloud API. If your internet is down, you can still use the physical keypad/badge/fingerprint on the device itself.
- **API trial renewal**: IoT Core and Smart Lock Open Service are free but require renewal approximately every 6 months on iot.tuya.com.
- **Optimistic state**: State updates are optimistic with post-command verification (polls the cloud 5 seconds after a command). There is no real-time push from the device.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `uri path invalid` on lock/unlock | Your IoT Core or Smart Lock Open Service subscription has expired. Renew it on [iot.tuya.com](https://iot.tuya.com). |
| `permission deny` | Your Smart Life / Tuya app account is not linked to the IoT project. See Prerequisites step 2. |
| No devices found during setup | Make sure your app account is linked and your device is a supported lock category. |
| Unlock command succeeds but door doesn't open | Enable Remote Unlock in the Tuya / Smart Life app settings for your device. |
| `invalid_auth` during setup | Double-check your Access ID and Access Secret. Make sure you're using the credentials from the correct project. |

## Support

If this integration is useful to you, consider buying me a coffee!

[![Buy Me A Beer](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/nicolasglg)

## License

MIT License - see [LICENSE](LICENSE) for details.
