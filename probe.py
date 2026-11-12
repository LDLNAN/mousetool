#!/usr/bin/env python3
"""Read-only probe for mouse HID feature reports over hidraw.

Sends the same 90-byte feature reports the web page uses and prints the
decoded replies, so the protocol can be checked without a browser.
Only GET commands are sent unless --roundtrip-stages is passed, which
writes the current DPI stages back unchanged.
"""
import fcntl, glob, os, sys, time

VID, PIDS = 0x1532, {0x00A5: "wireless", 0x00A6: "wired", 0x00B3: "hyperpolling dongle"}


def ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


HIDIOCSFEATURE = lambda n: ioc(0x06, n)
HIDIOCGFEATURE = lambda n: ioc(0x07, n)


def find_node():
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        ue = open(f"{d}/device/uevent").read()
        hid_id = next((l.split("=")[1] for l in ue.splitlines() if l.startswith("HID_ID=")), "")
        _, v, p = hid_id.split(":")
        if int(v, 16) != VID or int(p, 16) not in PIDS:
            continue
        rd = open(f"{d}/device/report_descriptor", "rb").read()
        if b"\x95\x5a\xb1" in rd:  # 90-byte feature report = control interface
            return "/dev/" + os.path.basename(d), PIDS[int(p, 16)], int(p, 16)
    sys.exit("mouse control interface not found")


def build(cls, cmd, size, args=(), txn=0x1F):
    r = bytearray(90)
    r[1], r[5], r[6], r[7] = txn, size, cls, cmd
    r[8 : 8 + len(args)] = bytes(args)
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return r


WAIT = 0.04


def xfer(fd, cls, cmd, size, args=(), txn=0x1F):
    req = build(cls, cmd, size, args, txn)
    for _ in range(5):
        fcntl.ioctl(fd, HIDIOCSFEATURE(91), bytes([0]) + req)
        time.sleep(WAIT)
        buf = bytearray(91)
        fcntl.ioctl(fd, HIDIOCGFEATURE(91), buf)
        res = buf[1:]
        if res[0] == 0x01:  # busy
            time.sleep(0.02)
            continue
        if res[6] != cls or res[7] != cmd:
            continue
        return res
    return res


STATUS = {0: "new", 1: "busy", 2: "ok", 3: "fail", 4: "timeout", 5: "unsupported"}


def show(name, res):
    print(f"{name:<18} status={STATUS.get(res[0], res[0])}  args={res[8:8+res[5]].hex(' ')}")
    return res[8:]


def main():
    global WAIT
    node, mode, pid = find_node()
    hyper = pid == 0x00B3
    if hyper:
        WAIT = 0.065
    print(f"{node} ({mode})")
    fd = os.open(node, os.O_RDWR)
    a = show("firmware", xfer(fd, 0x00, 0x81, 0x02)); print(f"  -> v{a[0]}.{a[1]}")
    a = show("battery", xfer(fd, 0x07, 0x80, 0x02)); print(f"  -> {a[1] / 255 * 100:.0f}%")
    a = show("charging", xfer(fd, 0x07, 0x84, 0x02)); print(f"  -> {bool(a[1])}")
    if hyper:
        a = show("polling (v2)", xfer(fd, 0x00, 0xC0, 0x01))
        print(f"  -> {({1: 8000, 2: 4000, 4: 2000, 8: 1000, 0x10: 500, 0x20: 250, 0x40: 125}).get(a[1], a[1])} Hz")
        a = show("dongle led", xfer(fd, 0x07, 0x90, 0x01, txn=0xFF)); print(f"  -> mode {a[0]}")
    else:
        a = show("polling", xfer(fd, 0x00, 0x85, 0x01)); print(f"  -> {({1: 1000, 2: 500, 8: 125}).get(a[0], a[0])} Hz")
    a = show("dpi", xfer(fd, 0x04, 0x85, 0x07, [0x00])); print(f"  -> {a[1] << 8 | a[2]} x {a[3] << 8 | a[4]}")
    res = xfer(fd, 0x04, 0x86, 0x26, [0x01]); a = show("dpi stages", res)
    active, n = a[1], a[2]
    stages = [(a[3 + i * 7], a[4 + i * 7] << 8 | a[5 + i * 7], a[6 + i * 7] << 8 | a[7 + i * 7]) for i in range(n)]
    print(f"  -> active={active} stages={stages}")
    a = show("idle time", xfer(fd, 0x07, 0x83, 0x02)); print(f"  -> {a[0] << 8 | a[1]} s")
    a = show("low batt thresh", xfer(fd, 0x07, 0x81, 0x01)); print(f"  -> raw {a[0]:#x} = {a[0] / 255 * 100:.0f}%")

    if "--roundtrip-stages" in sys.argv:
        args = [0x01, active, n]
        for sid, x, y in stages:
            args += [sid, x >> 8, x & 0xFF, y >> 8, y & 0xFF, 0, 0]
        show("SET dpi stages", xfer(fd, 0x04, 0x06, 0x26, args))
        a = show("dpi stages again", xfer(fd, 0x04, 0x86, 0x26, [0x01]))
        print("  -> unchanged" if bytes(a[:3 + n * 7]) == bytes(res[8:11 + n * 7]) else "  -> DIFFERS")


main()
