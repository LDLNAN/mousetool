"""Razer 90-byte HID feature report protocol driver."""
import sys
import time

if sys.platform != "win32":
    import fcntl

from ..registry import (
    ReadOnlyTierError,
    assert_operation_allowed,
    get_registry,
    is_razer_cmd_allowed,
    operation_spec,
    parse_hex_int,
    tier_label,
)
from .base import Asleep, HIDIOCGFEATURE, HIDIOCSFEATURE, LOD_NAMES, LOD_PRESETS

RAZER_VID = 0x1532
VID = RAZER_VID  # backward-compatible alias

# Loaded from shared devices/devices.json registry (single source of truth)
RAZER_DEVICES = get_registry().build_razer_devices()
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


def xfer(fd, cls, cmd, size, args=(), txn=0x1F, meta=None):
    """One Razer 90-byte feature report exchange with Tier 0/1 dispatch allowlist enforcement."""
    if meta is not None and not is_razer_cmd_allowed(meta, cls, cmd, txn):
        name = meta.get("name") or meta.get("device") or "Razer device"
        tier = meta.get("tier", 0)
        raise ReadOnlyTierError(
            f"Blocked at dispatch: {name} ({tier_label(tier)}) does not allow "
            f"Razer command cls=0x{cls:02x} cmd=0x{cmd:02x} txn=0x{txn:02x}"
        )
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


def _op_txn(meta, op_name, default=0x1F):
    if not meta:
        return default
    return parse_hex_int(operation_spec(meta, op_name).get("txn"), default)


def _op_storage(meta, op_name, default=0x00):
    if not meta:
        return default
    return parse_hex_int(operation_spec(meta, op_name).get("storage"), default)


def get_firmware(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "firmware", write=False)
    use_txn = _op_txn(meta, "firmware", 0x1F) if txn is None else txn
    a = xfer(fd, 0x00, 0x81, 0x02, (), txn=use_txn, meta=meta)
    return f"v{a[0]}.{a[1]}"


def get_serial(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "serial", write=False)
    use_txn = _op_txn(meta, "serial", 0x1F) if txn is None else txn
    a = xfer(fd, 0x00, 0x82, 0x16, (), txn=use_txn, meta=meta)
    return bytes(a[:22]).split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def get_dpi(fd, txn=None, storage=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "dpi", write=False)
    use_txn = _op_txn(meta, "dpi", 0x1F) if txn is None else txn
    use_storage = _op_storage(meta, "dpi", 0x00) if storage is None else storage
    a = xfer(fd, 0x04, 0x85, 0x07, [use_storage], txn=use_txn, meta=meta)
    return (a[1] << 8 | a[2], a[3] << 8 | a[4])


def set_dpi(fd, x, y, dpi_max=35000, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "dpi", write=True)
    if not (100 <= x <= dpi_max and 100 <= y <= dpi_max):
        raise ValueError(f"DPI must be between 100 and {dpi_max}")
    use_txn = _op_txn(meta, "dpi", 0x1F)
    xfer(fd, 0x04, 0x05, 0x07, [0x01, x >> 8, x & 255, y >> 8, y & 255, 0, 0], txn=use_txn, meta=meta)


def get_stages(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "stages", write=False)
    use_txn = _op_txn(meta, "stages", 0x1F) if txn is None else txn
    a = xfer(fd, 0x04, 0x86, 0x26, [0x01], txn=use_txn, meta=meta)
    active, n = a[1], min(a[2], 5)
    stages = []
    for i in range(n):
        o = 3 + i * 7
        stages.append((a[o + 1] << 8 | a[o + 2], a[o + 3] << 8 | a[o + 4]))
    return {"active": active, "stages": stages}


def set_stages(fd, active, stages, dpi_max=35000, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "stages", write=True)
    if not (1 <= len(stages) <= 5):
        raise ValueError("must have between 1 and 5 stages")
    if not (1 <= active <= len(stages)):
        raise ValueError(f"active stage must be between 1 and {len(stages)}")
    args = [0x01, active, len(stages)]
    for i, (x, y) in enumerate(stages, 1):
        if not (100 <= x <= dpi_max and 100 <= y <= dpi_max):
            raise ValueError(f"stage {i} DPI must be between 100 and {dpi_max}")
        args.extend([i, x >> 8, x & 255, y >> 8, y & 255, 0, 0])
    use_txn = _op_txn(meta, "stages", 0x1F)
    xfer(fd, 0x04, 0x06, 0x26, args, txn=use_txn, meta=meta)


def get_poll(fd, hyper=False, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "polling", write=False)
    use_txn = _op_txn(meta, "polling", 0x1F) if txn is None else txn
    if hyper:
        return POLL2_CODES.get(xfer(fd, 0x00, 0xC0, 0x01, (), txn=use_txn, meta=meta)[1])
    return POLL_CODES.get(xfer(fd, 0x00, 0x85, 0x01, (), txn=use_txn, meta=meta)[0])


def set_poll(fd, hz, hyper=False, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "polling", write=True)
    use_txn = _op_txn(meta, "polling", 0x1F)
    if hyper:
        if hz not in POLL2_HZ:
            raise ValueError(f"unsupported polling rate {hz}; valid: {sorted(POLL2_HZ)}")
        xfer(fd, 0x00, 0x40, 0x02, [0x00, POLL2_HZ[hz]], txn=use_txn, meta=meta)
        try:
            xfer(fd, 0x00, 0x40, 0x02, [0x01, POLL2_HZ[hz]], txn=use_txn, meta=meta)
        except (OSError, Asleep):
            pass
    else:
        if hz not in (125, 500, 1000):
            raise ValueError("unsupported polling rate on this link; valid: 125, 500, 1000")
        xfer(fd, 0x00, 0x05, 0x01, [POLL_HZ[hz]], txn=use_txn, meta=meta)


