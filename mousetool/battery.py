"""Battery polling, caching, Waybar output, D-Bus notifications, and BatteryWatcher."""
import contextlib
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

if sys.platform != "win32":
    import fcntl

from . import hid_discovery
from .drivers import (
    Asleep,
    _cache_key,
    _compx_ctx,
    _open_notify,
    classic_read_status,
    compx_xfer,
    core2_read_status,
    hidpp_read_status,
    pulsar_pro_xfer,
    pulsar_xs1_xfer,
    xfer,
)

CACHE = os.path.expanduser("~/.cache/mousetool/battery.json")


def read_battery(meta=None):
    if meta is None:
        mice = [m for m in hid_discovery.discover_mice() if m.get("wireless")]
        if not mice:
            raise FileNotFoundError("no wireless mouse found")
        meta = dict(mice[0])
        meta["model_unique"] = sum((m["vid"], m["pid"]) == (meta["vid"], meta["pid"]) for m in mice) == 1
    try:
        return dict(_read_mouse_battery(meta), **_battery_identity(meta))
    except (Asleep, OSError) as exc:
        exc.battery_meta = meta
        raise


def _battery_key(meta):
    if meta.get("vid") is not None and meta.get("pid") is not None:
        return _cache_key(meta)
    return f"{meta['name']}:{meta['link']}:{meta.get('hidraw', '')}"


def _battery_identity(meta):
    return {"device_key": _battery_key(meta), "vid": meta.get("vid"), "pid": meta.get("pid"),
            "serial": meta.get("uniq") or "", "model_unique": meta.get("model_unique")}


