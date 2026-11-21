#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/.

Run from anywhere:  python3 docs/capture_screenshots.py [--out DIR] [--browser PATH]

Everything on screen is synthetic. The script never talks to the real helper
(`mousetool.py serve`), never opens a real device and never reads maccel, the
desktop configuration or sysfs:

  * a throw-away HTTP server on a random 127.0.0.1 port serves a copy of
    index.html whose helper URL points at itself, plus fixture responses for
    /api/status, /api/maccel, /api/desktop and a /api/motion event stream;
  * a small script injected before the page loads replaces `navigator.hid`
    with one synthetic Pulsar Pro (Pulsar 3710:5405 protocol, fake serial);
  * headless Chromium/Brave is driven over the DevTools pipe (stdlib only, no
    Playwright or Selenium) at 16:9 (1600x900 PNGs).

Byte-reproduction baseline (Arch Linux x86_64): Brave 157.1.99.57
(brave-bin 1:1.99.57-1), fontconfig 2.18.3, FreeType 2.15.0 and ttf-dejavu
2.37+18+g9b5d1b2f-8. The private fontconfig below exposes only DejaVu Sans
and Sans Mono regular and bold from that package. Each browser starts with an
empty home/profile, C.UTF-8 locale, software rendering and sRGB output.
The PNG encoder is Brave's Page.captureScreenshot (PNG, default compression).
Save its bytes directly. No Pillow or external PNG encoder is involved.
Other Chromium builds can be selected with --browser for alternate captures.
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1600, 900
MAX_BYTES = 400 * 1024

# ---------------------------------------------------------------------------
# fixtures (all invented)
# ---------------------------------------------------------------------------
MACCEL = {
    "installed": True, "loaded": True, "writable": True, "in_group": True, "group_exists": True,
    "mode": "natural",
    "params": {
        "SENS_MULT": 1.0, "YX_RATIO": 1.0, "INPUT_DPI": 1600.0, "ANGLE_ROTATION": 0.0,
        "ACCEL": 0.3, "OFFSET": 2.0, "OUTPUT_CAP": 2.0,
        "DECAY_RATE": 0.1, "LIMIT": 2.4,
        "GAMMA": 1.0, "SMOOTH": 0.5, "MOTIVITY": 1.5, "SYNC_SPEED": 5.0,
    },
}
DESKTOP = {
    "backend": "niri", "label": "niri", "writable": True,
    "accel_profile": "flat", "accel_speed": 0.0, "message": "Saved to niri/config.kdl",
}
SYSTEM_MICE = [{  # a passive, not-yet-connected generic mouse beside the synthetic Pulsar
    "name": "USB Optical Mouse", "vid": "0x1234", "pid": "0x5678", "driver": "generic",
    "configurable": False, "id": "sys-mouse1", "hidraw": None, "link": "USB / HID",
    "phys": None, "battery_percent": None, "charging": None,
}]
MOTION = {  # one frame of the live telemetry stream: mid-swipe, peak held
    "speed": 9.4, "raw_speed": 9.4, "peak": 22.0, "peak_age_ms": 0, "peak_hold_ms": 2800,
    "swipe_min": 4.2, "swipe_max": 22.0, "swipe_id": 3, "active": True, "dpi": 1600.0,
    "source": "maccel+evdev", "devices": 2,
}

