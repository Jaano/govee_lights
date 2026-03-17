# Govee LAN Communication

All UDP traffic between Home Assistant and a Govee LAN device, as implemented in `govee_lan.py`.

Reference: [Govee WLAN Guide](https://app-h5.govee.com/user-manual/wlan-guide)

---

## Network transport

All communication uses UDP. Device and HA must be on the same local network segment.

| Port | Direction | Purpose |
|------|-----------|------|
| 4001 | HA → device | Discovery scan requests (multicast or unicast) |
| 4002 | device → HA | Discovery responses (HA listens here) |
| 4003 | HA → device | Control commands and state queries |

Discovery uses multicast group `239.255.255.250` (SSDP address).  
A device enabled for LAN control joins this group and responds to scan broadcasts on port 4001.  
Commands are sent directly as unicast UDP packets to the device's IP on port 4003.

All packets are JSON, wrapped in a `{"msg": {...}}` envelope.

---

## Packet format

### Request (HA → device)

```json
{
  "msg": {
    "cmd": "<command>",
    "data": { ... }
  }
}
```

### Response / notification (device → HA)

Responses arrive on port 4002. The device can also push updates without being queried.

---

## Message types

### `scan` — Device discovery

Sent as a multicast UDP broadcast to `239.255.255.250:4001` or as unicast to a known IP.

```json
{
  "msg": {
    "cmd": "scan",
    "data": { "account_topic": "reserve" }
  }
}
```

The device responds on port 4002 with its identity:

```json
{
  "msg": {
    "cmd": "scan",
    "data": {
      "ip": "192.168.0.50",
      "device": "AA:BB:CC:DD:EE:FF",
      "sku": "H619A",
      "bleVersionHard": "3.01.01",
      "bleVersionSoft": "1.04.26",
      "wifiVersionHard": "1.00.10",
      "wifiVersionSoft": "1.02.12"
    }
  }
}
```

### `devStatus` — State query

Triggers the device to report its current state.

```json
{
  "msg": {
    "cmd": "devStatus",
    "data": {}
  }
}
```

The device responds on port 4002:

```json
{
  "msg": {
    "cmd": "devStatus",
    "data": {
      "onOff": 1,
      "brightness": 75,
      "color": { "r": 255, "g": 128, "b": 0 },
      "colorTemInKelvin": 0
    }
  }
}
```

When `colorTemInKelvin > 0` the device is in white-balance (CT) mode; `color` is ignored.  
When `colorTemInKelvin == 0` the device is in RGB mode; `color` carries the active color.

### `turn` — Power on/off

```json
{ "msg": { "cmd": "turn", "data": { "value": 1 } } }   ← on
{ "msg": { "cmd": "turn", "data": { "value": 0 } } }   ← off
```

### `brightness` — Set brightness

Value is a percentage **0–100** (unlike BLE which uses 0–255 raw or also 0–100 percent depending on model).

```json
{ "msg": { "cmd": "brightness", "data": { "value": 75 } } }
```

HA converts from HA scale (0–255) before sending: `value = round(brightness * 100 / 255)`.

### `colorwc` — Set color or color temperature

RGB color:

```json
{
  "msg": {
    "cmd": "colorwc",
    "data": {
      "color": { "r": 255, "g": 128, "b": 0 },
      "colorTemInKelvin": 0
    }
  }
}
```

Color temperature (kelvin):

```json
{
  "msg": {
    "cmd": "colorwc",
    "data": {
      "color": { "r": 0, "g": 0, "b": 0 },
      "colorTemInKelvin": 4000
    }
  }
}
```

When `colorTemInKelvin > 0` the device uses native white-balance mode — no client-side kelvin→RGB conversion is needed (unlike the BLE path). The `color` field is set to `0,0,0` as a placeholder.

### `ptReal` — Scene / effect

Scene payloads are sent as a list of base64-encoded command strings. Sourced from the same scene JSON library used by the BLE path.

```json
{
  "msg": {
    "cmd": "ptReal",
    "data": {
      "command": ["<base64_frame_1>", "<base64_frame_2>", "..."]
    }
  }
}
```

After sending the ptReal packet, a separate `turn` on command is sent to ensure the device powers on.

---

## 1. Setup and discovery

### 1.1 Unicast discovery (config entry setup)

When HA loads a configured entry, `GoveeLANCoordinator.async_create()` targets the stored IP directly:

```
GoveeController(discovery_enabled=False, update_enabled=True, update_interval=10)
controller.add_device_to_discovery_queue(ip)
await controller.start()

HA  →  device:4001  UDP  scan { "account_topic": "reserve" }
device  →  HA:4002  UDP  scan { "ip", "device", "sku", … }

# Times out after 5 s → raises ConfigEntryNotReady (HA will retry)
```

### 1.2 Broadcast discovery (config flow)

`GoveeLANCoordinator.discover_devices()` broadcasts to the multicast group to find all LAN-enabled devices on the network:

```
GoveeController(discovery_enabled=True, discovery_interval=60, update_enabled=False)
await controller.start()
await asyncio.sleep(5.0)   ← scan window

HA  →  239.255.255.250:4001  UDP  scan { "account_topic": "reserve" }
# All LAN-enabled devices respond on port 4002

controller.cleanup()
return found devices
```

### 1.3 Connectivity test (config flow)

`GoveeLANCoordinator.test_connectivity(ip)` is used to validate a manually entered IP before creating the config entry:

```
GoveeController(discovery_enabled=False, update_enabled=False)
controller.add_device_to_discovery_queue(ip)
await controller.start()
await asyncio.wait_for(device_found.wait(), timeout=5.0)
→ True / False
```

---

## 2. Periodic state updates

`GoveeController` is created with `update_enabled=True, update_interval=10`.  
The library automatically sends a `devStatus` query to the device every **10 seconds**:

```
(every 10 s)
HA  →  device:4003  UDP  devStatus {}
device  →  HA:4002  UDP  devStatus { onOff, brightness, color, colorTemInKelvin }
→  _on_device_update() fires
→  coordinator state updated
→  async_set_updated_data() → HA entity refreshed
```

The LAN coordinator learns device state only from `devStatus` responses — there is no initial state query after setup. The first periodic update arrives within 10 s, though `async_setup()` calls `controller.send_update_message()` once to force an immediate first update.

---

## 3. Commands

All commands are sent as unicast UDP to `<device_ip>:4003` via the `govee-local-api` library (delegated to `GoveeDevice` methods or directly via the controller transport).

### 3.1 Power on

```
HA  →  device:4003:  turn { value: 1 }
```

### 3.2 Power off

```
HA  →  device:4003:  turn { value: 0 }
```

### 3.3 Brightness

HA brightness (0–255) → percent (0–100):

```
HA  →  device:4003:  brightness { value: <percent> }
```

### 3.4 RGB color

```
HA  →  device:4003:  colorwc { color: {r,g,b}, colorTemInKelvin: 0 }
```

### 3.5 Color temperature

Kelvin value sent directly — no RGB conversion required:

```
HA  →  device:4003:  colorwc { color: {r:0,g:0,b:0}, colorTemInKelvin: <kelvin> }
```

> Unlike BLE (which converts kelvin → RGB client-side), LAN sends the raw kelvin value and the device handles white-balance conversion internally.

### 3.6 Scene / ptReal effect

```
HA  →  device:4003:  ptReal { command: ["<b64>", …] }
HA  →  device:4003:  turn   { value: 1 }
```

The ptReal packet is sent via a direct `transport.sendto()` call using `PtRealMessage` (bypasses the `GoveeDevice` API). The follow-up `turn on` goes via `device.turn_on()`.

---

## 4. State updates (inbound)

`_on_device_update(device: GoveeDevice)` is registered as the device update callback and called by `govee-local-api` every time a `devStatus` response arrives:

| `GoveeDevice` field      | Coordinator field      | Notes                                     |
|--------------------------|------------------------|-------------------------------------------|
| `device.on`              | `is_on`                | `True` / `False`                          |
| `device.brightness`      | `brightness_raw`       | 0–100 (percent); converted to 0–255 for HA |
| `device.rgb_color`       | `rgb_color`            | Set when `temperature_color == 0`         |
| `device.temperature_color` | `color_temp_kelvin`  | Set when `> 0`; clears `rgb_color`        |

After updating fields, `async_set_updated_data()` pushes to the HA entity via the coordinator listener.

---

## 5. Error handling

The LAN coordinator has no retry or reconnect logic of its own — the `govee-local-api` library handles socket errors internally. If a device stops responding:

- Periodic `devStatus` queries continue silently.
- The coordinator stays available (no `_available = False` transition) — it assumes the last known state is still valid.
- When the device comes back, the next `devStatus` response triggers `_on_device_update()` normally.

> There is no "unavailable" state for LAN devices analogous to the BLE advertisement watcher. The LAN API relies on the device always being reachable at the configured IP.

---

## 6. HA startup

```
async_setup_entry()                        (__init__.py)
  └─ GoveeLANCoordinator.async_create()
       └─ GoveeController.start()          (binds UDP socket, starts listener)
       └─ unicast scan → device responds
       └─ device.set_update_callback()
  └─ async_forward_entry_setups → light.py

async_added_to_hass()                      (light.py)
  └─ coordinator.async_load_effects()      (loads scene JSON in executor)
  └─ async_get_last_state()               (RestoreEntity for optimistic state)
  └─ coordinator.async_add_listener()     (subscribe to coordinator updates)
  └─ await coordinator.async_setup()      (setup_in_background = False)
       └─ controller.send_update_message() (immediate devStatus query)
```

The LAN setup runs synchronously in `async_added_to_hass` (not in a background task), so the initial state update completes before the entity becomes available to HA.

---

## 7. Comparison: BLE vs. LAN

| Aspect | BLE (`GoveeBLECoordinator`) | LAN (`GoveeLANCoordinator`) |
|---|---|---|
| Transport | GATT over Bluetooth LE | UDP over Wi-Fi |
| Command format | 20-byte binary frames | JSON over UDP |
| State query | Binary `0xAA` query packets; responses via GATT notify | `devStatus` JSON; response on port 4002 |
| Push notifications | Yes — GATT notify subscription | No — poll only (10 s interval) |
| Post-command state verify | Yes — 0.5 s delayed re-query after every command | No — next periodic update (≤10 s) |
| Color temperature | Kelvin → RGB conversion client-side | Raw kelvin sent; device converts |
| Brightness scale | Raw 0–255, or 0–100 % for PERCENT_MODELS | Always 0–100 % |
| Reconnect | Advertisement watcher → `async_setup()` | No reconnect; polling resumes when device returns |
| Setup background task | Yes (`setup_in_background = True`) | No (synchronous) |
| Music modes | Yes | No |
| Scene effects | Yes (ptReal via BLE frames) | Yes (ptReal via UDP JSON) |

---

## 8. HA shutdown

```
async_unload_entry()
  └─ coordinator.cleanup()
       └─ device.set_update_callback(None)
       └─ controller.cleanup()            (closes UDP socket, stops listener)
  └─ async_unload_platforms()
```