def _read_mouse_battery(meta):
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        drv = meta["driver"]
        if drv == "razer":
            pct = round(xfer(fd, 0x07, 0x80, 0x02)[1] / 255 * 100)
            charging = bool(xfer(fd, 0x07, 0x84, 0x02)[1])
            if meta.get("hyper") and pct == 0:
                raise Asleep("HyperPolling dongle returned no battery reading")
            return {"device": meta["name"], "percent": pct, "charging": charging,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "hidpp":
            st = hidpp_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery feature not supported")
            return {"device": st["device"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        if drv in ("compx", "egg_we"):
            if not meta.get("wireless"):
                raise OSError(f"{meta['name']} is wired (no battery)")
            feat_mode, _, _ = _compx_ctx(meta)
            b = compx_xfer(fd, 0x04, (), feat_mode, nfd)
            return {"device": meta["name"], "percent": b[5], "charging": b[6] != 0,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "pulsar_xs1":
            if not meta.get("wireless"):
                raise OSError(f"{meta['name']} is wired (no battery)")
            b = pulsar_xs1_xfer(fd, [0x08, 0x81, 0x01])
            return {"device": meta["name"], "percent": b[6], "charging": False,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "pulsar_pro":
            b = pulsar_pro_xfer(fd, 0xB4)
            return {"device": meta["name"], "percent": b[1], "charging": b[6] == 1,
                    "link": meta["link"], "time": int(time.time())}
        if drv == "glorious_core2":
            st = core2_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery not reported")
            return {"device": meta["name"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        if drv == "glorious_classic":
            st = classic_read_status(fd, meta)
            if "battery_percent" not in st:
                raise OSError("battery not reported")
            return {"device": meta["name"], "percent": st["battery_percent"],
                    "charging": bool(st.get("charging")), "link": meta["link"], "time": int(time.time())}
        raise OSError(f"{meta['name']} does not report battery via HID")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)


def _write_battery_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as tmp:
        try:
            json.dump(value, tmp)
            tmp.flush()
            os.replace(tmp.name, path)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)


def _write_battery_cache(b, path=None):
    try:
        _write_battery_json(path or CACHE, b)
    except OSError:
        pass


def _read_battery_cache(path=None):
    """Last good reading from ~/.cache/mousetool/battery.json, or None. A cached 0 % is not a reading."""
    try:
        with open(path or CACHE) as f:
            b = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(b, dict) or not _battery_percent_ok(b.get("percent")):
        return None
    if (type(b.get("time")) is not int or not isinstance(b.get("charging"), bool)
            or not isinstance(b.get("device"), str) or not isinstance(b.get("link"), str)):
        return None
    return b


def _battery_percent_ok(pct):
    return isinstance(pct, int) and not isinstance(pct, bool) and 0 < pct <= 100


def _mouse_cache(key):
    digest = hashlib.sha256(key.encode()).hexdigest()
    return os.path.join(os.path.dirname(CACHE), "batteries", digest + ".json")


def _stale_battery(meta, reason, now, cache_path=None):
    key = _battery_key(meta) if meta else None
    cached = _read_battery_cache(cache_path or (_mouse_cache(key) if key else CACHE))
    if key and (not cached or cached.get("device_key") != key):
        cached = _read_battery_cache(CACHE)
        if cached and cached.get("device_key") != key:
            cached = None
    if cached:
        return dict(cached, stale=True, age=max(0, now - cached["time"]), reason=reason)
    return {"device": meta["name"] if meta else None, "percent": None, "charging": False,
            "link": meta["link"] if meta else None, "stale": True, "age": None, "reason": reason,
            **(_battery_identity(meta) if meta else {})}


def battery_snapshot(meta=None, now=None, reader=None, cache_path=None):
    """One battery reading with the stale fallback shared by the CLI and the service's watcher.

    A fresh reading is written to the cache. When the mouse is asleep, out of range or missing, the last
    cached reading comes back with stale=True and its age. The HyperPolling dongle (and other dongles)
    can't reach a sleeping mouse, so a 0 % reply that isn't charging is treated the same way: a mouse that
    answers is never at 0 %, and bars must never show 0 % for "unknown".
    """
    reader = reader or read_battery
    now = int(time.time()) if now is None else int(now)
    try:
        b = reader(meta)
        if not _battery_percent_ok(b.get("percent")):
            raise Asleep(f"mouse reported {b.get('percent')}% (asleep or out of range)")
        if meta:
            b = dict(b, **_battery_identity(meta))
        if b.get("device_key"):
            _write_battery_cache(b, _mouse_cache(b["device_key"]))
        if not meta or cache_path:
            _write_battery_cache(b, cache_path)
        b = dict(b, stale=False, age=0)
    except (Asleep, OSError) as e:  # FileNotFoundError (no wireless mouse) is an OSError
        b = _stale_battery(meta or getattr(e, "battery_meta", None), str(e), now, cache_path)
    return b


def _battery_lock(suffix):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    return open(CACHE + suffix, "a+")


def _owner_port():
    with _battery_lock(".owner") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            port = lock.read().strip()
            return int(port) if port.isdecimal() else 0
    return None


def _cli_sample(meta=None, direct=False):
    port = _owner_port()
    sample = None
    if port or (port is None and not direct and not meta):
        sample = _service_battery(port or SERVICE_PORT)
    if sample and (not meta or sample.get("device_key") == _battery_key(meta)):
        return sample
    known_other = meta and sample and sample.get("device_key")
    # Only a positively identified different mouse can bypass an active watcher.
    with _battery_lock(".reader") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            if known_other or _owner_port() is None:
                return dict(battery_snapshot(meta), source="direct")
    return dict(_stale_battery(meta, "battery reader is busy", int(time.time())), source="cache")


# ----------------------------------------------------------------------------- battery watcher, bar output & notifications
# The service (`serve`) runs one BatteryWatcher: it is the only regular hidraw battery reader, keeps the
# shared cache fresh, answers GET /api/battery, and sends low-battery desktop notifications.
SERVICE_PORT = 8766
BATTERY_CORE_KEYS = ("device_key", "vid", "pid", "serial", "model_unique", "device", "percent", "charging", "link", "time", "stale", "age", "reason")
BATTERY_DEFAULTS = {"notifications": True, "low": 20, "critical": 10, "interval": 60}
BATTERY_HYSTERESIS = 5          # re-arm a level once the battery is this many points above its threshold
BATTERY_STALE_POLLS = 3         # a "fresh" reading older than this many intervals is reported stale
NOTIFY_STATE = os.path.expanduser("~/.cache/mousetool/battery_notify.json")


def battery_settings_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "mousetool", "battery.json")


def _validate_battery_settings(raw, base=None):
    st = dict(base or BATTERY_DEFAULTS)
    for k, v in raw.items():
        if k not in BATTERY_DEFAULTS:
            raise ValueError(f"unknown battery setting: {k}")
        if k == "notifications":
            if not isinstance(v, bool):
                raise ValueError("notifications must be true or false")
        elif not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"{k} must be a whole number")
        st[k] = v
    if not 2 <= st["low"] <= 50:
        raise ValueError("low must be between 2 and 50")
    if not 1 <= st["critical"] < st["low"]:
        raise ValueError("critical must be at least 1 and below low")
    if not 15 <= st["interval"] <= 3600:
        raise ValueError("interval must be between 15 and 3600 seconds")
    return st


def load_battery_settings(path=None):
    """Settings from $XDG_CONFIG_HOME/mousetool/battery.json; a missing or broken file means defaults."""
    try:
        with open(path or battery_settings_path()) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        return _validate_battery_settings({k: v for k, v in raw.items() if k in BATTERY_DEFAULTS})
    except (OSError, ValueError):
        return dict(BATTERY_DEFAULTS)


@contextlib.contextmanager
def _settings_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save_battery_settings(patch, path=None):
    path = path or battery_settings_path()
    with _settings_lock(path):
        st = _validate_battery_settings(patch, load_battery_settings(path))
        _write_battery_json(path, st)
    return st


def _ago(sec):
    if sec is None:
        return "at an unknown time"
    if sec < 90:
        return "just now"
    if sec < 5400:
        return f"{round(sec / 60)} min ago"
    return f"{round(sec / 3600)} h ago"


def battery_class(b, low=20, critical=10):
    """normal / low / critical / charging / stale. Stale wins: an old number says nothing about now."""
    pct = b.get("percent")
    if b.get("stale") or not _battery_percent_ok(pct):
        return "stale"
    if b.get("charging"):
        return "charging"
    if pct <= critical:
        return "critical"
    if pct <= low:
        return "low"
    return "normal"


def waybar_output(b, low=20, critical=10):
    """Waybar `custom` module JSON (return-type: json). `percentage` is null when unknown, never 0."""
    cls = battery_class(b, low, critical)
    pct = b.get("percent")
    known = _battery_percent_ok(pct)
    name = b.get("device") or "Mouse"
    if not known:
        text = "–"
        tooltip = f"{name}: no battery reading yet"
        if b.get("reason"):
            tooltip += f"\n{b['reason']}"
    elif cls == "stale":
        text = f"{pct}%"
        tooltip = f"{name}: {pct}% (last read {_ago(b.get('age'))})\nAsleep or out of range"
    else:
        text = f"{pct}%"
        tooltip = f"{name}: {pct}%" + (" · charging" if b.get("charging") else "")
        if b.get("link"):
            tooltip += f"\n{b['link']}"
    return {"text": text, "tooltip": tooltip, "class": cls, "alt": cls, "percentage": pct if known else None}


def _gvariant_str(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


NOTIFY_NAME = "org.freedesktop.Notifications"


def notification_owner():
    """Return the bus and unique connection owning Notifications, or None before activation."""
    if shutil.which("gdbus"):
        base = ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
                "--object-path", "/org/freedesktop/DBus", "--method"]
        calls = [base + ["org.freedesktop.DBus.GetId"], base + ["org.freedesktop.DBus.GetNameOwner", NOTIFY_NAME]]
    elif shutil.which("busctl"):
        base = ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus"]
        calls = [base + ["GetId"], base + ["GetNameOwner", "s", NOTIFY_NAME]]
    else:
        return None
    identity = []
    for cmd, pattern in zip(calls, (r"[a-fA-F0-9]{32}", r":[0-9]+[.][0-9]+")):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(pattern, result.stdout)
        if result.returncode or not match:
            return None
        identity.append(match.group())
    return tuple(identity)


class InvalidNotificationId(OSError):
    pass


def _check_replacement(result, replace_id):
    if replace_id and result.returncode and re.search(
            r"invalidid|(?:invalid|unknown|expired|closed).*?(?:replacement|notification|id)",
            result.stderr or "", re.I):
        raise InvalidNotificationId(result.stderr.strip())


def send_notification(summary, body, urgency=1, icon="", replace_id=0, run=None, which=None, destination=NOTIFY_NAME):
    """org.freedesktop.Notifications.Notify on the session bus; returns the notification id.

    Tries gdbus (glib), then busctl (systemd), then notify-send. Only basic spec fields are used
    (summary, body, icon, urgency, category, replaces_id) so it works with any notification daemon.
    """
    run = run or subprocess.run
    which = which or shutil.which
    urgency = max(0, min(2, int(urgency)))
    errors = []
    if which("gdbus"):
        hints = "{'urgency': <byte %d>, 'category': <'device'>, 'desktop-entry': <'mousetool'>}" % urgency
        cmd = ["gdbus", "call", "--session", "--dest", destination,
               "--object-path", "/org/freedesktop/Notifications",
               "--method", "org.freedesktop.Notifications.Notify", "--",
               _gvariant_str("mousetool"), str(int(replace_id)), _gvariant_str(icon),
               _gvariant_str(summary), _gvariant_str(body), "@as []", hints, "-1"]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=5)
            _check_replacement(r, replace_id)
            m = re.search(r"uint32 (\d+)", r.stdout or "")
            if r.returncode == 0 and m:
                return int(m.group(1))
            errors.append(f"gdbus: {(r.stderr or r.stdout).strip()}")
        except InvalidNotificationId:
            raise
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"gdbus: {e}")
    if which("busctl"):
        cmd = ["busctl", "--user", "call", destination, "/org/freedesktop/Notifications",
               "--", "org.freedesktop.Notifications", "Notify", "susssasa{sv}i",
               "mousetool", str(int(replace_id)), icon, summary, body, "0",
               "3", "urgency", "y", str(urgency), "category", "s", "device", "desktop-entry", "s", "mousetool", "-1"]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=5)
            _check_replacement(r, replace_id)
            m = re.match(r"u (\d+)", (r.stdout or "").strip())
            if r.returncode == 0 and m:
                return int(m.group(1))
            errors.append(f"busctl: {(r.stderr or r.stdout).strip()}")
        except InvalidNotificationId:
            raise
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"busctl: {e}")
    if destination == NOTIFY_NAME and which("notify-send"):
        cmd = ["notify-send", "-a", "mousetool", "-u", ("low", "normal", "critical")[urgency],
               "-c", "device", "-p"]
        if icon:
            cmd += ["-i", icon]
        if replace_id:
            cmd += ["-r", str(int(replace_id))]
        cmd += [summary, body]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=5)
            _check_replacement(r, replace_id)
            if r.returncode == 0:
                m = re.search(r"\d+", r.stdout or "")
                return int(m.group(0)) if m else 0
            errors.append(f"notify-send: {(r.stderr or r.stdout).strip()}")
        except InvalidNotificationId:
            raise
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"notify-send: {e}")
    raise OSError("could not send a desktop notification: " + ("; ".join(errors) or "no gdbus, busctl or notify-send"))


