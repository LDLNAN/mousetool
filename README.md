# Mouse Tool

A lightweight web and CLI utility for configuring supported mice on Linux without vendor bloatware.

## Features

- **Web Interface (`index.html`)**: WebHID-powered interface to configure DPI stages, polling rate, lift-off distance, button mappings, and battery level.
- **CLI Helper (`mousetool.py`)**:
  - `battery`: Reads battery level via HID feature reports (formatted for status bars such as Noctalia).
  - `serve`: Local HTTP API service for managing kernel-level mouse acceleration curves via [`maccel`](https://www.maccel.org).
  - `maccel`: Queries current `maccel` driver parameters and state.
- **Diagnostics (`probe.py`)**: Read-only diagnostic tool to inspect mouse HID feature reports directly via `/dev/hidraw*`.
- **Udev Rules (`60-mousetool.rules`)**: Grants logged-in user permissions for USB cable and wireless dongle interfaces.

## Setup

### Udev Rules
Copy the udev rules to permit user-space access to the mouse's hidraw nodes:
```bash
sudo cp 60-mousetool.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### Web Interface
Open `index.html` in a WebHID-compatible browser (such as Brave, Chrome, or Chromium):
```bash
brave --app=file://$(pwd)/index.html
```

### CLI Helper
```bash
# Print battery level
./mousetool.py battery

# Print battery level in JSON format
./mousetool.py battery --json

# Start local maccel server
./mousetool.py serve
```
