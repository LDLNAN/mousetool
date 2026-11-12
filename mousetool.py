#!/usr/bin/env python3
"""Mouse Tool helper.

  mousetool.py battery [--json]   print the mouse battery (for the noctalia bar widget)
  mousetool.py serve [--port N]   local API the page uses for in-game acceleration (maccel)
  mousetool.py maccel             print the maccel driver state as JSON

The battery is read over the same 90-byte Razer feature reports the page uses. When the mouse is
asleep the dongle can't reach it, so the last good reading is printed with a "stale" flag.

Acceleration goes through maccel (https://www.maccel.org), a kernel module that changes the mouse's
movement before libinput, the compositor or any game sees it — so it also applies to games that use
raw input. The page draws the curve; this helper only reads and writes maccel's sysfs parameters,
using the same fixed-point encoding and reset scripts as the maccel CLI so values survive reboots.
"""
import argparse, fcntl, glob, json, math, os, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------- razer hid
VID = 0x1532
PIDS = {0x00A5: "HyperSpeed dongle", 0x00A6: "USB cable", 0x00B3: "HyperPolling dongle"}


def _ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


HIDIOCSFEATURE, HIDIOCGFEATURE = _ioc(0x06, 91), _ioc(0x07, 91)


class Asleep(Exception):
    pass


def find_mouse():
    """Return (hidraw path, pid) for the mouse's control interface; cable preferred over dongles."""
    found = []
    for d in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            ue = open(f"{d}/device/uevent").read()
            hid_id = next(l.split("=")[1] for l in ue.splitlines() if l.startswith("HID_ID="))
            _, v, p = hid_id.split(":")
            v, p = int(v, 16), int(p, 16)
            if v != VID or p not in PIDS:
                continue
            if b"\x95\x5a\xb1" not in open(f"{d}/device/report_descriptor", "rb").read():
                continue
            found.append(({0x00A6: 0, 0x00B3: 1, 0x00A5: 2}[p], "/dev/" + os.path.basename(d), p))
        except (OSError, StopIteration, ValueError):
            continue
    if not found:
        raise FileNotFoundError("mouse not found")
    _, path, pid = min(found)
    return path, pid


def _build(cls, cmd, size, args=(), txn=0x1F):
    r = bytearray(90)
    r[1], r[5], r[6], r[7] = txn, size, cls, cmd
    r[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return r


def xfer(fd, cls, cmd, size, args=()):
    """One command. The page may be talking to the dongle too, so replies to other commands are skipped."""
    last = None
    for _ in range(3):
        fcntl.ioctl(fd, HIDIOCSFEATURE, bytes([0]) + _build(cls, cmd, size, args))
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
            break
        time.sleep(0.12)
    if last == 4:
        raise Asleep("mouse didn't answer (asleep or out of range)")
    raise OSError(f"no reply (status {last})")


CACHE = os.path.expanduser("~/.cache/mousetool/battery.json")


def read_battery():
    path, pid = find_mouse()
    fd = os.open(path, os.O_RDWR)
    try:
        pct = round(xfer(fd, 0x07, 0x80, 0x02)[1] / 255 * 100)
        charging = bool(xfer(fd, 0x07, 0x84, 0x02)[1])
    finally:
        os.close(fd)
    return {"percent": pct, "charging": charging, "link": PIDS[pid], "time": int(time.time())}


def battery(args):
    now = int(time.time())
    try:
        b = read_battery()
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE + ".tmp", "w") as f:
            json.dump(b, f)
        os.replace(CACHE + ".tmp", CACHE)
        b.update(stale=False, age=0)
    except (Asleep, OSError, FileNotFoundError) as e:
        try:
            b = json.load(open(CACHE))
            b.update(stale=True, age=now - b.get("time", now), reason=str(e))
        except (OSError, ValueError):
            b = {"percent": None, "charging": False, "link": None, "stale": True, "age": None, "reason": str(e)}
    if args.json:
        print(json.dumps(b))
    else:
        # one line the bar script can split: "<percent|-> <charging 0/1> <stale 0/1> <age seconds|->"
        print(f"{b['percent'] if b['percent'] is not None else '-'} {int(b['charging'])} {int(b['stale'])} "
              f"{b['age'] if b['age'] is not None else '-'}")


# ----------------------------------------------------------------------------- maccel
SYS = "/sys/module/maccel/parameters"
RESETS = "/var/opt/maccel/resets"
FIX = 1 << 32                      # maccel is built with 64-bit fixed point on x86_64: value * 2^32
MODES = ["linear", "natural", "synchronous", "no_accel"]
# name -> (min, max). bounds are sanity limits for a hand-held mouse, wider than anything useful.
PARAMS = {
    "SENS_MULT": (0.01, 50), "YX_RATIO": (0.01, 50), "INPUT_DPI": (50, 50000), "ANGLE_ROTATION": (-180, 180),
    "ACCEL": (0, 50), "OFFSET": (0, 1000), "OUTPUT_CAP": (0, 100),
    "DECAY_RATE": (0.0001, 100), "LIMIT": (1, 100),
    "GAMMA": (0.0001, 100), "SMOOTH": (0, 1), "MOTIVITY": (1.0001, 100), "SYNC_SPEED": (0.0001, 1000),
}


