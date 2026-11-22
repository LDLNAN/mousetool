"""Local HTTP/SSE API server and safe static asset serving for web/."""
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import select
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.platform != "win32":
    import fcntl

from . import battery as battery_mod
from . import desktop_pointer
from . import hid_discovery
from . import motion as motion_mod
from .accel import linux as linux_accel

ALLOWED_ORIGINS = {"null"}
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
DEVICES_DIR = REPO_ROOT / "devices"

STATIC_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class PathTraversalError(PermissionError):
    """Raised when a static asset request attempts path traversal or escapes web/."""


def _has_traversal_tokens(raw_path):
    """Check raw, single-decoded, and double-decoded paths for traversal attempts."""
    if not raw_path or not raw_path.startswith("/"):
        return True
    p1 = urllib.parse.unquote(raw_path)
    p2 = urllib.parse.unquote(p1)
    for candidate in (raw_path, p1, p2):
        if "\x00" in candidate or "\\" in candidate or "//" in candidate:
            return True
        for part in candidate.split("/")[1:]:
            if part in (".", ".."):
                return True
    return False


def resolve_static_asset(url_path, web_dir=None, repo_root=None, devices_dir=None):
    """Resolve a static URL path strictly inside web/, /devices/*.json, or /icon.svg at repo root.

    Returns (Path, mime_type) for an existing allowed file, None for missing/unsupported files,
    or raises PathTraversalError for any traversal attempt or symlink escaping the root.
    """
    if _has_traversal_tokens(url_path):
        raise PathTraversalError("forbidden path")

    decoded = urllib.parse.unquote(url_path)
    root_dir = Path(repo_root or REPO_ROOT).resolve()
    web_root = Path(web_dir or WEB_DIR).resolve()

    if decoded.startswith("/devices/"):
        rel_dev = decoded[len("/devices/"):]
        if not rel_dev:
            return None
        posix_dev = PurePosixPath(rel_dev)
        if posix_dev.is_absolute() or any(part in ("", ".", "..") for part in posix_dev.parts):
            raise PathTraversalError("forbidden path")
        dev_root = Path(devices_dir or (root_dir / "devices")).resolve()
        candidate = dev_root.joinpath(*posix_dev.parts)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise PathTraversalError("unresolvable path") from exc
        if not resolved.is_relative_to(dev_root):
            raise PathTraversalError("path escapes devices root")
        if not resolved.is_file() or resolved.suffix.lower() != ".json":
            return None
        return resolved, STATIC_MIME_TYPES[".json"]

    if decoded in ("/", "/index.html"):
        rel = "index.html"
    elif decoded == "/icon.svg":
        web_icon = web_root / "icon.svg"
        if web_icon.exists() or web_icon.is_symlink():
            resolved = web_icon.resolve()
            if not resolved.is_relative_to(web_root):
                raise PathTraversalError("symlink escapes web root")
            if resolved.is_file():
                return resolved, STATIC_MIME_TYPES[".svg"]
        root_icon = root_dir / "icon.svg"
        if root_icon.exists() or root_icon.is_symlink():
            resolved = root_icon.resolve()
            if not resolved.is_relative_to(root_dir):
                raise PathTraversalError("symlink escapes repository root")
            if resolved.is_file():
                return resolved, STATIC_MIME_TYPES[".svg"]
        return None
    else:
        rel = decoded.lstrip("/")

    if not rel:
        return None
    posix = PurePosixPath(rel)
    if posix.is_absolute() or any(part in ("", ".", "..") for part in posix.parts):
        raise PathTraversalError("forbidden path")

    candidate = web_root.joinpath(*posix.parts)
    # Check both existence and symlink containment before reading.
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise PathTraversalError("unresolvable path") from exc

    if not resolved.is_relative_to(web_root):
        raise PathTraversalError("path escapes web root")

    if not resolved.is_file():
        return None

    mime = STATIC_MIME_TYPES.get(resolved.suffix.lower())
    if not mime:
        return None
    return resolved, mime


def _request_target_path(handler):
    """Extract the raw request-target path before query/fragment (preserving leading //)."""
    raw = handler.path
    reqline = getattr(handler, "requestline", "")
    parts = reqline.split(" ", 2)
    if len(parts) >= 2 and parts[1]:
        raw = parts[1]
    return raw.split("?", 1)[0].split("#", 1)[0]


def serve_static_asset(handler, url_path, web_dir=None, repo_root=None):
    """Serve a resolved static asset or send 403/404 via the HTTP handler."""
    try:
        found = resolve_static_asset(url_path, web_dir=web_dir, repo_root=repo_root)
    except PathTraversalError:
        return handler._send(403, {"error": "forbidden"})
    if found is None:
        return handler._send(404, {"error": "not found"})
    asset_path, mime = found
    try:
        data = asset_path.read_bytes()
    except OSError:
        return handler._send(404, {"error": "not found"})
    handler.send_response(200)
    origin = handler.headers.get("Origin")
    if origin and handler._origin_ok():
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


