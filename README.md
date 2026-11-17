# Mouse Tool

A lightweight, universal web and CLI suite for managing **multiple connected mice at once**, kernel-level mouse acceleration (`maccel`), and `niri` desktop pointer settings on Linux — without vendor bloatware.

## Supported Protocols & Hardware

- **Razer (`VID 1532`)** — 90-byte HID feature report protocol (DPI stages, split X/Y, polling up to 8 kHz via HyperPolling dongle, preset & asymmetric LOD, Normal + HyperShift button remaps, power/sleep, wireless battery, pairing).
- **Endgame Gear 8k (`VID 3367`)** — Full read-modify-write NVRAM & real-time event protocol (`OP1 8k`, `XM2 8k`, `OP1 8k Purple Frost`, `OP1 8k v2`, `XM2 8k v2`): CPI stages (`10`/`50` DPI steps), polling (`1000–8000 Hz`), discrete/glass LOD (`0.7–1.7 mm` / `1–2 mm`), Motion Sync, Angle Snapping, Ripple Control, Slamclick Filter, Motion Jitter Filter, Angle Tuning (`v2`), Force Max Sensor FPS (`v2`), Disable LED on Lift-Off (`v2`), GX Speed/Safe SPDT & multiclick filter, left-handed button swap, and factory reset.
- **Endgame Gear `-we` (`VID 3367`)** — CompX Feature `0x08` + Input `0x09` protocol (`OP1we`, `XM2we` wired & wireless, with automatic `0xA1` exclusion so `OP1w 4K v2` is never misclassified): DPI stages, polling (`125–1000 Hz`), LOD, debounce, Motion Sync, Angle Snapping, Ripple Control, sleep timeout, and battery.
- **Logitech HID++ 2.0 (`VID 046D`)** — Multi-device support for Superlight / G Pro / G502 / MX Series (DPI, polling rate, firmware, serial, and unified/sysfs battery reporting).
- **Pulsar (`VID 3710`, `25A7`, `3554`)** — Full coverage across all Pulsar hardware generations:
  - **Pulsar XS-1 (`3710`)** (`X2A v3`, `X3 M`, `XS-1`, `8K Dongle`): DPI, read-only polling readout, LOD, debounce, Motion Sync, Angle Snapping, Ripple Control, firmware, and wireless battery.
  - **Pulsar Pro (`3710:5405`)**: Full multi-stage DPI, `125–8000 Hz` polling, LOD, debounce, Motion Sync, Ripple Control, Angle Snapping, Angle Tuning, Turbo mode, sleep timeout, and battery.
  - **Pulsar Areson (`25A7:FA7B/FA7C`)** (`X2 v2 / X2H / Xlite v3`) & **Pulsar 4K Receiver (`3554:F507–F509`)** & **Pulsar CompX (`3710`)**: 3-tier `10/50/100` DPI encoding, stages, polling (`125–4000/8000 Hz`), LOD, debounce, filters, and battery.
- **CompX Report `0x08` ODM Ecosystem (`VID 3554`, `373B`)** — Protocol-verified support for **ATK / VXE** (with dynamic `0x10` product probing across `F1 Ultimate 2.0`, `A9 Mini+`, `A9 Plus NearLink`, `VXE R1 / Pro Max / SE+`, `X1 Pro Max`), **Scyrox** (`V8`, `V8S`, `V8S Turbo`, including 6-byte DPI up to 50,000, 5-stop LOD, and 20K Scan Mode), **Teevolution Terra Pro**, **VGN F2 Master+**, and **Lamzu Atlantis** (1K & 4K).
- **Glorious (`VID 258A`, `22D4`, `320F`, `3794`, `093A`)** —
  - **Sinowealth** (`Model O/O-`, `Model D/D-`, `Model O Eternal`): 123B+ flash config readout & sensor-gated stage/polling writes (safe sensor ID check in WebHID).
  - **CORE 2 (`Model O 2 Pro / D 2 Pro 4K/8K`)**: Firmware/battery queries plus write-only DPI stages, `125–8000 Hz` polling, debounce, Motion Sync, backed by persistent local state cache (`~/.cache/mousetool/device_state.json` & `localStorage`).
  - **Classic (`Model O/D/I/O2/D2/I2 Wireless/Wired`, `Series One Pro`, `Forge`, `3794` CORE 2 Classic)**: Descriptor-length (`64B`) and generation (`core2` vs `legacy`) safety-gated writes + wireless battery queries.
  - **Pixart (`093A`)** (`Model O 2 Mini`, `Series 2 Pro`, `Model D 2 Mini`, `Model O 3`, `Model D 3`): `64B` Feature `0x03` config packet for DPI, polling, LOD, and debounce with local cache persistence.
