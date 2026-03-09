# Enhanced Govee Lights for Home Assistant
![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

Control Govee lighting devices via local LAN or direct BLE from Home Assistant.
Includes patches from [cralex96](https://github.com/cralex96/govee_ble_lights) and [Rombond](https://github.com/Rombond/h617a_govee_ble_lights).

Segmented lighting is not supported.

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

- **Direct BLE Control**: Control Govee devices directly over Bluetooth without any bridge or middleware.
- **LAN Control**: Local Wi-Fi control for supported devices — no cloud required.
- **Scene Selection**: Choose from all available device scenes.
- **Lighting Control**: Adjust brightness, color, and power state.

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

BLE scene effects are loaded from a JSON file at:

```
custom_components/govee_lights/jsons/<MODEL>.json
```

If no file exists for your model, the integration downloads scene data from the Govee API at startup and logs the equivalent `curl` command so you can save the file manually.

### Downloading scene data manually

The integration accepts two file formats:

- **Flat list** — the compact format produced by govee2mqtt and similar tools. Copy from your govee2mqtt `jsons/` folder if you have it.
- **Raw API response** — the full JSON returned by the Govee API. Larger file, but parsed automatically.

To download the raw API response, replace `H617C` with your model SKU:

```bash
curl -s 'https://app2.govee.com/appsku/v1/light-effect-libraries?sku=H617C' \
  -H 'AppVersion: 5.6.01' \
  -H 'User-Agent: GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4' \
  -o 'custom_components/govee_lights/jsons/H617C.json'
```

Saving the file avoids a runtime download on every restart.

---

## Troubleshooting

1. **BLE range**: Make sure the Govee device is within Bluetooth range of the Home Assistant host or a configured [Bluetooth proxy](https://www.home-assistant.io/integrations/bluetooth/#remote-adapters-bluetooth-proxies).
2. **Model selection**: Confirm the correct model was selected during setup.
3. **Logs**: Check **Settings > System > Logs** for errors from the Govee integration.

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