class BatteryNotifier:
    """Low/critical threshold state machine with hysteresis: one notification per downward crossing.

    `level` is the highest level already notified (0 none, 1 low, 2 critical). It only re-arms when the
    battery charges or climbs BATTERY_HYSTERESIS points above a threshold, so 20 % ↔ 21 % flapping stays
    quiet. Stale or unknown readings (mouse asleep, dongle can't reach it) never notify or re-arm. The
    state is saved so a service restart doesn't repeat a warning.
    """

    def __init__(self, send=None, state_path=None, log=None):
        self.send = send
        self.owner = None
        self.state_path = state_path or NOTIFY_STATE
        self.log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self.level, self.device, self.notification_id = 0, None, 0
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            self.level = int(st.get("level", 0)) if st.get("level") in (0, 1, 2) else 0
            self.device = st.get("device")
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _deliver(self, note):
        if self.send:
            return self.send(**note, replace_id=self.notification_id)
        owner = notification_owner()
        if not owner or owner != self.owner:
            self.notification_id = 0
        replacement = self.notification_id
        self.owner = owner
        destination = owner[1] if owner else NOTIFY_NAME
        try:
            return send_notification(**note, replace_id=replacement, destination=destination)
        except InvalidNotificationId:
            self.notification_id = 0
            return send_notification(**note, replace_id=0, destination=destination)

    def _save(self):
        try:
            _write_battery_json(self.state_path, {"level": self.level, "device": self.device})
        except OSError:
            pass

    def update(self, b, settings):
        """Feed one reading; returns the notification sent as a dict, or None."""
        pct = b.get("percent")
        if b.get("stale") or not _battery_percent_ok(pct):
            return None
        low, crit = settings.get("low", 20), settings.get("critical", 10)
        before = (self.level, self.device)
        if b.get("device") != self.device:
            self.device, self.level = b.get("device"), 0
        if b.get("charging"):
            self.level = 0
        else:
            if self.level >= 2 and pct >= crit + BATTERY_HYSTERESIS:
                self.level = 1
            if self.level >= 1 and pct >= low + BATTERY_HYSTERESIS:
                self.level = 0
        now_level = 0 if b.get("charging") else 2 if pct <= crit else 1 if pct <= low else 0
        sent = None
        if now_level > self.level and settings.get("notifications", True):
            name = b.get("device") or "Your mouse"
            if now_level == 2:
                note = {"summary": "Mouse battery critical", "body": f"{name} is at {pct}%. Charge it now.",
                        "urgency": 2, "icon": "battery-caution"}
            else:
                note = {"summary": "Mouse battery low", "body": f"{name} is at {pct}%. Charge it soon.",
                        "urgency": 1, "icon": "battery-low"}
            try:
                self.notification_id = self._deliver(note) or 0
                self.level = now_level
                sent = dict(note, level=now_level, id=self.notification_id)
            except OSError as e:
                self.log(f"mousetool: {e}")  # level stays put, so the next poll retries once
        if sent or (self.level, self.device) != before:
            self._save()
        return sent