- **Any Generic HID / USB / Bluetooth Mouse** — Connect any mouse via WebHID (**Any HID…**) or view it passively in the fleet bar via `/sys/class/input/mouse*` with live pointer polling rate measurement, live button/scroll tester, raw HID input report snooper, and local DPI reference storage for `maccel` normalization.

| Vendor | Supported PIDs |
| --- | --- |
| **Razer** (`1532`) | Viper V2 Pro (`00a5`, `00a6`), DeathAdder V3 / V3 Pro (`00b2`, `00b6`, `00b7`, `00c2`, `00c3`), DeathAdder V3 HyperSpeed (`00c4`, `00c5`), Viper V3 Pro / HyperSpeed (`00c0`, `00c1`, `00b8`), Cobra Pro (`00af`, `00b0`), Basilisk V3 Pro / 35K (`00aa`, `00ab`, `00cc`, `00cd`, `00d6`, `00d7`), HyperPolling Dongle (`00b3`) |
| **Endgame Gear** (`3367`) | **8k:** OP1 8k (`1964`), XM2 8k (`1966`), OP1 8k Purple Frost (`1976`), OP1 8k v2 (`1978`), XM2 8k v2 (`1980`) · **`-we`:** XM2we (`1960`, `1968`, `1982`), OP1we (`1961`, `1962`, `1970`, `1972`) |
| **Logitech HID++** (`046D`) | G Pro / Superlight / Superlight 2 (`c088`, `c092`, `c094`, `c099`), G305 (`c095`), G502 Hero / Lightspeed / X (`c08b`, `c093`, `c09d`, `c09e`), MX Master 3/3S (`b023`, `b034`), Unifying / Lightspeed / Bolt (`c52b`, `c539`, `c53f`, `c547`, `c548`, `c54d`) |
| **Pulsar** (`3710`, `25A7`, `3554`) | **XS-1 (`3710`):** `3404`, `3409`, `3410`, `5402`, `5403` · **Pro (`3710`):** `5405` · **Areson (`25A7`):** `fa7b`, `fa7c` · **4K Receiver (`3554`):** `f507`, `f508`, `f509` (plus CompX fallback on `3710`) |
| **CompX ODM** (`3554`, `373B`) | **Scyrox:** `f515`, `f517`, `f5f7` · **Teevolution:** `f520`, `f522`, `f523`, `f5bb` · **VGN F2:** `fb56`, `fb57` · **Lamzu Atlantis:** `f50d`, `f50f`, `f510` · **ATK / VXE (`3554`/`373B`):** `f58a`, `f58c`, `f58e`, `f58f`, `1085`, `10c9`, `11d9` |
| **Glorious** (`258A`, `22D4`, `320F`, `3794`, `093A`) | **Sinowealth:** `258a:0033`, `258a:0036`, `3794:a000` · **CORE 2:** `258a:201b`, `258a:201c`, `258a:2035`, `258a:2036` · **Classic:** `258a:2011–2039`, `22d4:1503`, `320f:821a/823a/831a`, `3794:a300/a312` · **Pixart (`093A`):** `821b–832a` |

## Features

