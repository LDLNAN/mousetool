"""HID descriptor parsing and Linux hidraw/sysfs mouse discovery."""
import glob
import os
import re

from .drivers.egg import EGG_DEVICES, EGG_VID, EGG_WE_DEVICES
from .drivers.glorious import (
    GLORIOUS_CLASSIC,
    GLORIOUS_CORE2,
    GLORIOUS_PIXART,
    GLORIOUS_SINOWEALTH,
)
from .drivers.logitech import LOGI_DEVICES, LOGI_VID
from .drivers.pulsar import (
    ARESON_DEVICES,
    ARESON_VID,
    ATK_VID,
    COMPX_DEVICES,
    COMPX_VID,
    PULSAR_PRO_DEVICES,
    PULSAR_VID,
    PULSAR_XS1_DEVICES,
)
from .drivers.razer import RAZER_DEVICES, RAZER_VID

ALL_KNOWN_VENDORS = {
    RAZER_VID: set(RAZER_DEVICES.keys()),
    EGG_VID: set(EGG_DEVICES.keys()) | set(EGG_WE_DEVICES.keys()),
    LOGI_VID: set(LOGI_DEVICES.keys()),
    PULSAR_VID: set(PULSAR_XS1_DEVICES.keys()) | set(PULSAR_PRO_DEVICES.keys()),
    ARESON_VID: set(ARESON_DEVICES.keys()),
}


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


def open_mouse():
    path, pid = find_mouse()
    return os.open(path, os.O_RDWR), path, pid