class Api(BaseHTTPRequestHandler):
    server_version = "mousetool/2"

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

    def _serve_motion_sse(self, query):
        dpi = None
        if "dpi" in query and query["dpi"]:
            try:
                dpi = float(query["dpi"][0])
                if not math.isfinite(dpi) or not (50 <= dpi <= 50000):
                    raise ValueError()
            except ValueError:
                return self._send(400, {"error": "dpi must be between 50 and 50000"})

        self.send_response(200)
        o = self.headers.get("Origin")
        if o and self._origin_ok():
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        hub = motion_mod.MOTION_HUB
        with hub._lock:
            before_seq = hub._seq
        hub.subscribe()
        try:
            # Give reader a brief beat on first subscribe so initial frame carries live source info.
            last_seq = hub.wait_update(before_seq, timeout=0.05)
            last_payload = None
            last_send = 0.0
            frame_dt = 1.0 / 60.0
            while True:
                if self._client_closed():
                    break
                snap = hub.snapshot(dpi=dpi)
                payload = json.dumps(snap, separators=(",", ":"))
                now = time.monotonic()
                moving_or_holding = snap["active"] or snap["speed"] > 0 or snap["peak"] > 0
                if payload != last_payload:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    last_payload = payload
                    last_send = now
                elif now - last_send >= 15.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_send = now

                if moving_or_holding:
                    rem = frame_dt - (time.monotonic() - now)
                    if rem > 0:
                        time.sleep(rem)
                    with hub._lock:
                        last_seq = hub._seq
                else:
                    last_seq = hub.wait_update(last_seq, timeout=0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe()

    def _client_closed(self):
        conn = getattr(self, "connection", None)
        if conn is None or not hasattr(conn, "fileno"):
            return False
        try:
            r, _, _ = select.select([conn], [], [], 0)
            if r:
                data = conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
                return data == b""
        except BlockingIOError:
            return False
        except OSError:
            return True
        return False

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
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/desktop":
            return self._send(200, {"desktop": desktop_pointer.desktop_state()})
        if parsed.path in ("/api/accel", "/api/maccel"):
            st = linux_accel.maccel_state()
            return self._send(200, {"accel": st, "maccel": st})
        if parsed.path == "/api/status":
            st = linux_accel.maccel_state()
            return self._send(200, {
                "version": 2,
                "accel": st,            # detected backend: mouseaccel, or maccel (legacy)
                "maccel": st,           # same object, kept for older UIs
                "accel_backend": st.get("backend"),
                "niri": desktop_pointer.niri_state(),
                "desktop": desktop_pointer.desktop_state(),
                "system_mice": hid_discovery.discover_system_mice(),
            })
        if parsed.path == "/api/devices":
            return self._send(200, {"mice": hid_discovery.discover_system_mice()})
        if parsed.path == "/api/battery":
            return self._send(200, battery_mod._api_battery_state())
        if parsed.path in ("/api/motion", "/api/motion/stream"):
            return self._serve_motion_sse(urllib.parse.parse_qs(parsed.query))
        if parsed.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        return serve_static_asset(self, _request_target_path(self))

    def do_POST(self):
        if not self._origin_ok() or self.headers.get("X-Mouse-Tool") != "1":
            return self._send(403, {"error": "forbidden"})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ("/api/accel", "/api/maccel", "/api/niri", "/api/desktop", "/api/battery"):
            return self._send(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected an object")
            if parsed.path in ("/api/accel", "/api/maccel"):
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                expected = query.get("backend", [None])
                if len(expected) != 1:
                    raise ValueError("expected one backend")
                st = linux_accel.maccel_set(body, expected[0])
                motion_mod.MOTION_HUB.refresh_dpi()
                self._send(200, {"accel": st, "maccel": st})
            elif parsed.path == "/api/desktop":
                self._send(200, {"desktop": desktop_pointer.desktop_state(body)})
            elif parsed.path == "/api/battery":
                battery_mod.save_battery_settings(body)
                self._send(200, battery_mod._api_battery_state())
            else:
                with desktop_pointer.LOCK:
                    self._send(200, {"niri": desktop_pointer.niri_set(body)})
        except PermissionError as e:
            if parsed.path == "/api/battery":
                return self._send(500, {"error": str(e)})
            if parsed.path == "/api/desktop":
                return self._send(409, {"error": str(e), "desktop": desktop_pointer.desktop_state()})
            st = linux_accel.maccel_state()
            self._send(409, {"error": str(e), "accel": st, "maccel": st})
        except (ValueError, TypeError) as e:
            self._send(400, {"error": str(e)})
        except OSError as e:
            self._send(500, {"error": str(e)})


def serve(args):
    if sys.platform == "win32":
        from .accel.windows import WindowsAccel, WindowsRoutes

        class WindowsApi(WindowsRoutes, Api):
            pass

        with ThreadingHTTPServer(("127.0.0.1", args.port), WindowsApi) as srv:
            srv.acceleration = WindowsAccel(Path(os.environ["LOCALAPPDATA"]) / "mousetool/state")
            url = f"http://127.0.0.1:{srv.server_port}/"
            print(f"mousetool listening on {url}", flush=True)
            if not args.no_browser:
                os.startfile("microsoft-edge:" + url)
            srv.serve_forever()
        return
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Api)
    owner = None
    if not getattr(args, "no_battery_watch", False):
        owner = battery_mod._battery_lock(".owner")
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner.seek(0)
        owner.truncate()
        owner.write(str(srv.server_port))
        owner.flush()
        battery_mod.BATTERY_WATCHER = battery_mod.BatteryWatcher().start()
    print(f"mousetool listening on http://127.0.0.1:{srv.server_port}", flush=True)
    try:
        srv.serve_forever()
    finally:
        if battery_mod.BATTERY_WATCHER:
            battery_mod.BATTERY_WATCHER.stop()
            battery_mod.BATTERY_WATCHER = None
        if owner:
            owner.close()
        srv.server_close()
