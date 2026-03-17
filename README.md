# Enhanced Govee Lights for Home Assistant

![Home Assistant](https://img.shields.io/badge/home%20assistant-%2341BDF5.svg?style=for-the-badge&logo=home-assistant&logoColor=white)
[![hacs](https://img.shields.io/badge/HACS-Integration-blue.svg?style=for-the-badge)](https://github.com/hacs/integration)
![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
<img src="assets/govee-logo.png" alt="Govee Logo" width="125">

Controls Govee lighting from Home Assistant over local LAN or direct BLE. No cloud, no bridge.

---

## Features

- Direct BLE control, no bridge or middleware needed
- Local Wi-Fi (LAN) control for supported devices, no cloud
- Full scene list sourced from the Govee API and cached locally
- Brightness, color, and power state

---

## Installation

1. [Install HACS](https://hacs.xyz/docs/use/).
2. Find **Enhanced Govee Lights** in the HACS integrations list and install it.
3. Restart Home Assistant.

## Configuration

For BLE: make sure Home Assistant has Bluetooth access on your host machine.

For LAN: enable the LAN API in the Govee Home app under Settings → Devices → your device → LAN Control.

## Usage

After setup the device shows up as a light entity in HA. For BLE devices, make sure you pick the right model during setup — the model determines which scene effects and brightness encoding are used.

---

## Scene Effects

Scene effects are loaded from a JSON file stored in your Home Assistant config directory:

```
/config/.storage/govee_lights/<MODEL>.json
```

If no file exists for your model, the integration downloads it from the Govee API on first startup and saves it there.

### Adding scene data manually

The integration accepts two formats:

- Flat list: the compact format produced by govee2mqtt. Copy `<MODEL>.json` from govee2mqtt's `jsons/` folder directly into `/config/.storage/govee_lights/`.
- Raw API response: the full JSON from the Govee API, fetched via `curl`:

```bash
curl -s 'https://app2.govee.com/appsku/v1/light-effect-libraries?sku=H617C' \
  -H 'AppVersion: 5.6.01' \
  -H 'User-Agent: GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4' \
  -o 'config/.storage/govee_lights/H617C.json'
```

Replace `H617C` with your model SKU.

---

## Troubleshooting

1. BLE range: the device must be within Bluetooth range of your HA host or a configured [Bluetooth proxy](https://www.home-assistant.io/integrations/bluetooth/#remote-adapters-bluetooth-proxies).
2. Model selection: confirm the correct model was chosen during setup.
3. Logs: check Settings > System > Logs for errors from the integration.

---

## Removal

Via the HA UI: Settings > Devices & Services, find the entry, open the three-dot menu, select Delete.

Via HACS: open HACS > Integrations, find Enhanced Govee Lights, three-dot menu > Remove, then restart HA.

---

## Support

Report bugs or missing device support in the [issue tracker](https://github.com/Jaano/govee_lights/issues). PRs welcome.

---

## Credits

- [@cralex96](https://github.com/cralex96) and [@Rombond](https://github.com/Rombond) — original BLE patches this integration grew from
- [@Laserology](https://github.com/Laserology/govee_ble_lights) — original BLE implementation and model reverse-engineering, which this integration's BLE code is based on
- [@teh-hippo](https://github.com/teh-hippo/govee_ble_lights) — independent BLE implementation, particularly useful for H6199 and state-reading
- [@wez](https://github.com/wez/govee2mqtt) — govee2mqtt's LAN protocol work and scene handling saved a lot of guesswork here

---

## License

MIT License. See the [LICENSE file](https://github.com/Jaano/govee_lights/blob/main/LICENSE) for details.
