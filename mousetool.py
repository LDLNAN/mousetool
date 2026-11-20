#!/usr/bin/env python3
"""Universal multi-mouse configuration & acceleration helper.

  mousetool.py list [--json]                        list all connected mice (HID + Linux input)
  mousetool.py status [--device D] [--all] [--json] inspect mouse state (DPI, polling, LOD, battery, ...)
  mousetool.py battery [--device D] [--all] [--json] print wireless/kernel battery (for status bar widgets)
  mousetool.py dpi [-d D] [X [Y]]                   get or set active DPI
  mousetool.py stages [-d D] [--active N] [--set ...] get or set DPI stages
  mousetool.py poll [-d D] [HZ]                     get or set polling rate
  mousetool.py lod [-d D] [--preset P | --async L R] get or set lift-off distance
  mousetool.py power [-d D] [--idle MIN] [--low PCT] get or set wireless power settings
  mousetool.py tune [-d D] [flags...]               get or set sensor, click & performance filters
  mousetool.py maccel [--set K=V ...]               print or update maccel kernel parameters
  mousetool.py serve [--port N]                     local HTTP API for maccel, desktop settings, and fleet discovery
  mousetool.py open                                 open index.html in a WebHID-capable browser

Supported protocols:
  - Razer 90-byte feature reports (VID 1532: Viper, DeathAdder, Cobra, Basilisk, HyperPolling)
  - Endgame Gear 8k (VID 3367: OP1 8k, XM2 8k, OP1 8k Purple Frost, OP1 8k v2, XM2 8k v2)
  - Endgame Gear -we (VID 3367: OP1we, XM2we)
  - Logitech HID++ 2.0 (VID 046d: dynamic feature discovery for DPI, report rate, battery)
  - Pulsar XS-1 / Pro / Areson / 4K / CompX (VID 3710, 25a7, 3554)
  - CompX Report 0x08 ODM ecosystem (ATK / VXE, Scyrox, Teevolution, VGN F2, Lamzu Atlantis)
  - Glorious (Sinowealth, CORE 2, Classic, Pixart across VIDs 258a, 22d4, 320f, 3794, 093a)
  - Generic Linux fallback via /sys/class/input, /sys/class/hidraw, and /sys/class/power_supply
"""
import argparse
import fcntl
import glob
import json
import math
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import desktop_pointer
from desktop_pointer import NIRI_CONFIG, desktop_state, niri_set, niri_state

# ----------------------------------------------------------------------------- known devices
RAZER_VID = 0x1532
VID = RAZER_VID  # backward-compatible alias

# pid -> (device name, link label, priority: cable 0 < hyperpolling 1 < hyperspeed 2, wireless, hyper, dpi_max)
RAZER_DEVICES = {
    0x00A5: ("Razer Viper V2 Pro", "USB cable", 0, True, False, 30000),
    0x00A6: ("Razer Viper V2 Pro", "HyperSpeed dongle", 2, True, False, 30000),
    0x00B3: ("Razer HyperPolling Dongle", "HyperPolling dongle", 1, True, True, 35000),
    0x00B2: ("Razer DeathAdder V3", "USB cable", 0, False, False, 30000),
    0x00B6: ("Razer DeathAdder V3 Pro", "USB cable", 0, True, False, 35000),
    0x00B7: ("Razer DeathAdder V3 Pro", "HyperSpeed dongle", 2, True, False, 35000),
    0x00C2: ("Razer DeathAdder V3 Pro", "USB cable", 0, True, False, 35000),
    0x00C3: ("Razer DeathAdder V3 Pro", "HyperSpeed dongle", 2, True, False, 35000),
    0x00C4: ("Razer DeathAdder V3 HyperSpeed", "USB cable", 0, True, False, 26000),
    0x00C5: ("Razer DeathAdder V3 HyperSpeed", "HyperSpeed dongle", 2, True, False, 26000),
    0x00B8: ("Razer Viper V3 HyperSpeed", "HyperSpeed dongle", 2, True, False, 30000),
    0x00C0: ("Razer Viper V3 Pro", "USB cable", 0, True, False, 35000),
    0x00C1: ("Razer Viper V3 Pro", "HyperSpeed dongle", 2, True, False, 35000),
    0x00AF: ("Razer Cobra Pro", "USB cable", 0, True, False, 30000),
    0x00B0: ("Razer Cobra Pro", "HyperSpeed dongle", 2, True, False, 30000),
    0x00AA: ("Razer Basilisk V3 Pro", "USB cable", 0, True, False, 30000),
    0x00AB: ("Razer Basilisk V3 Pro", "HyperSpeed dongle", 2, True, False, 30000),
    0x00CC: ("Razer Basilisk V3 Pro 35K", "USB cable", 0, True, False, 35000),
    0x00CD: ("Razer Basilisk V3 Pro 35K", "HyperSpeed dongle", 2, True, False, 35000),
    0x00D6: ("Razer Basilisk V3 Pro 35K", "USB cable", 0, True, False, 35000),
    0x00D7: ("Razer Basilisk V3 Pro 35K", "HyperSpeed dongle", 2, True, False, 35000),
}
DEVICES = RAZER_DEVICES  # backward-compatible alias
PIDS = {pid: info[1] for pid, info in RAZER_DEVICES.items()}

EGG_VID = 0x3367
EGG_DEVICES = {
    0x1964: {
        "name": "Endgame Gear OP1 8k",
        "family": "v1",
        "cpi_min": 50, "cpi_max": 26000, "cpi_step_lo": 50, "cpi_step_hi": 50,
        "lod_options": ["0.7 mm", "1 mm", "2 mm"],
        "lod_glass": False, "angle_tuning": False, "force_max_fps": False, "led_liftoff": False,
        "motion_jitter": True,
    },
    0x1966: {
        "name": "Endgame Gear XM2 8k",
        "family": "v1",
        "cpi_min": 50, "cpi_max": 26000, "cpi_step_lo": 50, "cpi_step_hi": 50,
        "lod_options": ["0.7 mm", "1 mm", "2 mm"],
        "lod_glass": False, "angle_tuning": False, "force_max_fps": False, "led_liftoff": False,
        "motion_jitter": True,
    },
    0x1976: {
        "name": "Endgame Gear OP1 8k Purple Frost",
        "family": "v1",
        "cpi_min": 50, "cpi_max": 30000, "cpi_step_lo": 50, "cpi_step_hi": 50,
        "lod_options": ["0.7 mm", "1 mm", "2 mm"],
        "lod_glass": True, "angle_tuning": False, "force_max_fps": False, "led_liftoff": False,
        "motion_jitter": True,
    },
    0x1978: {
        "name": "Endgame Gear OP1 8k v2",
        "family": "v2",
        "cpi_min": 10, "cpi_max": 30000, "cpi_step_lo": 10, "cpi_step_hi": 50,
        "lod_options": [f"{0.7 + i * 0.1:.1f} mm" for i in range(11)],
        "lod_glass": True, "angle_tuning": True, "force_max_fps": True, "led_liftoff": True,
        "motion_jitter": True,
    },
    0x1980: {
        "name": "Endgame Gear XM2 8k v2",
        "family": "v2",
        "cpi_min": 10, "cpi_max": 30000, "cpi_step_lo": 10, "cpi_step_hi": 50,
        "lod_options": [f"{0.7 + i * 0.1:.1f} mm" for i in range(11)],
        "lod_glass": True, "angle_tuning": True, "force_max_fps": True, "led_liftoff": True,
        "motion_jitter": True,
    },
}
EGG_GLASS_LOD = ["1.0 mm", "2.0 mm"]

# Endgame Gear -we (note: 0x1970 is shared with OP1w 4K v2 dongle which exposes Feature 0xA1)
EGG_WE_DEVICES = {
    0x1960: ("Endgame Gear XM2we", "Wireless dongle", True, 2),
    0x1961: ("Endgame Gear OP1we", "Wireless dongle", True, 2),
    0x1962: ("Endgame Gear OP1we", "USB cable", False, 0),
    0x1968: ("Endgame Gear XM2we", "Wireless dongle", True, 2),
    0x1970: ("Endgame Gear OP1we", "Wireless dongle", True, 2),
    0x1972: ("Endgame Gear OP1we", "USB cable", False, 0),
    0x1982: ("Endgame Gear XM2we", "Wireless dongle", True, 2),
}

LOGI_VID = 0x046D
LOGI_DEVICES = {
    0xC088: ("Logitech G Pro Wireless", "USB cable"),
    0xC08B: ("Logitech G502 Hero", "USB cable"),
    0xC092: ("Logitech G Pro X Superlight", "USB cable"),
    0xC093: ("Logitech G502 Lightspeed", "USB cable"),
    0xC094: ("Logitech G Pro Wireless", "USB cable"),
    0xC095: ("Logitech G305", "USB"),
    0xC099: ("Logitech G Pro X Superlight 2", "USB cable"),
    0xC09D: ("Logitech G502 X Plus", "USB cable"),
    0xC09E: ("Logitech G502 X Lightspeed", "USB cable"),
    0xC52B: ("Logitech Unifying Receiver", "Unifying receiver"),
    0xC539: ("Logitech Lightspeed Receiver", "Lightspeed dongle"),
    0xC53F: ("Logitech Lightspeed Receiver", "Lightspeed dongle"),
    0xC547: ("Logitech Superlight Receiver", "Lightspeed dongle"),
    0xC548: ("Logitech Bolt Receiver", "Bolt receiver"),
    0xC54D: ("Logitech Superlight 2 Receiver", "Lightspeed 2 dongle"),
    0xB023: ("Logitech MX Master 3", "Bluetooth"),
    0xB034: ("Logitech MX Master 3S", "Bluetooth"),
}

PULSAR_VID = 0x3710
PULSAR_XS1_DEVICES = {
    0x3404: ("Pulsar X2A v3", "USB cable", False, 1000),
    0x3409: ("Pulsar XS-1 Mouse", "USB cable", False, 1000),
    0x3410: ("Pulsar XS-1 Mouse", "USB cable", False, 1000),
    0x5402: ("Pulsar X3 M", "Wireless", True, 1000),
    0x5403: ("Pulsar 8K Dongle", "8K dongle", True, 8000),
}
PULSAR_PRO_DEVICES = {
    0x5405: ("Pulsar Pro", "8K dongle", True, 8000),
}

ARESON_VID = 0x25A7
ARESON_DEVICES = {
    0xFA7B: ("Pulsar Areson (X2 v2 / X2H / Xlite v3)", "USB cable", False),
    0xFA7C: ("Pulsar Areson (X2 v2 / X2H / Xlite v3)", "Wireless dongle", True),
}

COMPX_VID = 0x3554
ATK_VID = 0x373B

