# Govee BLE Communication

All BLE traffic between Home Assistant and a Govee device, as implemented in `govee_ble.py`.

---

## GATT Characteristics

| Role    | UUID                                   | Direction         |
|---------|----------------------------------------|-------------------|
| Control | `00010203-0405-0607-0809-0a0b0c0d2b11` | HA → device (write without response) |
| Notify  | `00010203-0405-0607-0809-0a0b0c0d2b10` | device → HA (notifications)           |

---

## Packet format

### Command packets (HA → device, 20 bytes)

```
Byte  0     : 0x33  (command prefix)
Byte  1     : action byte (see table below)
Bytes 2–18  : parameters, zero-padded
Byte  19    : XOR checksum of bytes 0–18
```

### Query packets (HA → device, 20 bytes)

```
Byte  0     : 0xAA  (query prefix)
Byte  1     : query byte (same values as action byte)
Bytes 2–18  : 0x00
Byte  19    : XOR checksum of bytes 0–18
```

### Notification packets (device → HA, variable length ≥ 3 bytes)

```
Byte  0     : 0xAA  (notification prefix)
Byte  1     : domain byte (same values as action byte)
Bytes 2+    : response payload
```

### Action / domain bytes

| Constant      | Value  | Meaning              |
|---------------|--------|----------------------|
| `POWER`       | `0x01` | Power on/off         |
| `BRIGHTNESS`  | `0x04` | Brightness level     |
| `COLOR`       | `0x05` | Color / mode         |

### LED mode bytes (byte 2 of COLOR command payload)

| Constant    | Value  | Meaning                              |
|-------------|--------|--------------------------------------|
| `MANUAL`    | `0x02` | Solid RGB color                      |
| `SCENES`    | `0x05` | Scene / ptReal effect                |
| `MUSIC`     | `0x13` | Music-reactive mode                  |
| `SEGMENTS`  | `0x15` | Per-segment RGB (H6053, H6072, etc.) |

---

## 1. Connection

### 1.1 Discovery

```
HA Bluetooth scanner  →  passive advertisement from device
GoveeBLECoordinator   →  bluetooth.async_ble_device_from_address(address, connectable=True)
                          retried up to 4 times with 2 s backoff if None is returned
```

### 1.2 GATT connect

```
GoveeBLECoordinator  →  brc.establish_connection(BleakClient, ble_device, address,
                             disconnected_callback=_handle_ble_disconnect)
```

`bleak_retry_connector` handles per-attempt timeouts, backoff and service-cache validation internally, up to 4 attempts.

### 1.3 Subscribe to notifications

```
HA  →  BleakClient.start_notify(NOTIFY_UUID, _notify_callback)
```

All subsequent device → HA state updates arrive via this subscription.

### 1.4 Initial state query (after every fresh connect)

```
HA  →  device  WRITE(CONTROL_UUID):  AA 01 00 … <chk>  (query power state)
HA  →  device  WRITE(CONTROL_UUID):  AA 04 00 … <chk>  (query brightness)
HA  →  device  WRITE(CONTROL_UUID):  AA 05 00 … <chk>  (query color/mode)

device  →  HA  NOTIFY:  AA 01 <on=01|off=00> …
device  →  HA  NOTIFY:  AA 04 <brightness_raw> …
device  →  HA  NOTIFY:  AA 05 <mode> <r> <g> <b> …
```

> **Race condition note**: on a fresh connect the initial state queries are sent *before* the triggering command is dispatched. The query responses therefore reflect the **pre-command** device state and will overwrite the entity's optimistic state update when they arrive (~100–200 ms later). A post-command state query (§2.9) corrects this.

---

## 2. Commands

All commands are sent via `WRITE_WITHOUT_RESPONSE` to `CONTROL_UUID`.

### 2.1 Power on

```
HA  →  device:  33 01 01 00 … <chk>
```

### 2.2 Power off

```
HA  →  device:  33 01 00 00 … <chk>
```

### 2.3 Brightness

Brightness is HA scale 0–255. For `PERCENT_MODELS` (H617A, H617C) it is converted: `val = brightness * 100 / 255`.

```
HA  →  device:  33 04 <val> 00 … <chk>
```

### 2.4 RGB color (standard models)

```
HA  →  device:  33 05 02 <R> <G> <B> 00 … <chk>
                         ^^
                         mode = MANUAL (0x02)
```

### 2.5 RGB color (segmented models: H6053, H6072, H6102, H6199, H617A, H617C)

```
HA  →  device:  33 05 15 01 <R> <G> <B> 00 00 00 00 00 FF 7F 00 … <chk>
                         ^^
                         mode = SEGMENTS (0x15)
```

### 2.6 Color temperature

