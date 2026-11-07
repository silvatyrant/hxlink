# Hxlight Bulb → Home Assistant via K8s BLE Bridge

Nov 1, 2026 · @Alain

## Goal and context

Build a small Kubernetes service that lets Home Assistant control a DXC "Hxlight" tunable-white E27 bulb by broadcasting BLE advertisements from the Bluetooth dongle already attached to one cluster node. HA should see a normal light entity with on/off, brightness, colour temperature, plus night mode and a sleep timer.

**Existing pattern to copy:** a pod on the same node already talks to a Xiaomi smart scale over that dongle and forwards readings to HA. This service follows the same shape, but in the transmit direction: HA command → MQTT → pod → BLE advertisement → bulb.

**Environment facts (owner-supplied):**

- Home lab on Proxmox; Kubernetes runs in VMs.
- One k8s node VM has the USB Bluetooth dongle passed through from the host. The agent must verify the passthrough works for raw HCI, not just BlueZ scanning.
- The Xiaomi scale pod already uses this dongle. The new pod must coexist with it.
- HA runs elsewhere and is reached over MQTT (assumed; confirm the broker the scale pod uses and reuse it).

**Out of scope:** listening to the bulb's physical remote. It uses a native XN297/nRF24 radio that a BLE adapter cannot hear, so HA state will drift if someone uses the remote. HA state is optimistic (assumed).

## Hardware and protocol

The bulb obeys connectionless BLE advertisements that wrap an emulated XN297 packet; the encoder is already written and MIT-licensed in `ha-ble-adv` as codec `hxlight_v0`.