- **Web Interface (`index.html`)**:
  - **Multi-Mouse Fleet Switcher**: Connect and manage multiple simultaneous WebHID mice (`+ Add mouse` for supported devices or `Any HID…` for generic mice)—even multiple identical VID:PID mice side by side (1-to-1 split-endpoint pairing and instance badges)—while also showing passive system-detected mice from `mousetool.py serve`.
  - **Sensitivity & Sensor**: DPI/CPI stages (optional split X/Y), one-off DPI, polling rate selector (`125–8000 Hz`, or read-only badge where hardware-locked such as Pulsar XS-1), Razer preset/async LOD, EGG discrete/glass LOD, Scyrox 5-stop LOD, or Low/Med/High preset LOD.
  - **01.3 Sensor, click & performance tuning**: Capability-driven controls for Click Debounce (`ms`), Motion Sync, Angle Snapping, Ripple Control, Performance/Turbo mode, 20K Scan Mode (Scyrox V8S), Anti-mistouch (ATK F1 Ultimate 2.0), plus Endgame Gear Slamclick, Motion Jitter, Glass Mode, Lift-Off LED disable, Force Max FPS, Angle Tuning, and GX Speed/Safe SPDT.
  - **Live Polling Rate Meter & Input Tester**: Works with every connected mouse (`pointerrawupdate` / `getCoalescedEvents`) alongside onboard Razer Normal + HyperShift remaps and EGG left-handed swap.
  - **Power & Device Info**: Battery/sleep controls for wireless mice, protocol & HID collection diagnostics, cached write-only state notices where hardware offers no getter, EGG factory reset, and a **raw HID input snooper**.
  - **Acceleration (`maccel` + `niri`)**: Interactive SVG curve visualizer and parameter editor for `Linear`, `Natural`, `Synchronous`, and `No accel` modes, `niri` pointer profile controls, and one-click `Use mouse DPI` synced to the active fleet mouse.
- **CLI & Local API (`mousetool.py`)**:
  - `list [--json]`: Discover all configurable and passive generic mice connected to the system.
  - `status [-d SEL | --all] [--json]`: Full hardware readout across one or all connected mice (supports index `1`/`2`, `VID:PID`, `/dev/hidrawN`, driver name, or substring).
  - `battery [-d SEL | --all] [--json]`: Battery percentage and charging state with offline/asleep cache fallback (`~/.cache/mousetool/battery.json`).
  - `dpi [-d SEL] [X [Y]]`, `stages [-d SEL] ...`, `poll [-d SEL] [HZ]`, `lod [-d SEL] ...`, `power [-d SEL] ...`, `tune [-d SEL] [--debounce MS ...]`: Configure one or more mice directly from the terminal.
  - `maccel [--set K=V ...]`: Inspect or update `maccel` kernel parameters and reboot-persistent reset scripts.
  - `serve [--port N]`: Local HTTP API (`127.0.0.1:8766`) serving `/api/status` (with `system_mice`), `/api/devices`, `/api/maccel`, and `/api/niri`.
  - `open`: Launches `index.html` in a WebHID-capable browser (`brave`, `chromium`, or `google-chrome`).
- **Diagnostics (`probe.py`)**: Read-only diagnostic tool that scans all connected HID mice (`Razer`, `Endgame Gear 8k / -we`, `Logitech HID++`, `Pulsar`, `CompX ODM`, `Glorious`, and generic mice) without altering device state.

## Setup

### 1. Desktop launcher & background helper service
Run `install.sh` to install `mousetool.desktop` (`~/.local/share/applications/`) and enable the `mousetool.service` systemd user service (`mousetool.py serve` on `127.0.0.1:8766`):
```bash
./install.sh
# Or also install/update udev rules (prompts for sudo):
./install.sh --udev
```

### 2. Udev rules (for WebHID & non-root hidraw access)
If upgrading from an older version, reinstall `60-mousetool.rules` to pick up all newly supported vendor rules:
```bash
sudo cp 60-mousetool.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### 3. `maccel` write permissions (optional, for Acceleration tab)
If using [`maccel`](https://www.maccel.org), add your user to the `maccel` group and log out/in:
```bash
sudo usermod -aG maccel "$USER"
```

## Usage

### Web Interface
Launch from your app menu (**Mouse Tool**), or run:
```bash
./mousetool.py open
```

### CLI Examples
```bash
# List all connected mice (configurable + generic)
./mousetool.py list

# Full status for all supported connected mice
./mousetool.py status --all

# Target a specific mouse by index, driver, model substring, VID:PID, or hidraw
./mousetool.py status -d op1
./mousetool.py dpi -d viper 1600
./mousetool.py poll -d 3367:1964 4000
./mousetool.py tune -d op1 --motion-sync on --spdt gx_speed
./mousetool.py tune -d pulsar --debounce 2 --motion-sync on

# Battery JSON (for status bars such as Noctalia)
./mousetool.py battery --json

# Inspect or update maccel parameters
./mousetool.py maccel
./mousetool.py maccel --set MODE=natural LIMIT=1.5 DECAY_RATE=0.1
```

## Tests
```bash
python3 -m unittest -v test_mousetool.py
```
