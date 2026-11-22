"""Endgame Gear 8k (0xA0/0xA1) protocol driver and -we device registry."""
import sys
import time

if sys.platform != "win32":
    import fcntl

from .base import HIDIOCGFEATURE_N, HIDIOCSFEATURE_N

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


def egg8k_read_status(fd, meta):
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