Kelvin is converted to RGB via Tanner Helland's curve-fit algorithm (clamped to 1000–40 000 K), then the same RGB packet as §2.4 / §2.5 is sent.

```
kelvin → (R, G, B)
HA  →  device:  33 05 02 <R> <G> <B> 00 … <chk>   (or SEGMENTS variant)
```

### 2.7 Music mode

```
HA  →  device:  33 05 13 <mode_id> <sensitivity=100> 00 00 … <chk>
HA  →  device:  33 01 01 00 … <chk>   (power on)
```

Music mode IDs:

| Name                   | `mode_id` |
|------------------------|-----------|
| Music mode - Energic   | `0x05`    |
| Music mode - Rhythm    | `0x03`    |
| Music mode - Spectrum  | `0x04`    |
| Music mode - Rolling   | `0x06`    |

### 2.8 Scene / ptReal effect

A scene is encoded as one or more base64 `ptreal_cmds` strings loaded from the JSON scene library. Each decoded frame is sent with a 50 ms inter-frame delay, followed by a power-on packet.

```
for each frame in ptreal_cmds:
    HA  →  device:  WRITE <decoded_frame>    (50 ms gap between frames)
HA  →  device:  33 01 01 00 … <chk>          (power on)
```

### 2.9 Post-command state verification

After every successful command dispatch (`_dispatch()` returns without error) a background task `_query_state_after_command()` is created:

```
# 500 ms after command packet is written:
HA  →  device  WRITE(CONTROL_UUID):  AA 01 00 … <chk>  (query power state)
HA  →  device  WRITE(CONTROL_UUID):  AA 04 00 … <chk>  (query brightness)
HA  →  device  WRITE(CONTROL_UUID):  AA 05 00 … <chk>  (query color/mode)
```

This corrects the stale state that the **initial state query** (§1.4) may have pushed into the coordinator when the command was the first one on a fresh connection:

```
t=0ms   connect + subscribe + send initial queries (AA 01, AA 04, AA 05)
t=~5ms  send actual command (e.g. 33 01 01 = power on)
t=130ms initial query responses arrive → notify_callback fires → is_on = False  ← stale!
t=500ms post-command queries sent
t=630ms post-command responses arrive → notify_callback fires → is_on = True  ← correct
```

On reused connections (§4) there is no initial query, so the post-command query also serves as the only state confirmation (Govee devices do not echo `0x33` commands as unsolicited `0xAA` notifications).

---

### 2.10 Multi-packet segmented burst (legacy helper)

Used for large scene payloads that exceed a single 20-byte frame. The payload is split into fragments with a protocol header:

```
frame[0]  : protocol_type | seq=0   | frame_count | header … data_chunk_0
frame[1…] : protocol_type | seq=N   | data_chunk_N …
frame[-1] : protocol_type | seq=255 | last_data_chunk
```

Each frame is 20 bytes with XOR checksum at byte 19. Frames are sent with 50 ms inter-frame delay.

---

## 3. Notifications

The device sends unsolicited `0xAA` notifications on `NOTIFY_UUID` in response to state queries or after commands.

### 3.1 Power state

```
device  →  HA:  AA 01 <state> …
                         01 = on, 00 = off
```

### 3.2 Brightness

```
device  →  HA:  AA 04 <brightness_raw> …
```

`brightness_raw` is 0–255 (raw) or 0–100 (percent models). HA scale is derived by the coordinator.

### 3.3 Color / mode

```
device  →  HA:  AA 05 <mode> …
```

Subsequent bytes depend on `mode`:

| Mode      | Bytes 3+            | Description                         |
|-----------|---------------------|-------------------------------------|
| `MANUAL`  | R G B               | Solid color                         |
| `SEGMENTS`| 01 R G B …          | Segment color (byte 2 = 0x01)       |
| `MUSIC`   | music_mode_id       | Active music mode ID                |
| `SCENES`  | (no color data)     | Scene active, RGB not reported      |

---

## 4. Idle disconnect

After the last write AND its post-command state query return, a 5-second idle timer is (re)started.  
When it expires: `BleakClient.disconnect()` → `_client = None`.  
No `_available = False` is set; the device is just disconnected at the GATT level. The next command reconnects transparently.

---

## 5. Error handling and reconnect

### 5.1 Write failure (command in flight)

`_dispatch()` retries up to 3 times with exponential backoff (2 s, 4 s). After all retries are exhausted:

- `_available = False` → HA entity shows **Unavailable**
- Advertisement watcher is registered (see §5.3)

### 5.2 Unexpected GATT disconnect

Bleak calls `_handle_ble_disconnect` immediately when the connection drops (regardless of whether a command was in flight):

- Idle timer cancelled
- `_client = None`
- `_available = False` → HA entity shows **Unavailable**
- Advertisement watcher registered (see §5.3)