def get_lod(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "lod", write=False)
    use_txn = _op_txn(meta, "lod", 0x1F) if txn is None else txn
    for _ in range(3):
        a = xfer(fd, 0x0B, 0x85, 0x01, (), txn=use_txn, meta=meta)
        if a[2] == 0x01 and a[3] <= 2:
            return {"mode": "preset", "level": LOD_NAMES[a[3]]}
        if a[2] == 0x04:
            return {"mode": "async", "lift": a[4] + 1, "land": a[5] + 1}
        time.sleep(0.06)
    return None


def set_lod_preset(fd, level, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "lod", write=True)
    if isinstance(level, str):
        key = level.lower()
        if key not in LOD_PRESETS:
            raise ValueError("LOD preset must be low, medium, or high")
        level = LOD_PRESETS[key]
    use_txn = _op_txn(meta, "lod", 0x1F)
    xfer(fd, 0x0B, 0x03, 0x03, [0x00, 0x04, 0x28], txn=use_txn, meta=meta)
    xfer(fd, 0x0B, 0x0B, 0x04, [0x00, 0x04, 0x01, level], txn=use_txn, meta=meta)


def set_lod_async(fd, lift, land, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "lod", write=True)
    if not (2 <= lift <= 26 and 1 <= land <= 25 and lift > land):
        raise ValueError("async LOD requires 2 <= lift <= 26, 1 <= land <= 25, and lift > land")
    use_txn = _op_txn(meta, "lod", 0x1F)
    xfer(fd, 0x0B, 0x03, 0x03, [0x00, 0x04, 0x01], txn=use_txn, meta=meta)
    xfer(fd, 0x0B, 0x0B, 0x04, [0x00, 0x04, 0x04, 0x00], txn=use_txn, meta=meta)
    xfer(fd, 0x0B, 0x05, 0x04, [0x00, 0x04, lift - 1, land - 1], txn=use_txn, meta=meta)


def get_idle(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "power", write=False)
    use_txn = _op_txn(meta, "power", 0x1F) if txn is None else txn
    a = xfer(fd, 0x07, 0x83, 0x02, (), txn=use_txn, meta=meta)
    return a[0] << 8 | a[1]


def set_idle(fd, sec, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "power", write=True)
    if not (60 <= sec <= 900):
        raise ValueError("idle timer must be between 60 and 900 seconds (1–15 min)")
    use_txn = _op_txn(meta, "power", 0x1F)
    xfer(fd, 0x07, 0x03, 0x02, [sec >> 8, sec & 255], txn=use_txn, meta=meta)


def get_low(fd, txn=None, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "power", write=False)
    use_txn = _op_txn(meta, "power", 0x1F) if txn is None else txn
    return round(xfer(fd, 0x07, 0x81, 0x01, (), txn=use_txn, meta=meta)[0] / 255 * 100)


def set_low(fd, pct, meta=None):
    if meta is not None:
        assert_operation_allowed(meta, "power", write=True)
    if not (5 <= pct <= 100):
        raise ValueError("low battery threshold must be between 5 and 100%")
    raw = max(0x0C, min(0xFF, round(pct / 100 * 255)))
    use_txn = _op_txn(meta, "power", 0x1F)
    xfer(fd, 0x07, 0x01, 0x01, [raw], txn=use_txn, meta=meta)


def razer_read_status(fd, meta):
    pid = meta["pid"]
    tier = meta.get("tier", 2)
    if tier == 0:
        raise ReadOnlyTierError(f"Blocked: {meta.get('name', 'Razer device')} is Tier 0 (zero vendor reports)")

    wireless = bool(meta.get("wireless", False))
    hyper = bool(meta.get("hyper", False))
    dpi_max = meta.get("dpi_max", 30000)
    ops = meta.get("operations")

    def _can_read(op_name, default=True):
        if ops is None:
            return default
        return bool((ops.get(op_name) or {}).get("read", False))

    info = {
        "id": meta["id"],
        "driver": "razer",
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{pid:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "tier": tier,
        "tier_label": meta.get("tier_label", tier_label(tier)),
        "profile_id": meta.get("profile_id"),
        "write_enabled": bool(meta.get("write_enabled", tier >= 2)),
        "wireless": wireless,
        "hyperpolling": hyper,
        "dpi_max": dpi_max,
    }

    if _can_read("firmware", True):
        info["firmware"] = get_firmware(fd, meta=meta)
    if _can_read("serial", tier >= 2):
        info["serial"] = get_serial(fd, meta=meta)
    if _can_read("dpi", True):
        info["dpi"] = list(get_dpi(fd, meta=meta))
    if _can_read("stages", tier >= 2):
        st = get_stages(fd, meta=meta)
        info["active"] = st["active"]
        info["stages"] = [list(s) for s in st["stages"]]
    if _can_read("polling", tier >= 2):
        info["polling_hz"] = get_poll(fd, hyper, meta=meta)
    if _can_read("lod", tier >= 2):
        info["lod"] = get_lod(fd, meta=meta)
    if wireless and _can_read("battery", tier >= 2):
        b_txn = _op_txn(meta, "battery", 0x1F)
        info["battery_percent"] = round(xfer(fd, 0x07, 0x80, 0x02, (), txn=b_txn, meta=meta)[1] / 255 * 100)
        info["charging"] = bool(xfer(fd, 0x07, 0x84, 0x02, (), txn=b_txn, meta=meta)[1])
    if wireless and _can_read("power", tier >= 2):
        info["idle_seconds"] = get_idle(fd, meta=meta)
        info["low_battery_percent"] = get_low(fd, meta=meta)
    return info