# Subfamily table for known CompX VID:PID pairs
# kind -> name, link, wireless, dpi_max, dpi_min, dpi_enc, rates, lod_mode
COMPX_DEVICES = {
    # Pulsar 4K Wireless Receiver
    (0x3554, 0xF507): {"brand": "pulsar_4k", "name": "Pulsar 4K Receiver", "link": "4K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000]},
    (0x3554, 0xF508): {"brand": "pulsar_4k", "name": "Pulsar 4K Receiver", "link": "4K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000]},
    (0x3554, 0xF509): {"brand": "pulsar_4k", "name": "Pulsar 4K Receiver", "link": "4K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000]},
    # Scyrox V8 / V8S
    (0x3554, 0xF515): {"brand": "scyrox", "model": "v8s", "name": "Scyrox V8S", "link": "USB cable", "wireless": False, "dpi_max": 50000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF517): {"brand": "scyrox", "model": "v8s", "name": "Scyrox V8S Turbo", "link": "8K dongle", "wireless": True, "dpi_max": 50000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF5F7): {"brand": "scyrox", "model": "v8", "name": "Scyrox V8", "link": "8K dongle", "wireless": True, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    # Teevolution Terra Pro
    (0x3554, 0xF520): {"brand": "teevolution", "name": "Teevolution Terra Pro", "link": "Wireless", "wireless": True, "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF522): {"brand": "teevolution", "name": "Teevolution Terra Pro", "link": "USB cable", "wireless": False, "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF523): {"brand": "teevolution", "name": "Teevolution Terra Pro", "link": "8K dongle", "wireless": True, "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF5BB): {"brand": "teevolution", "name": "Teevolution Terra Pro", "link": "Wireless dongle", "wireless": True, "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    # VGN F2 Master+
    (0x3554, 0xFB56): {"brand": "vgn", "name": "VGN F2 Master+", "link": "8K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xFB57): {"brand": "vgn", "name": "VGN F2 Master+", "link": "USB cable", "wireless": False, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    # Lamzu Atlantis
    (0x3554, 0xF50D): {"brand": "lamzu", "name": "Lamzu Atlantis", "link": "1K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000]},
    (0x3554, 0xF50F): {"brand": "lamzu", "name": "Lamzu Atlantis", "link": "USB cable", "wireless": False, "dpi_max": 26000, "rates": [125, 250, 500, 1000]},
    (0x3554, 0xF510): {"brand": "lamzu", "name": "Lamzu Atlantis 4K", "link": "4K dongle", "wireless": True, "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000]},
    # ATK / VXE
    (0x3554, 0xF58A): {"brand": "atk", "name": "ATK / VXE Mouse", "link": "Wireless dongle", "wireless": True, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF58C): {"brand": "atk", "name": "ATK / VXE Mouse", "link": "USB cable", "wireless": False, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF58E): {"brand": "atk", "name": "ATK / VXE Mouse", "link": "Wireless dongle", "wireless": True, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x3554, 0xF58F): {"brand": "atk", "name": "ATK / VXE Mouse", "link": "8K dongle", "wireless": True, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x373B, 0x1085): {"brand": "atk", "name": "VXE R1 SE+ (Beken)", "link": "1K dongle", "wireless": True, "dpi_max": 18000, "rates": [250, 500, 1000]},
    (0x373B, 0x10C9): {"brand": "atk", "name": "ATK NearLink Mouse", "link": "NearLink dongle", "wireless": True, "dpi_max": 30000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
    (0x373B, 0x11D9): {"brand": "atk", "name": "ATK F1 Ultimate 2.0", "link": "8K dongle", "wireless": True, "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000]},
}

ATK_PRODUCTS = {
    (1, 8):   {"name": "ATK Blazing Sky F1 Ultimate 2.0", "sensor": "PAW3950Ultra", "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000], "max_stages": 6, "is_r1": False},
    (1, 31):  {"name": "ATK A9 Mini+", "sensor": "PAW3955Master", "dpi_max": 50000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000], "max_stages": 6, "is_r1": False, "dpi6": True},
    (1, 52):  {"name": "ATK A9 Mini+", "sensor": "PAW3955Master", "dpi_max": 50000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000], "max_stages": 6, "is_r1": False, "dpi6": True},
    (2, 11):  {"name": "VXE R1", "sensor": "PAW3395", "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000], "max_stages": 4, "is_r1": True},
    (2, 12):  {"name": "VXE R1", "sensor": "PAW3395", "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000], "max_stages": 4, "is_r1": True},
    (2, 27):  {"name": "VXE R1 Pro Max", "sensor": "PAW3395", "dpi_max": 26000, "rates": [125, 250, 500, 1000, 2000, 4000], "max_stages": 4, "is_r1": True},
    (2, 32):  {"name": "VXE R1 SE+", "sensor": "PAW3395SE", "dpi_max": 18000, "rates": [250, 500, 1000], "max_stages": 4, "is_r1": True},
    (2, 39):  {"name": "ATK Blazing Sky X1 Pro Max", "sensor": "PAW3950", "dpi_max": 42000, "rates": [125, 250, 500, 1000, 2000, 4000, 8000], "max_stages": 6, "is_r1": False},
    (2, 83):  {"name": "ATK A9 Plus NearLink", "sensor": "PAW3395", "dpi_max": 26000, "rates": [125, 250, 500, 1000], "max_stages": 6, "is_r1": False},
}

# Glorious registries across 5 VIDs
GLORIOUS_SINOWEALTH = {
    (0x258A, 0x0033): ("Glorious Model D / D-", "USB cable"),
    (0x258A, 0x0036): ("Glorious Model O / O-", "USB cable"),
    (0x3794, 0xA000): ("Glorious Model O Eternal", "USB cable"),
}
GLORIOUS_CORE2 = {
    (0x258A, 0x201B): ("Glorious Model O 2 Pro", "USB cable", False, 8000),
    (0x258A, 0x2035): ("Glorious Model O 2 Pro 4K/8K", "Wireless dongle", True, 4000),
    (0x258A, 0x201C): ("Glorious Model D 2 Pro", "USB cable", False, 8000),
    (0x258A, 0x2036): ("Glorious Model D 2 Pro 4K/8K", "Wireless dongle", True, 4000),
}
GLORIOUS_CLASSIC = {
    (0x258A, 0x2011): {"name": "Glorious Model O Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2012): {"name": "Glorious Model D Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2013): {"name": "Glorious Model O- Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2014): {"name": "Glorious Model D- Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2015): {"name": "Glorious Series One Pro", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2018): {"name": "Glorious Model I 2 Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2019): {"name": "Glorious Model O 2 Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x201A): {"name": "Glorious Model D 2 Wireless", "link": "USB cable", "wireless": True, "gen": "legacy"},
    (0x258A, 0x201D): {"name": "Glorious Model O 2", "link": "USB cable", "wireless": False, "gen": "legacy"},
    (0x258A, 0x2022): {"name": "Glorious Model O Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2023): {"name": "Glorious Model D Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2024): {"name": "Glorious Model O- Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2025): {"name": "Glorious Model D- Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2027): {"name": "Glorious Series One Pro", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2031): {"name": "Glorious Model I 2 Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2033): {"name": "Glorious Model O 2 Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2034): {"name": "Glorious Model D 2 Wireless", "link": "Wireless dongle", "wireless": True, "gen": "legacy"},
    (0x258A, 0x2039): {"name": "Glorious Forge Mouse", "link": "USB / Wireless", "wireless": True, "gen": "legacy"},
    (0x22D4, 0x1503): {"name": "Glorious Model I", "link": "USB cable", "wireless": False, "gen": "legacy"},
    (0x320F, 0x821A): {"name": "Glorious Model O 2", "link": "USB cable", "wireless": False, "gen": "legacy"},
    (0x320F, 0x823A): {"name": "Glorious Model O 2 (v2)", "link": "USB cable", "wireless": False, "gen": "legacy"},
    (0x320F, 0x831A): {"name": "Glorious Model D 2", "link": "USB cable", "wireless": False, "gen": "legacy"},
    (0x3794, 0xA300): {"name": "Glorious Classic (CORE 2)", "link": "USB cable", "wireless": True, "gen": "core2"},
    (0x3794, 0xA312): {"name": "Glorious Classic (CORE 2)", "link": "Wireless dongle", "wireless": True, "gen": "core2"},
}
GLORIOUS_PIXART = {
    (0x093A, 0x821B): ("Glorious Model O 2 Mini", "USB cable", False),
    (0x093A, 0x821D): ("Glorious Model O 2 Mini", "Wireless dongle", True),
    (0x093A, 0x822A): ("Glorious Model O 2 Mini Wireless", "USB cable", True),
    (0x093A, 0x822B): ("Glorious Model O 2 Mini Wireless", "Wireless dongle", True),
    (0x093A, 0x822D): ("Glorious Series 2 Pro", "USB cable", True),
    (0x093A, 0x824A): ("Glorious Series 2 Pro", "Wireless dongle", True),
    (0x093A, 0x824B): ("Glorious Model D 2 Mini", "USB cable", False),
    (0x093A, 0x824D): ("Glorious Model D 2 Mini", "Wireless dongle", True),
    (0x093A, 0x825A): ("Glorious Model D 2 Mini Wireless", "Wireless dongle", True),
    (0x093A, 0x826A): ("Glorious Model O 3", "USB cable", False),
    (0x093A, 0x826B): ("Glorious Model O 3 Wireless", "USB cable", True),
    (0x093A, 0x826D): ("Glorious Model O 3 Wireless", "Wireless dongle", True),
    (0x093A, 0x827A): ("Glorious Model D 3 Wireless", "Wireless dongle", True),
    (0x093A, 0x832A): ("Glorious Pixart Mouse", "USB / Wireless", True),
}

ALL_KNOWN_VENDORS = {
    RAZER_VID: set(RAZER_DEVICES.keys()),
    EGG_VID: set(EGG_DEVICES.keys()) | set(EGG_WE_DEVICES.keys()),
    LOGI_VID: set(LOGI_DEVICES.keys()),
    PULSAR_VID: set(PULSAR_XS1_DEVICES.keys()) | set(PULSAR_PRO_DEVICES.keys()),
    ARESON_VID: set(ARESON_DEVICES.keys()),
}

POLL_CODES = {1: 1000, 2: 500, 4: 250, 8: 125}
POLL_HZ = {hz: code for code, hz in POLL_CODES.items()}
POLL2_CODES = {0x01: 8000, 0x02: 4000, 0x04: 2000, 0x08: 1000, 0x10: 500, 0x20: 250, 0x40: 125}
POLL2_HZ = {hz: code for code, hz in POLL2_CODES.items()}
LOD_PRESETS = {"low": 0, "med": 1, "medium": 1, "high": 2}
LOD_NAMES = {0: "low", 1: "medium", 2: "high"}


def _ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


HIDIOCSFEATURE_91, HIDIOCGFEATURE_91 = _ioc(0x06, 91), _ioc(0x07, 91)
HIDIOCSFEATURE, HIDIOCGFEATURE = HIDIOCSFEATURE_91, HIDIOCGFEATURE_91


def HIDIOCSFEATURE_N(n):
    return _ioc(0x06, n)


def HIDIOCGFEATURE_N(n):
    return _ioc(0x07, n)


class Asleep(Exception):
    pass


# ----------------------------------------------------------------------------- descriptor helpers, state cache & discovery
DEVICE_STATE_CACHE = os.path.expanduser("~/.cache/mousetool/device_state.json")


def _load_device_cache():
    try:
        with open(DEVICE_STATE_CACHE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_device_cache(key, patch):
    data = _load_device_cache()
    cur = data.get(key) or {}
    cur.update(patch)
    cur["updated"] = int(time.time())
    data[key] = cur
    try:
        os.makedirs(os.path.dirname(DEVICE_STATE_CACHE), exist_ok=True)
        with open(DEVICE_STATE_CACHE + ".tmp", "w") as f:
            json.dump(data, f, indent=2)
        os.replace(DEVICE_STATE_CACHE + ".tmp", DEVICE_STATE_CACHE)
    except OSError:
        pass


def _cache_key(meta):
    ident = meta.get("uniq") or meta.get("phys") or meta.get("hidraw") or "default"
    return f"{meta['vid']:04x}:{meta['pid']:04x}:{ident}"


def _parse_hid_collections(rd):
    """Parse a USB HID report descriptor into top-level collections and report byte lengths."""
    cols = []
    stack = []
    usage_page = 0
    report_size = 0
    report_count = 0
    report_id = 0
    usages = []
    depth = 0
    cur_col = None

    def _uval(raw):
        return int.from_bytes(raw, "little")

    i = 0
    n = len(rd)
    while i < n:
        b = rd[i]
        if b == 0xFE:  # Long item
            if i + 2 >= n:
                break
            dlen = rd[i + 1]
            i += 3 + dlen
            continue
        size_code = b & 0x03
        dlen = 4 if size_code == 3 else size_code
        if i + 1 + dlen > n:
            break
        raw = rd[i + 1:i + 1 + dlen]
        item_type = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        val = _uval(raw) if dlen else 0
        i += 1 + dlen

        if item_type == 1:  # Global
            if tag == 0x0:
                usage_page = val
            elif tag == 0x7:
                report_size = val
            elif tag == 0x8:
                report_id = val
            elif tag == 0x9:
                report_count = val
            elif tag == 0xA:
                stack.append((usage_page, report_size, report_count, report_id))
            elif tag == 0xB and stack:
                usage_page, report_size, report_count, report_id = stack.pop()
        elif item_type == 2:  # Local
            if tag == 0x0:
                usages.append(val)
        elif item_type == 0:  # Main
            if tag == 0xA:  # Collection
                if depth == 0:
                    cur_col = {
                        "usage_page": usage_page,
                        "usage": usages[0] if usages else 0,
                        "in_bits": {},
                        "out_bits": {},
                        "feat_bits": {},
                    }
                    cols.append(cur_col)
                depth += 1
                usages.clear()
            elif tag == 0xC:  # End Collection
                if depth > 0:
                    depth -= 1
                if depth == 0:
                    cur_col = None
                usages.clear()
            elif tag in (0x8, 0x9, 0xB):
                if cur_col is not None and report_size and report_count:
                    bucket = {0x8: "in_bits", 0x9: "out_bits", 0xB: "feat_bits"}[tag]
                    cur_col[bucket][report_id] = cur_col[bucket].get(report_id, 0) + report_size * report_count
                usages.clear()
            else:
                usages.clear()

    out = []
    for c in cols:
        out.append({
            "usage_page": c["usage_page"],
            "usage": c["usage"],
            "in_lens": {rid: (bits + 7) // 8 for rid, bits in c["in_bits"].items()},
            "out_lens": {rid: (bits + 7) // 8 for rid, bits in c["out_bits"].items()},
            "feat_lens": {rid: (bits + 7) // 8 for rid, bits in c["feat_bits"].items()},
        })
    return out


def _cols_has(cols, page=None, usage=None, feat_id=None, in_id=None, out_id=None):
    for c in cols:
        if page is not None and c["usage_page"] != page:
            continue
        if usage is not None and c["usage"] != usage:
            continue
        if feat_id is not None and feat_id not in c["feat_lens"]:
            continue
        if in_id is not None and in_id not in c["in_lens"]:
            continue
        if out_id is not None and out_id not in c["out_lens"]:
            continue
        return c
    return None


def _parse_hid_id(uevent_text):
    for line in uevent_text.splitlines():
        if line.startswith("HID_ID="):
            _, v, p = line.split("=", 1)[1].split(":")
            return int(v, 16), int(p, 16)
    return None, None


def _uevent_dict(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _is_razer_control(rd, cols=None):
    return b"\x95\x5a\xb1" in rd or any(90 in c["feat_lens"].values() for c in (cols or ()))


def _is_egg_control(rd, cols=None):
    if cols and _cols_has(cols, page=0xFF01, feat_id=0xA0) and _cols_has(cols, page=0xFF01, feat_id=0xA1):
        return True
    return b"\x06\x01\xff" in rd and b"\x85\xa0" in rd and b"\x85\xa1" in rd


def _is_logitech_hidpp(rd, cols=None):
    if cols:
        for c in cols:
            if c["usage_page"] in (0xFF00, 0xFF43) and (0x10 in c["out_lens"] or 0x11 in c["out_lens"]):
                return True
    return (b"\x06\x00\xff" in rd or b"\x06\x43\xff" in rd) and (b"\x85\x10" in rd or b"\x85\x11" in rd)


def _read_text(path, default=""):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return f.read().strip()


def _sysfs_batteries():
    """Return list of kernel power_supply device batteries."""
    items = []
    for d in sorted(glob.glob("/sys/class/power_supply/*")):
        try:
            scope = _read_text(f"{d}/scope")
            if scope.lower() != "device":
                continue
            cap_s = _read_text(f"{d}/capacity")
            if not cap_s:
                continue
            cap = int(cap_s)
            status = _read_text(f"{d}/status")
            model = _read_text(f"{d}/model_name") or os.path.basename(d)
            serial = _read_text(f"{d}/serial_number")
            items.append({
                "name": model,
                "percent": cap,
                "charging": status.lower() == "charging",
                "status": status,
                "serial": serial.lower(),
                "path": d,
            })
        except (OSError, ValueError):
            continue
    return items


def _phys_base(phys):
    # Strip trailing /inputN or USB interface suffix (.0 / .1 / .2) so sibling interfaces of one mouse share base
    p = re.sub(r"/input\d+$", "", phys or "")
    return p


def _usb_dev_phys(phys):
    # More general physical device path stripping interface suffix for split-endpoint matching
    p = _phys_base(phys)
    return re.sub(r":\d+\.\d+$", "", p)


def discover_mice():
    """Return list of configurable mice dicts sorted deterministically (one per physical mouse)."""
    raw_nodes = []
    egg_a1_vps = set()
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        ue = _uevent_dict(f"{d}/device/uevent")
        hid_id = ue.get("HID_ID", "")
        if ":" not in hid_id:
            continue
        try:
            bus_s, v_s, p_s = hid_id.split(":")
            vid, pid = int(v_s, 16), int(p_s, 16)
        except ValueError:
            continue
        try:
            with open(f"{d}/device/report_descriptor", "rb") as f:
                rd = f.read()
        except OSError:
            continue
        cols = _parse_hid_collections(rd)
        if vid == EGG_VID and (_cols_has(cols, feat_id=0xA1) or b"\x85\xa1" in rd):
            egg_a1_vps.add((vid, pid))
        raw_nodes.append({
            "node": d,
            "devpath": "/dev/" + os.path.basename(d),
            "bus": "bluetooth" if int(bus_s, 16) == 5 else "usb",
            "vid": vid,
            "pid": pid,
            "name": ue.get("HID_NAME", "").strip() or f"{vid:04x}:{pid:04x}",
            "phys": _phys_base(ue.get("HID_PHYS", "")),
            "usb_phys": _usb_dev_phys(ue.get("HID_PHYS", "")),
            "uniq": ue.get("HID_UNIQ", ""),
            "rd": rd,
            "cols": cols,
        })

    found = []
    by_split_key = {}  # (driver, vid, pid, usb_phys) -> entry for split cmd/notify nodes
    seen_phys = set()

    for nd in raw_nodes:
        vid, pid, rd, cols = nd["vid"], nd["pid"], nd["rd"], nd["cols"]
        devpath, name, phys, usb_phys, uniq = nd["devpath"], nd["name"], nd["phys"], nd["usb_phys"], nd["uniq"]
        entry = None
        split_notify_of = None

        if vid == RAZER_VID and _is_razer_control(rd, cols):
            if pid in RAZER_DEVICES:
                mname, link, prio, wireless, hyper, dpi_max = RAZER_DEVICES[pid]
            else:
                mname = name or f"Razer Mouse (0x{pid:04x})"
                link, prio, wireless, hyper, dpi_max = "USB / Wireless", 5, True, False, 35000
            entry = {
                "id": f"razer-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                "driver": "razer", "vid": vid, "pid": pid,
                "name": mname, "link": link, "prio": prio,
                "wireless": wireless, "hyper": hyper, "dpi_max": dpi_max,
                "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
            }
        elif vid == EGG_VID and _is_egg_control(rd, cols):
            prof = EGG_DEVICES.get(pid, {
                "name": name or f"Endgame Gear 8k (0x{pid:04x})",
                "family": "v1",
                "cpi_min": 50, "cpi_max": 26000, "cpi_step_lo": 50, "cpi_step_hi": 50,
                "lod_options": ["0.7 mm", "1 mm", "2 mm"],
                "lod_glass": False, "angle_tuning": False, "force_max_fps": False, "led_liftoff": False,
                "motion_jitter": True,
            })
            entry = {
                "id": f"egg-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                "driver": "egg8k", "vid": vid, "pid": pid,
                "name": prof["name"], "link": "USB 8K", "prio": 0,
                "wireless": False, "hyper": True, "dpi_max": prof["cpi_max"],
                "profile": prof, "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": "usb",
            }
        elif vid == EGG_VID and pid in EGG_WE_DEVICES and (vid, pid) not in egg_a1_vps:
            has_feat8 = _cols_has(cols, page=0xFF02, feat_id=0x08) or _cols_has(cols, feat_id=0x08)
            has_in9 = _cols_has(cols, page=0xFF01, in_id=0x09) or _cols_has(cols, in_id=0x09)
            mname, link, wireless, prio = EGG_WE_DEVICES[pid]
            if has_feat8:
                entry = {
                    "id": f"egg_we-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "egg_we", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": prio,
                    "wireless": wireless, "hyper": False, "dpi_max": 12800, "dpi_min": 50,
                    "hidraw": devpath, "notify_hidraw": devpath if has_in9 else None,
                    "phys": phys, "usb_phys": usb_phys, "uniq": uniq, "bus": nd["bus"],
                }
            elif has_in9:
                split_notify_of = ("egg_we", vid, pid, usb_phys or devpath, devpath)
        elif vid == LOGI_VID and _is_logitech_hidpp(rd, cols):
            mname, link = LOGI_DEVICES.get(pid, (name or f"Logitech HID++ (0x{pid:04x})", "HID++"))
            entry = {
                "id": f"hidpp-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                "driver": "hidpp", "vid": vid, "pid": pid,
                "name": mname, "link": link,
                "prio": 3 if "receiver" in link.lower() or "dongle" in link.lower() else 1,
                "wireless": True, "hyper": False, "dpi_max": 32000,
                "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
            }
        elif vid == PULSAR_VID:
            if pid in PULSAR_PRO_DEVICES and _cols_has(cols, page=0x0001, usage=0x0000, in_id=0, out_id=0):
                mname, link, wireless, max_poll = PULSAR_PRO_DEVICES[pid]
                entry = {
                    "id": f"pulsar_pro-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "pulsar_pro", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": 2,
                    "wireless": wireless, "hyper": max_poll > 1000, "max_poll": max_poll,
                    "dpi_min": 50, "dpi_max": 32000,
                    "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                }
            elif _cols_has(cols, page=0xFFFF, usage=0x0001, feat_id=0) and not _cols_has(cols, in_id=0x08):
                feat_c = _cols_has(cols, page=0xFFFF, usage=0x0001, feat_id=0)
                if feat_c["feat_lens"].get(0, 0) >= 64:
                    mname, link, wireless, max_poll = PULSAR_XS1_DEVICES.get(
                        pid, (name or f"Pulsar XS-1 (0x{pid:04x})", "USB / Wireless", True, 8000 if "8k" in name.lower() else 1000)
                    )
                    entry = {
                        "id": f"pulsar_xs1-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                        "driver": "pulsar_xs1", "vid": vid, "pid": pid,
                        "name": mname, "link": link, "prio": 1 if not wireless else 2,
                        "wireless": wireless, "hyper": max_poll > 1000, "max_poll": max_poll,
                        "dpi_min": 50, "dpi_max": 26000,
                        "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                    }
            elif _cols_has(cols, in_id=0x08, out_id=0x08):
                entry = {
                    "id": f"compx-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "compx", "brand": "pulsar_compx", "vid": vid, "pid": pid,
                    "name": name or f"Pulsar CompX (0x{pid:04x})",
                    "link": "Dongle" if "dongle" in name.lower() else "USB / Wireless",
                    "prio": 2, "wireless": True, "hyper": True, "dpi_min": 10, "dpi_max": 32000,
                    "rates": [125, 250, 500, 1000, 2000, 4000, 8000],
                    "hidraw": devpath, "notify_hidraw": devpath,
                    "phys": phys, "usb_phys": usb_phys, "uniq": uniq, "bus": nd["bus"],
                }
        elif vid == ARESON_VID and pid in ARESON_DEVICES and not _cols_has(cols, page=0xFFFF, usage=0x0002):
            mname, link, wireless = ARESON_DEVICES[pid]
            has_feat8 = _cols_has(cols, feat_id=0x08)
            has_in9 = _cols_has(cols, in_id=0x09)
            if has_feat8:
                entry = {
                    "id": f"compx-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "compx", "brand": "areson", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": 2 if wireless else 0,
                    "wireless": wireless, "hyper": False, "dpi_min": 50, "dpi_max": 20000,
                    "rates": [125, 250, 500, 1000],
                    "hidraw": devpath, "notify_hidraw": devpath if has_in9 else None,
                    "phys": phys, "usb_phys": usb_phys, "uniq": uniq, "bus": nd["bus"],
                }
            elif has_in9:
                split_notify_of = ("compx", vid, pid, usb_phys or devpath, devpath)
        elif (vid, pid) in COMPX_DEVICES or (vid in (COMPX_VID, ATK_VID) and _cols_has(cols, page=0xFF02, usage=0x0002)):
            if _cols_has(cols, out_id=0x08) or _cols_has(cols, in_id=0x08):
                spec = COMPX_DEVICES.get((vid, pid), {
                    "brand": "atk" if vid == ATK_VID else "compx",
                    "name": name or f"CompX Mouse ({vid:04x}:{pid:04x})",
                    "link": "Dongle" if "dongle" in name.lower() else "USB / Wireless",
                    "wireless": True, "dpi_max": 30000,
                    "rates": [125, 250, 500, 1000, 2000, 4000, 8000],
                })
                in_c = _cols_has(cols, in_id=0x08)
                entry = {
                    "id": f"compx-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "compx", "brand": spec["brand"], "model": spec.get("model"),
                    "vid": vid, "pid": pid,
                    "name": spec["name"], "link": spec["link"],
                    "prio": 2 if spec.get("wireless") else 0,
                    "wireless": bool(spec.get("wireless", True)),
                    "hyper": max(spec.get("rates", [1000])) > 1000,
                    "dpi_min": 50, "dpi_max": spec.get("dpi_max", 30000),
                    "rates": spec.get("rates", [125, 250, 500, 1000, 2000, 4000, 8000]),
                    "hidraw": devpath, "notify_hidraw": devpath if in_c else None,
                    "phys": phys, "usb_phys": usb_phys, "uniq": uniq, "bus": nd["bus"],
                }
        elif (vid, pid) in GLORIOUS_SINOWEALTH:
            c = _cols_has(cols, page=0xFF00, feat_id=0x05)
            if c and (0x04 in c["feat_lens"] or 0x06 in c["feat_lens"]):
                mname, link = GLORIOUS_SINOWEALTH[(vid, pid)]
                cfg_rid = 0x06 if 0x06 in c["feat_lens"] else 0x04
                entry = {
                    "id": f"glorious_sino-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "glorious_sinowealth", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": 1,
                    "wireless": False, "hyper": False, "dpi_min": 100, "dpi_max": 16000,
                    "cfg_rid": cfg_rid, "cfg_len": c["feat_lens"][cfg_rid],
                    "cmd_len": c["feat_lens"].get(0x05, 6),
                    "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                }
        elif (vid, pid) in GLORIOUS_CORE2:
            c = _cols_has(cols, page=0xFFFF, feat_id=0)
            if c and c["feat_lens"].get(0, 0) >= 64:
                mname, link, wireless, max_poll = GLORIOUS_CORE2[(vid, pid)]
                entry = {
                    "id": f"glorious_core2-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "glorious_core2", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": 2 if wireless else 0,
                    "wireless": wireless, "hyper": max_poll > 1000, "max_poll": max_poll,
                    "dpi_min": 50, "dpi_max": 26000,
                    "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                }
        elif (vid, pid) in GLORIOUS_CLASSIC:
            for c in cols:
                if c["usage_page"] in (0xFF01, 0xFF00, 0xFFFF) and c["feat_lens"]:
                    rid, flen = next(iter(c["feat_lens"].items()))
                    spec = GLORIOUS_CLASSIC[(vid, pid)]
                    entry = {
                        "id": f"glorious_classic-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                        "driver": "glorious_classic", "vid": vid, "pid": pid,
                        "name": spec["name"], "link": spec["link"], "prio": 2 if spec["wireless"] else 0,
                        "wireless": spec["wireless"], "hyper": False,
                        "dpi_min": 100, "dpi_max": 19000,
                        "gen": spec["gen"], "feat_rid": rid, "feat_len": flen,
                        "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                    }
                    break
        elif (vid, pid) in GLORIOUS_PIXART:
            c = _cols_has(cols, page=0xFF00, feat_id=0x03)
            if c:
                mname, link, wireless = GLORIOUS_PIXART[(vid, pid)]
                entry = {
                    "id": f"glorious_pixart-{vid:04x}:{pid:04x}-{os.path.basename(devpath)}",
                    "driver": "glorious_pixart", "vid": vid, "pid": pid,
                    "name": mname, "link": link, "prio": 2 if wireless else 0,
                    "wireless": wireless, "hyper": False, "dpi_min": 100, "dpi_max": 26000,
                    "feat_len": c["feat_lens"][0x03],
                    "hidraw": devpath, "phys": phys, "uniq": uniq, "bus": nd["bus"],
                }

        if split_notify_of is not None:
            skey = split_notify_of[:4]
            if skey in by_split_key:
                by_split_key[skey]["notify_hidraw"] = split_notify_of[4]
            else:
                by_split_key[("pending_notify",) + skey] = split_notify_of[4]
            continue

        if entry is not None:
            # Merge split cmd + notify endpoints on the SAME USB device path, never different physical mice
            skey = (entry["driver"], vid, pid, usb_phys or devpath)
            pending_key = ("pending_notify",) + skey
            if pending_key in by_split_key and not entry.get("notify_hidraw"):
                entry["notify_hidraw"] = by_split_key.pop(pending_key)
            key = (entry["driver"], vid, pid, phys or devpath)
            if key in seen_phys:
                continue
            seen_phys.add(key)
            by_split_key[skey] = entry
            found.append(entry)

    found.sort(key=lambda m: (m["prio"], m["driver"], m["hidraw"]))
    return found


def discover_system_mice():
    """Passive sysfs scan of ALL Linux mice (never opens hidraw, safe while WebHID runs)."""
    configurable = discover_mice()
    by_phys = {m["phys"]: m for m in configurable if m.get("phys")}
    by_usb_phys = {m["usb_phys"]: m for m in configurable if m.get("usb_phys")}
    remaining_vp = {}
    for m in configurable:
        remaining_vp.setdefault((m["vid"], m["pid"]), []).append(m)
    used_cfg_ids = set()
    batteries = _sysfs_batteries()
    mice = []
    seen_phys = set()

    for d in sorted(glob.glob("/sys/class/input/mouse*")):
        dev_dir = f"{d}/device"
        name_path = f"{dev_dir}/name"
        if not os.path.exists(name_path):
            continue
        try:
            name = _read_text(name_path)
        except OSError:
            continue
        if "libvirtualhid" in name.lower():
            continue
        ue = _uevent_dict(f"{dev_dir}/uevent")
        prod = ue.get("PRODUCT", "")
        phys = _phys_base(ue.get("PHYS", ""))
        usb_phys = _usb_dev_phys(ue.get("PHYS", ""))
        uniq = ue.get("UNIQ", "").lower()
        vid = pid = None
        parts = prod.split("/")
        if len(parts) >= 3:
            try:
                vid, pid = int(parts[1], 16), int(parts[2], 16)
            except ValueError:
                pass
        if phys and phys in seen_phys:
            continue
        if phys:
            seen_phys.add(phys)

        cfg = by_phys.get(phys) or by_usb_phys.get(usb_phys)
        if cfg and cfg["id"] in used_cfg_ids:
            cfg = None
        if cfg is None and vid is not None:
            lst = remaining_vp.get((vid, pid)) or []
            while lst and lst[0]["id"] in used_cfg_ids:
                lst.pop(0)
            if lst:
                cfg = lst.pop(0)
        if cfg:
            used_cfg_ids.add(cfg["id"])

        batt = None
        for b in batteries:
            if uniq and b["serial"] and uniq == b["serial"]:
                batt = b
                break
            if b["name"] and (b["name"].lower() in name.lower() or name.lower() in b["name"].lower()):
                batt = b
                break

        mice.append({
            "name": cfg["name"] if cfg else name,
            "vid": f"0x{vid:04x}" if vid is not None else None,
            "pid": f"0x{pid:04x}" if pid is not None else None,
            "driver": cfg["driver"] if cfg else "generic",
            "configurable": cfg is not None,
            "id": cfg["id"] if cfg else f"sys-{os.path.basename(d)}",
            "hidraw": cfg["hidraw"] if cfg else None,
            "link": cfg["link"] if cfg else ("Bluetooth" if prod.startswith("5/") else "USB / HID"),
            "phys": phys,
            "battery_percent": batt["percent"] if batt else None,
            "charging": batt["charging"] if batt else None,
        })

    # Include any configurable dongle/mouse not yet matched to an input/mouseN node
    for cfg in configurable:
        if cfg["id"] in used_cfg_ids:
            continue
        mice.append({
            "name": cfg["name"],
            "vid": f"0x{cfg['vid']:04x}",
            "pid": f"0x{cfg['pid']:04x}",
            "driver": cfg["driver"],
            "configurable": True,
            "id": cfg["id"],
            "hidraw": cfg["hidraw"],
            "link": cfg["link"],
            "phys": cfg.get("phys"),
            "battery_percent": None,
            "charging": None,
        })
    return mice


def select_mice(selector=None, all_devices=False, driver=None):
    """Resolve user selector (--device / --all) against discovered configurable mice."""
    mice = discover_mice()
    if driver:
        if isinstance(driver, (list, tuple, set)):
            mice = [m for m in mice if m["driver"] in driver]
        else:
            mice = [m for m in mice if m["driver"] == driver]
    if not mice:
        raise FileNotFoundError("no supported mouse found")
    if all_devices or selector is None:
        return mice if all_devices else [mice[0]]

    s = selector.strip()
    if s.isdigit():
        idx = int(s) - 1
        if 0 <= idx < len(mice):
            return [mice[idx]]
        raise FileNotFoundError(f"device index {selector!r} out of range (1..{len(mice)})")

    sl = s.lower()
    matches = []
    for m in mice:
        vp = f"{m['vid']:04x}:{m['pid']:04x}"
        if (sl == m["id"].lower() or sl == m["hidraw"].lower()
                or sl == os.path.basename(m["hidraw"]).lower()
                or sl == vp or sl == f"0x{m['pid']:04x}"):
            return [m]
        if sl in m["name"].lower() or sl in m["id"].lower() or sl in m["driver"].lower():
            matches.append(m)
    if not matches:
        raise FileNotFoundError(f"no mouse matching {selector!r}")
    return [matches[0]]


# backward-compatible single-device helpers
def find_mouse():
    """Return (hidraw path, pid) for the highest-priority Razer mouse control interface."""
    razer = [m for m in discover_mice() if m["driver"] == "razer"]
    if not razer:
        raise FileNotFoundError("mouse not found")
    return razer[0]["hidraw"], razer[0]["pid"]


# ----------------------------------------------------------------------------- razer protocol
def _build(cls, cmd, size, args=(), txn=0x1F):
    r = bytearray(90)
    r[1], r[5], r[6], r[7] = txn, size, cls, cmd
    r[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return r


def xfer(fd, cls, cmd, size, args=(), txn=0x1F):
    """One Razer 90-byte feature report exchange."""
    last = None
    for _ in range(3):
        fcntl.ioctl(fd, HIDIOCSFEATURE, bytes([0]) + _build(cls, cmd, size, args, txn))
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.4:
            time.sleep(0.008)
            buf = bytearray(91)
            fcntl.ioctl(fd, HIDIOCGFEATURE, buf)
            r = buf[1:]
            if (r[6], r[7]) != (cls, cmd) or r[0] < 2:
                continue
            last = r[0]
            if r[0] == 2:
                return r[8:]
            if r[0] == 5:
                raise OSError("command unsupported by firmware")
            if r[0] == 3:
                raise OSError("mouse rejected command")
            break
        time.sleep(0.12)
    if last == 4:
        raise Asleep("mouse didn't answer (asleep or out of range)")
    raise OSError(f"no reply (status {last})")


def open_mouse():
    path, pid = find_mouse()
    return os.open(path, os.O_RDWR), path, pid


def get_firmware(fd):
    a = xfer(fd, 0x00, 0x81, 0x02)
    return f"v{a[0]}.{a[1]}"


def get_serial(fd):
    a = xfer(fd, 0x00, 0x82, 0x16)
    return bytes(a[:22]).split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def get_dpi(fd):
    a = xfer(fd, 0x04, 0x85, 0x07, [0x00])
    return (a[1] << 8 | a[2], a[3] << 8 | a[4])


def set_dpi(fd, x, y, dpi_max=35000):
    if not (100 <= x <= dpi_max and 100 <= y <= dpi_max):
        raise ValueError(f"DPI must be between 100 and {dpi_max}")
    xfer(fd, 0x04, 0x05, 0x07, [0x01, x >> 8, x & 255, y >> 8, y & 255, 0, 0])


def get_stages(fd):
    a = xfer(fd, 0x04, 0x86, 0x26, [0x01])
    active, n = a[1], min(a[2], 5)
    stages = []
    for i in range(n):
        o = 3 + i * 7
        stages.append((a[o + 1] << 8 | a[o + 2], a[o + 3] << 8 | a[o + 4]))
    return {"active": active, "stages": stages}


def set_stages(fd, active, stages, dpi_max=35000):
    if not (1 <= len(stages) <= 5):
        raise ValueError("must have between 1 and 5 stages")
    if not (1 <= active <= len(stages)):
        raise ValueError(f"active stage must be between 1 and {len(stages)}")
    args = [0x01, active, len(stages)]
    for i, (x, y) in enumerate(stages, 1):
        if not (100 <= x <= dpi_max and 100 <= y <= dpi_max):
            raise ValueError(f"stage {i} DPI must be between 100 and {dpi_max}")
        args.extend([i, x >> 8, x & 255, y >> 8, y & 255, 0, 0])
    xfer(fd, 0x04, 0x06, 0x26, args)


def get_poll(fd, hyper=False):
    if hyper:
        return POLL2_CODES.get(xfer(fd, 0x00, 0xC0, 0x01)[1])
    return POLL_CODES.get(xfer(fd, 0x00, 0x85, 0x01)[0])


def set_poll(fd, hz, hyper=False):
    if hyper:
        if hz not in POLL2_HZ:
            raise ValueError(f"unsupported polling rate {hz}; valid: {sorted(POLL2_HZ)}")
        xfer(fd, 0x00, 0x40, 0x02, [0x00, POLL2_HZ[hz]])
        try:
            xfer(fd, 0x00, 0x40, 0x02, [0x01, POLL2_HZ[hz]])
        except (OSError, Asleep):
            pass
    else:
        if hz not in (125, 500, 1000):
            raise ValueError("unsupported polling rate on this link; valid: 125, 500, 1000")
        xfer(fd, 0x00, 0x05, 0x01, [POLL_HZ[hz]])


def get_lod(fd):
    for _ in range(3):
        a = xfer(fd, 0x0B, 0x85, 0x01)
        if a[2] == 0x01 and a[3] <= 2:
            return {"mode": "preset", "level": LOD_NAMES[a[3]]}
        if a[2] == 0x04:
            return {"mode": "async", "lift": a[4] + 1, "land": a[5] + 1}
        time.sleep(0.06)
    return None


def set_lod_preset(fd, level):
    if isinstance(level, str):
        key = level.lower()
        if key not in LOD_PRESETS:
            raise ValueError("LOD preset must be low, medium, or high")
        level = LOD_PRESETS[key]
    xfer(fd, 0x0B, 0x03, 0x03, [0x00, 0x04, 0x28])
    xfer(fd, 0x0B, 0x0B, 0x04, [0x00, 0x04, 0x01, level])


def set_lod_async(fd, lift, land):
    if not (2 <= lift <= 26 and 1 <= land <= 25 and lift > land):
        raise ValueError("async LOD requires 2 <= lift <= 26, 1 <= land <= 25, and lift > land")
    xfer(fd, 0x0B, 0x03, 0x03, [0x00, 0x04, 0x01])
    xfer(fd, 0x0B, 0x0B, 0x04, [0x00, 0x04, 0x04, 0x00])
    xfer(fd, 0x0B, 0x05, 0x04, [0x00, 0x04, lift - 1, land - 1])


def get_idle(fd):
    a = xfer(fd, 0x07, 0x83, 0x02)
    return a[0] << 8 | a[1]


def set_idle(fd, sec):
    if not (60 <= sec <= 900):
        raise ValueError("idle timer must be between 60 and 900 seconds (1–15 min)")
    xfer(fd, 0x07, 0x03, 0x02, [sec >> 8, sec & 255])


def get_low(fd):
    return round(xfer(fd, 0x07, 0x81, 0x01)[0] / 255 * 100)


def set_low(fd, pct):
    if not (5 <= pct <= 100):
        raise ValueError("low battery threshold must be between 5 and 100%")
    raw = max(0x0C, min(0xFF, round(pct / 100 * 255)))
    xfer(fd, 0x07, 0x01, 0x01, [raw])


# ----------------------------------------------------------------------------- endgame gear 8k protocol
EGG_CFG_LEN = 1041
EGG_CMD_LEN = 64
EGG_POLL_RATES = [1000, 2000, 4000, 8000]


def egg_clamp_cpi(profile, val):
    lo, hi = profile["cpi_min"], profile["cpi_max"]
    v = max(lo, min(hi, int(round(val))))
    step = profile["cpi_step_lo"] if v <= 10000 else profile["cpi_step_hi"]
    q = ((v + step // 2) // step) * step
    return max(lo, min(hi, q))


def egg_format_version(buf):
    # Hardware verified: OP1 8k v1.23 -> buf[17]=0x23, buf[18]=0x01 (BCD)
    # OP1 8k v2 V1.07 -> buf[17]=0x07, buf[18]=0x01
    for off in (17, 16, 18, 2, 4, 6):
        if off + 1 >= len(buf):
            continue
        patch_b, major_b = buf[off], buf[off + 1]
        hi_n, lo_n = patch_b >> 4, patch_b & 0xF
        if 1 <= major_b <= 9 and hi_n <= 9 and lo_n <= 9:
            return f"v{major_b}.{hi_n}{lo_n}"
    for off in (17, 16, 18, 2, 4, 6):
        if off + 1 >= len(buf):
            continue
        v = buf[off] | (buf[off + 1] << 8)
        if 50 <= v <= 5000:
            return f"v{v / 100:.2f}"
    return None


def egg_blob_ok(buf):
    if len(buf) < 131 or buf[1] != 0x01:
        return False
    stages = buf[30]
    active = buf[29]
    div = buf[21]
    cpi1 = buf[52] | (buf[53] << 8)
    return 1 <= stages <= 4 and active <= 3 and 1 <= div <= 64 and 10 <= cpi1 <= 30000


def egg_parse_config(blob, profile=None):
    b = blob
    n = min(max(b[30], 1), 4)
    active0 = min(b[29], n - 1)
    stages = []
    for i in range(4):
        o = 51 + i * 5
        stages.append((b[o + 1] | (b[o + 2] << 8), b[o + 3] | (b[o + 4] << 8)))
    div = b[21] or 1
    hz = round(8000 / div) if 8000 % div == 0 else None
    glass = bool(b[127]) if (profile and profile.get("lod_glass")) else False
    lod_list = EGG_GLASS_LOD if glass else (profile["lod_options"] if profile else ["0.7 mm", "1 mm", "2 mm"])
    lod_idx = b[25]
    lod_label = lod_list[lod_idx] if 0 <= lod_idx < len(lod_list) else f"index {lod_idx}"
    ho = b[71:77]
    left_handed = not (ho[0] == 0 and ho[1] == 1 and not any(ho[2:6])) and any(ho)
    spdt_map = {0xF0: "gx_safe", 0xF1: "gx_speed"}
    buttons = []
    for i in range(7):
        o = 77 + i * 7
        ctl = b[o]
        buttons.append({
            "ctl": ctl,
            "spdt": spdt_map.get(ctl, "off") if i < 2 else None,
            "multiclick": None if ctl >= 0xF0 else ctl,
            "type": b[o + 1],
            "params": list(b[o + 2:o + 7]),
        })
    angle_raw = b[128]
    angle_tune = angle_raw - 256 if angle_raw > 127 else angle_raw
    return {
        "polling_hz": hz,
        "polling_divider": div,
        "active": active0 + 1,
        "stages": stages[:n],
        "all_stages": stages,
        "dpi": stages[active0],
        "lod": {"mode": "index", "index": lod_idx, "label": lod_label},
        "slamclick": bool(b[22] & 0x01),
        "motion_jitter": bool(b[22] & 0x10),
        "angle_snapping": bool(b[26]),
        "ripple_control": bool(b[27]),
        "motion_sync": bool(b[28]),
        "glass_mode": glass,
        "angle_tuning": angle_tune if (profile and profile.get("angle_tuning")) else None,
        "force_max_fps": bool(b[129]) if (profile and profile.get("force_max_fps")) else None,
        "led_liftoff_disabled": (b[24] == 0) if (profile and profile.get("led_liftoff")) else None,
        "left_handed": left_handed,
        "buttons": buttons,
    }


def egg_serialize_config(base, updates, profile):
    out = bytearray(EGG_CFG_LEN)
    n = min(len(base), EGG_CFG_LEN)
    out[:n] = base[:n]
    out[0] = 0xA0
    out[1] = 0x11  # store op

    if "polling_hz" in updates and updates["polling_hz"] is not None:
        hz = int(updates["polling_hz"])
        if hz not in (125, 250, 500, 1000, 2000, 4000, 8000):
            raise ValueError("polling rate must be 1000, 2000, 4000, or 8000 Hz (or 125/250/500)")
        out[21] = 8000 // hz
    if "slamclick" in updates and updates["slamclick"] is not None:
        out[22] = (out[22] | 0x01) if updates["slamclick"] else (out[22] & ~0x01)
    if "motion_jitter" in updates and updates["motion_jitter"] is not None:
        out[22] = (out[22] | 0x10) if updates["motion_jitter"] else (out[22] & ~0x10)
    if "lod_index" in updates and updates["lod_index"] is not None:
        out[25] = int(updates["lod_index"]) & 0xFF
    if "angle_snapping" in updates and updates["angle_snapping"] is not None:
        out[26] = 1 if updates["angle_snapping"] else 0
    if "ripple_control" in updates and updates["ripple_control"] is not None:
        out[27] = 1 if updates["ripple_control"] else 0
    if "motion_sync" in updates and updates["motion_sync"] is not None:
        out[28] = 1 if updates["motion_sync"] else 0

    if "stages" in updates and updates["stages"] is not None:
        st = updates["stages"]
        if not (1 <= len(st) <= 4):
            raise ValueError("Endgame Gear 8k mice support 1 to 4 DPI stages")
        out[30] = len(st)
        for i, (x, y) in enumerate(st):
            cx, cy = egg_clamp_cpi(profile, x), egg_clamp_cpi(profile, y)
            o = 51 + i * 5
            out[o] = 1 if cx != cy else 0
            out[o + 1] = cx & 0xFF
            out[o + 2] = (cx >> 8) & 0xFF
            out[o + 3] = cy & 0xFF
            out[o + 4] = (cy >> 8) & 0xFF

    if "active" in updates and updates["active"] is not None:
        act = int(updates["active"])
        cnt = max(1, min(4, out[30]))
        if not (1 <= act <= cnt):
            raise ValueError(f"active stage must be between 1 and {cnt}")
        out[29] = act - 1

    if "dpi" in updates and updates["dpi"] is not None:
        dx, dy = updates["dpi"]
        cx, cy = egg_clamp_cpi(profile, dx), egg_clamp_cpi(profile, dy)
        act0 = min(max(out[29], 0), 3)
        o = 51 + act0 * 5
        out[o] = 1 if cx != cy else 0
        out[o + 1] = cx & 0xFF
        out[o + 2] = (cx >> 8) & 0xFF
        out[o + 3] = cy & 0xFF
        out[o + 4] = (cy >> 8) & 0xFF

    if profile.get("lod_glass") and "glass_mode" in updates and updates["glass_mode"] is not None:
        out[127] = 1 if updates["glass_mode"] else 0
    if profile.get("family") == "v2":
        if "led_liftoff_disabled" in updates and updates["led_liftoff_disabled"] is not None:
            out[24] = 0 if updates["led_liftoff_disabled"] else 1
        if "angle_tuning" in updates and updates["angle_tuning"] is not None:
            val = max(-127, min(127, int(updates["angle_tuning"])))
            out[128] = val & 0xFF
        if "force_max_fps" in updates and updates["force_max_fps"] is not None:
            out[129] = 1 if updates["force_max_fps"] else 0

    if "left_handed" in updates and updates["left_handed"] is not None:
        lh = bool(updates["left_handed"])
        ho = bytes(out[71:77])
        cur_lh = not (ho[:2] == b"\x00\x01" and not any(ho[2:6])) and any(ho)
        if lh != cur_lh:
            if lh:
                # swap physical Right action from rec[0] into Left slot (71..76), put Left Click in rec[0]
                rec0_act = bytes(out[78:84])
                if not any(rec0_act):
                    rec0_act = b"\x00\x02\x00\x00\x00\x00"
                out[71:77] = rec0_act
                out[78:84] = b"\x00\x01\x00\x00\x00\x00"
            else:
                left_act = bytes(out[71:77])
                if left_act[:2] == b"\x00\x01" or not any(left_act):
                    left_act = b"\x00\x02\x00\x00\x00\x00"
                out[71:77] = b"\x00\x01\x00\x00\x00\x00"
                out[78:84] = left_act

    if "spdt" in updates and updates["spdt"] is not None:
        mode = updates["spdt"].lower()
        code = {"gx_safe": 0xF0, "safe": 0xF0, "gx_speed": 0xF1, "speed": 0xF1, "off": 8}.get(mode)
        if code is None:
            raise ValueError("spdt must be 'off', 'gx_speed', or 'gx_safe'")
        out[77] = code
        out[84] = code

    return out


def egg_send_cmd(fd, op):
    buf = bytearray(EGG_CMD_LEN)
    buf[0] = 0xA1
    buf[1] = op
    fcntl.ioctl(fd, HIDIOCSFEATURE_N(EGG_CMD_LEN), bytes(buf))


def egg_read_version(fd):
    egg_send_cmd(fd, 0x02)
    time.sleep(0.05)
    for _ in range(4):
        buf = bytearray(EGG_CMD_LEN)
        buf[0] = 0xA1
        fcntl.ioctl(fd, HIDIOCGFEATURE_N(EGG_CMD_LEN), buf)
        if buf[1] == 0x01:
            return egg_format_version(buf)
        time.sleep(0.06)
    return egg_format_version(buf)


def egg_read_config(fd):
    for _ in range(3):
        egg_send_cmd(fd, 0x12)
        time.sleep(0.08)
        delay = 0.06
        for _ in range(6):
            for rid in (0xA0, 0xA1):
                buf = bytearray(EGG_CFG_LEN)
                buf[0] = rid
                fcntl.ioctl(fd, HIDIOCGFEATURE_N(EGG_CFG_LEN), buf)
                buf[0] = 0xA0
                if egg_blob_ok(buf):
                    return buf
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
    raise OSError("failed to read Endgame Gear 8k config blob")


def egg_write_config(fd, blob):
    payload = bytearray(EGG_CFG_LEN)
    payload[:len(blob)] = blob[:EGG_CFG_LEN]
    payload[0] = 0xA0
    payload[1] = 0x11
    for attempt in range(4):
        try:
            fcntl.ioctl(fd, HIDIOCSFEATURE_N(EGG_CFG_LEN), bytes(payload))
            break
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.05)
    time.sleep(0.30)
    delay = 0.06
    for _ in range(8):
        ack = bytearray(EGG_CMD_LEN)
        ack[0] = 0xA1
        fcntl.ioctl(fd, HIDIOCGFEATURE_N(EGG_CMD_LEN), ack)
        if ack[1] == 0x01:
            return
        time.sleep(delay)
        delay = min(delay * 2, 0.4)
    raise OSError("Endgame Gear 8k write was not acknowledged")


def egg_update_config(fd, profile, updates):
    base = egg_read_config(fd)
    blob = egg_serialize_config(base, updates, profile)
    egg_write_config(fd, blob)
    return egg_parse_config(egg_read_config(fd), profile)


# ----------------------------------------------------------------------------- logitech HID++ 2.0 protocol
HIDPP_SHORT = 0x10
HIDPP_LONG = 0x11
HIDPP_SW = 0x0A


def hidpp_xfer(fd, dev_idx, feat_idx, fn, params=(), timeout=0.6):
    """Send a HID++ 2.0 message and read matching reply from hidraw."""
    pkt = bytearray(20 if len(params) > 3 else 7)
    pkt[0] = HIDPP_LONG if len(pkt) == 20 else HIDPP_SHORT
    pkt[1] = dev_idx & 0xFF
    pkt[2] = feat_idx & 0xFF
    pkt[3] = ((fn & 0x0F) << 4) | HIDPP_SW
    for i, b in enumerate(params[:len(pkt) - 4]):
        pkt[4 + i] = b & 0xFF
    os.write(fd, bytes(pkt))
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise Asleep("HID++ device did not answer")
        r, _, _ = select.select([fd], [], [], rem)
        if not r:
            raise Asleep("HID++ device timed out")
        rep = os.read(fd, 64)
        if len(rep) < 7 or rep[0] not in (HIDPP_SHORT, HIDPP_LONG):
            continue
        if rep[1] != (dev_idx & 0xFF):
            continue
        if rep[2] in (0x8F, 0xFF) and rep[3] == feat_idx and rep[4] == pkt[3]:
            raise OSError(f"HID++ error 0x{rep[5]:02x}")
        if rep[2] == (feat_idx & 0xFF) and (rep[3] & 0xFF) == pkt[3]:
            return rep[4:]


def hidpp_probe_device(fd):
    """Find responsive device_index (0xFF direct or 0x01..0x06 receiver) and feature table."""
    for dev_idx in (0xFF, 0x01, 0x02):
        try:
            ver = hidpp_xfer(fd, dev_idx, 0x00, 0x01, timeout=0.25)
            if ver[0] >= 2:
                return dev_idx, f"{ver[0]}.{ver[1]}"
        except (OSError, Asleep):
            continue
    raise Asleep("no HID++ 2.0 mouse responded on this node")


def hidpp_get_feature(fd, dev_idx, feat_id):
    p = hidpp_xfer(fd, dev_idx, 0x00, 0x00, [feat_id >> 8, feat_id & 0xFF])
    return p[0]


def hidpp_read_status(fd, meta):
    dev_idx, proto = hidpp_probe_device(fd)
    info = {
        "device": meta["name"],
        "driver": "hidpp",
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "hidpp_version": proto,
        "dev_idx": dev_idx,
        "wireless": True,
    }
    name_idx = hidpp_get_feature(fd, dev_idx, 0x0005)
    if name_idx:
        nlen = hidpp_xfer(fd, dev_idx, name_idx, 0x00)[0]
        raw = bytearray()
        while len(raw) < nlen:
            raw.extend(hidpp_xfer(fd, dev_idx, name_idx, 0x01, [len(raw)]))
        nm = bytes(raw[:nlen]).split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
        if nm:
            info["device"] = nm

    fw_idx = hidpp_get_feature(fd, dev_idx, 0x0003)
    if fw_idx:
        fw = hidpp_xfer(fd, dev_idx, fw_idx, 0x01, [0])
        info["firmware"] = f"v{fw[4]:x}.{fw[5]:02x}"

    ubatt_idx = hidpp_get_feature(fd, dev_idx, 0x1004)
    batt_idx = hidpp_get_feature(fd, dev_idx, 0x1000)
    if ubatt_idx:
        b = hidpp_xfer(fd, dev_idx, ubatt_idx, 0x01)
        info["battery_percent"] = b[0]
        info["charging"] = b[2] in (1, 2)
    elif batt_idx:
        b = hidpp_xfer(fd, dev_idx, batt_idx, 0x00)
        info["battery_percent"] = b[0]
        info["charging"] = b[2] in (1, 2, 3)

    dpi_idx = hidpp_get_feature(fd, dev_idx, 0x2201)
    if dpi_idx:
        d = hidpp_xfer(fd, dev_idx, dpi_idx, 0x02, [0])
        cur = (d[1] << 8) | d[2]
        info["dpi"] = [cur, cur]
        info["stages"] = [[cur, cur]]
        info["active"] = 1

    rate_idx = hidpp_get_feature(fd, dev_idx, 0x8060)
    if rate_idx:
        r = hidpp_xfer(fd, dev_idx, rate_idx, 0x01, [0])
        if r[0]:
            info["polling_hz"] = round(1000 / r[0])
    return info


# ----------------------------------------------------------------------------- endgame gear -we & compx report 0x08 protocol
def compx_checksum(pkt16):
    return (0x55 - (0x08 + sum(pkt16[:15]))) & 0xFF


def _compx_build(cmd, data=()):
    pkt = bytearray(16)
    pkt[0] = cmd & 0xFF
    for i, b in enumerate(data[:14]):
        pkt[1 + i] = b & 0xFF
    pkt[15] = compx_checksum(pkt)
    return pkt


def compx_xfer(fd, cmd, data=(), feat_mode=False, notify_fd=None, timeout=0.6):
    """Exchange one 16-byte CompX Report 0x08 (or Feature 0x08 + Input 0x09) packet."""
    pkt = _compx_build(cmd, data)
    if feat_mode:
        fcntl.ioctl(fd, HIDIOCSFEATURE_N(17), bytes([0x08]) + pkt)
        wait_rids = (0x09, 0x08)
    else:
        os.write(fd, bytes([0x08]) + pkt)
        wait_rids = (0x08, 0x09)
    rfd = notify_fd if notify_fd is not None else fd
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise Asleep("mouse did not answer (asleep or out of range)")
        r, _, _ = select.select([rfd], [], [], rem)
        if not r:
            raise Asleep("mouse timed out")
        rep = os.read(rfd, 64)
        if len(rep) < 17 or rep[0] not in wait_rids:
            continue
        body = rep[1:17]
        if body[0] == (cmd & 0xFF):
            return body


def compx_read_flash(fd, addr, length, feat_mode=False, notify_fd=None):
    out = bytearray()
    off = 0
    while off < length:
        chunk = min(8, length - off)
        a = addr + off
        rep = compx_xfer(fd, 0x08, [0x00, (a >> 8) & 0xFF, a & 0xFF, chunk], feat_mode, notify_fd)
        out.extend(rep[5:5 + chunk])
        off += chunk
        if feat_mode or off < length:
            time.sleep(0.01)
    return bytes(out)


def compx_write_flash(fd, addr, data, feat_mode=False, notify_fd=None):
    off = 0
    while off < len(data):
        chunk = data[off:off + 8]
        a = addr + off
        compx_xfer(fd, 0x07, [0x00, (a >> 8) & 0xFF, a & 0xFF, len(chunk), *chunk], feat_mode, notify_fd)
        off += len(chunk)
        time.sleep(0.01)


def compx_write_pair(fd, addr, val, feat_mode=False, notify_fd=None):
    v = val & 0xFF
    compx_write_flash(fd, addr, bytes([v, (0x55 - v) & 0xFF]), feat_mode, notify_fd)


def compx_online(fd, feat_mode=False, notify_fd=None, tries=20):
    for _ in range(tries):
        try:
            rep = compx_xfer(fd, 0x03, (), feat_mode, notify_fd, timeout=0.25)
            if rep[1] == 0x01:
                return True
        except (Asleep, OSError):
            pass
        time.sleep(0.01)
    return False


COMPX_RATES_ALL = {0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000, 0x10: 2000, 0x20: 4000, 0x40: 8000}
COMPX_RATES_INV = {v: k for k, v in COMPX_RATES_ALL.items()}
COMPX_R1_RATES = {3: 250, 2: 500, 1: 1000}
COMPX_R1_RATES_INV = {v: k for k, v in COMPX_R1_RATES.items()}


def compx_decode_dpi4(b, enc="flat50"):
    lo_x, lo_y, flags = b[0], b[1], b[2]
    if enc == "byte50":
        return ((lo_x + 1) * 50, (lo_y + 1) * 50)
    hi_x = (flags >> 2) & 0x03
    hi_y = (flags >> 6) & 0x03
    rx = ((hi_x << 8) | lo_x) + 1
    ry = ((hi_y << 8) | lo_y) + 1
    if enc == "pulsar":
        mx, my = flags & 0x03, (flags >> 4) & 0x03
        step_x = 100 if mx == 3 else (50 if mx == 2 else 10)
        step_y = 100 if my == 3 else (50 if my == 2 else 10)
        return (rx * step_x, ry * step_y)
    if enc == "paw3950":
        mx, my = flags & 0x03, (flags >> 4) & 0x03
        step_x = 100 if mx == 1 else 50
        step_y = 100 if my == 1 else 50
        return (rx * step_x, ry * step_y)
    return (rx * 50, ry * 50)


def compx_encode_dpi4(x, y, enc="flat50"):
    def _one(v):
        v = max(10, int(round(v)))
        if enc == "byte50":
            q = max(50, min(12800, round(v / 50) * 50))
            return q // 50 - 1, 0, 0
        if enc == "pulsar":
            if v <= 10000:
                q = max(10, round(v / 10) * 10)
                r, mode = q // 10 - 1, 0
            elif v <= 30000:
                q = round(v / 50) * 50
                r, mode = q // 50 - 1, 2
            else:
                q = min(32000, round(v / 100) * 100)
                r, mode = q // 100 - 1, 3
            return r & 0xFF, (r >> 8) & 0x03, mode
        if enc == "paw3950":
            if v <= 30000:
                q = max(50, round(v / 50) * 50)
                r, mode = q // 50 - 1, 0
            else:
                q = min(42000, round(v / 100) * 100)
                r, mode = q // 100 - 1, 1
            return r & 0xFF, (r >> 8) & 0x03, mode
        q = max(50, min(30000, round(v / 50) * 50))
        r = q // 50 - 1
        return r & 0xFF, (r >> 8) & 0x03, 0

    lx, hx, mx = _one(x)
    ly, hy, my = _one(y)
    flags = ((hx << 2) | mx) | (((hy << 2) | my) << 4)
    crc = (0x55 - (lx + ly + flags)) & 0xFF
    return bytes([lx, ly, flags, crc])


def compx_decode_dpi6(b):
    rx = b[0] | (b[1] << 8)
    ry = b[2] | (b[3] << 8)
    return ((rx + 1) * 50, (ry + 1) * 50)


def compx_encode_dpi6(x, y):
    rx = max(0, round(x / 50) - 1)
    ry = max(0, round(y / 50) - 1)
    body = [rx & 0xFF, (rx >> 8) & 0xFF, ry & 0xFF, (ry >> 8) & 0xFF, 0x00]
    crc = (0x55 - sum(body)) & 0xFF
    return bytes(body + [crc])


def _compx_ctx(meta):
    drv = meta["driver"]
    brand = meta.get("brand", "")
    feat_mode = drv == "egg_we" or brand == "areson"
    dpi_enc = "byte50" if drv == "egg_we" else (
        "pulsar" if brand == "pulsar_compx" else (
            "paw3950" if brand == "teevolution" else "flat50"
        )
    )
    return feat_mode, dpi_enc, brand


def compx_read_status(fd, meta, notify_fd=None):
    drv = meta["driver"]
    feat_mode, dpi_enc, brand = _compx_ctx(meta)
    use_online = drv == "compx" and brand not in ("areson", "vgn")
    if use_online and meta.get("wireless"):
        compx_online(fd, feat_mode, notify_fd, tries=8)

    name = meta["name"]
    dpi_max = meta.get("dpi_max", 26000)
    rates = meta.get("rates", [125, 250, 500, 1000])
    is_r1 = False
    is_v8s = (brand == "scyrox" and meta.get("model") == "v8s")
    dpi6 = False
    max_stages = 8

    if drv == "compx" and brand == "atk":
        try:
            ident = compx_xfer(fd, 0x10, (), feat_mode, notify_fd, timeout=0.3)
            cid, mid = ident[5], ident[6]
            prod = ATK_PRODUCTS.get((cid, mid))
            if prod:
                name = prod["name"]
                dpi_max = prod["dpi_max"]
                rates = prod["rates"]
                max_stages = prod["max_stages"]
                is_r1 = prod.get("is_r1", False)
                dpi6 = prod.get("dpi6", False)
                if prod["sensor"] in ("PAW3950", "PAW3950Ultra"):
                    dpi_enc = "paw3950"
        except (Asleep, OSError):
            pass

    fw = None
    if not feat_mode:
        try:
            ver = compx_xfer(fd, 0x02, (), feat_mode, notify_fd, timeout=0.3)
            fw = f"v{ver[5]}.{ver[6]:02d}"
        except (Asleep, OSError):
            pass

    cfg = compx_read_flash(fd, 0x00, 16, feat_mode, notify_fd)
    hz = COMPX_RATES_ALL.get(cfg[0])
    stage_cnt = max(1, min(max_stages, cfg[2] or 1))
    active = max(1, min(stage_cnt, cfg[4] + 1))

    if is_v8s or dpi6:
        addr = 6912 if is_v8s else 0x1B00
        raw_st = compx_read_flash(fd, addr, stage_cnt * 6, feat_mode, notify_fd)
        stages = [list(compx_decode_dpi6(raw_st[i * 6:(i + 1) * 6])) for i in range(stage_cnt)]
    else:
        raw_st = compx_read_flash(fd, 0x0C, stage_cnt * 4, feat_mode, notify_fd)
        stages = [list(compx_decode_dpi4(raw_st[i * 4:(i + 1) * 4], dpi_enc)) for i in range(stage_cnt)]

    lod_raw = cfg[10]
    if is_v8s:
        stops = ["0.7 mm", "0.9 mm", "1.2 mm", "1.4 mm", "1.6 mm"]
        idx = max(0, min(len(stops) - 1, lod_raw - 1 if lod_raw >= 1 else lod_raw))
        lod = {"mode": "index", "index": idx, "label": stops[idx]}
    elif brand in ("pulsar_compx", "pulsar_4k", "vgn", "teevolution"):
        lvl = {3: "low", 1: "medium", 2: "high"}.get(lod_raw, f"raw:{lod_raw}")
        lod = {"mode": "preset", "level": lvl}
    else:
        lvl = {1: "low" if brand in ("lamzu", "atk") else "medium", 2: "medium" if brand == "lamzu" else "high"}.get(lod_raw, f"raw:{lod_raw}")
        lod = {"mode": "preset", "level": lvl}

    sys_blk = compx_read_flash(fd, 0xA8, 28, feat_mode, notify_fd)
    debounce = sys_blk[1]  # addr 0xA9 (169)
    sleep_10s = sys_blk[5]  # addr 0xAD (173)
    motion_sync = bool(sys_blk[7]) if len(sys_blk) > 7 else None  # addr 0xAF (175)
    angle_snap = bool(sys_blk[9]) if len(sys_blk) > 9 else None   # addr 0xB1 (177)
    ripple = bool(sys_blk[11]) if len(sys_blk) > 11 else None     # addr 0xB3 (179)

    info = {
        "id": meta["id"],
        "driver": drv,
        "brand": brand or drv,
        "device": name,
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": bool(meta.get("wireless")),
        "dpi_min": meta.get("dpi_min", 50),
        "dpi_max": dpi_max,
        "rates": rates,
        "dpi": stages[active - 1],
        "active": active,
        "stages": stages,
        "polling_hz": hz,
        "lod": lod,
        "debounce_ms": debounce,
        "motion_sync": motion_sync,
        "angle_snapping": angle_snap,
        "ripple_control": ripple,
        "idle_seconds": sleep_10s * 10 if sleep_10s else None,
        "is_r1": is_r1,
        "is_v8s": is_v8s,
        "dpi6": dpi6,
        "dpi_enc": dpi_enc,
    }
    if fw:
        info["firmware"] = fw
    if meta.get("wireless"):
        try:
            batt = compx_xfer(fd, 0x04, (), feat_mode, notify_fd, timeout=0.4)
            info["battery_percent"] = batt[5]
            info["charging"] = batt[6] != 0
        except (Asleep, OSError):
            pass
    return info


# ----------------------------------------------------------------------------- pulsar XS-1 protocol
XS1_POLL_RATES = {240: 125, 120: 250, 60: 500, 30: 1000, 15: 2000, 8: 4000, 4: 8000}
XS1_LOD = {0x07: "low", 0x0A: "medium", 0x14: "high"}
XS1_LOD_INV = {v: k for k, v in XS1_LOD.items()}


def pulsar_xs1_xfer(fd, payload):
    pkt = bytearray(64)
    pkt[0] = 0x00
    pkt[1:1 + len(payload)] = bytes(payload)
    chk = sum(pkt[:62]) & 0xFFFF
    pkt[62] = chk & 0xFF
    pkt[63] = (chk >> 8) & 0xFF
    fcntl.ioctl(fd, HIDIOCSFEATURE_N(65), bytes([0x00]) + pkt)
    time.sleep(0.025)
    buf = bytearray(65)
    fcntl.ioctl(fd, HIDIOCGFEATURE_N(65), buf)
    return buf[1:65]


def pulsar_xs1_read_status(fd, meta):
    fw_r = pulsar_xs1_xfer(fd, [0x01, 0x87, 0x04])
    poll_r = pulsar_xs1_xfer(fd, [0x08, 0x85, 0x03])
    dpi_r = pulsar_xs1_xfer(fd, [0x05, 0x82, 0x05])
    lod_r = pulsar_xs1_xfer(fd, [0x07, 0x82, 0x03])
    deb_r = pulsar_xs1_xfer(fd, [0x04, 0x83, 0x03])
    ms_r = pulsar_xs1_xfer(fd, [0x07, 0x85, 0x02])
    as_r = pulsar_xs1_xfer(fd, [0x07, 0x84, 0x02])
    rc_r = pulsar_xs1_xfer(fd, [0x07, 0x83, 0x02])
    dpi_x = dpi_r[7] | (dpi_r[8] << 8)
    dpi_y = dpi_r[9] | (dpi_r[10] << 8) or dpi_x
    info = {
        "id": meta["id"],
        "driver": "pulsar_xs1",
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": bool(meta.get("wireless")),
        "dpi_min": 50,
        "dpi_max": 26000,
        "firmware": f"v{fw_r[7]:02x}.{fw_r[6]:02x}",
        "dpi": [dpi_x, dpi_y],
        "polling_hz": XS1_POLL_RATES.get(poll_r[7]),
        "polling_readonly": True,
        "lod": {"mode": "preset", "level": XS1_LOD.get(lod_r[8], f"raw:0x{lod_r[8]:02x}")},
        "debounce_ms": deb_r[7],
        "motion_sync": bool(ms_r[7]),
        "angle_snapping": bool(as_r[7]),
        "ripple_control": bool(rc_r[7]),
    }
    if meta.get("wireless"):
        bat_r = pulsar_xs1_xfer(fd, [0x08, 0x81, 0x01])
        info["battery_percent"] = bat_r[6]
        info["charging"] = False
    return info


# ----------------------------------------------------------------------------- pulsar pro protocol
PULSAR_PRO_SLEEP_MS = {10000: 1, 30000: 3, 60000: 6, 120000: 12, 300000: 30, 600000: 60, 1800000: 180}


def pulsar_pro_xfer(fd, cmd, params=(), timeout=0.6):
    pkt = bytearray(63)
    pkt[0] = cmd & 0xFF
    pkt[1:1 + len(params)] = bytes(params)
    for _ in range(8):
        os.write(fd, bytes([0x00]) + pkt)
        deadline = time.monotonic() + timeout
        busy = False
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise Asleep("Pulsar Pro timed out")
            r, _, _ = select.select([fd], [], [], rem)
            if not r:
                raise Asleep("Pulsar Pro timed out")
            rep = os.read(fd, 64)
            body = rep[1:] if len(rep) == 64 and rep[0] == 0x00 else rep
            if not body:
                continue
            if body[0] == 0xA6:
                raise OSError(f"Pulsar Pro rejected cmd 0x{cmd:02x}")
            if body[0] == 0xA7:
                busy = True
                break
            if body[0] == (cmd & 0xFF):
                return body
        if busy:
            time.sleep(0.05)
    raise OSError(f"Pulsar Pro busy on cmd 0x{cmd:02x}")


def pulsar_pro_commit(fd):
    pulsar_pro_xfer(fd, 0xC3, [0x01])


def pulsar_pro_read_status(fd, meta):
    fw_r = pulsar_pro_xfer(fd, 0xBF)
    bat_r = pulsar_pro_xfer(fd, 0xB4)
    dpi_r = pulsar_pro_xfer(fd, 0xB1)
    poll_r = pulsar_pro_xfer(fd, 0xB2)
    lod_r = pulsar_pro_xfer(fd, 0xB3)
    ms_r = pulsar_pro_xfer(fd, 0xB5)
    rc_r = pulsar_pro_xfer(fd, 0xB6)
    as_r = pulsar_pro_xfer(fd, 0xB7)
    at_r = pulsar_pro_xfer(fd, 0xB8)
    tur_r = pulsar_pro_xfer(fd, 0xC4)
    deb_r = pulsar_pro_xfer(fd, 0xC5)
    pwr_r = pulsar_pro_xfer(fd, 0xC2)
    cnt = max(1, min(6, dpi_r[2] or 1))
    active = max(1, min(cnt, dpi_r[3] or 1))
    stages = []
    for i in range(cnt):
        o = 4 + i * 4
        x = dpi_r[o] | (dpi_r[o + 1] << 8)
        y = dpi_r[o + 2] | (dpi_r[o + 3] << 8)
        stages.append([x, y or x])
    sleep_ms = int.from_bytes(pwr_r[2:6], "little")
    at_val = at_r[2] - 256 if at_r[2] > 127 else at_r[2]
    return {
        "id": meta["id"],
        "driver": "pulsar_pro",
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": True,
        "dpi_min": 50,
        "dpi_max": 32000,
        "firmware": f"v{fw_r[1]}.{fw_r[2]}.{fw_r[3]}",
        "battery_percent": bat_r[1],
        "charging": bat_r[6] == 1,
        "dpi": stages[active - 1],
        "active": active,
        "stages": stages,
        "polling_hz": poll_r[2] | (poll_r[3] << 8),
        "lod": {"mode": "preset", "level": {1: "low", 2: "medium", 3: "high"}.get(lod_r[2], "medium")},
        "motion_sync": ms_r[2] == 1,
        "ripple_control": rc_r[2] == 1,
        "angle_snapping": as_r[2] == 1,
        "angle_tuning": at_val,
        "performance": tur_r[2] == 1,
        "debounce_ms": deb_r[2],
        "idle_seconds": sleep_ms // 1000 if sleep_ms else None,
    }


# ----------------------------------------------------------------------------- glorious protocols
SINO_SENSORS = {
    0x06: ("PMW3360", 12000, True),
    0x08: ("PMW3212", 7200, False),
    0x0E: ("PMW3327", 10200, True),
    0x0F: ("PMW3389", 16000, False),
}
SINO_RATES = {1: 125, 2: 250, 3: 500, 4: 1000}
SINO_RATES_INV = {v: k for k, v in SINO_RATES.items()}


def sino_read_block(fd, meta):
    cmd = bytearray(1 + meta.get("cmd_len", 6))
    cmd[0] = 0x05
    cmd[1] = 0x11
    fcntl.ioctl(fd, HIDIOCSFEATURE_N(len(cmd)), bytes(cmd))
    time.sleep(0.06)
    rid = meta["cfg_rid"]
    buf = bytearray(1 + meta["cfg_len"])
    buf[0] = rid
    fcntl.ioctl(fd, HIDIOCGFEATURE_N(len(buf)), buf)
    body = buf[1:]
    if len(body) < 123:
        raise OSError(f"Sinowealth block too short ({len(body)}B < 123B)")
    return body


def sino_parse_block(body):
    sensor_id = body[9]
    sensor_name, dpi_max, offset1 = SINO_SENSORS.get(sensor_id, (f"unknown:0x{sensor_id:02x}", None, False))
    xy_mode = bool(body[0x0A] & 0x80)
    hz = SINO_RATES.get(body[0x0A] & 0x0F)
    active = max(1, (body[0x0B] >> 4) & 0x0F)
    disabled_mask = body[0x0C]
    stages = []
    for i in range(6):
        if disabled_mask & (1 << i):
            continue
        if xy_mode:
            vx = (body[0x0D + i * 2] + (1 if offset1 else 0)) * 100
            vy = (body[0x0D + i * 2 + 1] + (1 if offset1 else 0)) * 100
        else:
            vx = vy = (body[0x0D + i] + (1 if offset1 else 0)) * 100
        stages.append([vx, vy])
    if not stages:
        stages = [[800, 800]]
    active = min(active, len(stages))
    return {
        "sensor": sensor_name,
        "sensor_writable": sensor_id in SINO_SENSORS,
        "dpi_max": dpi_max or 16000,
        "polling_hz": hz,
        "active": active,
        "stages": stages,
        "dpi": stages[active - 1],
    }


def core2_xfer(fd, target, bank, reg, data=(), read_reply=False):
    pkt = bytearray(64)
    pkt[2] = target & 0xFF
    pkt[3] = len(data) & 0xFF
    pkt[4] = bank & 0xFF
    pkt[5] = reg & 0xFF
    pkt[6:6 + len(data)] = bytes(data)
    fcntl.ioctl(fd, HIDIOCSFEATURE_N(65), bytes([0x00]) + pkt)
    if not read_reply:
        return None
    time.sleep(0.03)
    buf = bytearray(65)
    fcntl.ioctl(fd, HIDIOCGFEATURE_N(65), buf)
    return buf[1:65]


CORE2_RATES = {125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01, 2000: 0x20, 4000: 0x40, 8000: 0x80}


def core2_read_status(fd, meta):
    target = 0x00 if meta.get("wireless") else 0x02
    cached = _load_device_cache().get(_cache_key(meta), {})
    info = {
        "id": meta["id"],
        "driver": "glorious_core2",
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": bool(meta.get("wireless")),
        "dpi_min": 50,
        "dpi_max": 26000,
        "write_only_settings": True,
        "dpi": cached.get("dpi", [800, 800]),
        "stages": cached.get("stages", [[400, 400], [800, 800], [1600, 1600], [3200, 3200]]),
        "active": cached.get("active", 2),
        "polling_hz": cached.get("polling_hz", 1000),
        "debounce_ms": cached.get("debounce_ms", 4),
        "motion_sync": cached.get("motion_sync", False),
    }
    try:
        fw_r = core2_xfer(fd, target, 0x00, 0x81, read_reply=True)
        if fw_r and any(fw_r[6:10]):
            info["firmware"] = f"v{fw_r[6]}.{fw_r[7]}.{fw_r[8]}.{fw_r[9]}"
    except OSError:
        pass
    if meta.get("wireless"):
        try:
            b = core2_xfer(fd, 0x00, 0x00, 0x83, read_reply=True)
            if b and b[0] == 0xA1:
                info["battery_percent"] = b[7]
                info["charging"] = b[6] == 1
        except OSError:
            pass
    return info


def classic_read_status(fd, meta):
    cached = _load_device_cache().get(_cache_key(meta), {})
    flen = meta.get("feat_len", 0)
    rid = meta.get("feat_rid", 0)
    info = {
        "id": meta["id"],
        "driver": "glorious_classic",
        "gen": meta.get("gen", "legacy"),
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": bool(meta.get("wireless")),
        "dpi_min": 100,
        "dpi_max": 19000,
        "write_only_settings": True,
        "writable": flen == 64,
        "dpi": cached.get("dpi", [800, 800]),
        "stages": cached.get("stages", [[400, 400], [800, 800], [1600, 1600], [3200, 3200]]),
        "active": cached.get("active", 2),
        "polling_hz": cached.get("polling_hz", 1000),
        "lod": {"mode": "preset", "level": cached.get("lod", "medium")},
        "debounce_ms": cached.get("debounce_ms", 10),
    }
    if flen == 64 and meta.get("wireless"):
        try:
            pkt = bytearray(64)
            pkt[2] = 2  # bfr[3] = 2
            pkt[3] = 2  # bfr[4] = 2
            pkt[5] = 0x83  # bfr[6] = 0x83
            fcntl.ioctl(fd, HIDIOCSFEATURE_N(65), bytes([rid]) + pkt)
            time.sleep(0.03)
            buf = bytearray(65)
            buf[0] = rid
            fcntl.ioctl(fd, HIDIOCGFEATURE_N(65), buf)
            bfr = buf[1:]
            if bfr[0] == 0xA1 and bfr[5] == 0x83:
                info["charging"] = bfr[6] == 1
                info["battery_percent"] = bfr[7]
        except OSError:
            pass
    return info


def pixart_write_config(fd, meta, dpi=None, stages=None, active=None, hz=None, lod=None, debounce_ms=None):
    cached = _load_device_cache().get(_cache_key(meta), {})
    st = stages or cached.get("stages") or [[400, 400], [800, 800], [1600, 1600], [3200, 3200]]
    if dpi is not None:
        act = active or cached.get("active", 1)
        st = list(st)
        st[min(act, len(st)) - 1] = [dpi[0], dpi[1]]
    else:
        act = active or cached.get("active", 1)
    hz = hz or cached.get("polling_hz", 1000)
    lod_s = lod or cached.get("lod", "medium")
    deb = debounce_ms if debounce_ms is not None else cached.get("debounce_ms", 4)
    poll_code = {125: 2, 250: 3, 500: 4, 1000: 1}.get(hz, 1)
    lod_code = {"medium": 1, "low": 1, "high": 2}.get(lod_s, 1)

    for seq in range(4):
        pkt = bytearray(64)  # Total report size incl. ID 0x03 is 64B
        pkt[0] = 0x03
        pkt[1] = 0x04
        pkt[2] = 0xFB
        pkt[3] = seq
        pkt[4] = 0x01
        if seq == 0:
            pkt[5] = poll_code
            pkt[6] = len(st)
            pkt[7] = act - 1
            pkt[8] = lod_code
            pkt[9] = max(0, min(16, deb))
            for i, (dx, dy) in enumerate(st[:6]):
                vx = max(2, min(520, round(dx / 50)))
                vy = max(2, min(520, round(dy / 50)))
                o = 10 + i * 4
                pkt[o:o + 4] = bytes([vx & 0xFF, vx >> 8, vy & 0xFF, vy >> 8])
        fcntl.ioctl(fd, HIDIOCSFEATURE_N(64), bytes(pkt))
        time.sleep(0.015)

    _save_device_cache(_cache_key(meta), {
        "dpi": list(st[min(act, len(st)) - 1]),
        "stages": st,
        "active": act,
        "polling_hz": hz,
        "lod": lod_s,
        "debounce_ms": deb,
    })


# ----------------------------------------------------------------------------- unified device ops
def _open_notify(meta):
    npath = meta.get("notify_hidraw")
    if npath and npath != meta["hidraw"]:
        return os.open(npath, os.O_RDONLY)
    return None


def read_device_status(meta):
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            pid = meta["pid"]
            wireless, hyper, dpi_max = meta["wireless"], meta["hyper"], meta["dpi_max"]
            info = {
                "id": meta["id"],
                "driver": "razer",
                "device": meta["name"],
                "vid": f"0x{meta['vid']:04x}",
                "pid": f"0x{pid:04x}",
                "hidraw": meta["hidraw"],
                "link": meta["link"],
                "wireless": wireless,
                "hyperpolling": hyper,
                "dpi_max": dpi_max,
                "firmware": get_firmware(fd),
                "serial": get_serial(fd),
                "dpi": list(get_dpi(fd)),
                **get_stages(fd),
                "polling_hz": get_poll(fd, hyper),
                "lod": get_lod(fd),
            }
            info["stages"] = [list(s) for s in info["stages"]]
            if wireless:
                info["battery_percent"] = round(xfer(fd, 0x07, 0x80, 0x02)[1] / 255 * 100)
                info["charging"] = bool(xfer(fd, 0x07, 0x84, 0x02)[1])
                info["idle_seconds"] = get_idle(fd)
                info["low_battery_percent"] = get_low(fd)
            return info
        if drv == "egg8k":
            prof = meta["profile"]
            blob = egg_read_config(fd)
            parsed = egg_parse_config(blob, prof)
            return {
                "id": meta["id"],
                "driver": "egg8k",
                "device": meta["name"],
                "vid": f"0x{meta['vid']:04x}",
                "pid": f"0x{meta['pid']:04x}",
                "hidraw": meta["hidraw"],
                "link": meta["link"],
                "family": prof["family"],
                "wireless": False,
                "dpi_min": prof["cpi_min"],
                "dpi_max": prof["cpi_max"],
                "firmware": egg_read_version(fd),
                **{k: v for k, v in parsed.items() if k not in ("all_stages",)},
                "dpi": list(parsed["dpi"]),
                "stages": [list(s) for s in parsed["stages"]],
            }
        if drv == "hidpp":
            info = hidpp_read_status(fd, meta)
            info["id"] = meta["id"]
            return info
        if drv in ("compx", "egg_we"):
            return compx_read_status(fd, meta, nfd)
        if drv == "pulsar_xs1":
            return pulsar_xs1_read_status(fd, meta)
        if drv == "pulsar_pro":
            return pulsar_pro_read_status(fd, meta)
        if drv == "glorious_sinowealth":
            blk = sino_read_block(fd, meta)
            parsed = sino_parse_block(blk)
            return {
                "id": meta["id"], "driver": drv, "device": meta["name"],
                "vid": f"0x{meta['vid']:04x}", "pid": f"0x{meta['pid']:04x}",
                "hidraw": meta["hidraw"], "link": meta["link"], "wireless": False,
                "dpi_min": 100, **parsed,
            }
        if drv == "glorious_core2":
            return core2_read_status(fd, meta)
        if drv == "glorious_classic":
            return classic_read_status(fd, meta)
        if drv == "glorious_pixart":
            cached = _load_device_cache().get(_cache_key(meta), {})
            return {
                "id": meta["id"], "driver": drv, "device": meta["name"],
                "vid": f"0x{meta['vid']:04x}", "pid": f"0x{meta['pid']:04x}",
                "hidraw": meta["hidraw"], "link": meta["link"],
                "wireless": bool(meta.get("wireless")),
                "dpi_min": 100, "dpi_max": 26000, "write_only_settings": True,
                "dpi": cached.get("dpi", [800, 800]),
                "stages": cached.get("stages", [[400, 400], [800, 800], [1600, 1600], [3200, 3200]]),
                "active": cached.get("active", 2),
                "polling_hz": cached.get("polling_hz", 1000),
                "lod": {"mode": "preset", "level": cached.get("lod", "medium")},
                "debounce_ms": cached.get("debounce_ms", 4),
            }
        raise ValueError(f"unsupported driver {drv!r}")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


# ----------------------------------------------------------------------------- commands
CACHE = os.path.expanduser("~/.cache/mousetool/battery.json")


def read_battery(meta=None):
    if meta is None:
        mice = [m for m in discover_mice() if m.get("wireless")]
        if not mice:
            raise FileNotFoundError("no wireless mouse found")
        meta = mice[0]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        drv = meta["driver"]
        if drv == "razer":
            pct = round(xfer(fd, 0x07, 0x80, 0x02)[1] / 255 * 100)
            charging = bool(xfer(fd, 0x07, 0x84, 0x02)[1])
            return {"device": meta["name"], "percent": pct, "charging": charging,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "hidpp":
            st = hidpp_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery feature not supported")
            return {"device": st["device"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        if drv in ("compx", "egg_we"):
            if not meta.get("wireless"):
                raise OSError(f"{meta['name']} is wired (no battery)")
            feat_mode, _, _ = _compx_ctx(meta)
            b = compx_xfer(fd, 0x04, (), feat_mode, nfd)
            return {"device": meta["name"], "percent": b[5], "charging": b[6] != 0,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "pulsar_xs1":
            if not meta.get("wireless"):
                raise OSError(f"{meta['name']} is wired (no battery)")
            b = pulsar_xs1_xfer(fd, [0x08, 0x81, 0x01])
            return {"device": meta["name"], "percent": b[6], "charging": False,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "pulsar_pro":
            b = pulsar_pro_xfer(fd, 0xB4)
            return {"device": meta["name"], "percent": b[1], "charging": b[6] == 1,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "glorious_core2":
            st = core2_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery not reported")
            return {"device": meta["name"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        if drv == "glorious_classic":
            st = classic_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery not reported")
            return {"device": meta["name"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        raise OSError(f"{meta['name']} does not report battery via HID")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def battery(args):
    now = int(time.time())
    if getattr(args, "all", False):
        results = []
        for m in discover_mice():
            if not m.get("wireless"):
                continue
            try:
                b = read_battery(m)
                b.update(stale=False, age=0, hidraw=m["hidraw"])
            except (Asleep, OSError) as e:
                b = {"device": m["name"], "percent": None, "charging": False, "link": m["link"],
                     "hidraw": m["hidraw"], "stale": True, "age": None, "reason": str(e)}
            results.append(b)
        for sb in _sysfs_batteries():
            if not any(sb["name"].lower() in (r.get("device") or "").lower() for r in results):
                results.append({
                    "device": sb["name"], "percent": sb["percent"], "charging": sb["charging"],
                    "link": "Kernel power_supply", "stale": False, "age": 0,
                })
        if args.json:
            print(json.dumps({"batteries": results}, indent=2))
        else:
            for b in results:
                pct = f"{b['percent']}%" if b["percent"] is not None else "-"
                chg = " (charging)" if b["charging"] else ""
                print(f"{b['device']}: {pct}{chg} [{b['link']}]")
        return

    meta = select_mice(getattr(args, "device", None))[0] if getattr(args, "device", None) else None
    try:
        b = read_battery(meta)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE + ".tmp", "w") as f:
            json.dump(b, f)
        os.replace(CACHE + ".tmp", CACHE)
        b.update(stale=False, age=0)
    except (Asleep, OSError, FileNotFoundError) as e:
        try:
            with open(CACHE) as f:
                b = json.load(f)
            b.update(stale=True, age=now - b.get("time", now), reason=str(e))
        except (OSError, ValueError):
            b = {"percent": None, "charging": False, "link": None, "stale": True, "age": None, "reason": str(e)}
    if args.json:
        print(json.dumps(b))
    else:
        print(f"{b['percent'] if b['percent'] is not None else '-'} {int(b['charging'])} {int(b['stale'])} "
              f"{b['age'] if b['age'] is not None else '-'}")


def list_cmd(args):
    sys_mice = discover_system_mice()
    if args.json:
        print(json.dumps({"mice": sys_mice}, indent=2))
        return
    if not sys_mice:
        print("No mice detected.")
        return
    for i, m in enumerate(sys_mice, 1):
        tag = f"[{m['driver']}]" if m["configurable"] else "[generic]"
        extra = []
        if m.get("vid") and m.get("pid"):
            extra.append(f"{m['vid']}:{m['pid']}")
        if m.get("hidraw"):
            extra.append(m["hidraw"])
        if m.get("battery_percent") is not None:
            extra.append(f"{m['battery_percent']}%" + ("+" if m.get("charging") else ""))
        print(f"#{i} {m['name']}  {tag}  {m['link']}  ({', '.join(extra)})")


def _print_status(info):
    x, y = info.get("dpi", (None, None))
    dpi_s = (f"{x}" if x == y else f"{x}x{y}") if x is not None else "–"
    fw = f", {info['firmware']}" if info.get("firmware") else ""
    note = " (cached/write-only)" if info.get("write_only_settings") else ""
    print(f"{info['device']} [{info['driver']}] ({info['link']}{fw}, {info['hidraw']}){note}")
    if x is not None:
        st = info.get("stages") or []
        act = info.get("active", 1)
        st_info = f"  (active stage {act}/{len(st)})" if st else ""
        print(f"  DPI:      {dpi_s}{st_info}")
        if st:
            print(f"  Stages:   {', '.join(f'{sx}' if sx == sy else f'{sx}x{sy}' for sx, sy in st)}")
    if info.get("polling_hz"):
        ro = " (read-only)" if info.get("polling_readonly") else ""
        print(f"  Polling:  {info['polling_hz']} Hz{ro}")
    if info.get("lod"):
        lod = info["lod"]
        if lod["mode"] == "preset":
            print(f"  LOD:      {lod['level']}")
        elif lod["mode"] == "async":
            print(f"  LOD:      async lift={lod['lift']} land={lod['land']}")
        else:
            print(f"  LOD:      {lod['label']}")
    flags = [k for k, v in (
        ("MotionSync", info.get("motion_sync")),
        ("AngleSnap", info.get("angle_snapping")),
        ("RippleCtrl", info.get("ripple_control")),
        ("Slamclick", info.get("slamclick")),
        ("JitterFilter", info.get("motion_jitter")),
        ("GlassMode", info.get("glass_mode")),
        ("MaxFPS", info.get("force_max_fps")),
        ("PerfMode", info.get("performance")),
        ("LeftHanded", info.get("left_handed")),
    ) if v]
    extra = []
    if info.get("debounce_ms") is not None:
        extra.append(f"Debounce={info['debounce_ms']}ms")
    if info["driver"] == "egg8k" and info.get("buttons"):
        spdt = info["buttons"][0].get("spdt")
        if spdt and spdt != "off":
            extra.append(f"SPDT={spdt}")
    if flags or extra:
        print(f"  Filters:  {', '.join(flags) if flags else 'none'}" + (f"  {' '.join(extra)}" if extra else ""))
    if info.get("wireless") and "battery_percent" in info:
        chg = " (charging)" if info.get("charging") else ""
        tail = ""
        if info.get("low_battery_percent") is not None:
            tail += f"  low-warn={info['low_battery_percent']}%"
        if info.get("idle_seconds") is not None:
            tail += f"  sleep={info['idle_seconds']}s"
        print(f"  Battery:  {info['battery_percent']}%{chg}{tail}")


def status_cmd(args):
    want_all = getattr(args, "all", False) or (args.device is None and not args.json)
    targets = select_mice(args.device, all_devices=want_all)
    infos = []
    for m in targets:
        try:
            infos.append(read_device_status(m))
        except (Asleep, OSError, FileNotFoundError) as e:
            if not want_all and not getattr(args, "all", False):
                raise
            infos.append({"id": m["id"], "device": m["name"], "driver": m["driver"],
                          "hidraw": m["hidraw"], "link": m["link"], "error": str(e)})
    if args.json:
        if getattr(args, "all", False):
            print(json.dumps({"mice": infos}, indent=2))
        else:
            print(json.dumps(infos[0], indent=2))
    else:
        for idx, info in enumerate(infos):
            if idx:
                print()
            if "error" in info:
                print(f"{info['device']} [{info['driver']}] ({info['link']}, {info['hidraw']}): {info['error']}")
            else:
                _print_status(info)


def dpi_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                set_dpi(fd, args.x, y, meta["dpi_max"])
            x, y = get_dpi(fd)
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "egg8k":
            prof = meta["profile"]
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                parsed = egg_update_config(fd, prof, {"dpi": (args.x, y)})
            else:
                parsed = egg_parse_config(egg_read_config(fd), prof)
            x, y = parsed["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "hidpp":
            dev_idx, _ = hidpp_probe_device(fd)
            dpi_idx = hidpp_get_feature(fd, dev_idx, 0x2201)
            if not dpi_idx:
                raise OSError("device does not expose HID++ AdjustableDpi (0x2201)")
            if args.x is not None:
                hidpp_xfer(fd, dev_idx, dpi_idx, 0x03, [0, args.x >> 8, args.x & 0xFF])
            d = hidpp_xfer(fd, dev_idx, dpi_idx, 0x02, [0])
            print((d[1] << 8) | d[2])
        elif drv in ("compx", "egg_we"):
            st = compx_read_status(fd, meta, nfd)
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                feat_mode, _, _ = _compx_ctx(meta)
                act = st["active"]
                if st.get("is_v8s") or st.get("dpi6"):
                    addr = (6912 if st.get("is_v8s") else 0x1B00) + (act - 1) * 6
                    compx_write_flash(fd, addr, compx_encode_dpi6(args.x, y), feat_mode, nfd)
                else:
                    addr = 0x0C + (act - 1) * 4
                    compx_write_flash(fd, addr, compx_encode_dpi4(args.x, y, st["dpi_enc"]), feat_mode, nfd)
                st = compx_read_status(fd, meta, nfd)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "pulsar_xs1":
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                qx = max(50, min(26000, round(args.x / 50) * 50))
                qy = max(50, min(26000, round(y / 50) * 50))
                pulsar_xs1_xfer(fd, [0x05, 0x02, 0x05, 0, 0, 1, qx & 0xFF, qx >> 8, qy & 0xFF, qy >> 8])
            st = pulsar_xs1_read_status(fd, meta)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "pulsar_pro":
            st = pulsar_pro_read_status(fd, meta)
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                stages = [list(s) for s in st["stages"]]
                stages[st["active"] - 1] = [max(50, min(32000, args.x)), max(50, min(32000, y))]
                flat = []
                for sx, sy in stages:
                    flat += [sx & 0xFF, sx >> 8, sy & 0xFF, sy >> 8]
                pulsar_pro_xfer(fd, 0xB1, [1, len(stages), st["active"], *flat])
                pulsar_pro_commit(fd)
                st = pulsar_pro_read_status(fd, meta)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "glorious_core2":
            st = core2_read_status(fd, meta)
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                target = 0x00 if meta.get("wireless") else 0x02
                stages = [list(s) for s in st["stages"]]
                stages[st["active"] - 1] = [args.x, y]
                flat = [0x01]
                for sx, sy in stages:
                    flat += [sx >> 8, sx & 0xFF, sy >> 8, sy & 0xFF]
                core2_xfer(fd, target, 0x01, 0x01, flat)
                _save_device_cache(_cache_key(meta), {"dpi": [args.x, y], "stages": stages})
                st = core2_read_status(fd, meta)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "glorious_classic":
            if meta.get("gen") == "core2":
                raise OSError("Classic (CORE 2 generation) only supports battery and debounce writes")
            if meta.get("feat_len") != 64:
                raise OSError(f"refusing write on classic descriptor length {meta.get('feat_len')} != 64")
            st = classic_read_status(fd, meta)
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                stages = [list(s) for s in st["stages"]]
                stages[st["active"] - 1] = [args.x, y]
                pkt = bytearray(64)
                pkt[2] = 0x02; pkt[3] = 0x10; pkt[4] = 0x02; pkt[5] = 0x01; pkt[6] = 0x01
                for i, (sx, sy) in enumerate(stages[:4]):
                    o = 8 + i * 4
                    pkt[o:o + 4] = bytes([sx >> 8, sx & 0xFF, sy >> 8, sy & 0xFF])
                fcntl.ioctl(fd, HIDIOCSFEATURE_N(65), bytes([meta["feat_rid"]]) + pkt)
                _save_device_cache(_cache_key(meta), {"dpi": [args.x, y], "stages": stages})
                st = classic_read_status(fd, meta)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
        elif drv == "glorious_pixart":
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                pixart_write_config(fd, meta, dpi=(args.x, y))
            st = read_device_status(meta)
            x, y = st["dpi"]
            print(f"{x}" if x == y else f"{x} {y}")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def _parse_stage(token):
    if ":" in token or "x" in token.lower():
        sep = ":" if ":" in token else ("x" if "x" in token else "X")
        a, b = token.split(sep, 1)
        return (int(a), int(b))
    v = int(token)
    return (v, v)


def stages_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            cur = get_stages(fd)
            if args.set or args.active is not None:
                stages = [_parse_stage(s.strip()) for s in args.set.split(",") if s.strip()] if args.set else cur["stages"]
                active = args.active if args.active is not None else min(cur["active"], len(stages))
                set_stages(fd, active, stages, meta["dpi_max"])
                cur = get_stages(fd)
            print(json.dumps(cur))
        elif drv == "egg8k":
            prof = meta["profile"]
            cur = egg_parse_config(egg_read_config(fd), prof)
            updates = {}
            if args.set:
                updates["stages"] = [_parse_stage(s.strip()) for s in args.set.split(",") if s.strip()]
            if args.active is not None:
                updates["active"] = args.active
            if updates:
                cur = egg_update_config(fd, prof, updates)
            print(json.dumps({"active": cur["active"], "stages": [list(s) for s in cur["stages"]]}))
        elif drv in ("compx", "egg_we"):
            st = compx_read_status(fd, meta, nfd)
            feat_mode, _, _ = _compx_ctx(meta)
            if args.set or args.active is not None:
                stages = [_parse_stage(s.strip()) for s in args.set.split(",") if s.strip()] if args.set else st["stages"]
                active = args.active if args.active is not None else min(st["active"], len(stages))
                compx_write_pair(fd, 0x02, len(stages), feat_mode, nfd)
                compx_write_pair(fd, 0x04, active - 1, feat_mode, nfd)
                for i, (sx, sy) in enumerate(stages):
                    if st.get("is_v8s") or st.get("dpi6"):
                        addr = (6912 if st.get("is_v8s") else 0x1B00) + i * 6
                        compx_write_flash(fd, addr, compx_encode_dpi6(sx, sy), feat_mode, nfd)
                    else:
                        addr = 0x0C + i * 4
                        compx_write_flash(fd, addr, compx_encode_dpi4(sx, sy, st["dpi_enc"]), feat_mode, nfd)
                st = compx_read_status(fd, meta, nfd)
            print(json.dumps({"active": st["active"], "stages": st["stages"]}))
        elif drv == "pulsar_pro":
            st = pulsar_pro_read_status(fd, meta)
            if args.set or args.active is not None:
                stages = [_parse_stage(s.strip()) for s in args.set.split(",") if s.strip()] if args.set else st["stages"]
                active = args.active if args.active is not None else min(st["active"], len(stages))
                flat = []
                for sx, sy in stages[:6]:
                    flat += [sx & 0xFF, sx >> 8, sy & 0xFF, sy >> 8]
                pulsar_pro_xfer(fd, 0xB1, [1, len(stages[:6]), active, *flat])
                pulsar_pro_commit(fd)
                st = pulsar_pro_read_status(fd, meta)
            print(json.dumps({"active": st["active"], "stages": st["stages"]}))
        else:
            raise OSError(f"{drv} does not support onboard stage editing")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def poll_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.hz is not None:
                set_poll(fd, args.hz, meta["hyper"])
            print(f"{get_poll(fd, meta['hyper'])} Hz")
        elif drv == "egg8k":
            prof = meta["profile"]
            if args.hz is not None:
                parsed = egg_update_config(fd, prof, {"polling_hz": args.hz})
            else:
                parsed = egg_parse_config(egg_read_config(fd), prof)
            print(f"{parsed['polling_hz']} Hz")
        elif drv == "hidpp":
            dev_idx, _ = hidpp_probe_device(fd)
            rate_idx = hidpp_get_feature(fd, dev_idx, 0x8060)
            if not rate_idx:
                raise OSError("device does not expose HID++ AdjustableReportRate (0x8060)")
            if args.hz is not None:
                if args.hz not in (125, 250, 500, 1000):
                    raise ValueError("HID++ 0x8060 polling rate must be 125, 250, 500, or 1000 Hz")
                hidpp_xfer(fd, dev_idx, rate_idx, 0x02, [1000 // args.hz])
            r = hidpp_xfer(fd, dev_idx, rate_idx, 0x01, [0])
            print(f"{round(1000 / r[0])} Hz" if r[0] else "unknown")
        elif drv in ("compx", "egg_we"):
            st = compx_read_status(fd, meta, nfd)
            if args.hz is not None:
                if args.hz not in st["rates"]:
                    raise ValueError(f"unsupported polling rate {args.hz}; valid: {st['rates']}")
                feat_mode, _, _ = _compx_ctx(meta)
                code = COMPX_RATES_INV[args.hz]
                compx_write_pair(fd, 0x00, code, feat_mode, nfd)
                st = compx_read_status(fd, meta, nfd)
            print(f"{st['polling_hz']} Hz")
        elif drv == "pulsar_xs1":
            if args.hz is not None:
                raise ValueError("Pulsar XS-1 polling rate is read-only over USB HID (hardware dongle button only)")
            st = pulsar_xs1_read_status(fd, meta)
            print(f"{st['polling_hz']} Hz (read-only)")
        elif drv == "pulsar_pro":
            if args.hz is not None:
                if args.hz not in (125, 250, 500, 1000, 2000, 4000, 8000):
                    raise ValueError("invalid polling rate")
                pulsar_pro_xfer(fd, 0xB2, [1, args.hz & 0xFF, args.hz >> 8])
                pulsar_pro_commit(fd)
            st = pulsar_pro_read_status(fd, meta)
            print(f"{st['polling_hz']} Hz")
        elif drv == "glorious_core2":
            if args.hz is not None:
                if args.hz not in CORE2_RATES:
                    raise ValueError(f"unsupported polling rate {args.hz}")
                target = 0x00 if meta.get("wireless") else 0x02
                wcode = CORE2_RATES[args.hz]
                wlcode = CORE2_RATES[min(args.hz, 4000)]
                core2_xfer(fd, target, 0x01, 0x0A, [0x01, wcode, wlcode])
                _save_device_cache(_cache_key(meta), {"polling_hz": args.hz})
            print(f"{core2_read_status(fd, meta)['polling_hz']} Hz")
        elif drv == "glorious_pixart":
            if args.hz is not None:
                if args.hz not in (125, 250, 500, 1000):
                    raise ValueError("Pixart polling rate must be 125, 250, 500, or 1000 Hz")
                pixart_write_config(fd, meta, hz=args.hz)
            print(f"{read_device_status(meta)['polling_hz']} Hz")
        else:
            raise OSError(f"{drv} does not support polling rate changes")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def lod_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.preset:
                set_lod_preset(fd, args.preset)
            elif args.async_vals:
                set_lod_async(fd, args.async_vals[0], args.async_vals[1])
            print(json.dumps(get_lod(fd)))
        elif drv == "egg8k":
            prof = meta["profile"]
            cur = egg_parse_config(egg_read_config(fd), prof)
            if args.async_vals:
                raise ValueError("Endgame Gear 8k uses discrete LOD steps, not Razer asymmetric LOD")
            if args.preset or args.index is not None:
                opts = EGG_GLASS_LOD if cur["glass_mode"] else prof["lod_options"]
                if args.index is not None:
                    idx = args.index
                else:
                    p = args.preset.lower()
                    if p in LOD_PRESETS:
                        idx = min(LOD_PRESETS[p], len(opts) - 1) if p != "high" else len(opts) - 1
                    else:
                        raise ValueError("use --preset low|medium|high or --index N")
                cur = egg_update_config(fd, prof, {"lod_index": idx})
            print(json.dumps(cur["lod"]))
        elif drv in ("compx", "egg_we"):
            feat_mode, _, brand = _compx_ctx(meta)
            st = compx_read_status(fd, meta, nfd)
            if args.preset or args.index is not None:
                if st.get("is_v8s"):
                    idx = args.index if args.index is not None else {"low": 0, "med": 2, "medium": 2, "high": 4}[args.preset.lower()]
                    compx_write_pair(fd, 0x0A, idx + 1, feat_mode, nfd)
                else:
                    p = (args.preset or "medium").lower()
                    if p == "med":
                        p = "medium"
                    if brand in ("pulsar_compx", "pulsar_4k", "vgn", "teevolution"):
                        code = {"low": 3, "medium": 1, "high": 2}[p]
                    else:
                        code = {"low": 1, "medium": 1 if drv == "egg_we" or brand == "areson" else 2, "high": 2}[p]
                    compx_write_pair(fd, 0x0A, code, feat_mode, nfd)
                st = compx_read_status(fd, meta, nfd)
            print(json.dumps(st["lod"]))
        elif drv == "pulsar_xs1":
            if args.preset:
                p = "medium" if args.preset.lower() == "med" else args.preset.lower()
                pulsar_xs1_xfer(fd, [0x07, 0x02, 0x03, 0, 0, 1, 2, XS1_LOD_INV[p]])
            print(json.dumps(pulsar_xs1_read_status(fd, meta)["lod"]))
        elif drv == "pulsar_pro":
            if args.preset:
                p = "medium" if args.preset.lower() == "med" else args.preset.lower()
                pulsar_pro_xfer(fd, 0xB3, [1, {"low": 1, "medium": 2, "high": 3}[p]])
                pulsar_pro_commit(fd)
            print(json.dumps(pulsar_pro_read_status(fd, meta)["lod"]))
        else:
            raise OSError(f"{drv} does not support LOD")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def power_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    if not meta.get("wireless"):
        sys.exit("selected device is wired (no wireless power settings)")
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.idle is not None:
                set_idle(fd, args.idle * 60)
            if args.low is not None:
                set_low(fd, args.low)
            print(json.dumps({"idle_minutes": round(get_idle(fd) / 60), "low_battery_percent": get_low(fd)}))
        elif drv in ("compx", "egg_we"):
            feat_mode, _, brand = _compx_ctx(meta)
            if args.idle is not None:
                val = max(1, min(180, round((args.idle * 60) / 10)))
                compx_write_pair(fd, 0xAD, val, feat_mode, nfd)
                if brand in ("pulsar_compx", "pulsar_4k", "scyrox"):
                    compx_write_pair(fd, 0xB7, val, feat_mode, nfd)
            st = compx_read_status(fd, meta, nfd)
            print(json.dumps({"idle_seconds": st.get("idle_seconds"), "battery_percent": st.get("battery_percent")}))
        elif drv == "pulsar_pro":
            if args.idle is not None:
                ms = max(10000, min(1800000, args.idle * 60000))
                pulsar_pro_xfer(fd, 0xC2, [1, ms & 0xFF, (ms >> 8) & 0xFF, (ms >> 16) & 0xFF, (ms >> 24) & 0xFF])
                pulsar_pro_commit(fd)
            if args.low is not None:
                pulsar_pro_xfer(fd, 0xBE, [1, max(0, min(30, args.low))])
                pulsar_pro_commit(fd)
            st = pulsar_pro_read_status(fd, meta)
            print(json.dumps({"idle_seconds": st.get("idle_seconds"), "battery_percent": st.get("battery_percent")}))
        else:
            raise OSError(f"{drv} does not support wireless power tuning")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def _bool_arg(v):
    if v is None:
        return None
    if v.lower() in ("1", "true", "on", "yes"):
        return True
    if v.lower() in ("0", "false", "off", "no"):
        return False
    raise ValueError(f"expected on/off or true/false, got {v!r}")


def tune_cmd(args):
    meta = select_mice(args.device)[0]
    drv = meta["driver"]
    updates = {}
    for k in ("motion_sync", "angle_snapping", "ripple_control", "slamclick",
              "motion_jitter", "glass_mode", "force_max_fps", "led_liftoff_disabled", "left_handed"):
        val = getattr(args, k, None)
        if val is not None:
            updates[k] = _bool_arg(val)
    if args.angle_tuning is not None:
        updates["angle_tuning"] = args.angle_tuning
    if args.spdt is not None:
        updates["spdt"] = args.spdt
    deb = getattr(args, "debounce", None)

    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "egg8k":
            prof = meta["profile"]
            st = egg_update_config(fd, prof, updates) if updates else egg_parse_config(egg_read_config(fd), prof)
            print(json.dumps({
                "device": meta["name"], "driver": drv,
                "motion_sync": st["motion_sync"], "angle_snapping": st["angle_snapping"],
                "ripple_control": st["ripple_control"], "slamclick": st["slamclick"],
                "motion_jitter": st["motion_jitter"], "glass_mode": st["glass_mode"],
                "angle_tuning": st["angle_tuning"], "force_max_fps": st["force_max_fps"],
                "led_liftoff_disabled": st["led_liftoff_disabled"], "left_handed": st["left_handed"],
                "spdt_left": st["buttons"][0]["spdt"], "spdt_right": st["buttons"][1]["spdt"],
            }, indent=2))
        elif drv in ("compx", "egg_we"):
            feat_mode, _, _ = _compx_ctx(meta)
            if deb is not None:
                compx_write_pair(fd, 0xA9, max(0, min(30, deb)), feat_mode, nfd)
            if "motion_sync" in updates:
                compx_write_pair(fd, 0xAF, 1 if updates["motion_sync"] else 0, feat_mode, nfd)
            if "angle_snapping" in updates:
                compx_write_pair(fd, 0xB1, 1 if updates["angle_snapping"] else 0, feat_mode, nfd)
            if "ripple_control" in updates:
                compx_write_pair(fd, 0xB3, 1 if updates["ripple_control"] else 0, feat_mode, nfd)
            print(json.dumps(compx_read_status(fd, meta, nfd), indent=2))
        elif drv == "pulsar_xs1":
            if deb is not None:
                pulsar_xs1_xfer(fd, [0x04, 0x03, 0x03, 0, 0, 1, max(0, min(15, deb))])
            if "motion_sync" in updates:
                pulsar_xs1_xfer(fd, [0x07, 0x05, 0x02, 0, 0, 1, int(updates["motion_sync"])])
            if "angle_snapping" in updates:
                pulsar_xs1_xfer(fd, [0x07, 0x04, 0x02, 0, 0, 1, int(updates["angle_snapping"])])
            if "ripple_control" in updates:
                pulsar_xs1_xfer(fd, [0x07, 0x03, 0x02, 0, 0, 1, int(updates["ripple_control"])])
            print(json.dumps(pulsar_xs1_read_status(fd, meta), indent=2))
        elif drv == "pulsar_pro":
            changed = False
            if deb is not None:
                pulsar_pro_xfer(fd, 0xC5, [1, max(0, min(20, deb))]); changed = True
            if "motion_sync" in updates:
                pulsar_pro_xfer(fd, 0xB5, [1, int(updates["motion_sync"])]); changed = True
            if "ripple_control" in updates:
                pulsar_pro_xfer(fd, 0xB6, [1, int(updates["ripple_control"])]); changed = True
            if "angle_snapping" in updates:
                pulsar_pro_xfer(fd, 0xB7, [1, int(updates["angle_snapping"])]); changed = True
            if "angle_tuning" in updates:
                at = max(-30, min(30, updates["angle_tuning"])) & 0xFF
                pulsar_pro_xfer(fd, 0xB8, [1, at]); changed = True
            if changed:
                pulsar_pro_commit(fd)
            print(json.dumps(pulsar_pro_read_status(fd, meta), indent=2))
        elif drv == "glorious_core2":
            target = 0x00 if meta.get("wireless") else 0x02
            patch = {}
            if deb is not None:
                d = max(4, min(16, round(deb / 2) * 2))
                core2_xfer(fd, target, 0x00, 0x08, [0x01, d, 0, 0, 0, 0, 0])
                patch["debounce_ms"] = d
            if "motion_sync" in updates:
                core2_xfer(fd, target, 0x01, 0x09, [0x01, int(updates["motion_sync"])])
                patch["motion_sync"] = updates["motion_sync"]
            if patch:
                _save_device_cache(_cache_key(meta), patch)
            print(json.dumps(core2_read_status(fd, meta), indent=2))
        elif drv == "glorious_classic" and deb is not None:
            if meta.get("feat_len") != 64:
                raise OSError("Classic feature descriptor != 64 bytes")
            d = max(0, min(32, deb))
            pkt = bytearray(64)
            pkt[2] = 0x02; pkt[3] = 0x03; pkt[4] = 0x02; pkt[5] = 0x08; pkt[6] = 0x01; pkt[7] = d
            fcntl.ioctl(fd, HIDIOCSFEATURE_N(65), bytes([meta["feat_rid"]]) + pkt)
            _save_device_cache(_cache_key(meta), {"debounce_ms": d})
            print(json.dumps(classic_read_status(fd, meta), indent=2))
        elif drv == "glorious_pixart" and deb is not None:
            pixart_write_config(fd, meta, debounce_ms=max(0, min(16, deb)))
            print(json.dumps(read_device_status(meta), indent=2))
        else:
            raise OSError(f"{drv} does not support tune flags")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)



# ----------------------------------------------------------------------------- maccel
SYS = "/sys/module/maccel/parameters"
RESETS = "/var/opt/maccel/resets"
FIX = 1 << 32                      # maccel is built with 64-bit fixed point on x86_64: value * 2^32
MODES = ["linear", "natural", "synchronous", "no_accel"]
PARAMS = {
    "SENS_MULT": (0.01, 50), "YX_RATIO": (0.01, 50), "INPUT_DPI": (50, 50000), "ANGLE_ROTATION": (-180, 180),
    "ACCEL": (0, 50), "OFFSET": (0, 1000), "OUTPUT_CAP": (0, 100),
    "DECAY_RATE": (0.0001, 100), "LIMIT": (1, 100),
    "GAMMA": (0.0001, 100), "SMOOTH": (0, 1), "MOTIVITY": (1.0001, 100), "SYNC_SPEED": (0.0001, 1000),
}


def maccel_state():
    st = {"installed": os.path.isdir("/usr/src") and bool(glob.glob("/usr/src/maccel-*")),
          "loaded": os.path.isdir(SYS), "writable": False, "params": {}, "mode": None,
          "in_group": False, "group_exists": False}
    try:
        import grp
        g = grp.getgrnam("maccel")
        st["group_exists"] = True
        st["in_group"] = g.gr_gid in os.getgroups()
    except (KeyError, ImportError):
        pass
    if not st["loaded"]:
        return st
    st["writable"] = os.access(f"{SYS}/ACCEL", os.W_OK)
    for name in PARAMS:
        try:
            with open(f"{SYS}/{name}") as f:
                st["params"][name] = int(f.read().strip()) / FIX
        except (OSError, ValueError):
            pass
    try:
        with open(f"{SYS}/MODE") as f:
            m = int(f.read().strip())
        st["mode"] = MODES[m % len(MODES)]
    except (OSError, ValueError):
        pass
    return st


def _write_param(name, raw):
    with open(f"{SYS}/{name}", "w") as f:
        f.write(str(raw))
    try:
        with open(f"{RESETS}/set_last_{name}_value.sh", "w") as f:
            f.write(f"echo {raw} > /sys/module/maccel/parameters/{name};")
    except OSError:
        pass


def maccel_set(updates):
    st = maccel_state()
    if not st["loaded"]:
        raise PermissionError("maccel isn't loaded")
    if not st["writable"]:
        raise PermissionError("no write access to maccel — add yourself to the maccel group and log in again")
    todo = []
    for k, v in updates.items():
        if k == "MODE":
            if v not in MODES:
                raise ValueError(f"unknown mode {v!r}")
            todo.append(("MODE", MODES.index(v)))
            continue
        if k not in PARAMS:
            raise ValueError(f"unknown parameter {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{k} must be a number")
        lo, hi = PARAMS[k]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        todo.append((k, int(round(v * FIX))))
    for k, raw in todo:
        _write_param(k, raw)
    return maccel_state()


def maccel_cmd(args):
    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                sys.exit(f"expected KEY=VAL, got {item!r}")
            k, v = item.split("=", 1)
            k = k.strip().upper()
            v = v.strip()
            updates[k] = v if k == "MODE" else float(v)
        print(json.dumps(maccel_set(updates), indent=2))
    else:
        print(json.dumps(maccel_state(), indent=2))


def maccel_live_config():
    """Read-only snapshot of the loaded maccel module as {"MODE": ..., PARAM: value} (for rawaccel --live)."""
    st = maccel_state()
    if not st["loaded"]:
        raise PermissionError("maccel isn't loaded; use --input FILE or --set KEY=VAL instead of --live")
    missing = [k for k in PARAMS if k not in st["params"]]
    if st["mode"] is None or missing:
        raise OSError(f"could not read maccel MODE/{', '.join(missing) or 'params'} from {SYS}")
    return {"MODE": st["mode"], **st["params"]}


def rawaccel_cmd(args):
    import rawaccel_export  # pure module; offline preview/export only, never applies settings
    code = rawaccel_export.main(args.argv, live_loader=maccel_live_config, prog="mousetool.py rawaccel")
    if code:
        sys.exit(code)


# ----------------------------------------------------------------------------- live mouse motion telemetry
EV_SYN = 0x00
SYN_REPORT = 0x00
EV_REL = 0x02
REL_X = 0x00
REL_Y = 0x01
INPUT_EVENT_STRUCT = struct.Struct("llHHi")
MACCEL_DEV = "/dev/maccel"
NORMALIZED_DPI = 1000.0


def _parse_sysfs_bitmap(text):
    """Parse Linux sysfs capability bitmap (space-separated hex words, MSB first)."""
    bits = 0
    word_bits = struct.calcsize("L") * 8
    for tok in (text or "").strip().split():
        try:
            bits = (bits << word_bits) | int(tok, 16)
        except ValueError:
            return 0
    return bits


def discover_evdev_mice(sys_input_glob="/sys/class/input/event*", dev_dir="/dev/input"):
    """Return sorted /dev/input/event* paths that expose EV_REL with REL_X and REL_Y."""
    out = []
    for ev_dir in sorted(glob.glob(sys_input_glob)):
        ev_file = os.path.join(ev_dir, "device", "capabilities", "ev")
        rel_file = os.path.join(ev_dir, "device", "capabilities", "rel")
        try:
            with open(ev_file) as f:
                ev_bits = _parse_sysfs_bitmap(f.read())
            with open(rel_file) as f:
                rel_bits = _parse_sysfs_bitmap(f.read())
        except OSError:
            continue
        if (ev_bits & (1 << EV_REL)) and (rel_bits & (1 << REL_X)) and (rel_bits & (1 << REL_Y)):
            out.append(os.path.join(dev_dir, os.path.basename(ev_dir)))
    return out


def parse_evdev_events(data, pending=None):
    """Parse raw struct input_event bytes into (ts_sec, dx, dy) tuples emitted on SYN_REPORT."""
    if pending is None:
        pending = {"dx": 0, "dy": 0, "buf": b""}
    buf = pending.get("buf", b"") + data
    sz = INPUT_EVENT_STRUCT.size
    n_complete = (len(buf) // sz) * sz
    frames = []
    dx = pending.get("dx", 0)
    dy = pending.get("dy", 0)
    for off in range(0, n_complete, sz):
        tv_sec, tv_usec, ev_type, code, value = INPUT_EVENT_STRUCT.unpack_from(buf, off)
        if ev_type == EV_REL:
            if code == REL_X:
                dx += value
            elif code == REL_Y:
                dy += value
        elif ev_type == EV_SYN and code == SYN_REPORT:
            if dx != 0 or dy != 0:
                frames.append((tv_sec + tv_usec * 1e-6, dx, dy))
                dx = 0
                dy = 0
    pending["dx"] = dx
    pending["dy"] = dy
    pending["buf"] = buf[n_complete:]
    return frames, pending


def _read_maccel_fd(fd):
    """Read maccel kernel module's LAST_INPUT_MOUSE_SPEED (big-endian Q32.32 fixed-point counts/ms)."""
    try:
        raw = os.read(fd, 8)
    except OSError:
        return None
    if len(raw) == 8:
        val = struct.unpack(">q", raw)[0]
        return val, max(0.0, val / FIX)
    if len(raw) == 4:
        val = struct.unpack(">i", raw)[0]
        return val, max(0.0, val / (1 << 16))
    return None


def read_maccel_speed(dev_path=MACCEL_DEV):
    """Read (raw_fixed, speed_counts_per_ms) from /dev/maccel, or None if unreadable."""
    try:
        fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return _read_maccel_fd(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def compute_normalized_speed(dx, dy, dt_ms, input_dpi=NORMALIZED_DPI):
    """Compute maccel-equivalent input speed in counts/ms normalized to 1000 DPI.

    Matches maccel accel.h / speed.h:
      dpi_factor = 1000.0 / input_dpi
      speed = hypot(dx * dpi_factor, dy * dpi_factor) / dt_ms
    """
    if dt_ms is None or dt_ms <= 0 or not math.isfinite(dt_ms):
        return 0.0
    if input_dpi is None or input_dpi <= 0 or not math.isfinite(input_dpi):
        return 0.0
    dpi_factor = NORMALIZED_DPI / float(input_dpi)
    return math.hypot(dx * dpi_factor, dy * dpi_factor) / float(dt_ms)


class MotionTracker:
    """Tracks live smoothed speed, active swipe range, idle decay, and recent swipe peak hold."""

    def __init__(self, input_dpi=NORMALIZED_DPI, smooth_tau_ms=10.0, idle_ms=45.0,
                 decay_tau_ms=35.0, swipe_gap_s=0.22, swipe_threshold=0.35, peak_hold_s=2.8):
        self.input_dpi = float(input_dpi) if input_dpi and input_dpi > 0 else NORMALIZED_DPI
        self.smooth_tau_ms = float(smooth_tau_ms)
        self.idle_ms = float(idle_ms)
        self.decay_tau_ms = float(decay_tau_ms)
        self.swipe_gap_s = float(swipe_gap_s)
        self.swipe_threshold = float(swipe_threshold)
        self.peak_hold_s = float(peak_hold_s)
        self.raw_speed = 0.0
        self._speed = 0.0
        self._last_event_ts = None
        self._last_motion_ts = None
        self._peak = 0.0
        self._peak_ts = None
        self._swipe_min = 0.0
        self._swipe_max = 0.0
        self._in_swipe = False
        self.swipe_id = 0

    def set_input_dpi(self, dpi):
        if dpi and math.isfinite(dpi) and dpi > 0:
            scale = self.input_dpi / dpi
            self._speed *= scale
            self.raw_speed *= scale
            self._peak *= scale
            self._swipe_min *= scale
            self._swipe_max *= scale
            self.input_dpi = float(dpi)

    def record_delta(self, dx, dy, dt_ms, now=None):
        spd = compute_normalized_speed(dx, dy, dt_ms, self.input_dpi)
        self.record_speed(spd, dt_ms=dt_ms, now=now)
        return spd

    def record_speed(self, inst_speed, dt_ms=None, now=None):
        if inst_speed is None or not math.isfinite(inst_speed):
            return
        inst = max(0.0, float(inst_speed))
        now = time.monotonic() if now is None else float(now)
        gap_s = (now - self._last_event_ts) if self._last_event_ts is not None else float("inf")
        if dt_ms is None or not math.isfinite(dt_ms) or dt_ms <= 0:
            eff_dt = min(50.0, max(0.1, gap_s * 1000.0)) if math.isfinite(gap_s) else self.smooth_tau_ms
        else:
            eff_dt = float(dt_ms)

        self.raw_speed = inst
        if self._speed <= 0.05 or gap_s >= self.idle_ms / 1000.0:
            self._speed = inst
        else:
            alpha = 1.0 - math.exp(-max(0.1, eff_dt) / self.smooth_tau_ms)
            self._speed = self._speed + alpha * (inst - self._speed)
        self._last_event_ts = now

        if self._speed >= self.swipe_threshold:
            new_swipe = (
                not self._in_swipe
                and (self._last_motion_ts is None or (now - self._last_motion_ts) >= self.swipe_gap_s)
            ) or self._peak <= 0.0
            if new_swipe:
                self.swipe_id += 1
                self._peak = self._speed
                self._peak_ts = now
                self._swipe_min = self._speed
                self._swipe_max = self._speed
                self._in_swipe = True
            else:
                self._in_swipe = True
                if self._speed > self._peak:
                    self._peak = self._speed
                self._peak_ts = now
                self._swipe_min = min(self._swipe_min, self._speed)
                self._swipe_max = max(self._swipe_max, self._speed)
            self._last_motion_ts = now

    def snapshot(self, now=None, dpi=None):
        now = time.monotonic() if now is None else float(now)
        idle_s = (now - self._last_event_ts) if self._last_event_ts is not None else float("inf")
        idle_ms = idle_s * 1000.0
        active = False

        if self._last_event_ts is None:
            cur_speed = 0.0
        elif idle_ms <= self.idle_ms:
            cur_speed = self._speed
            active = cur_speed > 0.05
        else:
            decay_ms = idle_ms - self.idle_ms
            cur_speed = self._speed * math.exp(-decay_ms / self.decay_tau_ms)
            if cur_speed < 0.05:
                cur_speed = 0.0
                self._speed = 0.0
                self.raw_speed = 0.0
                self._in_swipe = False

        if idle_s >= self.swipe_gap_s:
            self._in_swipe = False

        peak_age_ms = 0
        if self._peak > 0.0 and self._peak_ts is not None:
            peak_age_s = max(0.0, now - self._peak_ts)
            if not active and peak_age_s >= self.peak_hold_s:
                self._peak = 0.0
                self._swipe_min = 0.0
                self._swipe_max = 0.0
                self._peak_ts = None
            else:
                peak_age_ms = int(round(peak_age_s * 1000.0))

        base_dpi = self.input_dpi if self.input_dpi > 0 else NORMALIZED_DPI
        out_dpi = float(dpi) if (dpi is not None and math.isfinite(dpi) and dpi > 0) else base_dpi
        scale = base_dpi / out_dpi
        return {
            "speed": round(cur_speed * scale, 3),
            "raw_speed": round((self.raw_speed if active else 0.0) * scale, 3),
            "peak": round(self._peak * scale, 3),
            "peak_age_ms": peak_age_ms,
            "peak_hold_ms": int(round(self.peak_hold_s * 1000.0)),
            "swipe_min": round(self._swipe_min * scale, 3),
            "swipe_max": round(self._swipe_max * scale, 3),
            "swipe_id": self.swipe_id,
            "active": active,
            "dpi": round(out_dpi, 2),
        }


def _default_kernel_dpi():
    try:
        st = maccel_state()
        if st.get("loaded"):
            d = st.get("params", {}).get("INPUT_DPI")
            if d and math.isfinite(d) and d > 0:
                return float(d)
    except Exception:
        pass
    return NORMALIZED_DPI


def _raw_evdev_available():
    # Only a confirmed absent module permits interpreting REL events as input.
    try:
        os.stat(os.path.dirname(SYS))
    except FileNotFoundError:
        return os.path.isdir("/sys/module")
    except OSError:
        pass
    return False


class MotionHub:
    """Lazy, hotplug-safe live mouse motion hub shared by SSE subscribers.

    Starts the evdev + /dev/maccel reader thread on the first subscriber and
    stops it cleanly (closing all descriptors) when the last subscriber leaves.
    """

    def __init__(self, evdev_discover=discover_evdev_mice, maccel_dev=MACCEL_DEV,
                 dpi_provider=_default_kernel_dpi, raw_evdev_available=_raw_evdev_available):
        self._evdev_discover = evdev_discover
        self._maccel_dev = maccel_dev
        self._dpi_provider = dpi_provider
        self._raw_evdev_available = raw_evdev_available
        self._lifecycle = threading.Lock()
        self._last_kernel_sample = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._tracker = MotionTracker(input_dpi=NORMALIZED_DPI)
        self._subscribers = 0
        self._thread = None
        self._stop = threading.Event()
        self._wake_r = None
        self._wake_w = None
        self._source = "none"
        self._device_count = 0
        self._last_dpi_refresh = 0.0
        self._seq = 0

    @property
    def subscriber_count(self):
        with self._lock:
            return self._subscribers

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def refresh_dpi(self):
        dpi = self._dpi_provider()
        with self._lock:
            self._tracker.set_input_dpi(dpi)
            self._last_dpi_refresh = time.monotonic()

    def _wake_reader(self):
        w = self._wake_w
        if w is not None:
            try:
                os.write(w, b"x")
            except OSError:
                pass

    def subscribe(self):
        with self._lifecycle, self._lock:
            self._subscribers += 1
            if self._subscribers == 1 and (self._thread is None or not self._thread.is_alive()):
                self._stop.clear()
                self._wake_r, self._wake_w = os.pipe()
                os.set_blocking(self._wake_r, False)
                os.set_blocking(self._wake_w, False)
                self._tracker.set_input_dpi(self._dpi_provider())
                self._last_dpi_refresh = time.monotonic()
                t = threading.Thread(target=self._run, name="mousetool-motion", daemon=True)
                self._thread = t
                t.start()

    def unsubscribe(self):
        with self._lifecycle:
            self._unsubscribe()

    def _unsubscribe(self):
        t = None
        with self._lock:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0 and self._thread is not None:
                self._stop.set()
                self._wake_reader()
                t = self._thread
                self._cond.notify_all()
        if t is not None and t is not threading.current_thread():
            t.join()
        with self._lock:
            if self._subscribers == 0:
                self._thread = None
                self._tracker = MotionTracker()
                self._source = "none"
                self._device_count = 0
                self._last_kernel_sample = None
                for fd in (self._wake_r, self._wake_w):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                self._wake_r = None
                self._wake_w = None

    def snapshot(self, dpi=None, now=None):
        with self._lock:
            snap = self._tracker.snapshot(now=now, dpi=dpi)
            if self._source == "maccel":
                last = self._last_kernel_sample
                snap["last_speed"] = (last[0] * last[1] / snap["dpi"]
                                      if last is not None else None)
            snap["source"] = self._source
            snap["devices"] = self._device_count
            return snap

    def wait_update(self, last_seq, timeout=1.0):
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout=timeout)
            return self._seq

    def _open_maccel_fd(self):
        if not self._maccel_dev:
            return None
        try:
            return os.open(self._maccel_dev, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None

    def _sync_evdev_fds(self, fds_by_path):
        wanted = set(self._evdev_discover() or [])
        for path in list(fds_by_path):
            if path not in wanted:
                fd, _ = fds_by_path.pop(path)
                try:
                    os.close(fd)
                except OSError:
                    pass
        for path in sorted(wanted):
            if path in fds_by_path:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            fds_by_path[path] = (fd, {"dx": 0, "dy": 0, "buf": b"", "last_ts": None})

    def _update_source(self, has_evdev, has_maccel, dev_count):
        if has_evdev and has_maccel:
            src = "maccel+evdev"
        elif has_evdev and self._raw_evdev_available():
            src = "evdev"
        elif has_maccel:
            src = "maccel"
        else:
            src = "none"
        with self._cond:
            changed = (src != self._source) or (dev_count != self._device_count)
            if src != self._source:
                self._last_kernel_sample = None
                self._tracker = MotionTracker(input_dpi=self._tracker.input_dpi)
            self._source = src
            self._device_count = dev_count
            if changed:
                self._seq += 1
                self._cond.notify_all()

    def _run(self):
        fds_by_path = {}
        maccel_fd = None
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_scan >= 2.0:
                    self._sync_evdev_fds(fds_by_path)
                    if maccel_fd is None:
                        maccel_fd = self._open_maccel_fd()
                    self._update_source(bool(fds_by_path), maccel_fd is not None, len(fds_by_path))
                    last_scan = now

                if now - self._last_dpi_refresh >= 0.5:
                    self.refresh_dpi()

                wake_r = self._wake_r
                if wake_r is None:
                    break

                if fds_by_path:
                    fd_to_path = {fd: p for p, (fd, _) in fds_by_path.items()}
                    snap = self.snapshot(now=now)
                    busy = snap["active"] or snap["speed"] > 0 or snap["peak"] > 0
                    timeout = 0.016 if busy else 2.0
                    try:
                        rlist, _, _ = select.select([wake_r] + list(fd_to_path), [], [], timeout)
                    except (OSError, ValueError):
                        last_scan = 0.0
                        continue

                    if wake_r in rlist:
                        try:
                            os.read(wake_r, 64)
                        except OSError:
                            pass
                        if self._stop.is_set():
                            break

                    now = time.monotonic()
                    any_motion = False
                    rescan = False
                    for fd in rlist:
                        if fd == wake_r:
                            continue
                        path = fd_to_path.get(fd)
                        if not path or path not in fds_by_path:
                            continue
                        _, st = fds_by_path[path]
                        try:
                            chunk = os.read(fd, 4096)
                        except BlockingIOError:
                            continue
                        except OSError:
                            chunk = b""
                        if not chunk:
                            fds_by_path.pop(path, None)
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            rescan = True
                            continue

                        frames, _ = parse_evdev_events(chunk, st)
                        if not frames:
                            continue

                        if maccel_fd is not None:
                            res = _read_maccel_fd(maccel_fd)
                            if res is not None:
                                _, spd = res
                                last_ts = st["last_ts"]
                                end_ts = frames[-1][0]
                                dt_ms = (end_ts - last_ts) * 1000.0 if last_ts is not None else None
                                st["last_ts"] = end_ts
                                with self._lock:
                                    self._tracker.record_speed(spd, dt_ms=dt_ms, now=now)
                                any_motion = True
                            else:
                                os.close(maccel_fd)
                                maccel_fd = None
                                self._update_source(True, False, len(fds_by_path))

                        elif self._source == "evdev" and self._raw_evdev_available():
                            with self._lock:
                                for ts_s, dx, dy in frames:
                                    last_ts = st["last_ts"]
                                    st["last_ts"] = ts_s
                                    if last_ts is not None:
                                        dt_ms = (ts_s - last_ts) * 1000.0
                                        if dt_ms > 0:
                                            self._tracker.record_delta(dx, dy, dt_ms, now=now)
                                            any_motion = True

                    if any_motion:
                        with self._cond:
                            self._seq += 1
                            self._cond.notify_all()
                    if rescan:
                        last_scan = 0.0
                elif maccel_fd is not None:
                    try:
                        rlist, _, _ = select.select([wake_r], [], [], 0.016)
                    except (OSError, ValueError):
                        break
                    if wake_r in rlist:
                        try:
                            os.read(wake_r, 64)
                        except OSError:
                            pass
                        if self._stop.is_set():
                            break
                    res = _read_maccel_fd(maccel_fd)
                    if res is None:
                        os.close(maccel_fd)
                        maccel_fd = None
                        self._update_source(False, False, 0)
                        continue
                    _, speed = res
                    with self._cond:
                        if self._last_kernel_sample is None or speed != self._last_kernel_sample[0]:
                            self._last_kernel_sample = (speed, self._tracker.input_dpi)
                            self._seq += 1
                            self._cond.notify_all()
                else:
                    try:
                        rlist, _, _ = select.select([wake_r], [], [], 2.0)
                    except (OSError, ValueError):
                        break
                    if wake_r in rlist:
                        try:
                            os.read(wake_r, 64)
                        except OSError:
                            pass
        finally:
            for fd, _ in fds_by_path.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            fds_by_path.clear()
            if maccel_fd is not None:
                try:
                    os.close(maccel_fd)
                except OSError:
                    pass


MOTION_HUB = MotionHub()


# ----------------------------------------------------------------------------- local api
ALLOWED_ORIGINS = {"null"}


class Api(BaseHTTPRequestHandler):
    server_version = "mousetool/2"

    def log_message(self, fmt, *a):
        pass

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o in ALLOWED_ORIGINS or re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d+)?", o or "")

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        o = self.headers.get("Origin")
        if o and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_motion_sse(self, query):
        dpi = None
        if "dpi" in query and query["dpi"]:
            try:
                dpi = float(query["dpi"][0])
                if not math.isfinite(dpi) or not (50 <= dpi <= 50000):
                    raise ValueError()
            except ValueError:
                return self._send(400, {"error": "dpi must be between 50 and 50000"})

        self.send_response(200)
        o = self.headers.get("Origin")
        if o and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        hub = MOTION_HUB
        with hub._lock:
            before_seq = hub._seq
        hub.subscribe()
        try:
            # Give reader a brief beat on first subscribe so initial frame carries live source info.
            last_seq = hub.wait_update(before_seq, timeout=0.05)
            last_payload = None
            last_send = 0.0
            frame_dt = 1.0 / 60.0
            while True:
                if self._client_closed():
                    break
                snap = hub.snapshot(dpi=dpi)
                payload = json.dumps(snap, separators=(",", ":"))
                now = time.monotonic()
                moving_or_holding = snap["active"] or snap["speed"] > 0 or snap["peak"] > 0
                if payload != last_payload:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_payload = payload
                    last_send = now
                elif now - last_send >= 15.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_send = now

                if moving_or_holding:
                    rem = frame_dt - (time.monotonic() - now)
                    if rem > 0:
                        time.sleep(rem)
                    with hub._lock:
                        last_seq = hub._seq
                else:
                    last_seq = hub.wait_update(last_seq, timeout=0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe()

    def _client_closed(self):
        conn = getattr(self, "connection", None)
        if conn is None or not hasattr(conn, "fileno"):
            return False
        try:
            r, _, _ = select.select([conn], [], [], 0)
            if r:
                data = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                return data == b""
        except BlockingIOError:
            return False
        except OSError:
            return True
        return False

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "null"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mouse-Tool")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed"})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/desktop":
            return self._send(200, {"desktop": desktop_state()})
        if parsed.path == "/api/status":
            return self._send(200, {
                "version": 2,
                "maccel": maccel_state(),
                "niri": niri_state(),
                "desktop": desktop_state(),
                "system_mice": discover_system_mice(),
            })
        if parsed.path == "/api/devices":
            return self._send(200, {"mice": discover_system_mice()})
        if parsed.path in ("/api/motion", "/api/motion/stream"):
            return self._serve_motion_sse(urllib.parse.parse_qs(parsed.query))
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._origin_ok() or self.headers.get("X-Mouse-Tool") != "1":
            return self._send(403, {"error": "forbidden"})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ("/api/maccel", "/api/niri", "/api/desktop"):
            return self._send(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            if parsed.path == "/api/maccel":
                st = maccel_set(body)
                MOTION_HUB.refresh_dpi()
                self._send(200, {"maccel": st})
            elif parsed.path == "/api/desktop":
                self._send(200, {"desktop": desktop_state(body)})
            else:
                with desktop_pointer.LOCK:
                    self._send(200, {"niri": niri_set(body)})
        except PermissionError as e:
            if parsed.path == "/api/desktop":
                return self._send(409, {"error": str(e), "desktop": desktop_state()})
            self._send(409, {"error": str(e), "maccel": maccel_state()})
        except (ValueError, TypeError) as e:
            self._send(400, {"error": str(e)})
        except OSError as e:
            self._send(500, {"error": str(e)})


def serve(args):
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Api)
    print(f"mousetool listening on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


def open_ui(_args):
    page = "file://" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    for browser in ("brave", "brave-browser", "chromium", "chromium-browser", "google-chrome"):
        if shutil.which(browser):
            subprocess.Popen([browser, f"--app={page}"], start_new_session=True)
            return
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", page], start_new_session=True)
        return
    sys.exit(f"no WebHID browser found; open {page} manually")


def main():
    ap = argparse.ArgumentParser(prog="mousetool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list"); ls.add_argument("--json", action="store_true"); ls.set_defaults(fn=list_cmd)

    b = sub.add_parser("battery")
    b.add_argument("-d", "--device", help="device selector (index, name, vid:pid, or /dev/hidrawN)")
    b.add_argument("--all", action="store_true", help="query all connected wireless mice")
    b.add_argument("--json", action="store_true")
    b.set_defaults(fn=battery)

    st = sub.add_parser("status")
    st.add_argument("-d", "--device", help="device selector")
    st.add_argument("--all", action="store_true", help="query all connected configurable mice")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=status_cmd)

    dp = sub.add_parser("dpi")
    dp.add_argument("-d", "--device", help="device selector")
    dp.add_argument("x", nargs="?", type=int)
    dp.add_argument("y", nargs="?", type=int)
    dp.set_defaults(fn=dpi_cmd)

    sg = sub.add_parser("stages")
    sg.add_argument("-d", "--device", help="device selector")
    sg.add_argument("--active", type=int)
    sg.add_argument("--set", help="comma-separated DPI values, e.g. 400,800,1600 or 800:1000")
    sg.set_defaults(fn=stages_cmd)

    pl = sub.add_parser("poll")
    pl.add_argument("-d", "--device", help="device selector")
    pl.add_argument("hz", nargs="?", type=int)
    pl.set_defaults(fn=poll_cmd)

    ld = sub.add_parser("lod")
    ld.add_argument("-d", "--device", help="device selector")
    ld.add_argument("--preset", choices=["low", "med", "medium", "high"])
    ld.add_argument("--index", type=int, help="LOD step index (Endgame Gear 8k)")
    ld.add_argument("--async", dest="async_vals", nargs=2, type=int, metavar=("LIFT", "LAND"))
    ld.set_defaults(fn=lod_cmd)

    pw = sub.add_parser("power")
    pw.add_argument("-d", "--device", help="device selector")
    pw.add_argument("--idle", type=int, help="sleep timer in minutes (1-15)")
    pw.add_argument("--low", type=int, help="low battery warning percent (5-100)")
    pw.set_defaults(fn=power_cmd)

    tn = sub.add_parser("tune", help="Sensor, click & performance filters")
    tn.add_argument("-d", "--device", help="device selector")
    tn.add_argument("--debounce", type=int, help="click debounce time in ms")
    tn.add_argument("--motion-sync", choices=["on", "off"])
    tn.add_argument("--angle-snap", dest="angle_snapping", choices=["on", "off"])
    tn.add_argument("--ripple", dest="ripple_control", choices=["on", "off"])
    tn.add_argument("--slamclick", choices=["on", "off"])
    tn.add_argument("--jitter", dest="motion_jitter", choices=["on", "off"])
    tn.add_argument("--glass", dest="glass_mode", choices=["on", "off"])
    tn.add_argument("--angle-tune", dest="angle_tuning", type=int, help="v2 only (-127..127)")
    tn.add_argument("--max-fps", dest="force_max_fps", choices=["on", "off"])
    tn.add_argument("--led-liftoff", dest="led_liftoff_disabled", choices=["on", "off"])
    tn.add_argument("--left-handed", choices=["on", "off"])
    tn.add_argument("--spdt", choices=["off", "gx_speed", "gx_safe"])
    tn.set_defaults(fn=tune_cmd)

    m = sub.add_parser("maccel")
    m.add_argument("--set", nargs="+", metavar="KEY=VAL", help="update maccel parameters, e.g. MODE=natural LIMIT=1.5")
    m.set_defaults(fn=maccel_cmd)

    ra = sub.add_parser("rawaccel", add_help=False,
                        help="offline Raw Accel (Windows) preview/export of maccel curves; never applies settings")
    ra.add_argument("argv", nargs=argparse.REMAINDER, help="export | preview | import ... (see `rawaccel --help`)")
    ra.set_defaults(fn=rawaccel_cmd)

    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8766); s.set_defaults(fn=serve)
    o = sub.add_parser("open"); o.set_defaults(fn=open_ui)

    if sys.argv[1:2] == ["rawaccel"]:
        # Hand everything after `rawaccel` (including --help) to rawaccel_export's own parser.
        args = argparse.Namespace(fn=rawaccel_cmd, argv=sys.argv[2:])
    else:
        args = ap.parse_args()
    try:
        args.fn(args)
    except (FileNotFoundError, Asleep, ValueError, PermissionError, OSError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
