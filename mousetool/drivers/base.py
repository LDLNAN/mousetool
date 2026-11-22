"""Shared HID transport helpers, ioctl constants, and write-only device state cache."""
import json
import os
import sys
import time

if sys.platform != "win32":
    import fcntl  # noqa: F401  (re-exported for callers/tests)


def _ioc(nr, size):
    return (3 << 30) | (size << 16) | (ord("H") << 8) | nr


HIDIOCSFEATURE_91, HIDIOCGFEATURE_91 = _ioc(0x06, 91), _ioc(0x07, 91)
HIDIOCSFEATURE, HIDIOCGFEATURE = HIDIOCSFEATURE_91, HIDIOCGFEATURE_91


def HIDIOCSFEATURE_N(n):
    return _ioc(0x06, n)


def HIDIOCGFEATURE_N(n):
    return _ioc(0x07, n)


LOD_PRESETS = {"low": 0, "med": 1, "medium": 1, "high": 2}
LOD_NAMES = {0: "low", 1: "medium", 2: "high"}


class Asleep(Exception):
    pass


DEVICE_STATE_CACHE = os.path.expanduser("~/.cache/mousetool/device_state.json")


def _load_device_cache():
    try:
        with open(DEVICE_STATE_CACHE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_device_cache(key, patch):
    data = _load_device_cache()
    cur = data.get(key) or {}
    cur.update(patch)
    cur["updated"] = int(time.time())
    data[key] = cur
    try:
        os.makedirs(os.path.dirname(DEVICE_STATE_CACHE), exist_ok=True)
        with open(DEVICE_STATE_CACHE + ".tmp", "w") as f:
            json.dump(data, f, indent=2)
        os.replace(DEVICE_STATE_CACHE + ".tmp", DEVICE_STATE_CACHE)
    except OSError:
        pass


def _cache_key(meta):
    ident = meta.get("uniq") or meta.get("phys") or meta.get("hidraw") or "default"
    return f"{meta['vid']:04x}:{meta['pid']:04x}:{ident}"


def _open_notify(meta):
    npath = meta.get("notify_hidraw")
    if npath and npath != meta["hidraw"]:
        return os.open(npath, os.O_RDONLY)
    return None
