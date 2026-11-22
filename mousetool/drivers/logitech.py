"""Logitech HID++ 2.0 protocol driver."""
import os
import select
import time

from .base import Asleep

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
