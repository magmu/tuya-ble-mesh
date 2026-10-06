# Tuya BLE Mesh for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?logo=homeassistantcommunitystore)](https://github.com/hacs/integration)
[![CI](https://github.com/magmu/tuya-ble-mesh/actions/workflows/ci.yml/badge.svg)](https://github.com/magmu/tuya-ble-mesh/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/version-0.42.12-blue.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![HA 2024.1+](https://img.shields.io/badge/HA-2024.1%2B-blue.svg)](https://www.home-assistant.io)
[![Tests](https://img.shields.io/badge/tests-2300%20passing-brightgreen.svg)](https://github.com/magmu/tuya-ble-mesh/actions)

A fully local Home Assistant integration for controlling Tuya BLE Mesh devices. No cloud. No Tuya account required for daily use.

> **This is a fork of [11z4t/tuya-ble-mesh](https://github.com/11z4t/tuya-ble-mesh)** that adds SIG Mesh **lights** over an ESPHome Bluetooth proxy, with the Classy Caps Lumineer solar post cap as the tested device. See [Changes in this fork](#changes-in-this-fork).

## What is this?

Many affordable smart lighting products (sold under brands like AwoX, Malmbergs, and others) use **Tuya BLE Mesh** firmware internally. They're typically controlled via the Tuya Smart app through Tuya's cloud servers.

This integration replaces cloud control with **direct BLE communication**, keeping everything local on your network. Your smart lights respond faster, work without internet, and don't depend on any external servers.

### How it works

There are two connection modes:

**Mode 1: Bridge daemon (RPi)**
```
Home Assistant  ←HTTP→  Bridge Daemon (RPi)  ←BLE Mesh→  Devices
```
1. A Raspberry Pi with Bluetooth runs the bridge daemon near your BLE mesh devices
2. The HA integration communicates with the bridge over your local network
3. The bridge translates commands to/from the BLE mesh protocol

**Mode 2: ESPHome BLE Proxy**
```
Home Assistant  ←API→  ESPHome BLE Proxy  ←BLE Mesh→  Devices
```
For SIG Mesh devices, any ESPHome device with BLE proxy enabled can be used instead of a dedicated RPi. This is simpler to set up and doesn't require a separate bridge daemon.

In both modes, Home Assistant itself doesn't need Bluetooth hardware.

## Tested Devices

| Device | Brand | Type | Status |
|--------|-------|------|--------|
| LED Driver 9952126 | Malmbergs | Dimmable LED driver | ✅ Tested — on/off, brightness |
| Smart Plug S17 | Malmbergs | BLE Mesh relay plug | ✅ Tested — on/off, SIG Mesh provisioned |
| Lumineer solar post cap | Classy Caps | SIG Mesh light (Telink, CID `07D0`, PID `0300`) | ✅ Tested (this fork) — on/off, brightness, white temperature, colour, firmware, signal strength, solar sleep handling |

### Potentially Compatible

Devices using the Tuya BLE Mesh / Telink stack with service UUID `fe07`:

| Brand | Example Products | Vendor ID | Status |
|-------|-----------------|-----------|--------|
| **AwoX** | Mesh lights | `0x0160` | Protocol compatible, untested |
| **Malmbergs** | LED drivers, plugs | `0x1001` | Hardware tested |
| **Dimond/retsimx** | Mesh lights | `0x0211` | Protocol compatible, untested |

## Changes in this fork

Everything below was added for SIG Mesh lights reached through an ESPHome Bluetooth proxy, and tested on Classy Caps Lumineer solar post caps.

**SIG Mesh lights**
- Light Lightness, CTL and HSL control for SIG Mesh lights over direct BLE (`sig_light` device type).
- White temperature uses Lightness Set plus CTL Temperature Set on the CTL Temperature element, because some lights answer CTL Get but drop CTL Set. The light's full reported range (800–20000 K on the post cap) is used.
- On/off and light state are read back after every connect, so HA matches the light after a restart without power cycling it.
- Sequence numbers are saved after every command, so restarts no longer replay old ones.

**Sleeping solar lights**
- A per-device **Solar powered** switch (configuration). When it is on, the light is expected to be offline from civil dawn to dusk (sun above −6°, from HA's own sun data): no warnings and no repairs while it sleeps.
- Reconnects pause while a device isn't advertising and resume as soon as it is seen again. If a solar light is still missing after dark, a device-not-found repair is raised.

**Status and diagnostics**
- The firmware version (`CID:07D0 PID:0300 VID:3235` style) stays visible while the device is offline.
- Signal strength is read from the latest advertisement.
- Diagnostics list every element's SIG and vendor models (the device's composition).
- A **Tuya data points** diagnostic sensor (disabled by default) shows every data point a device reports over the Tuya vendor model. It is only refreshed when the device reports, never polled.
- Tuya time sync: when a device asks for the time, the integration replies (calendar layout first, other layouts as fallbacks), waiting until the connection is ready.
- A quiet but reachable device is logged at debug level instead of a warning every 5 minutes.

**Project**
- CI runs and passes on the fork (ruff, mypy, pytest, hassfest, HACS), and a version bump in `manifest.json` on `main` publishes a release automatically, so HACS offers proper updates.

## Features

### Device Control
- **Power on/off** — instant local control, no cloud round-trip
- **Brightness** — 1–100% dimming with smooth transitions
- **Color temperature** — warm to cool white (CCT)
- **RGB color** — full color control on supported devices
- **Switch** — relay control for smart plugs

### Connectivity
- **Auto-discovery** — finds `out_of_mesh*` and `tymesh*` devices via BLE
- **HA Bluetooth integration** — uses Home Assistant's native Bluetooth API (no adapter conflicts)
- **ESPHome BLE proxy** — use any ESPHome device as a BLE bridge (SIG Mesh)
- **Auto-reconnect** — exponential backoff (5s → 5min) on connection loss
- **Keep-alive** — maintains BLE connections proactively to minimize latency
- **Command queue** — delivery with TTL and retry under rapid HA automations
- **Reconnect debounce** — prevents reconnect storms after transient failures

### Status & Monitoring
- **Push-based updates** — BLE notifications drive state changes; automatic fallback to poll mode
- **RSSI sensor** — signal strength from HA Bluetooth API, adaptive polling
- **Firmware version** — sensor for device firmware tracking
- **Staleness detection** — coordinator marks unavailable if no updates for configurable period
- **Connection statistics** — visible in HA diagnostics
- **Device triggers** — automation triggers for connection events
- **Logbook integration** — state changes logged in HA logbook

### Protocol Support
- **Tuya proprietary BLE Mesh** (Telink TLK8232 / TLK8258) — all light and plug features
- **SIG Mesh (Bluetooth Mesh)** — provisioning, proxy, segmentation/reassembly (experimental)
- **Dual-stack** — both protocols work simultaneously on the same HA instance

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=magmu&repository=tuya-ble-mesh&category=integration)

Click the button above to open this repository in HACS on your Home Assistant, then click **Download** and restart Home Assistant.

Or add it by hand:

1. Open **HACS** in Home Assistant
2. Go to **Integrations** → three-dot menu → **Custom repositories**
3. Add URL: `https://github.com/magmu/tuya-ble-mesh`
4. Category: **Integration**
5. Search for **"Tuya BLE Mesh"** and click **Download**
6. **Restart Home Assistant**

### Manual

1. Copy `custom_components/tuya_ble_mesh/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

### Adding a device

**Settings** → **Devices & Services** → **Add Integration** → search **"Tuya BLE Mesh"**

The integration will scan for nearby BLE Mesh devices automatically. Select your device from the list, or enter the MAC address manually.

| Field | Description | Default |
|-------|-------------|---------|
| Device type | Light or Plug | Light |
| MAC Address | BLE MAC (XX:XX:XX:XX:XX:XX) | *required* |
| Bridge Host | IP/hostname of the bridge RPi | *required* |
| Bridge Port | Bridge daemon HTTP port | `8099` |
| Mesh Name | Mesh network name | `out_of_mesh` |
| Mesh Password | Mesh network password | `123456` |
| Vendor ID | Vendor identifier (hex) | `0x1001` |

### Bridge Daemon

The bridge daemon runs on a Raspberry Pi with Bluetooth, close to your mesh devices:

```bash
# On the RPi
cd ~/tuya-ble-mesh
source venv/bin/activate
python scripts/ble_mesh_daemon.py --host 0.0.0.0 --port 8099
```

The daemon exposes a simple HTTP API that the HA integration uses to send commands and receive status.

### Vendor IDs

Different brands embed different vendor IDs in the Telink mesh protocol:

| Brand | Vendor ID |
|-------|-----------|
| Tuya (default) | `0x1001` |
| AwoX | `0x0160` |
| Malmbergs | `0x1001` |
| Dimond/retsimx | `0x0211` |

If commands don't work with the default, try `0x0160` (AwoX) or `0x0211` (Dimond).

## Entities

Each device creates:

| Entity | Type | Description |
|--------|------|-------------|
| `light.<name>` | Light | Power, brightness, color temperature |
| `switch.<name>` | Switch | Power on/off (plugs only) |
| `sensor.<name>_signal` | Sensor | BLE signal strength (RSSI) |
| `sensor.<name>_firmware` | Sensor | Device firmware version |
| `switch.<name>_solar_powered` | Switch (config) | Expect the light offline in daylight (SIG lights) |
| `sensor.<name>_tuya_data_points` | Sensor (diagnostic, disabled by default) | Count of reported Tuya data points; each one as a `dp_<id>` attribute |

## Hardware Setup

### What you need

- **Home Assistant** 2024.1 or later (any installation method)
- **Raspberry Pi** (3B+ or 4) with built-in Bluetooth — runs the bridge daemon
- **Tuya BLE Mesh devices** — compatible devices (see Tested Devices section)

### Bluetooth Setup

This integration uses **Home Assistant's native Bluetooth integration** (since v0.33). You no longer need a second USB adapter or ESPHome proxy to avoid conflicts.

**Recommended:** Enable Home Assistant's built-in Bluetooth integration and let it manage the adapter. The bridge daemon on the RPi handles direct BLE communication.

> **Note:** If you have an older setup with a second USB Bluetooth adapter or ESPHome proxy, these continue to work — the integration supports both modes.

### Network diagram

```
┌──────────────┐     HTTP      ┌──────────────┐     BLE Mesh     ┌─────────┐
│ Home         │◄─────────────►│ Raspberry Pi │◄────────────────►│ Light 1 │
│ Assistant    │   (port 8099) │ (Bridge)     │                  ├─────────┤
│              │               │              │◄────────────────►│ Light 2 │
└──────────────┘               └──────────────┘                  ├─────────┤
                                                                 │ Plug 1  │
                                                                 └─────────┘
```

### Optional hardware (for development/debugging)

- **Adafruit nRF51822 BLE Sniffer** — passive packet capture via serial
- **Shelly Plug S** — remote power cycling for factory reset procedures

## Architecture

The codebase is split into two independent layers:

```
custom_components/tuya_ble_mesh/lib/tuya_ble_mesh/  ← Standalone BLE mesh library (no HA dependency)
├── protocol.py             ← Tuya BLE Mesh packet encoding/decoding
├── crypto.py               ← Mesh encryption (AES-based)
├── connection.py           ← BLE GATT connection management
├── connection_manager.py   ← Connection lifecycle and backoff
├── device.py               ← High-level device abstraction
├── device_protocol.py      ← MeshDeviceProtocol interface
├── scanner.py              ← BLE device discovery
├── sig_mesh_protocol.py    ← SIG Mesh standard protocol
├── sig_mesh_crypto.py      ← SIG Mesh encryption
└── sig_mesh_device.py      ← SIG Mesh device with GATT proxy

custom_components/tuya_ble_mesh/   ← Home Assistant integration
├── __init__.py             ← Setup, config entry handling
├── config_flow/            ← UI configuration wizard (modular)
├── coordinator.py          ← Data update coordinator
├── connection_manager.py   ← BLE connection lifecycle
├── light.py                ← Light entity platform
├── switch.py               ← Switch entity platform (plugs)
├── sensor.py               ← Signal strength + firmware sensors
├── device_trigger.py       ← Automation triggers
├── logbook.py              ← Logbook integration
└── repairs.py              ← HA repair issues
```

The core library has no HA dependencies and can be used independently for scripts, testing, or other platforms.

## Development

```bash
# Clone and set up virtual environment
git clone https://github.com/magmu/tuya-ble-mesh.git
cd tuya-ble-mesh
python -m venv venv
source venv/bin/activate
pip install -e ".[test]"

# Run full check pipeline (must pass before committing)
bash scripts/run-checks.sh

# Scan for nearby BLE mesh devices
python scripts/scan.py

# Run tests only
python -m pytest tests/unit/ -q
```

### Check pipeline

CI runs: **ruff** (lint + format), **mypy** (strict), **pytest** (1922 tests), **HACS validation**.
Local-only: **bandit**, **safety**, **detect-secrets**.

All checks must pass before committing — enforced by `run-checks.sh`.

## Verification Status

| Area | CI Verified | Hardware Tested | Notes |
|------|-------------|-----------------|-------|
| Telink mesh protocol | ✅ Unit tests | 1 device (LED Driver 9952126) | on/off, brightness confirmed |
| SIG Mesh provisioning | ✅ Unit tests | 1 device (Smart Plug S17) | on/off confirmed |
| SIG Mesh lights via ESPHome proxy | ✅ Unit tests | 2 devices (Classy Caps post caps) | on/off, brightness, CTL, HSL, restart sync, solar sleep |
| SIG Mesh segmentation | ✅ Unit tests | Limited | SAR fragmentation tested in CI |
| HA integration layer | ✅ 1922 tests | 2 devices | Config flow, coordinator, entities |
| HA Bluetooth API | ✅ Unit tests | Indirect | HaBleakClientWrapper integration |
| BLE reconnection | ✅ Unit tests | Observed | Exponential backoff with debounce |

**Overall status: HACS beta-ready / stable for limited use.** Tested with 2 Malmbergs devices. SIG Mesh layer has known simplifications. Not broadly validated across vendors.

## Troubleshooting

### Device Not Found

**Symptom:** "Device not found" during setup

**Possible causes:**
- Device not powered on or too far away (>5m from adapter)
- Device already paired to Tuya Smart app (close the app completely)
- Device needs factory reset (power cycle 3-5 times quickly)

**Solution:** Ensure device is in pairing mode, close Tuya Smart app, move device closer to adapter.

### Connection Timeouts

**Symptom:** Connection times out during provisioning

**Possible causes:**
- Weak BLE signal
- Device in wrong state

**Solution:** Move device closer, try factory reset.

### Solar Light Offline During the Day

**Symptom:** A solar light (such as a Classy Caps post cap) shows unavailable or raises connection repairs in daylight.

**Cause:** Solar lights stop advertising while the sun is up.

**Solution:** Turn on the device's **Solar powered** switch. The integration then expects it offline from dawn to dusk and reconnects as soon as it advertises again.

### Device Shows Unavailable After HA Restart

**Symptom:** Entity shows unavailable after HA restart

**Cause:** Bridge daemon not running, or device temporarily out of range.

**Solution:** Ensure bridge daemon is running (`python scripts/ble_mesh_daemon.py`). The integration will auto-reconnect with exponential backoff once the bridge is reachable.

## Known Limitations

- **Bridge required** — HA cannot talk BLE mesh directly; the RPi bridge daemon must be running (for Telink devices)
- **Limited device testing** — 2 Malmbergs devices and the Classy Caps post cap tested; other brands are protocol-compatible but untested
- **No battery data on Classy Caps post caps** — they have no Generic Battery model and send no Tuya data points, even when asked to report everything, so battery level and charging state can't be shown
- **Factory reset** — some devices need 5x rapid power cycling to enter provisioning mode
- **No OTA** — firmware updates are out of scope
- **SIG Mesh Proxy SAR** — only COMPLETE PDUs supported; FIRST/CONTINUE/LAST fragmentation not implemented
- **SIG Mesh pending responses** — keyed by opcode only; concurrent requests for the same opcode may collide

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

Quick summary:
1. Fork the repository
2. Create a feature branch
3. Run `bash scripts/run-checks.sh` — all checks must pass
4. Submit a pull request with the PR template

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

MIT — see [LICENSE](LICENSE)
