"""Glorious (Sinowealth / CORE 2 / Classic / PixArt) protocol drivers."""
import sys
import time

if sys.platform != "win32":
    import fcntl

from ..registry import get_registry
from .base import (
    HIDIOCGFEATURE_N,
    HIDIOCSFEATURE_N,
    _cache_key,
    _load_device_cache,
    _save_device_cache,
)

# Glorious registries loaded from shared devices/devices.json
GLORIOUS_SINOWEALTH, GLORIOUS_CORE2, GLORIOUS_CLASSIC, GLORIOUS_PIXART = get_registry().build_glorious_tables()

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


def pixart_read_status(meta):
    cached = _load_device_cache().get(_cache_key(meta), {})
    return {
        "id": meta["id"],
        "driver": "glorious_pixart",
        "device": meta["name"],
        "vid": f"0x{meta['vid']:04x}",
        "pid": f"0x{meta['pid']:04x}",
        "hidraw": meta["hidraw"],
        "link": meta["link"],
        "wireless": bool(meta.get("wireless")),
        "dpi_min": 100,
        "dpi_max": 26000,
        "write_only_settings": True,
        "dpi": cached.get("dpi", [800, 800]),
        "stages": cached.get("stages", [[400, 400], [800, 800], [1600, 1600], [3200, 3200]]),
        "active": cached.get("active", 2),
        "polling_hz": cached.get("polling_hz", 1000),
        "lod": {"mode": "preset", "level": cached.get("lod", "medium")},
        "debounce_ms": cached.get("debounce_ms", 4),
    }
