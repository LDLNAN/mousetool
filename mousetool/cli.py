"""Command-line interface and subcommand handlers for mousetool."""
import argparse
import json
import os
import shutil
import subprocess
import sys

if sys.platform != "win32":
    import fcntl

from . import battery as battery_mod
from . import hid_discovery
from . import server as server_mod
from .accel import linux as linux_accel
from .registry import assert_operation_allowed
from .drivers import (
    COMPX_RATES_INV,
    CORE2_RATES,
    EGG_GLASS_LOD,
    HIDIOCSFEATURE_N,
    LOD_PRESETS,
    XS1_LOD_INV,
    Asleep,
    _cache_key,
    _compx_ctx,
    _open_notify,
    _save_device_cache,
    classic_read_status,
    compx_encode_dpi4,
    compx_encode_dpi6,
    compx_read_status,
    compx_write_flash,
    compx_write_pair,
    core2_read_status,
    core2_xfer,
    egg_parse_config,
    egg_read_config,
    egg_update_config,
    get_dpi,
    get_idle,
    get_lod,
    get_low,
    get_poll,
    get_stages,
    hidpp_get_feature,
    hidpp_probe_device,
    hidpp_xfer,
    pixart_write_config,
    pulsar_pro_commit,
    pulsar_pro_read_status,
    pulsar_pro_xfer,
    pulsar_xs1_read_status,
    pulsar_xs1_xfer,
    read_device_status,
    set_dpi,
    set_idle,
    set_lod_async,
    set_lod_preset,
    set_low,
    set_poll,
    set_stages,
)


def battery(args):
    if getattr(args, "notifications", None):
        st = battery_mod.save_battery_settings({"notifications": args.notifications == "on"})
        print(f"low-battery notifications {'on' if st['notifications'] else 'off'} "
              f"(low {st['low']}%, critical {st['critical']}%) — {battery_mod.battery_settings_path()}")
        return
    if getattr(args, "all", False):
        results = []
        for m in hid_discovery.discover_mice():
            if not m.get("wireless"):
                continue
            b = dict(battery_mod._cli_sample(m), hidraw=m["hidraw"])
            results.append(b)
        for sb in hid_discovery._sysfs_batteries():
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

    selector = getattr(args, "device", None)
    meta = hid_discovery.select_mice(selector)[0] if selector else None
    b = battery_mod._cli_sample(meta, getattr(args, "direct", False))
    if getattr(args, "waybar", False):
        st = battery_mod.load_battery_settings()
        print(json.dumps(battery_mod.waybar_output(b, st["low"], st["critical"]), ensure_ascii=False))
    elif args.json:
        print(json.dumps(b))
    else:
        print(f"{b['percent'] if b['percent'] is not None else '-'} {int(b['charging'])} {int(b['stale'])} "
              f"{b['age'] if b['age'] is not None else '-'}")


def list_cmd(args):
    sys_mice = hid_discovery.discover_system_mice()
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
    targets = hid_discovery.select_mice(args.device, all_devices=want_all)
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
    meta = hid_discovery.select_mice(args.device)[0]
    drv = meta["driver"]
    assert_operation_allowed(meta, "dpi", write=(args.x is not None))
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.x is not None:
                y = args.y if args.y is not None else args.x
                set_dpi(fd, args.x, y, meta["dpi_max"], meta=meta)
            x, y = get_dpi(fd, meta=meta)
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
    meta = hid_discovery.select_mice(args.device)[0]
    drv = meta["driver"]
    assert_operation_allowed(meta, "stages", write=bool(args.set or args.active is not None))
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            cur = get_stages(fd, meta=meta)
            if args.set or args.active is not None:
                stages = [_parse_stage(s.strip()) for s in args.set.split(",") if s.strip()] if args.set else cur["stages"]
                active = args.active if args.active is not None else min(cur["active"], len(stages))
                set_stages(fd, active, stages, meta["dpi_max"], meta=meta)
                cur = get_stages(fd, meta=meta)
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
    meta = hid_discovery.select_mice(args.device)[0]
    drv = meta["driver"]
    assert_operation_allowed(meta, "polling", write=(args.hz is not None))
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.hz is not None:
                set_poll(fd, args.hz, meta["hyper"], meta=meta)
            print(f"{get_poll(fd, meta['hyper'], meta=meta)} Hz")
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
    meta = hid_discovery.select_mice(args.device)[0]
    drv = meta["driver"]
    assert_operation_allowed(meta, "lod", write=bool(args.preset or args.index is not None or args.async_vals))
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.preset:
                set_lod_preset(fd, args.preset, meta=meta)
            elif args.async_vals:
                set_lod_async(fd, args.async_vals[0], args.async_vals[1], meta=meta)
            print(json.dumps(get_lod(fd, meta=meta)))
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
    meta = hid_discovery.select_mice(args.device)[0]
    drv = meta["driver"]
    assert_operation_allowed(meta, "power", write=bool(args.idle is not None or args.low is not None))
    if not meta.get("wireless"):
        sys.exit("selected device is wired (no wireless power settings)")
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            if args.idle is not None:
                set_idle(fd, args.idle * 60, meta=meta)
            if args.low is not None:
                set_low(fd, args.low, meta=meta)
            print(json.dumps({"idle_minutes": round(get_idle(fd, meta=meta) / 60), "low_battery_percent": get_low(fd, meta=meta)}))
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
    meta = hid_discovery.select_mice(args.device)[0]
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
    assert_operation_allowed(meta, "tuning", write=bool(updates or deb is not None))

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