# Injected before any page script. A synthetic Pulsar Pro that answers the
# command/response protocol makePulsarProApi() speaks (report 0 out, report 0 in).
HID_MOCK = r"""
(() => {
  const STAGES = [400, 800, 1600, 3200], ACTIVE = 3;
  const reply = (cmd) => {
    const u = new Uint8Array(32); u[0] = cmd;
    switch (cmd) {
      case 0xb1: u[2] = STAGES.length; u[3] = ACTIVE;
        STAGES.forEach((d, i) => { const o = 4 + i * 4; u[o] = d & 255; u[o + 1] = d >> 8; u[o + 2] = d & 255; u[o + 3] = d >> 8; }); break;
      case 0xb2: u[2] = 4000 & 255; u[3] = 4000 >> 8; break;      // polling 4000 Hz
      case 0xb3: u[2] = 2; break;                                  // lift-off: medium
      case 0xb5: u[2] = 1; break;                                  // motion sync on
      case 0xb6: u[2] = 0; break;                                  // ripple control off
      case 0xb7: u[2] = 0; break;                                  // angle snapping off
      case 0xb8: u[2] = 0; break;                                  // angle tuning 0
      case 0xc4: u[2] = 0; break;                                  // performance mode off
      case 0xc5: u[2] = 3; break;                                  // debounce 3 ms
      case 0xc2: { const ms = 300000; u[2] = ms & 255; u[3] = (ms >> 8) & 255; u[4] = (ms >> 16) & 255; u[5] = (ms >> 24) & 255; break; }
      case 0xbe: u[2] = 15; break;                                 // low-battery warning 15 %
      case 0xb4: u[1] = 82; u[6] = 0; break;                       // battery 82 %, not charging
      case 0xbf: u[1] = 1; u[2] = 2; u[3] = 7; break;              // firmware v1.2.7
    }
    return u;
  };
  const dev = new EventTarget();
  Object.assign(dev, {
    vendorId: 0x3710, productId: 0x5405, productName: "Pulsar Pro", serialNumber: "SYNTHETIC-0001", opened: false,
    collections: [{ usagePage: 1, usage: 2, children: [], featureReports: [],
                    inputReports: [{ reportId: 0, items: [{ reportSize: 8, reportCount: 8 }] }],
                    outputReports: [{ reportId: 0, items: [{ reportSize: 8, reportCount: 63 }] }] }],
    async open() { this.opened = true; },
    async close() { this.opened = false; },
    async sendReport(_id, data) {
      const cmd = new Uint8Array(data.buffer ? data.buffer : data, data.byteOffset || 0, 1)[0];
      setTimeout(() => {
        const ev = new Event("inputreport");
        ev.reportId = 0; ev.device = dev; ev.data = new DataView(reply(cmd).buffer);
        dev.dispatchEvent(ev);
      }, 2);
    },
    async sendFeatureReport() {}, async receiveFeatureReport() { return new DataView(new ArrayBuffer(8)); },
  });
  const hid = Object.assign(new EventTarget(), {
    async getDevices() { return [dev]; }, async requestDevice() { return [dev]; },
  });
  Object.defineProperty(navigator, "hid", { value: hid, configurable: true });
})();
"""


