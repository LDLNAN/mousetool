"""Windows localhost UI and verified Raw Accel writes."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import rawaccel_export as curves

APP = Path(__file__).resolve().parents[2]
LABEL = "Raw Accel (Windows)"
UNAVAILABLE = "Not available on Windows"
OFF = dict(curves.DEFAULTS, MODE="no_accel", ACCEL=0.005)


def _read_script_path():
    candidate = APP / "platform" / "windows" / "read-rawaccel.ps1"
    if candidate.is_file():
        return candidate
    return APP / "read-rawaccel.ps1"


def run(args):
    try:
        return subprocess.run(args, cwd=APP, capture_output=True, timeout=15,
                              creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{Path(args[0]).name} timed out") from exc


def read_active():
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                  "-File", _read_script_path()])
    if result.returncode:
        raise OSError(result.stderr.decode("utf-8", "replace").strip() or "Driver readback failed")
    return curves.loads(result.stdout.decode("utf-8-sig"))


def write_settings(path):
    # writer may complete without a useful exit status. Only driver readback proves application.
    run([APP / "rawaccel/writer.exe", path])
    time.sleep(2)


def driver_status():
    sc = Path(os.environ["SystemRoot"]) / "System32/sc.exe"
    result = run([sc, "query", "rawaccel"])
    if result.returncode == 1060:
        return "missing"
    if b"RUNNING" in result.stdout:
        return "running"
    return "unavailable"


@contextmanager
def writer_lock(folder):
    """Serialize driver changes across the server and the logon process."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "writer.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield  # Closing the handle releases the lock even on failure.


class WindowsAccel:
    def __init__(self, folder, reader=read_active, writer=write_settings, probe=driver_status):
        self.folder = Path(folder)
        self.saved = self.folder / "settings.json"
        self.read = reader
        self.write = writer
        self.probe = probe
        self.lock = threading.RLock()
        self.last = "No apply this session"

    def state(self):
        with self.lock:
            state = {"backend": {"name": "rawaccel", "label": LABEL}, "loaded": False,
                     "parameters_available": False, "writable": False, "last_apply": self.last,
                     "saved_state": "Unverified" if self.saved.exists() else "No saved curve"}
            try:
                active = self.read()
            except (OSError, ValueError) as exc:
                state.update(driver=self.probe(), message=f"Readback unavailable. {exc}")
                return state
            state.update(loaded=True, driver="running", active=active)
            state["backend"]["loaded"] = True
            if self.saved.exists():
                try:
                    matches = curves.loads(self.saved.read_bytes()) == active
                    state["saved_state"] = "Matches saved curve" if matches else "Differs from saved curve"
                except (ValueError, OSError):
                    state["saved_state"] = "Saved curve unreadable"
            try:
                cfg, report = curves.rawaccel_to_maccel(active)
                if active.get("devices"):
                    raise ValueError("Per-device profiles cannot be edited here")
                if report["lossy"]:
                    raise ValueError(" ".join(report["lossy"]))
                state.update(mode=cfg.pop("MODE"), params=cfg, writable=True, parameters_available=True,
                             message=" ".join(report["notes"]))
            except ValueError as exc:
                state["message"] = str(exc)
            return state

    def apply(self, body):
        with self.lock, writer_lock(self.folder):
            try:
                reset = body == {"reset": True} and body["reset"] is True
                doc = curves.export_document(OFF if reset else body)
                if not doc["curve_check"]["equivalent"]:
                    raise ValueError(doc["curve_check"]["result"])
                if doc["mapping"]["lossy"]:
                    raise ValueError(" ".join(doc["mapping"]["lossy"]))
                if not reset:
                    current = self.state()
                    if not current["writable"]:
                        raise ValueError(current["message"])
                settings = doc["rawaccel_settings"]
                pending = self.folder / "pending.json"
                try:
                    pending.write_text(curves.dumps(settings), encoding="utf-8")
                    self.write(pending)
                    if self.read() != settings:
                        raise OSError("Driver readback differs from requested settings")
                    pending.replace(self.saved)
                finally:
                    pending.unlink(missing_ok=True)
                self.last = "Verified by driver readback and saved"
            except (ValueError, OSError) as exc:
                self.last = str(exc)
                raise
            return self.state()


class WindowsRoutes:
    def _origin_ok(self):
        host = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host") == host and self.headers.get("Origin") in (None, f"http://{host}")

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "Forbidden"})
        path = urlsplit(self.path).path
        if path in ("/api/accel", "/api/maccel", "/api/status"):
            st = self.server.acceleration.state()
            self._send(200, {"version": 2, "accel": st, "maccel": st, "accel_backend": st["backend"],
                             "system_mice": [], "desktop": None, "niri": None})
        elif path == "/api/devices":
            self._send(200, {"mice": [], "message": "Connect with WebHID in Edge"})
        elif path in ("/api/motion", "/api/motion/stream", "/api/desktop", "/api/battery", "/api/niri"):
            self._send(501, {"error": UNAVAILABLE})
        elif path.startswith("/api/"):
            self._send(404, {"error": "Not found"})
        else:
            from ..server import _request_target_path, serve_static_asset
            serve_static_asset(self, _request_target_path(self), web_dir=APP / "web", repo_root=APP)

    def do_POST(self):
        if not self._origin_ok() or self.headers.get("X-Mouse-Tool") != "1":
            return self._send(403, {"error": "Forbidden"})
        url = urlsplit(self.path)
        if url.path not in ("/api/accel", "/api/maccel"):
            return self._send(501, {"error": UNAVAILABLE})
        try:
            if url.query not in ("", "backend=rawaccel"):
                raise ValueError("Expected Raw Accel backend")
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 4096:
                raise ValueError("Expected up to 4096 bytes of JSON")
            body = curves.loads(self.rfile.read(size))
            st = self.server.acceleration.apply(body)
            self._send(200, {"accel": st, "maccel": st})
        except (TypeError, ValueError) as exc:
            self._send(400, {"error": str(exc)})
        except OSError as exc:
            self._send(409, {"error": str(exc)})


def restore_saved(saved, reader=read_active, writer=write_settings):
    """Replay a previously verified curve from the current user's Startup shortcut."""
    if not saved.exists():
        return
    with writer_lock(saved.parent):
        with saved.open("rb") as handle:
            settings = curves.loads(handle.read(curves.MAX_INPUT_BYTES + 1))
        cfg, report = curves.rawaccel_to_maccel(settings)
        doc = curves.export_document(cfg)
        if (report["lossy"] or not doc["curve_check"]["equivalent"]
                or doc["rawaccel_settings"] != settings):
            raise ValueError("Saved curve is not a verified mousetool configuration")
        writer(saved)
        if reader() != settings:
            raise OSError("Logon apply failed driver readback")


if __name__ == "__main__":
    restore_saved(Path(os.environ["LOCALAPPDATA"]) / "mousetool/state/settings.json")
