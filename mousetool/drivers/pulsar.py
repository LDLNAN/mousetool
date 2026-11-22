"""Pulsar (CompX / Pro / XS-1 / Areson) and CompX Report 0x08 ODM protocol drivers."""
import os
import select
import sys
import time

if sys.platform != "win32":
    import fcntl

from ..registry import get_registry
from .base import Asleep, HIDIOCGFEATURE_N, HIDIOCSFEATURE_N

_REG = get_registry()
PULSAR_VID = 0x3710
PULSAR_XS1_DEVICES = _REG.build_pulsar_xs1_devices()
PULSAR_PRO_DEVICES = _REG.build_pulsar_pro_devices()

ARESON_VID = 0x25A7
ARESON_DEVICES = _REG.build_areson_devices()

COMPX_VID = 0x3554
ATK_VID = 0x373B

COMPX_DEVICES = _REG.build_compx_devices()
ATK_PRODUCTS = _REG.build_atk_products()


# ----------------------------------------------------------------------------- CompX Report 0x08 protocol
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


# ----------------------------------------------------------------------------- Pulsar XS-1 protocol
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


# ----------------------------------------------------------------------------- Pulsar Pro protocol
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
