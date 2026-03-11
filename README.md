# Enhanced Govee Lights for Home Assistant
![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

Controls Govee lighting from Home Assistant over local LAN or direct BLE. No cloud, no bridge.

Includes patches from [cralex96](https://github.com/cralex96/govee_ble_lights) and [Rombond](https://github.com/Rombond/h617a_govee_ble_lights).

Segmented lighting isn't supported.

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Scene Effects](#scene-effects)
- [Removal](#removal)
- [Support & Contribution](#support--contribution)
- [License](#license)

---

## Features

- Direct BLE control, no bridge or middleware needed
- Local Wi-Fi (LAN) control for supported devices, no cloud
- Full scene list loaded from your device
- Brightness, color, and power state

---

## Installation

1. [Install HACS](https://hacs.xyz/docs/use/).
2. Find **Enhanced Govee Lights** in the HACS integrations list and install it.
3. Restart Home Assistant.

## Configuration

**BLE:** Ensure Home Assistant has Bluetooth access on your host machine.

**LAN:** Enable the LAN API in the Govee Home app (Settings → Devices → your device → LAN Control).

## Usage

After setup, Govee devices appear as light entities in Home Assistant. Select the correct device model when adding a BLE device.

---

## Scene Effects

Scene effects are loaded from a JSON file stored in your Home Assistant config directory:

```
/config/.storage/govee_lights/<MODEL>.json
```

If no file exists for your model, the integration downloads it from the Govee API on first startup and saves it there.

### Adding scene data manually

If you prefer to supply the file yourself, the integration accepts two formats:

- **Flat list** — the compact format produced by govee2mqtt. Copy `<MODEL>.json` from govee2mqtt's `jsons/` folder directly into `/config/.storage/govee_lights/`.
- **Raw API response** — the full JSON returned by the Govee API, downloaded via `curl`:

```bash
curl -s 'https://app2.govee.com/appsku/v1/light-effect-libraries?sku=H617C' \
  -H 'AppVersion: 5.6.01' \
  -H 'User-Agent: GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4' \
  -o '/config/.storage/govee_lights/H617C.json'
```

Replace `H617C` with your model SKU.

---

## Troubleshooting

1. **BLE range**: the device must be within Bluetooth range of your HA host or a configured [Bluetooth proxy](https://www.home-assistant.io/integrations/bluetooth/#remote-adapters-bluetooth-proxies).
2. **Model selection**: confirm the correct model was chosen during setup.
3. **Logs**: check **Settings > System > Logs** for errors from the integration.

---

## Removal

**Via Home Assistant UI:**

1. Go to **Settings > Devices & Services**.
2. Find the integration entry and open it.
3. Click the three-dot menu and select **Delete**.

**Via HACS (removes component files):**

1. Open HACS and go to **Integrations**.
2. Find **Enhanced Govee Lights**, open the three-dot menu, and select **Remove**.
3. Restart Home Assistant.

---

## Support & Contribution

- **Issues**: Report bugs or missing device support in the [issue tracker](https://github.com/Jaano/govee_lights/issues).
- **Contributions**: Fork the repository and submit a pull request.

---

## License

MIT License. See the [LICENSE file](https://github.com/Jaano/govee_lights/blob/main/LICENSE) for details.