### 5.3 Advertisement watcher (auto-reconnect)

`bluetooth.async_register_callback` is called with a matcher for the device's MAC address and `ACTIVE` scanning mode. When the HA Bluetooth scanner sees an advertisement from the device (device powered on or came back in range):

```
HA scanner  →  advertisement from device
             →  _on_advertisement() fires
             →  hass.async_create_background_task(async_setup())
             →  _ensure_connected() → establish_connection()
             →  subscribe notifications + state query
             →  _available = True, watcher cancelled
             →  HA entity shows Available
```

The watcher is a no-op if the device is already available or connected.

---

## 6. HA startup

```
async_setup_entry()                          (__init__.py)
  └─ GoveeBLECoordinator.__init__()          (registers HASS_STOP listener)
  └─ async_forward_entry_setups → light.py

async_added_to_hass()                        (light.py)
  └─ coordinator.async_load_effects()        (loads scene JSON in executor)
  └─ async_get_last_state()                  (RestoreEntity for optimistic state)
  └─ coordinator.async_add_listener()        (subscribe to coordinator updates)
  └─ hass.async_create_background_task(coordinator.async_setup())
       └─ _ensure_connected()
       └─ _start_notify()
       └─ _send_state_queries()              (AA 01, AA 04, AA 05)
       └─ _available = True

Each subsequent command also spawns a background state-verification task (§2.10).
```

---

## 7. HA shutdown

```
EVENT_HOMEASSISTANT_STOP
  └─ _handle_hass_stop()
       └─ idle-disconnect timer cancelled
       └─ (client left connected; OS will close the socket)

async_unload_entry()
  └─ coordinator.cleanup()
       └─ advertisement watcher cancelled
  └─ async_unload_platforms()
```

---

## 8. Comparison: homebridge-govee BLE

Source: `homebridge-govee/lib/connection/ble.js` and `lib/platform.js`.

### Connection model

| Aspect | govee_lights (HA) | homebridge-govee |
|---|---|---|
| BLE library | `bleak` + `bleak_retry_connector` (Python, async) | `@stoprocent/noble` (Node.js, callback-based) |
| Persistent connection | Yes — GATT client kept open for 5 s after last write | No — fresh connect → write → disconnect on every command |
| State reading | GATT notifications subscribed after connect | No notifications; state is not read back |
| Reconnect strategy | `_handle_ble_disconnect` + advertisement watcher | `btClient.reset()` before every connect |
| Connection timeout | Handled by `bleak_retry_connector` defaults (20 s) | Hard 10 s `Promise.race` timeout |
| Write timeout | None (relies on Bleak/OS) | Hard 5 s `Promise.race` timeout on write |
| Multi-command (scenes) | Sent as a burst over one connection with 50 ms delay | Only one packet per `updateDevice()` call |

### Color temperature packet

Homebridge sends a native color-temperature packet with a dedicated sentinel structure:

```
HA (govee_lights):   33 05 02 <R> <G> <B> 00 … <chk>     ← RGB approximation only
Homebridge:          33 05 02 FF FF FF 01 <R> <G> <B> <chk>
                                    ^^^^ byte 5 = 0x01 signals "kelvin mode"
```

When byte 5 of the COLOR payload is `0x01`, the bytes that follow (positions 6–8) are the RGB equivalent of the kelvin value, and the device uses its native white-balance rendering rather than treating it as a solid RGB command. Models with `bleColourD` use mode `0x0D` instead of `0x02`.

Our integration converts kelvin → RGB and sends the plain RGB packet, which works but loses the device's native white rendering. This is noted in the homebridge source as a known TODO.

### Adapter reset before every connect

Homebridge calls `btClient.reset()` before each connection attempt to flush stale connection state from the Noble adapter:

```js
// Reset adapter to clear any stale connections
btClient.reset()
peripheral = await this.connectWithTimeout(bleAddress, 10000)
```

`bleak_retry_connector` handles similar cleanup internally via `close_stale_connections`, so the effect is equivalent.

### Control priority

Homebridge tries transports in order: LAN → AWS → BLE. BLE is the last resort. Our integration is BLE-only (or LAN-only with `GoveeLANCoordinator`).

### Scanning vs. connection

Homebridge's Noble scanner handles sensor advertisement decoding (H5075, H5101 temperature/humidity sensors). Light commands use a separate `connectAsync` call. The two modes cannot run simultaneously — homebridge pauses the scan, sends the command, then resumes:

```js
const wasScanning = this.isScanning
if (wasScanning) await this.stopDiscovery()
// … connect, write, disconnect …
if (wasScanning) this.startDiscovery(this.discoverCallback)
```

Our integration doesn't have this constraint — the HA Bluetooth scanner runs in the background independently, and `establish_connection` is called only on demand.
