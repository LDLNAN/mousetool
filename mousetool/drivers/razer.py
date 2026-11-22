"""Razer 90-byte HID feature report protocol driver."""
import sys
import time

if sys.platform != "win32":
    import fcntl

from .base import Asleep, HIDIOCGFEATURE, HIDIOCSFEATURE, LOD_NAMES, LOD_PRESETS

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

POLL_CODES = {1: 1000, 2: 500, 4: 250, 8: 125}
POLL_HZ = {hz: code for code, hz in POLL_CODES.items()}
POLL2_CODES = {0x01: 8000, 0x02: 4000, 0x04: 2000, 0x08: 1000, 0x10: 500, 0x20: 250, 0x40: 125}
POLL2_HZ = {hz: code for code, hz in POLL2_CODES.items()}


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


def razer_read_status(fd, meta):
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