class BatteryWatcher:
    """Polls the default wireless mouse every `interval` seconds in a daemon thread (see BATTERY_DEFAULTS)."""

    def __init__(self, snapshot=None, notifier="default", settings=None, clock=time.time):
        """notifier=None runs without notifications; the default is a BatteryNotifier on the session bus."""
        self._snapshot = snapshot or (lambda: battery_snapshot())
        self.notifier = BatteryNotifier() if notifier == "default" else notifier
        self._settings = settings or load_battery_settings
        self._clock = clock
        self._lock = threading.Lock()
        self._latest = None
        self._stop = threading.Event()
        self._thread = None

    def poll_once(self):
        try:
            with _battery_lock(".reader") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                b = self._snapshot()
        except Exception as e:  # never let one bad read kill the watcher
            b = {"device": None, "percent": None, "charging": False, "link": None,
                 "stale": True, "age": None, "reason": str(e)}
        b = dict(b, checked=int(self._clock()))
        with self._lock:
            self._latest = b
        if self.notifier:
            try:
                with _settings_lock(battery_settings_path()):
                    self.notifier.update(b, self._settings())
            except Exception as e:  # a notification problem must not stop the polling
                print(f"mousetool: battery notification failed: {e}", file=sys.stderr, flush=True)
        return b

    def state(self, now=None):
        """The shared reading as served by GET /api/battery (no hidraw access)."""
        now = int(self._clock() if now is None else now)
        st = self._settings()
        with self._lock:
            b = dict(self._latest) if self._latest else None
        if b is None:  # before the first poll: last cached value, marked stale
            cached = _read_battery_cache()
            b = (dict(cached, stale=True, reason="waiting for the first reading") if cached else
                 {"device": None, "percent": None, "charging": False, "link": None, "stale": True,
                  "reason": "waiting for the first reading"})
        return battery_api_payload(b, st, now)

    def start(self):
        def loop():
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(self._settings()["interval"])
        self._thread = threading.Thread(target=loop, name="battery-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()


def battery_api_payload(b, settings, now=None):
    now = int(time.time() if now is None else now)
    out = {k: b.get(k) for k in BATTERY_CORE_KEYS}
    if out["reason"] is None:
        out.pop("reason")
    t = b.get("time")
    out["age"] = max(0, now - int(t)) if isinstance(t, (int, float)) and _battery_percent_ok(b.get("percent")) else None
    out["stale"] = bool(b.get("stale")) or out["age"] is None or out["age"] > BATTERY_STALE_POLLS * settings["interval"]
    if out["stale"] and not b.get("stale"):
        out["reason"] = "reading is older than expected"
    out["class"] = battery_class(out, settings["low"], settings["critical"])
    out["notifications"] = {"enabled": settings["notifications"], "low": settings["low"],
                            "critical": settings["critical"]}
    out["interval"] = settings["interval"]
    return out


BATTERY_WATCHER = None


def _service_battery(port=SERVICE_PORT, timeout=0.5):
    """The running service's shared reading, or None when the service isn't there (or is too old)."""
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/api/battery")
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if r.status != 200 or not isinstance(data, dict) or "stale" not in data:
        return None
    b = {k: data.get(k) for k in BATTERY_CORE_KEYS if k in data}
    b.setdefault("percent", None)
    b.setdefault("charging", False)
    b["source"] = "service"
    return b


def _api_battery_state():
    """GET /api/battery: the watcher's shared reading; without a watcher, the cached one (never hidraw)."""
    if BATTERY_WATCHER is not None:
        return dict(BATTERY_WATCHER.state(), watching=True)
    cached = _read_battery_cache()
    b = (dict(cached, stale=True, reason="battery watcher is off") if cached else
         {"device": None, "percent": None, "charging": False, "link": None, "stale": True,
          "reason": "battery watcher is off"})
    return dict(battery_api_payload(b, load_battery_settings()), watching=False)