def maccel_state():
    st = {"installed": os.path.isdir("/usr/src") and bool(glob.glob("/usr/src/maccel-*")),
          "loaded": os.path.isdir(SYS), "writable": False, "params": {}, "mode": None,
          "in_group": False, "group_exists": False}
    try:
        import grp
        g = grp.getgrnam("maccel")
        st["group_exists"] = True
        st["in_group"] = g.gr_gid in os.getgroups()
    except (KeyError, ImportError):
        pass
    if not st["loaded"]:
        return st
    st["writable"] = os.access(f"{SYS}/ACCEL", os.W_OK)
    for name in PARAMS:
        try:
            st["params"][name] = int(open(f"{SYS}/{name}").read().strip()) / FIX
        except (OSError, ValueError):
            pass
    try:
        m = int(open(f"{SYS}/MODE").read().strip())
        st["mode"] = MODES[m % len(MODES)]
    except (OSError, ValueError):
        pass
    return st


def _write_param(name, raw):
    with open(f"{SYS}/{name}", "w") as f:
        f.write(str(raw))
    try:                               # same reset scripts the maccel CLI writes, so values survive reboots
        with open(f"{RESETS}/set_last_{name}_value.sh", "w") as f:
            f.write(f"echo {raw} > /sys/module/maccel/parameters/{name};")
    except OSError:
        pass


def maccel_set(updates):
    st = maccel_state()
    if not st["loaded"]:
        raise PermissionError("maccel isn't loaded")
    if not st["writable"]:
        raise PermissionError("no write access to maccel — add yourself to the maccel group and log in again")
    todo = []
    for k, v in updates.items():
        if k == "MODE":
            if v not in MODES:
                raise ValueError(f"unknown mode {v!r}")
            todo.append(("MODE", MODES.index(v)))
            continue
        if k not in PARAMS:
            raise ValueError(f"unknown parameter {k!r}")
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{k} must be a number")
        lo, hi = PARAMS[k]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        todo.append((k, int(round(v * FIX))))
    for k, raw in todo:
        _write_param(k, raw)
    return maccel_state()


def niri_state():
    """Desktop-only acceleration from niri's config (doesn't affect raw-input games)."""
    try:
        cfg = open(os.path.expanduser("~/.config/niri/config.kdl")).read()
        m = re.search(r"^\s*mouse\s*\{(.*?)^\s*\}", cfg, re.S | re.M)
        block = m.group(1) if m else ""
        prof = re.search(r'^\s*accel-profile\s+"(\w+)"', block, re.M)
        speed = re.search(r"^\s*accel-speed\s+(-?[\d.]+)", block, re.M)
        return {"accel_profile": prof.group(1) if prof else "adaptive", "accel_speed": float(speed.group(1)) if speed else 0.0}
    except OSError:
        return None


# ----------------------------------------------------------------------------- local api
ALLOWED_ORIGINS = {"null"}            # the page is opened from file://, whose origin is "null"


class Api(BaseHTTPRequestHandler):
    server_version = "mousetool/1"

    def log_message(self, fmt, *a):
        pass

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o in ALLOWED_ORIGINS or re.fullmatch(r"http://(127\.0\.0\.1|localhost)(:\d+)?", o or "")

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        o = self.headers.get("Origin")
        if o and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "null"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mouse-Tool")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "origin not allowed"})
        if self.path == "/api/status":
            return self._send(200, {"version": 1, "maccel": maccel_state(), "niri": niri_state()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # the custom header forces a CORS preflight, so ordinary web pages can't post here blind
        if not self._origin_ok() or self.headers.get("X-Mouse-Tool") != "1":
            return self._send(403, {"error": "forbidden"})
        if self.path != "/api/maccel":
            return self._send(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            self._send(200, {"maccel": maccel_set(body)})
        except PermissionError as e:
            self._send(409, {"error": str(e), "maccel": maccel_state()})
        except (ValueError, TypeError) as e:
            self._send(400, {"error": str(e)})
        except OSError as e:
            self._send(500, {"error": str(e)})


def serve(args):
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Api)
    print(f"mousetool listening on http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


def main():
    ap = argparse.ArgumentParser(prog="mousetool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("battery"); b.add_argument("--json", action="store_true"); b.set_defaults(fn=battery)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8766); s.set_defaults(fn=serve)
    m = sub.add_parser("maccel"); m.set_defaults(fn=lambda a: print(json.dumps(maccel_state(), indent=2)))
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