| Item | Detail |
| --- | --- |
| Bulb | Unbranded 9W A60 E27, AC100-245V, 3000-6500 K; matches [DXC B01A manual](https://manuals.plus/dxc/b01a-remote-bulb-manual.pdf) |
| Remote | "DXC-Remote V1.1 2023/07/17", 12 buttons, Shenzhen DXC Technology, [FCC ID 2BC7D-R01A](https://fccid.io/2BC7D-R01A) |
| Phone app | Hx-light (Android `com.dxc.delight`), Bluetooth, no account |
| Transport | BLE legacy advertising, AD flags `02 01 01` + manufacturer data, company ID `0xFFF0` |
| Codec source | [`codecs/hxlight.py`](https://github.com/NicoIIT/ha-ble-adv/blob/main/custom_components/ble_adv/codecs/hxlight.py), helpers `whiten`, `rev_crc16_ccit` in `codecs/utils.py` |

**On-air frame (hxlight\_v0):** `02 01 01` + `1B FF` + header `F0 FF 6D B6 43 59 3A 60 A7 A1` + 15 whitened bytes + footer `18`. The Hxlight app sends the trailing `0x18`; the sibling LED-Spot codec omits it.

**Encode steps (from the codec):**

1. Build 12 bytes: `[0x83 + arg3, id_hi, id_lo, index, param, cmd, arg0, b7, b8, b9, b10, tx_count]`.
2. Bytes 7-10: default `[~param, ~cmd, ~arg0, 0x00]`; colour temperature `[arg1, ~cmd, 0x55, 0x55]`; RGB (arg3 = 1) `[~param, ~cmd, 0x55, 0x55]`.
3. Append checksum `sum(12 bytes) & 0xFF`.
4. Append `rev_crc16_ccit(13 bytes, seed 0xB12B)` little-endian.
5. Whiten all 15 bytes with seed `0x59`.
6. `tx_count` increments per new command (max `0xFE`); the bulb ignores repeats of the same counter.

**Command table (`arg3` sets byte 0: 0 → 0x83, 1 → 0x84, 3 → 0x86):**

| Function | cmd | param | arg0 | arg3 | Notes |
| --- | --- | --- | --- | --- | --- |
| On | 0x01 | 0 | 0x01 | 3 |  |
| Off | 0x01 | 0 | 0x02 | 3 |  |
| Brightness | 0x65 | 0x05 | 1-100 | 0 | percent |
| Colour temp | 0x65 | 0x07 | 0-100 | 0 | arg0 = reversed CT (warm high), arg1 = CT; verify direction on the bulb |
| Night mode | 0x03 | 0 | 0x01 | 0 | turns bulb on, \~2% warm |
| Presets | 0x0E | 0 | 1/2/3 | 0 | Bright / Warm / Comfortable; ignored while off |
| Timer | 0x01 | 0x09 | 0 | 3 | arg1/arg2 = time split hours/minutes (check `split_copy`) |
| Cancel timer | 0x02 | 0x09 | 0 | 3 |  |
| Pair | 0x02 | 0 | 0x01 | 0 | send within 8 s of bulb power-on |
| Unpair | 0x02 | 0 | 0x02 | 0 |  |

**Identity:** `id` (2 bytes) is the controller's ID and `index` (1-4) is the group slot. Index 0 ("all") is not usable. The bulb only obeys IDs it has been paired with, so the service either pairs its own ID or reuses the app's captured ID.

## Architecture

Recommended: a small Python MQTT-to-BLE bridge pod pinned to the dongle node, exposing the bulb to HA through MQTT discovery. It mirrors the scale pod and keeps HA free of any Bluetooth dependency.

| Option | How | Verdict |
| --- | --- | --- |
| A. MQTT bridge pod (recommended) | Pod imports the `hxlight_v0` encoder, sets raw advertising on the dongle via HCI, publishes an MQTT light | Matches existing pattern; no new hardware |
| B. `ha-ble-adv` in HA | HACS integration, needs a BT adapter on the HA host, an ESPHome `ble_adv_proxy`, or a Shelly BT proxy | Use if A fails; not possible with a dongle on a k8s node |
| C. ESP32 `ble_adv_proxy` | Flash an ESP32 near the bulb, add `ha-ble-adv` | Most proven path; a fallback if the VM dongle cannot do raw advertising |

**Bridge responsibilities:**

- Subscribe to HA's MQTT light command topic (JSON schema) and translate to codec commands.
- Encode with a vendored copy of the `ha-ble-adv` codec, pinned to a release ≥ v2.2.14, with its MIT licence kept.
- Transmit: set advertising data, enable advertising for about 300-500 ms (non-connectable, 20-30 ms interval), then disable. Serialise all transmissions through one queue.
- Persist `tx_count`, the controller `id` and `index` (PVC or ConfigMap-backed state) so restarts don't replay stale counters or lose pairing.
- Publish optimistic state back to MQTT, with `retain` on state.
- Expose HA buttons for Pair, Night mode, presets and a timer, mirroring the remote's moon, bottle, book, laptop and 1H keys.

## Implementation steps

Work in this order; each step has a check before moving on.

1. **Recon the existing setup.**
   - Find the scale pod's manifest: node selector, `hostNetwork`, capabilities, how it reaches the dongle (BlueZ D-Bus socket vs raw HCI), and its MQTT broker and credentials.
   - On the node: `lsusb`, `hciconfig -a` / `btmgmt info`, adapter chipset and BT version. Check: adapter is `UP RUNNING` and supports LE advertising.
2. **Prove raw advertising works from the VM.**
   - From a debug pod or node shell, advertise a test payload with `btmgmt add-adv` or `hcitool -i hci0 cmd 0x08 0x0008 ...` (LE Set Advertising Data) and confirm it with a phone running nRF Connect. Check: the phone sees the `0xFFF0` manufacturer data.
   - If this fails through passthrough, stop and switch to option C.
3. **Capture the app's identity (optional but useful).**
   - Pair the bulb with the Hx-light app. Run `btmon` on the node while pressing On/Off in the app.
   - Feed captured frames to the codec's `decrypt` + `convert_to_enc` to recover `id` and `index`. Check: decoded `cmd` 0x01 with arg0 1/2 for on/off.
4. **Build the encoder module.**
   - Vendor `hxlight.py`, the `whiten` / `rev_crc16_ccit` helpers and the minimum of `models.py` needed, or re-implement the six encode steps directly (about 40 lines).
   - Unit test: round-trip encode → decode for every command in the table, and match a frame captured in step 3 byte-for-byte when given the same id, index and counter.
5. **Build the transmitter.**
   - Python with raw HCI socket, or `bluetooth-adapters` / `btsocket`. Issue LE Set Advertising Parameters, Set Advertising Data, Enable, sleep, Disable.
   - Don't take the adapter down or reset it; the scale pod needs it. Check: the bulb responds to On/Off from a CLI entry point.
6. **Pair.**
   - Either reuse the captured app ID (no pairing needed), or generate a random 16-bit ID, power-cycle the bulb and send Pair within 8 s. Check: bulb flashes 3 times.
7. **MQTT and HA discovery.**
   - Publish `homeassistant/light/hxlight_<id>/config` with JSON schema, `brightness: true`, `supported_color_modes: ["color_temp"]`, `min_kelvin: 3000`, `max_kelvin: 6500`, `effect_list: ["Night"]`, `optimistic: true`.
   - Add `button` entities for Pair, Unpair, Bright, Warm, Comfortable presets, and a `number` (or button set) for the timer.
   - Map HA kelvin to codec 0-100 and confirm direction on the real bulb.
   - When HA turns on with brightness and CT, send On, then Brightness, then CT as three frames with separate counters.
8. **Package and deploy.**
   - Container: Python slim + `paho-mqtt`; config via env (broker, credentials Secret, adapter `hci0`, bulb name).
   - Deployment with `replicas: 1`, `strategy: Recreate`, same `nodeSelector` as the scale pod, `hostNetwork: true`, capabilities `NET_ADMIN` and `NET_RAW`, a small PVC for state, liveness via MQTT heartbeat.
   - Ship with the same GitOps / manifest conventions as the scale pod.
9. **Write HA scripts mirroring the remote.** Night: effect Night. Feeding: 10%, warmest. Reading: 70%, \~3500 K. Work: 100%, 5000-6500 K. 1H: timer 60 min.

## Acceptance criteria

Done when HA controls the bulb reliably and the scale pod still works.

- [ ] HA shows a light entity auto-discovered over MQTT
- [ ] On/off works 20/20 times from HA, bulb within the same room as the dongle
- [ ] Brightness 1%, 50%, 100% visibly match
- [ ] Colour temperature at 3000 K and 6500 K go warm and cool respectively
- [ ] Night mode, three presets and the timer work from HA buttons
- [ ] Pod restart does not break control (counter and ID persisted)
- [ ] Bulb power-cycle does not break control (pairing survives)
- [ ] Xiaomi scale readings still reach HA during and after a burst of 50 bulb commands
- [ ] Codec unit tests pass and match at least one real captured frame
- [ ] Command latency HA → bulb under 1 s

## Risks and fallback

The biggest unknown is whether the passed-through dongle can do raw advertising alongside the scale pod; test that first (step 2).

| Risk | Mitigation |
| --- | --- |
| Adapter contention: BlueZ and raw HCI fighting over hci0, or the scale pod's scanning pausing during adverts | Prefer BlueZ's advertising API (`btmgmt add-adv` / D-Bus `LEAdvertisingManager1`) over raw HCI if the scale pod uses BlueZ; keep adverts short; test criterion 8 |
| BlueZ rewrites the flags AD (`02 01 06` instead of `02 01 01`) | Check with a phone; if the bulb rejects it, use raw HCI or `btmgmt add-adv` with explicit data |
| Range: dongle node far from the bulb | Measure; if marginal, fall back to an ESP32 `ble_adv_proxy` (option C) |
| Codec drift: hxlight codec is a month old and changed between v2.2.13 and v2.2.14 | Pin the commit, keep the tests, watch the repo |
| LED-Spot vs Hxlight confusion: byte 0 must be 0x86 for on/off on Hxlight | Use `hxlight_v0` only; verify with a capture from this bulb's app |
| State drift from the physical remote | Accept; or later add an nRF24L01+ receiver for the remote |
| CT direction or range wrong | Calibrate on the real bulb, then fix the kelvin mapping |

**Fallback:** if step 2 fails, buy an ESP32, flash ESPHome with `ble_adv_proxy`, install `ha-ble-adv` ≥ v2.2.14 via HACS and use "Duplicate config" from the Hx-light app. That route is already proven by its authors.