# ---------------------------------------------------------------------------
# fixture HTTP server
# ---------------------------------------------------------------------------
def make_handler(page):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj):
            self._send(json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                return self._send(page, "text/html; charset=utf-8")
            if path == "/icon.svg":
                return self._send((ROOT / "icon.svg").read_bytes(), "image/svg+xml")
            if path == "/api/status":
                return self._json({"version": 2, "maccel": MACCEL, "desktop": DESKTOP, "system_mice": SYSTEM_MICE})
            if path == "/api/maccel":
                return self._json({"maccel": MACCEL})
            if path == "/api/desktop":
                return self._json({"desktop": DESKTOP})
            if path == "/api/devices":
                return self._json({"mice": SYSTEM_MICE})
            if path == "/api/motion":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(f"data: {json.dumps(MOTION)}\n\n".encode())
                    self.wfile.flush()
                    while True:  # hold the stream open until the browser goes away
                        time.sleep(5)
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True
                return
            self.send_error(404)

        def do_POST(self):  # screenshots never write anything
            self.send_error(405)

    return Handler


def build_page(port):
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    old = 'const HELPER = "http://127.0.0.1:8766";'
    if old not in html:
        sys.exit("index.html no longer defines HELPER as expected; update capture_screenshots.py")
    return html.replace(old, f'const HELPER = "http://127.0.0.1:{port}";').encode("utf-8")


# ---------------------------------------------------------------------------
# minimal DevTools-over-pipe client
# ---------------------------------------------------------------------------
def browser_env(profile):
    root = Path(profile)
    fonts = root / "fonts"
    fonts.mkdir()
    for family in ("DejaVuSans", "DejaVuSansMono"):
        for suffix in ("", "-Bold"):
            name = f"{family}{suffix}.ttf"
            shutil.copyfile(Path("/usr/share/fonts/TTF") / name, fonts / name)
    config = root / "fonts.conf"
    config.write_text(f"""<fontconfig>
      <dir>{escape(str(fonts))}</dir><cachedir>{escape(str(root))}/font-cache</cachedir>
      <alias><family>system-ui</family><prefer><family>DejaVu Sans</family></prefer></alias>
      <alias><family>sans-serif</family><prefer><family>DejaVu Sans</family></prefer></alias>
      <alias><family>monospace</family><prefer><family>DejaVu Sans Mono</family></prefer></alias>
    </fontconfig>""")
    return {"PATH": os.defpath, "HOME": profile, "FONTCONFIG_FILE": str(config),
            "LANG": "C.UTF-8", "TZ": "UTC"}


class Browser:
    def __init__(self, exe, profile):
        import fcntl
        cmd_r, self._cmd_w = os.pipe()
        self._evt_r, evt_w = os.pipe()
        # keep the child's ends above fd 4 so dup2 onto 3/4 cannot clobber them
        high = [fcntl.fcntl(cmd_r, fcntl.F_DUPFD, 10), fcntl.fcntl(evt_w, fcntl.F_DUPFD, 10)]
        os.close(cmd_r)
        args = [
            exe, "--headless=new", "--remote-debugging-pipe", f"--user-data-dir={profile}",
            "--disable-gpu", "--lang=en-US", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            "--disable-background-networking", "--disable-component-update", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--force-color-profile=srgb", "--mute-audio",
            f"--window-size={WIDTH},{HEIGHT}", "about:blank",
        ]

        def child():  # the browser expects commands on fd 3 and answers on fd 4
            os.dup2(high[0], 3)
            os.dup2(high[1], 4)

        self.proc = subprocess.Popen(args, env=browser_env(profile), pass_fds=(3, 4), preexec_fn=child,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(high[0])
        os.close(high[1])
        os.close(evt_w)
        self._next_id = 0
        self._waiting = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        buf = b""
        with os.fdopen(self._evt_r, "rb", buffering=0) as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    msg = json.loads(raw)
                    slot = self._waiting.get(msg.get("id"))
                    if slot is not None:
                        slot["msg"] = msg
                        slot["ev"].set()

    def call(self, method, params=None, session=None, timeout=30):
        with self._lock:
            self._next_id += 1
            mid = self._next_id
            slot = {"ev": threading.Event(), "msg": None}
            self._waiting[mid] = slot
            msg = {"id": mid, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            os.write(self._cmd_w, json.dumps(msg).encode() + b"\0")
        if not slot["ev"].wait(timeout):
            raise TimeoutError(method)
        del self._waiting[mid]
        reply = slot["msg"]
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    def close(self):
        try:
            self.call("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Page:
    def __init__(self, browser):
        self.b = browser
        target = browser.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.sid = browser.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Page.addScriptToEvaluateOnNewDocument", {"source": HID_MOCK})

    def send(self, method, params=None, **kw):
        return self.b.call(method, params, session=self.sid, **kw)

    def eval(self, expr):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in r:
            raise RuntimeError(f"page error in {expr!r}: {r['exceptionDetails']}")
        return r["result"].get("value")

    def wait_for(self, expr, what, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.eval(expr):
                return
            time.sleep(0.1)
        raise TimeoutError(f"timed out waiting for {what}")

    def png(self):
        import base64
        r = self.send("Page.captureScreenshot", {
            "format": "png", "fromSurface": True, "captureBeyondViewport": False,
        })
        return base64.b64decode(r["data"])


# ---------------------------------------------------------------------------
# shots
# ---------------------------------------------------------------------------
READY = 'document.getElementById("hDpi")?.textContent.trim() !== "–" && !!document.getElementById("hDpi") && !!document.querySelector(".mcard.active")'
ACCEL_READY = ('!!document.querySelector("#accelParams input, #accelParams .setting")'
               ' && /Live/.test(document.getElementById("accelLiveSrc")?.textContent)'
               ' && /Peak: *\\d/.test(document.getElementById("accelLiveReadout")?.textContent)'
               ' && !document.getElementById("desktopGroup")?.hidden')

SHOTS = [
    # (file, colour scheme, tab, selector to scroll to, or None; px margin above it)
    ("sensitivity-dark.png", "dark", "perf", None, 16),
    ("sensitivity-light.png", "light", "perf", None, 16),
    ("acceleration-dark.png", "dark", "accel", ".panel[data-panel=accel]", 16),
    ("acceleration-light.png", "light", "accel", ".panel[data-panel=accel]", 16),
    ("desktop-pointer-dark.png", "dark", "accel", "#accelParams", 0),
    ("tuning-light.png", "light", "perf", "#stagesGroup ~ .group:not([hidden])", 16),
]


def capture(page, port, scheme, tab, scroll, margin):
    # Fit Sensitivity through its LOD row. Render every view directly at 1600x900.
    units = 77 if tab == "perf" and scroll is None else 80
    page.send("Emulation.setDeviceMetricsOverride", {
        "width": 16 * units, "height": 9 * units,
        "deviceScaleFactor": 100 / units, "mobile": False,
    })
    page.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": scheme}]})
    page.send("Page.navigate", {"url": "about:blank"})  # never evaluate against the previous shot's page
    page.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html"})
    page.wait_for(READY, "the synthetic mouse to load")
    page.eval(f'document.querySelector(\'.tab[data-tab="{tab}"]\').click()')
    if tab == "accel":
        page.wait_for(ACCEL_READY, "the acceleration tab and live telemetry")
    time.sleep(0.8)  # let SVG / transitions settle
    page.eval("window.scrollTo(0, 0)")
    if scroll:
        # extra room below the page so the target can sit exactly at the top of the frame
        page.eval('document.body.style.paddingBottom = "900px"')
        page.eval(f'(() => {{ const el = document.querySelector({json.dumps(scroll)});'
                  f' window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - {margin}); }})()')
        time.sleep(0.2)
    return page.png()


def png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


def find_browser(explicit):
    cands = [explicit, os.environ.get("MOUSETOOL_BROWSER"), "brave", "brave-browser", "chromium",
             "chromium-browser", "google-chrome", "google-chrome-stable"]
    for c in cands:
        if c and shutil.which(c):
            return shutil.which(c)
    sys.exit("no Chromium-based browser found; pass --browser PATH or set MOUSETOOL_BROWSER")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"), help="output directory")
    ap.add_argument("--browser", help="path to a Chromium-based browser")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    exe = find_browser(args.browser)

    with tempfile.TemporaryDirectory(prefix="mousetool-shots-") as profile:
        browser = Browser(exe, profile)  # start before any thread so fork stays simple
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
            port = srv.server_address[1]
            srv.RequestHandlerClass = make_handler(build_page(port))
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            page = Page(browser)
            for name, scheme, tab, scroll, margin in SHOTS:
                data = capture(page, port, scheme, tab, scroll, margin)
                if png_size(data) != (WIDTH, HEIGHT):
                    sys.exit(f"{name}: got {png_size(data)}, wanted {(WIDTH, HEIGHT)}")
                if len(data) > MAX_BYTES:
                    sys.exit(f"{name}: {len(data)} bytes exceeds {MAX_BYTES}")
                (out / name).write_bytes(data)
                print(f"{out / name}  {WIDTH}x{HEIGHT}  {len(data) // 1024} KB")
            srv.shutdown()
        finally:
            browser.close()


if __name__ == "__main__":
    main()
