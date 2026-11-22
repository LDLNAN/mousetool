#!/usr/bin/env python3
"""Read-only probe for connected mice over /dev/hidraw*.

Supports Razer (90-byte feature reports), Endgame Gear 8k (0xA0/0xA1),
Endgame Gear -we / CompX Report 0x08 (ATK/VXE/Scyrox/Teevolution/VGN/Lamzu/Areson),
Logitech HID++ 2.0, Pulsar (XS-1 / Pro / CompX), and Glorious
(Sinowealth / CORE 2 / Classic / Pixart).
Prints decoded device state without writing settings (unless --roundtrip-stages
is explicitly passed for Razer).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mousetool import (
    discover_system_mice,
    egg_parse_config,
    egg_read_config,
    egg_read_version,
    hidpp_get_feature,
    hidpp_read_status,
    read_device_status,
    select_mice,
    xfer,
)


STATUS = {0: "new", 1: "busy", 2: "ok", 3: "fail", 4: "timeout", 5: "unsupported"}


def _probe_razer(meta, roundtrip=False):
    fd = os.open(meta["hidraw"], os.O_RDWR)
    try:
        def show(label, raw_args):
            print(f"  {label:<18} {bytes(raw_args[:16]).hex(' ')}")
            return raw_args

        fw = show("firmware", xfer(fd, 0x00, 0x81, 0x02))
        print(f"    -> v{fw[0]}.{fw[1]}")
        if meta["wireless"]:
            b = show("battery", xfer(fd, 0x07, 0x80, 0x02))
            print(f"    -> {b[1] / 255 * 100:.0f}%")
            c = show("charging", xfer(fd, 0x07, 0x84, 0x02))
            print(f"    -> {bool(c[1])}")
        if meta["hyper"]:
            p = show("polling (v2)", xfer(fd, 0x00, 0xC0, 0x01))
            hz = {1: 8000, 2: 4000, 4: 2000, 8: 1000, 0x10: 500, 0x20: 250, 0x40: 125}.get(p[1], p[1])
            print(f"    -> {hz} Hz")
        else:
            p = show("polling", xfer(fd, 0x00, 0x85, 0x01))
            hz = {1: 1000, 2: 500, 4: 250, 8: 125}.get(p[0], p[0])
            print(f"    -> {hz} Hz")
        d = show("dpi", xfer(fd, 0x04, 0x85, 0x07, [0x00]))
        print(f"    -> {d[1] << 8 | d[2]} x {d[3] << 8 | d[4]}")
        s = show("dpi stages", xfer(fd, 0x04, 0x86, 0x26, [0x01]))
        active, n = s[1], min(s[2], 5)
        stages = [(s[3 + i * 7], s[4 + i * 7] << 8 | s[5 + i * 7], s[6 + i * 7] << 8 | s[7 + i * 7]) for i in range(n)]
        print(f"    -> active={active} stages={stages}")
        lod = show("lod", xfer(fd, 0x0B, 0x85, 0x01))
        if lod[2] == 0x01:
            print(f"    -> preset={lod[3]}")
        elif lod[2] == 0x04:
            print(f"    -> async lift={lod[4] + 1} land={lod[5] + 1}")
        if roundtrip:
            args = [0x01, active, n]
            for sid, x, y in stages:
                args += [sid, x >> 8, x & 0xFF, y >> 8, y & 0xFF, 0, 0]
            xfer(fd, 0x04, 0x06, 0x26, args)
            again = xfer(fd, 0x04, 0x86, 0x26, [0x01])
            print("  -> roundtrip unchanged" if bytes(again[:3 + n * 7]) == bytes(s[:3 + n * 7]) else "  -> DIFFERS")
    finally:
        os.close(fd)


def _probe_egg8k(meta):
    fd = os.open(meta["hidraw"], os.O_RDWR)
    try:
        prof = meta["profile"]
        fw = egg_read_version(fd)
        blob = egg_read_config(fd)
        parsed = egg_parse_config(blob, prof)
        print(f"  firmware:  {fw}")
        print(f"  blob[16:40]: {bytes(blob[16:40]).hex(' ')}")
        print(f"  polling:   {parsed['polling_hz']} Hz (div={parsed['polling_divider']})")
        print(f"  dpi:       {parsed['dpi']} (active {parsed['active']}/{len(parsed['stages'])}, stages={parsed['stages']})")
        print(f"  lod:       {parsed['lod']}")
        print(f"  filters:   sync={parsed['motion_sync']} angle={parsed['angle_snapping']} ripple={parsed['ripple_control']} slam={parsed['slamclick']} jitter={parsed['motion_jitter']}")
        print(f"  handed:    {'left' if parsed['left_handed'] else 'right'}  spdt_L={parsed['buttons'][0]['spdt']} spdt_R={parsed['buttons'][1]['spdt']}")
    finally:
        os.close(fd)


def _probe_hidpp(meta):
    fd = os.open(meta["hidraw"], os.O_RDWR)
    try:
        st = hidpp_read_status(fd, meta)
        dev_idx = st["dev_idx"]
        print(f"  HID++ {st['hidpp_version']} dev_idx=0x{dev_idx:02x} name={st['device']!r} fw={st.get('firmware')}")
        for feat_name, fid in (("BATTERY_1000", 0x1000), ("UNIFIED_BATT_1004", 0x1004),
                               ("ADJUSTABLE_DPI_2201", 0x2201), ("REPORT_RATE_8060", 0x8060),
                               ("ONBOARD_PROFILES_8100", 0x8100)):
            idx = hidpp_get_feature(fd, dev_idx, fid)
            if idx:
                print(f"  feature {feat_name}: idx=0x{idx:02x}")
        if "dpi" in st:
            print(f"  dpi: {st['dpi']}")
        if "polling_hz" in st:
            print(f"  polling: {st['polling_hz']} Hz")
        if "battery_percent" in st:
            print(f"  battery: {st['battery_percent']}% charging={st.get('charging')}")
    finally:
        os.close(fd)


def _probe_generic_driver(meta):
    st = read_device_status(meta)
    if meta.get("notify_hidraw"):
        print(f"  notify:    {meta['notify_hidraw']}")
    if st.get("brand"):
        print(f"  brand:     {st['brand']}")
    if st.get("firmware"):
        print(f"  firmware:  {st['firmware']}")
    if st.get("sensor"):
        print(f"  sensor:    {st['sensor']} (writable={st.get('sensor_writable')})")
    if st.get("write_only_settings"):
        extra = f" (feat_len={meta.get('feat_len')}, writable={st.get('writable', True)})" if "writable" in st else ""
        print(f"  mode:      write-only settings (cached values shown){extra}")
    if "dpi" in st:
        st_info = f" (active {st['active']}/{len(st['stages'])}, stages={st['stages']})" if st.get("stages") else ""
        print(f"  dpi:       {st['dpi']}{st_info}")
    if st.get("polling_hz") is not None:
        ro = " [read-only]" if st.get("polling_readonly") else ""
        print(f"  polling:   {st['polling_hz']} Hz{ro}")
    if st.get("lod"):
        print(f"  lod:       {st['lod']}")
    tune_bits = [
        f"{k}={st[k]}" for k in ("debounce_ms", "motion_sync", "angle_snapping", "ripple_control", "performance", "angle_tuning")
        if st.get(k) is not None
    ]
    if tune_bits:
        print(f"  tuning:    {' '.join(tune_bits)}")
    if st.get("idle_seconds") is not None:
        print(f"  idle:      {st['idle_seconds']} s")
    if st.get("battery_percent") is not None:
        print(f"  battery:   {st['battery_percent']}% charging={st.get('charging')}")


def main():
    ap = argparse.ArgumentParser(description="Read-only HID probe for connected mice")
    ap.add_argument("-d", "--device", help="probe only the matching mouse")
    ap.add_argument("--roundtrip-stages", action="store_true", help="Razer: write current DPI stages back unchanged")
    args = ap.parse_args()

    try:
        mice = select_mice(args.device, all_devices=args.device is None)
    except FileNotFoundError:
        mice = []

    for m in mice:
        print(f"{m['hidraw']} — {m['name']} [{m['driver']}] ({m['vid']:04x}:{m['pid']:04x}, {m['link']})")
        try:
            if m["driver"] == "razer":
                _probe_razer(m, roundtrip=args.roundtrip_stages)
            elif m["driver"] == "egg8k":
                _probe_egg8k(m)
            elif m["driver"] == "hidpp":
                _probe_hidpp(m)
            else:
                _probe_generic_driver(m)
        except Exception as e:
            print(f"  error: {e}")

    if args.device is None:
        generics = [g for g in discover_system_mice() if not g.get("configurable")]
        for g in generics:
            batt = f", battery={g['battery_percent']}%" if g.get("battery_percent") is not None else ""
            print(f"sysfs — {g['name']} [generic] ({g['vid']}:{g['pid']}{batt})")


if __name__ == "__main__":
    main()