def maccel_cmd(args):
    if args.set:
        from .accel import rawaccel_export as rx
        updates = {}
        for item in args.set:
            if "=" not in item:
                sys.exit(f"expected KEY=VAL, got {item!r}")
            k, v = item.split("=", 1)
            k = k.strip().upper()
            v = v.strip()
            if k == "MODE":
                updates[k] = v
            elif k == "LUT_POINTS":
                updates[k] = rx.parse_lut_points_arg(v)
            else:
                updates[k] = float(v)
        print(json.dumps(linux_accel.maccel_set(updates), indent=2))
    else:
        print(json.dumps(linux_accel.maccel_state(), indent=2))


def rawaccel_cmd(args):
    from .accel import rawaccel_export  # pure module; offline preview/export only, never applies settings
    code = rawaccel_export.main(args.argv, live_loader=linux_accel.maccel_live_config, prog="mousetool.py rawaccel")
    if code:
        sys.exit(code)


def open_ui(_args):
    port = None
    if sys.platform != "win32":
        try:
            port = battery_mod._owner_port()
        except OSError:
            port = None
    page = f"http://127.0.0.1:{port or battery_mod.SERVICE_PORT}/"
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
    b.add_argument("--waybar", action="store_true",
                   help="print Waybar custom-module JSON (text, tooltip, class, percentage)")
    b.add_argument("--direct", action="store_true",
                   help="prefer a direct read when no watcher owns the mouse")
    b.add_argument("--notifications", choices=["on", "off"],
                   help="turn the service's low-battery desktop notifications on or off")
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

    for cmd in ("accel", "maccel"):
        m = sub.add_parser(cmd, help="kernel acceleration (mouseaccel, or maccel legacy) parameters")
        m.add_argument("--set", nargs="+", metavar="KEY=VAL",
                       help="update kernel acceleration parameters, e.g. MODE=natural LIMIT=1.5")
        m.set_defaults(fn=maccel_cmd)

    ra = sub.add_parser("rawaccel", add_help=False,
                        help="offline Raw Accel (Windows) preview/export of maccel curves; never applies settings")
    ra.add_argument("argv", nargs=argparse.REMAINDER, help="export | preview | import ... (see `rawaccel --help`)")
    ra.set_defaults(fn=rawaccel_cmd)

    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=battery_mod.SERVICE_PORT)
    s.add_argument("--no-battery-watch", action="store_true",
                   help="don't poll the mouse battery or send low-battery notifications")
    s.add_argument("--no-browser", action="store_true", help="skip opening Edge on Windows")
    s.set_defaults(fn=server_mod.serve)
    o = sub.add_parser("open"); o.set_defaults(fn=open_ui)

    if sys.argv[1:2] == ["rawaccel"]:
        # Hand everything after `rawaccel` (including --help) to rawaccel_export's own parser.
        args = argparse.Namespace(fn=rawaccel_cmd, argv=sys.argv[2:])
    else:
        args = ap.parse_args()
        if args.cmd == "battery" and args.all and args.waybar:
            ap.error("--all and --waybar cannot be used together")
    try:
        args.fn(args)
    except (FileNotFoundError, Asleep, ValueError, PermissionError, OSError) as e:
        sys.exit(f"error: {e}")
