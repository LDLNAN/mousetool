#!/usr/bin/env python3
"""Offline mapping between mousetool's maccel curves and Raw Accel v1.7.1 settings.

Windows step 1 (MT-003): preview and export only. This module is pure: standard
library only (argparse, json, math, sys), no device, sysfs, driver or network
access, and nothing in it applies settings anywhere. It runs on any OS.

What it does
  * maccel_to_rawaccel(): maps the four maccel modes (linear, natural,
    synchronous, no_accel) plus SENS_MULT, YX_RATIO, ANGLE_ROTATION and
    INPUT_DPI onto a Raw Accel DriverConfig (the JSON that writer.exe reads),
    with a per-parameter report (exact / lossy) and explicit notes.
  * rawaccel_to_maccel(): the reverse, refusing Raw Accel features maccel
    cannot express instead of approximating them.
  * export_document() / import_document(): a versioned mousetool JSON wrapper
    ("schema_version") that round-trips the maccel parameters without loss.
  * curve_check(): evaluates the app's own curve (a port of index.html
    maccelEval) and the Raw Accel formula on the *exported* settings at sampled
    speeds and reports the maximum error against a stated tolerance.

Sources (Raw Accel v1.7.1, MIT licence, github.com/RawAccelOfficial/rawaccel,
tag v1.7.1; formulas below are short ports, cited per function):
  common/accel-classic.hpp, accel-natural.hpp, accel-synchronous.hpp,
  accel-noaccel.hpp, rawaccel.hpp (modifier::modify), rawaccel-base.hpp
  (defaults), rawaccel-validate.hpp, rawaccel-version.h, math-vec2.hpp,
  driver/driver.cpp (device/profile selection), wrapper/wrapper.cpp (JSON
  field names), writer/Program.cs (writer input).
maccel semantics: maccel 0.5.9 accel.h (f_accelerate, sensitivity) and
accel/{linear,natural,synchronous}.h.
"""

import argparse
import json
import math
import sys

# ----------------------------------------------------------------------------- constants

EXPORT_FORMAT = "mousetool.rawaccel-export"
SCHEMA_VERSION = 1
RAWACCEL_TARGET = "1.7.1"
# Tag v1.7.1's common/rawaccel-version.h still defines RA_VER_STRING "1.7.0";
# that is what DriverConfig.version holds in settings files written by v1.7.1.
# The writer does not validate this field (wrapper.cpp DriverConfig::Convert).
RAWACCEL_CONFIG_VERSION = "1.7.0"
PROFILE_NAME = "mousetool"
DEFAULT_TOLERANCE = 1e-6  # relative
# Supported speed range for curve evaluation (counts/ms at 1000 DPI). Below 1e-6
# the Studio preview floors the speed; 1e6 counts/ms is far beyond any sensor.
SPEED_MIN, SPEED_MAX = 1e-6, 1e6
# index.html maccelEval synchronous-mode rules that maccel 0.5.9 and Raw Accel do not have.
STUDIO_SPEED_FLOOR = 1e-6   # Math.log(Math.max(1e-6, x))
STUDIO_SYNC_WINDOW = 1e-9   # Math.abs(x - sync) < 1e-9 -> 1x (smooth branch only); upstream tests x == sync
STUDIO_RULES = [
    "Studio preview (index.html maccelEval), synchronous only: speeds below 1e-6 counts/ms are floored to 1e-6 "
    "before log(); maccel 0.5.9 and Raw Accel use the speed as is.",
    "Studio preview, synchronous with SMOOTH > 1/32: returns exactly 1x within 1e-9 counts/ms of SYNC_SPEED; "
    "maccel 0.5.9 and Raw Accel return 1x only at exactly SYNC_SPEED.",
]
MAX_INPUT_BYTES = 1 << 20   # import/--input files larger than 1 MiB are refused
MAX_JSON_DEPTH = 32         # exports nest 7 levels deep

# Mirrors mousetool.py MODES / PARAMS (kept here so this module has no Linux-only
# imports); test_rawaccel_export.py fails if the two ever drift apart.
MODES = ["linear", "natural", "synchronous", "no_accel", "lookup"]
PARAMS = {
    "SENS_MULT": (0.01, 50), "YX_RATIO": (0.01, 50), "INPUT_DPI": (50, 50000), "ANGLE_ROTATION": (-180, 180),
    "ACCEL": (0, 50), "OFFSET": (0, 1000), "OUTPUT_CAP": (0, 100),
    "DECAY_RATE": (0.0001, 100), "LIMIT": (1, 100),
    "GAMMA": (0.0001, 100), "SMOOTH": (0, 1), "MOTIVITY": (1.0001, 100), "SYNC_SPEED": (0.0001, 1000),
}
# Custom lookup-table (MODE="lookup") shared bounds with mouseaccel (MA-003) and Raw Accel Sensitivity LUT.
LUT_MIN_POINTS = 2
LUT_MAX_POINTS = 64
LUT_X_MIN = 0.0
LUT_X_MAX = 1000.0
LUT_DX_MIN = 0.001
LUT_Y_MIN = 0.05
LUT_Y_MAX = 10.0
LUT_DEFAULT_POINTS = ((0.0, 1.0), (10.0, 1.0))

# Mirrors index.html MACCEL_DEFAULTS.
DEFAULTS = {
    "MODE": "linear", "SENS_MULT": 1.0, "YX_RATIO": 1.0, "INPUT_DPI": 1000.0, "ANGLE_ROTATION": 0.0,
    "ACCEL": 0.0, "OFFSET": 0.0, "OUTPUT_CAP": 0.0,
    "DECAY_RATE": 0.1, "LIMIT": 1.5,
    "GAMMA": 1.0, "SMOOTH": 0.5, "MOTIVITY": 1.5, "SYNC_SPEED": 5.0,
}
SHARED_PARAMS = ("SENS_MULT", "YX_RATIO", "INPUT_DPI", "ANGLE_ROTATION")
MODE_PARAMS = {
    "linear": ("ACCEL", "OFFSET", "OUTPUT_CAP"),
    "natural": ("DECAY_RATE", "OFFSET", "LIMIT"),
    "synchronous": ("GAMMA", "SMOOTH", "MOTIVITY", "SYNC_SPEED"),
    "no_accel": (),
    "lookup": (),
}
NORMALIZED_DPI = 1000.0  # rawaccel-base.hpp NORMALIZED_DPI and maccel accel.h NORMALIZED_DPI

# Raw Accel JSON keys (wrapper/wrapper.cpp JsonProperty names, v1.7.1).
K_GAIN = "Gain / Velocity"
K_CAP = "Cap / Jump"
K_CAP_MODE = "Cap mode"
K_DOMAIN = "Stretches domain for horizontal vs vertical inputs"
K_RANGE = "Stretches accel range for horizontal vs vertical inputs"
K_ARGS_X = "Whole or horizontal accel parameters"
K_ARGS_Y = "Vertical accel parameters"
K_SPEED = "Input speed calculation parameters"
K_WHOLE = "Whole/combined accel (set false for 'by component' mode)"
K_IN_HALF = "Time in ms after which an input is weighted at half its original value."
K_SCALE_HALF = "Time in ms after which scale is weighted at half its original value."
K_OUT_HALF = "Time in ms after which an output is weighted at half its original value."
K_OUT_DPI = "Output DPI"
K_YX = "Y/X output DPI ratio (vertical sens multiplier)"
K_LR = "L/R output DPI ratio (left sens multiplier)"
K_UD = "U/D output DPI ratio (up sens multiplier)"
K_ROT = "Degrees of rotation"
K_SNAP = "Degrees of angle snapping"
K_SPEED_CAP = "Input Speed Cap"
K_POLL_LOCK = "Use constant time interval based on polling rate"
K_DPI = "DPI (normalizes input speed unit: counts/ms -> in/s)"
K_POLL = "Polling rate Hz (keep at 0 for automatic adjustment)"

ACCEL_MODES = ["classic", "jump", "natural", "synchronous", "power", "lut", "noaccel"]
CAP_MODES = ["in_out", "input", "output"]

# rawaccel-base.hpp accel_args defaults, under wrapper.cpp JSON names.
ACCEL_ARGS_DEFAULT = {
    "mode": "noaccel", K_GAIN: True,
    "inputOffset": 0.0, "outputOffset": 0.0, "acceleration": 0.005, "decayRate": 0.1,
    "gamma": 1.0, "motivity": 1.5, "exponentClassic": 2.0, "scale": 1.0, "exponentPower": 0.05,
    "limit": 1.5, "syncSpeed": 5.0, "smooth": 0.5,
    K_CAP: {"x": 15.0, "y": 1.5}, K_CAP_MODE: "output",
    "data": [],
}
SPEED_ARGS_DEFAULT = {K_WHOLE: True, "lpNorm": 2.0, K_IN_HALF: 0.0, K_SCALE_HALF: 0.0, K_OUT_HALF: 0.0}
# Profile fields maccel has no counterpart for; exports keep the neutral defaults
# and imports refuse anything else.
PROFILE_NEUTRAL = {
    K_DOMAIN: {"x": 1.0, "y": 1.0}, K_RANGE: {"x": 1.0, "y": 1.0},
    K_LR: 1.0, K_UD: 1.0, K_SNAP: 0.0, K_SPEED_CAP: 0.0,
}
# DeviceConfig as v1.7.1 serializes the default (setExtraInfo/minimumTime/
# maximumTime are omitted when default: wrapper.cpp ShouldSerialize*).
DEVICE_CONFIG_DEFAULT = {"disable": False, K_POLL_LOCK: False, K_DPI: 0, K_POLL: 0}
DEFAULT_TIME_MIN = 1000.0 / 8000 / 2  # rawaccel-base.hpp
DEFAULT_TIME_MAX = 100.0
LUT_RAW_DATA_CAPACITY = 514  # rawaccel-base.hpp

# maccel param -> where it lives in the exported settings (for reports).
FIELD_OF = {
    "SENS_MULT": f"profiles[0].{K_OUT_DPI} (= SENS_MULT x 1000)",
    "YX_RATIO": f"profiles[0].{K_YX}",
    "INPUT_DPI": f"defaultDeviceConfig.{K_DPI}",
    "ANGLE_ROTATION": f"profiles[0].{K_ROT}",
    "ACCEL": "argsX.acceleration",
    "OFFSET": "argsX.inputOffset",
    "OUTPUT_CAP": f"argsX.{K_CAP}.y",
    "DECAY_RATE": "argsX.decayRate",
    "LIMIT": "argsX.limit",
    "GAMMA": "argsX.gamma",
    "SMOOTH": "argsX.smooth",
    "MOTIVITY": "argsX.motivity",
    "SYNC_SPEED": "argsX.syncSpeed",
}
ARGS_FIELD = {
    "ACCEL": "acceleration", "OFFSET": "inputOffset", "DECAY_RATE": "decayRate", "LIMIT": "limit",
    "GAMMA": "gamma", "SMOOTH": "smooth", "MOTIVITY": "motivity", "SYNC_SPEED": "syncSpeed",
}


class RawAccelExportError(ValueError):
    """Invalid, out-of-range or unsupported input. `errors` lists every problem found."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


def _copy(obj):
    return json.loads(json.dumps(obj))


def _is_number(v):
    """A JSON number usable as a finite double (bools and ints too large for a double are not)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return v.bit_length() <= 1023
    return isinstance(v, float) and math.isfinite(v)


def _short(v, limit=60):
    """Bounded repr for error messages (huge ints, deep or long values never end up in full)."""
    if isinstance(v, int) and not isinstance(v, bool) and v.bit_length() > 64:
        return f"<integer of {v.bit_length()} bits>"
    try:
        r = repr(v)
    except (ValueError, RecursionError):
        r = f"<{type(v).__name__}>"
    return r if len(r) <= limit else r[:limit - 3] + "..."


# ----------------------------------------------------------------------------- maccel side

def default_lut_points():
    """Return a fresh copy of the default flat 1.0x custom lookup-table curve."""
    return [list(p) for p in LUT_DEFAULT_POINTS]


def validate_lut_points(points, label="LUT_POINTS"):
    """Validate and normalize a list of [x, y] lookup-table control points.

    Shared contract with mouseaccel (MA-003) and Raw Accel v1.7.1 Sensitivity LUT:
      - 2..64 [x, y] pairs
      - 0 <= x <= 1000, strictly increasing with x[i+1] - x[i] >= 0.001
      - 0.05 <= y <= 10.0
    """
    if not isinstance(points, (list, tuple)):
        raise RawAccelExportError(f"{label} must be an array of [x, y] control points (got {_short(points)})")
    n = len(points)
    if not LUT_MIN_POINTS <= n <= LUT_MAX_POINTS:
        raise RawAccelExportError(
            f"{label} must contain between {LUT_MIN_POINTS} and {LUT_MAX_POINTS} points (got {n})"
        )
    errors = []
    out = []
    prev_x = None
    for i, item in enumerate(points):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            errors.append(f"{label}[{i}] must be a [x, y] pair (got {_short(item)})")
            continue
        rx, ry = item
        if not _is_number(rx) or not _is_number(ry):
            errors.append(f"{label}[{i}] coordinates must be finite numbers (got {_short(item)})")
            continue
        x, y = float(rx), float(ry)
        if not LUT_X_MIN <= x <= LUT_X_MAX:
            errors.append(f"{label}[{i}].x must be between {LUT_X_MIN:g} and {LUT_X_MAX:g} (got {_short(rx)})")
        if not LUT_Y_MIN <= y <= LUT_Y_MAX:
            errors.append(f"{label}[{i}].y must be between {LUT_Y_MIN:g} and {LUT_Y_MAX:g} (got {_short(ry)})")
        if prev_x is not None:
            dx = x - prev_x
            if dx <= 0:
                errors.append(f"{label}[{i}].x ({x:g}) must be strictly greater than {label}[{i - 1}].x ({prev_x:g})")
            elif dx < LUT_DX_MIN - 1e-12:
                errors.append(
                    f"{label}[{i}].x - {label}[{i - 1}].x ({dx:g}) must be at least {LUT_DX_MIN:g}"
                )
        prev_x = x
        out.append([x, y])
    if errors:
        raise RawAccelExportError(errors)
    return out


def eval_lut_sens(x, points):
    """Exact piecewise-linear Sensitivity LUT evaluation matching mouseaccel & Raw Accel v1.7.1."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    lo, hi = 0, len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid][0] <= x:
            lo = mid
        else:
            hi = mid
    x0, y0 = points[lo]
    x1, y1 = points[lo + 1]
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def parse_lut_points_arg(raw):
    """Parse CLI LUT_POINTS value from JSON ('[[0,1],[10,1.5]]') or semicolon pairs ('0,1;10,1.5')."""
    text = raw.strip()
    if text.startswith("["):
        return validate_lut_points(loads(text))
    pairs = []
    for token in text.split(";"):
        parts = token.split(",")
        if len(parts) != 2:
            raise RawAccelExportError(f"LUT_POINTS entry must be x,y (got {_short(token)})")
        try:
            pairs.append([float(parts[0].strip()), float(parts[1].strip())])
        except ValueError:
            raise RawAccelExportError(f"LUT_POINTS entry must contain numbers (got {_short(token)})") from None
    return validate_lut_points(pairs)


def normalize_maccel(cfg):
    """Validate a complete maccel config {"MODE": str, <13 params>: number, optional LUT_POINTS}.

    Returns a new dict with float values (and normalized LUT_POINTS list when present).
    Raises RawAccelExportError listing every missing, unknown, non-numeric, non-finite
    or out-of-range entry.
    """
    if not isinstance(cfg, dict):
        raise RawAccelExportError("maccel config must be a JSON object")
    errors = []
    unknown = sorted(str(k) for k in set(cfg) - set(PARAMS) - {"MODE", "LUT_POINTS"})
    if unknown:
        errors.append(f"unknown maccel parameter(s): {', '.join(_short(k, 40) for k in unknown[:10])}")
    required = ["MODE", *PARAMS]
    if cfg.get("MODE") == "lookup":
        required.append("LUT_POINTS")
    missing = [k for k in required if k not in cfg]
    if missing:
        errors.append(f"missing maccel parameter(s): {', '.join(missing)}")
    out = {}
    if "MODE" in cfg:
        if cfg["MODE"] not in MODES:
            errors.append(f"MODE must be one of {', '.join(MODES)} (got {_short(cfg['MODE'])})")
        else:
            out["MODE"] = cfg["MODE"]
    for k, (lo, hi) in PARAMS.items():
        if k not in cfg:
            continue
        v = cfg[k]
        if not _is_number(v):
            errors.append(f"{k} must be a finite number (got {_short(v)})")
        elif not lo <= v <= hi:
            errors.append(f"{k} must be between {lo} and {hi} (got {_short(v)})")
        else:
            out[k] = float(v)
    if "LUT_POINTS" in cfg:
        try:
            out["LUT_POINTS"] = validate_lut_points(cfg["LUT_POINTS"])
        except RawAccelExportError as exc:
            errors.extend(exc.errors)
    if errors:
        raise RawAccelExportError(errors)
    return out


def with_defaults(partial):
    """Fill a partial maccel config from the app defaults; returns (cfg, defaulted_keys)."""
    if not isinstance(partial, dict):
        raise RawAccelExportError("maccel config must be a JSON object")
    defaulted = [k for k in DEFAULTS if k not in partial]
    merged = {**DEFAULTS, **partial}
    if merged.get("MODE") == "lookup" and "LUT_POINTS" not in partial:
        merged["LUT_POINTS"] = default_lut_points()
        defaulted.append("LUT_POINTS")
    return merged, defaulted


def app_sens(x, cfg, studio_rules=True):
    """Port of index.html maccelEval(x, cfg): returns (sens_x, sens_y).

    x is input speed in counts/ms normalized to 1000 DPI. test_rawaccel_export.py
    runs the JavaScript itself under node to confirm this port matches it.

    The Studio preview differs from maccel 0.5.9's kernel formulas (accel/*.h,
    which use the same expressions as Raw Accel) in exactly two synchronous-mode
    rules (see STUDIO_RULES). studio_rules=False drops them and gives the
    maccel kernel formula in doubles (the kernel itself runs in 32.32 fixed point).
    """
    m = cfg.get("MODE", "linear")
    s = 1.0
    if m == "linear":
        accel, off, cap = cfg["ACCEL"], cfg["OFFSET"], cfg["OUTPUT_CAP"]
        if x > off:
            b = accel * ((x - off) * (x - off)) / x
            sign = 1.0
            if cap > 0:
                c = cap - 1
                if c < 0:
                    c, sign = -c, -1.0
                b = min(b, c)
            s = 1 + sign * b
    elif m == "natural":
        decay, off, lim = cfg["DECAY_RATE"], cfg["OFFSET"], cfg["LIMIT"]
        if x > off and lim > 1 and decay > 0:
            lm = lim - 1
            a = decay / abs(lm)
            c = -lm / a
            ox = off - x
            out = lm * (math.exp(a * ox) / a - ox) + c
            s = out / x + 1
    elif m == "synchronous":
        gamma, smooth = cfg["GAMMA"], cfg["SMOOTH"]
        mot, sync = max(1.0001, cfg["MOTIVITY"]), max(0.0001, cfg["SYNC_SPEED"])
        log_m = math.log(mot)
        gc = gamma / log_m
        log_s = math.log(sync)
        sharp = 16.0 if smooth == 0 else 0.5 / smooth
        lx = math.log(max(STUDIO_SPEED_FLOOR, x) if studio_rules else x)
        if sharp >= 16:
            ls = max(-1.0, min(1.0, gc * (lx - log_s)))
            s = math.exp(ls * log_m)
        elif (abs(x - sync) < STUDIO_SYNC_WINDOW) if studio_rules else x == sync:
            s = 1.0
        else:
            ld = lx - log_s
            sign = 1.0 if ld > 0 else -1.0
            ls = gc * abs(ld)
            s = math.exp(sign * math.pow(math.tanh(math.pow(ls, sharp)), 1 / sharp) * log_m)
    elif m == "lookup":
        pts = cfg.get("LUT_POINTS")
        if pts and len(pts) >= LUT_MIN_POINTS:
            s = eval_lut_sens(x, pts)
    sx = s * cfg["SENS_MULT"]
    return sx, sx * cfg["YX_RATIO"]


def maccel_motion(dx, dy, time_ms, cfg):
    """Double-precision port of maccel 0.5.9 accel.h f_accelerate (no fixed point, no carry).

    Rotate the input, scale it by 1000 / INPUT_DPI, measure speed, then multiply
    by the curve sensitivity (SENS_MULT and YX_RATIO included). Returns (out_x, out_y).
    """
    rad = cfg["ANGLE_ROTATION"] * math.pi / 180
    if cfg["ANGLE_ROTATION"] != 0:
        dx, dy = dx * math.cos(rad) - dy * math.sin(rad), dx * math.sin(rad) + dy * math.cos(rad)
    f = NORMALIZED_DPI / cfg["INPUT_DPI"]
    dx, dy = dx * f, dy * f
    speed = math.hypot(dx, dy) / time_ms if time_ms > 0 else 0.0
    sx, sy = app_sens(speed, cfg, studio_rules=False) if speed > 0 else (cfg["SENS_MULT"], cfg["SENS_MULT"] * cfg["YX_RATIO"])
    return dx * sx, dy * sy


# ----------------------------------------------------------------------------- Raw Accel side

def default_profile(name=PROFILE_NAME):
    """A Raw Accel v1.7.1 default profile (rawaccel-base.hpp), JSON-shaped."""
    return {
        "name": name,
        K_DOMAIN: dict(PROFILE_NEUTRAL[K_DOMAIN]),
        K_RANGE: dict(PROFILE_NEUTRAL[K_RANGE]),
        K_ARGS_X: _copy(ACCEL_ARGS_DEFAULT),
        K_ARGS_Y: _copy(ACCEL_ARGS_DEFAULT),
        K_SPEED: dict(SPEED_ARGS_DEFAULT),
        K_OUT_DPI: NORMALIZED_DPI,
        K_YX: 1.0, K_LR: 1.0, K_UD: 1.0,
        K_ROT: 0.0, K_SNAP: 0.0, K_SPEED_CAP: 0.0,
    }


def build_settings(profile, dpi=0):
    """Wrap one profile in a DriverConfig shaped like v1.7.1 DriverConfig.ToJSON()."""
    dev = dict(DEVICE_CONFIG_DEFAULT)
    dev[K_DPI] = dpi
    return {
        "### Accel modes ###": " | ".join(ACCEL_MODES),
        "### Cap modes ###": " | ".join(CAP_MODES),
        "version": RAWACCEL_CONFIG_VERSION,
        "defaultDeviceConfig": dev,
        "profiles": [profile],
        "devices": [],
    }


def rawaccel_sens(x, args):
    """Raw Accel v1.7.1 sensitivity for one AccelArgs (JSON-shaped) at speed x > 0.

    Ports, per mode: classic<LEGACY> with cap mode "output"
    (accel-classic.hpp), natural<LEGACY>/natural<GAIN> (accel-natural.hpp),
    activation_framework<LEGACY> (accel-synchronous.hpp) and accel_noaccel.
    Other modes/variants are not ported and raise RawAccelExportError.
    """
    mode, gain = args["mode"], args[K_GAIN]
    if mode == "noaccel":
        return 1.0
    if mode == "classic" and not gain and args[K_CAP_MODE] == "output":
        power, off = args["exponentClassic"], args["inputOffset"]
        accel_raised = math.pow(args["acceleration"], power - 1)
        cap, sign = math.inf, 1.0
        if args[K_CAP]["y"] > 0:
            cap = args[K_CAP]["y"] - 1
            if cap < 0:
                cap, sign = -cap, -1.0
        if x <= off:
            return 1.0
        return sign * min(accel_raised * math.pow(x - off, power) / x, cap) + 1
    if mode == "natural":
        off, limit = args["inputOffset"], args["limit"] - 1
        accel = args["decayRate"] / abs(limit)
        if x <= off:
            return 1.0
        offset_x = off - x
        decay = math.exp(accel * offset_x)
        if not gain:
            return limit * (1 - (off - decay * offset_x) / x) + 1
        constant = -limit / accel
        return (limit * (decay / accel - offset_x) + constant) / x + 1
    if mode == "synchronous" and not gain:
        mot, sync, smooth = args["motivity"], args["syncSpeed"], args["smooth"]
        log_m = math.log(mot)
        gc = args["gamma"] / log_m
        log_s = math.log(sync)
        sharp = 16.0 if smooth == 0 else 0.5 / smooth
        if sharp >= 16:
            ls = gc * (math.log(x) - log_s)
            if ls < -1:
                return 1 / mot
            if ls > 1:
                return mot
            return math.exp(ls * log_m)
        if x == sync:
            return 1.0
        ld = math.log(x) - log_s
        if ld > 0:
            return math.exp(math.pow(math.tanh(math.pow(gc * ld, sharp)), 1 / sharp) * log_m)
        return math.exp(-math.pow(math.tanh(math.pow(-gc * ld, sharp)), 1 / sharp) * log_m)
    if mode == "lut" and not gain:
        data = args["data"]
        if not isinstance(data, list) or len(data) < 4 or len(data) % 2 != 0:
            raise RawAccelExportError("Raw Accel lookup mode requires an even data array with at least 2 points")
        pts = [(float(data[i]), float(data[i + 1])) for i in range(0, len(data), 2)]
        for i in range(1, len(pts)):
            if pts[i][0] <= pts[i - 1][0]:
                raise RawAccelExportError("Raw Accel lookup data x values must be strictly increasing")
        return eval_lut_sens(x, pts)
    raise RawAccelExportError(f"Raw Accel mode {mode!r} (gain={gain}, cap mode={args.get(K_CAP_MODE)!r}) is not ported")


def rawaccel_profile_sens(x, profile):
    """(sens_x, sens_y) Raw Accel applies to 1000-DPI-normalized counts at speed x.

    rawaccel.hpp modifier::modify, whole mode: scale = 1 + (f(x) - 1) * range
    weight, then output_dpi / 1000 (the device DPI factor cancels against the
    input normalization), Y additionally times the Y/X output DPI ratio.
    """
    scale = 1 + (rawaccel_sens(x, profile[K_ARGS_X]) - 1) * profile[K_RANGE]["x"]
    sx = scale * profile[K_OUT_DPI] / NORMALIZED_DPI
    return sx, sx * profile[K_YX]


def rawaccel_motion(dx, dy, time_ms, settings):
    """Port of rawaccel.hpp modifier::modify for the subset this module exports.

    Device 0 uses profiles[0] and defaultDeviceConfig (driver.cpp). Rotation
    uses math-vec2.hpp direction()/rotate(); dpi_factor = 1000 / device DPI
    (1 when DPI is 0). Driver-side time clamping is not modelled.
    """
    prof = settings["profiles"][0]
    dpi = settings["defaultDeviceConfig"][K_DPI]
    dpi_factor = NORMALIZED_DPI / dpi if dpi > 0 else 1.0
    if prof[K_ROT] != 0:
        r = prof[K_ROT] * math.pi / 180
        c, s = math.cos(r), math.sin(r)
        dx, dy = dx * c - dy * s, dx * s + dy * c
    ips = dpi_factor / time_ms
    speed = math.hypot(dx * ips * prof[K_DOMAIN]["x"], dy * ips * prof[K_DOMAIN]["y"])
    scale = 1 + (rawaccel_sens(speed, prof[K_ARGS_X]) - 1) * prof[K_RANGE]["x"] if speed > 0 else 1.0
    adj = prof[K_OUT_DPI] / NORMALIZED_DPI * dpi_factor
    return dx * scale * adj, dy * scale * adj * prof[K_YX]


PROFILE_REQUIRED = ("name", K_DOMAIN, K_RANGE, K_ARGS_X, K_ARGS_Y, K_SPEED, K_OUT_DPI, K_YX, K_LR, K_UD,
                    K_ROT, K_SNAP, K_SPEED_CAP)  # wrapper.cpp Profile: ItemRequired = Always
ARGS_NUMERIC = ("inputOffset", "outputOffset", "acceleration", "decayRate", "gamma", "motivity", "exponentClassic",
                "scale", "exponentPower", "limit", "syncSpeed", "smooth")


def validate_settings(settings):
    """Offline port of the checks writer.exe runs before applying a DriverConfig.

    Structure (wrapper.cpp, Json.NET): DriverConfig, Profile and DeviceConfig
    members marked ItemRequired = Always must be present; booleans must be JSON
    booleans and DPI / polling rate JSON integers. Both accel payloads need every
    AccelArgs member (a missing one deserializes as 0, not the C++ default) and a
    `data` array: AccelArgs::OnDeserializedMethod reads data->Length, so a missing
    or null array fails in the writer. Values: rawaccel-validate.hpp
    valid(profile) (argsY only in by-component mode, as upstream) and
    valid(device_settings). Returns a list of error strings (empty = would be
    accepted). Json.NET's own type coercion is deliberately not modelled: anything
    it might coerce is rejected here.
    """
    errs = []
    if not isinstance(settings, dict):
        return ["settings must be a JSON object"]
    for k in ("version", "defaultDeviceConfig", "profiles", "devices"):
        if k not in settings:
            errs.append(f"missing {k!r}")
    if errs:
        return errs
    if not isinstance(settings["version"], str):
        errs.append(f"version must be a string (got {_short(settings['version'])})")
    profiles, devices = settings["profiles"], settings["devices"]
    if not isinstance(profiles, list) or not isinstance(devices, list):
        return errs + ["profiles and devices must be lists"]

    def is_vec(w):
        return isinstance(w, dict) and _is_number(w.get("x")) and _is_number(w.get("y"))

    def accel_structure(a, where):
        """Shape and types of one AccelArgs; returns True if values can be checked."""
        if not isinstance(a, dict):
            errs.append(f"{where} must be an object")
            return False
        missing = [k for k in ACCEL_ARGS_DEFAULT if k not in a]
        if missing:
            errs.append(f"{where}: missing {', '.join(repr(k) for k in missing)}")
            return False
        ok = True
        if a["mode"] not in ACCEL_MODES:
            errs.append(f"{where}.mode must be one of {', '.join(ACCEL_MODES)} (got {_short(a['mode'])})")
            ok = False
        if a[K_CAP_MODE] not in CAP_MODES:
            errs.append(f"{where}.{K_CAP_MODE} must be one of {', '.join(CAP_MODES)} (got {_short(a[K_CAP_MODE])})")
            ok = False
        if not isinstance(a[K_GAIN], bool):
            errs.append(f"{where}.{K_GAIN} must be true/false (got {_short(a[K_GAIN])})")
            ok = False
        bad = [k for k in ARGS_NUMERIC if not _is_number(a[k])]
        if bad:
            errs.append(f"{where}: {', '.join(bad)} must be finite numbers")
            ok = False
        if not is_vec(a[K_CAP]):
            errs.append(f"{where}.{K_CAP} must be {{\"x\": number, \"y\": number}}")
            ok = False
        data = a["data"]
        if not isinstance(data, list) or not all(_is_number(d) for d in data):
            errs.append(f"{where}.data must be an array of numbers (got {_short(data)})")
            ok = False
        elif len(data) > LUT_RAW_DATA_CAPACITY:
            errs.append(f"{where}: data size > max ({LUT_RAW_DATA_CAPACITY})")
            ok = False
        return ok

    def accel_values(a, where):
        mode, cap_mode, cap = a["mode"], a[K_CAP_MODE], a[K_CAP]
        if mode == "lut":
            data = a["data"]
            if len(data) < 4:
                errs.append(f"{where}: lookup mode requires at least 2 points")
            elif len(data) % 2 != 0:
                errs.append(f"{where}: lookup mode data must contain x, y pairs")
            else:
                prev_x = None
                for idx in range(0, len(data), 2):
                    px, py = data[idx], data[idx + 1]
                    if px < 0 or py <= 0:
                        errs.append(f"{where}: lookup point ({px:g}, {py:g}) out of range")
                        break
                    if prev_x is not None and px <= prev_x:
                        errs.append(f"{where}: lookup x values must be strictly increasing")
                        break
                    prev_x = px
        if a["inputOffset"] < 0 or a["outputOffset"] < 0:
            errs.append(f"{where}: offset can not be negative")
        jump_or_io = mode == "jump" or (mode in ("classic", "power") and cap_mode == "in_out")
        for axis, label in (("x", "input"), ("y", "output")):
            if cap[axis] < 0:
                errs.append(f"{where}: cap ({label}) can not be negative")
            elif cap[axis] == 0 and jump_or_io:
                errs.append(f"{where}: cap ({label}) can not be 0")
        if (mode == "classic" and 0 < cap["x"] < a["inputOffset"] and cap_mode != "output") or \
                (mode == "power" and 0 < cap["y"] < a["outputOffset"] and cap_mode != "input"):
            errs.append(f"{where}: cap < offset")
        for k, label in (("acceleration", "acceleration"), ("scale", "scale"), ("gamma", "gamma"),
                         ("decayRate", "decay rate"), ("exponentPower", "exponent"), ("limit", "limit"),
                         ("syncSpeed", "synchronous speed")):
            if a[k] <= 0:
                errs.append(f"{where}: {label} must be positive")
        if a["motivity"] <= 1:
            errs.append(f"{where}: motivity must be greater than 1")
        if a["exponentClassic"] <= 1:
            errs.append(f"{where}: exponent must be greater than 1")
        if not 0 <= a["smooth"] <= 1:
            errs.append(f"{where}: smooth must be between 0 and 1")

    for i, p in enumerate(profiles):
        where = f"profiles[{i}]"
        if not isinstance(p, dict):
            errs.append(f"{where} must be an object")
            continue
        missing = [k for k in PROFILE_REQUIRED if k not in p]
        if missing:
            errs.append(f"{where}: missing {', '.join(repr(k) for k in missing)}")
            continue
        sp = p[K_SPEED]
        sp_ok = isinstance(sp, dict) and all(k in sp for k in SPEED_ARGS_DEFAULT)
        if not sp_ok:
            errs.append(f"{where}.{K_SPEED} must be an object with {', '.join(repr(k) for k in SPEED_ARGS_DEFAULT)}")
        elif not isinstance(sp[K_WHOLE], bool) or not all(_is_number(sp[k]) for k in SPEED_ARGS_DEFAULT if k != K_WHOLE):
            errs.append(f"{where}.{K_SPEED}: whole must be true/false and the rest finite numbers")
            sp_ok = False
        x_ok = accel_structure(p[K_ARGS_X], f"{where}.argsX")
        y_ok = accel_structure(p[K_ARGS_Y], f"{where}.argsY")
        if x_ok:
            accel_values(p[K_ARGS_X], f"{where}.argsX")
        if y_ok and sp_ok and sp[K_WHOLE] is False:
            accel_values(p[K_ARGS_Y], f"{where}.argsY")
        nums = (K_OUT_DPI, K_YX, K_LR, K_UD, K_ROT, K_SNAP, K_SPEED_CAP)
        if not all(_is_number(p[k]) for k in nums) or not (is_vec(p[K_DOMAIN]) and is_vec(p[K_RANGE])):
            errs.append(f"{where}: non-numeric profile field")
            continue
        if not isinstance(p["name"], str) or not p["name"]:
            errs.append(f"{where}: profile name can not be empty")
        if p[K_SPEED_CAP] < 0:
            errs.append(f"{where}: speed cap is negative")
        if not 0 <= p[K_SNAP] <= 45:
            errs.append(f"{where}: snap angle must be between 0 and 45 degrees")
        if p[K_OUT_DPI] == 0:
            errs.append(f"{where}: output DPI is 0")
        if p[K_YX] == 0:
            errs.append(f"{where}: Y/X output DPI ratio is 0")
        if p[K_DOMAIN]["x"] <= 0 or p[K_DOMAIN]["y"] <= 0:
            errs.append(f"{where}: domain weights must be positive")
        if p[K_LR] <= 0 or p[K_UD] <= 0:
            errs.append(f"{where}: output DPI ratio must be positive")
        if sp_ok and sp["lpNorm"] <= 0:
            errs.append(f"{where}: Lp norm must be positive (default=2)")
        if p[K_RANGE]["x"] < 0 or p[K_RANGE]["y"] < 0:
            errs.append(f"{where}: range weights must be positive")

    dev_cfgs = [("defaultDeviceConfig", settings["defaultDeviceConfig"])]
    for i, d in enumerate(devices):
        where = f"devices[{i}]"
        if not isinstance(d, dict) or not all(k in d for k in ("name", "profile", "id", "config")):
            errs.append(f"{where} must be an object with 'name', 'profile', 'id' and 'config'")
            continue
        if not all(isinstance(d[k], str) for k in ("name", "profile", "id")):
            errs.append(f"{where}: name, profile and id must be strings")
        dev_cfgs.append((f"{where}.config", d["config"]))
    for where, cfg in dev_cfgs:
        if not isinstance(cfg, dict):
            errs.append(f"{where} must be an object")
            continue
        missing = [k for k in ("disable", K_DPI, K_POLL) if k not in cfg]
        if missing:
            errs.append(f"{where}: missing {', '.join(repr(k) for k in missing)}")
            continue
        for k in ("disable", "setExtraInfo", K_POLL_LOCK):
            if k in cfg and not isinstance(cfg[k], bool):
                errs.append(f"{where}.{k} must be true/false (got {_short(cfg[k])})")
        for k, label in ((K_DPI, "dpi"), (K_POLL, "polling rate")):
            v = cfg[k]
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 2 ** 31 - 1:
                errs.append(f"{where}: {label} must be a non-negative integer (got {_short(v)})")
        tmin, tmax = cfg.get("minimumTime", DEFAULT_TIME_MIN), cfg.get("maximumTime", DEFAULT_TIME_MAX)
        if not (_is_number(tmin) and _is_number(tmax)) or tmin <= 0 or tmax < tmin:
            errs.append(f"{where}: minimum time must be positive and not above maximum time")
    return errs


# ----------------------------------------------------------------------------- forward mapping

def maccel_to_rawaccel(cfg):
    """Map a maccel config to a Raw Accel v1.7.1 DriverConfig.

    Returns (settings, mapping). `mapping` has: mode, rawaccel_mode, gain,
    formula (which upstream function is used and the maccel source it equals;
    a statement about the formulas, not a measurement: see curve_check), flat,
    params (one row per maccel parameter with field/value/status), lossy,
    unsupported (behaviour with no counterpart) and notes. Nothing is dropped
    silently: every parameter appears in `params`, and any value that could not
    be carried exactly is listed in `lossy`.

    All 13 numeric parameters are carried (Raw Accel keeps every mode's fields in
    one AccelArgs, like maccel), so switching modes later in the Raw Accel GUI
    keeps the user's other-mode values.
    """
    cfg = normalize_maccel(cfg)
    mode = cfg["MODE"]
    prof = default_profile()
    ax = prof[K_ARGS_X]
    rows, lossy, notes = [], [], []

    def row(param, value, field, status, note=None):
        r = {"param": param, "value": cfg[param], "field": field, "rawaccel_value": value, "status": status}
        if note:
            r["note"] = note
        rows.append(r)

    # shared parameters (maccel accel.h f_accelerate vs rawaccel.hpp modify)
    prof[K_OUT_DPI] = cfg["SENS_MULT"] * NORMALIZED_DPI
    row("SENS_MULT", prof[K_OUT_DPI], FIELD_OF["SENS_MULT"], "exact")
    prof[K_YX] = cfg["YX_RATIO"]
    row("YX_RATIO", prof[K_YX], FIELD_OF["YX_RATIO"], "exact")
    prof[K_ROT] = cfg["ANGLE_ROTATION"]
    row("ANGLE_ROTATION", prof[K_ROT], FIELD_OF["ANGLE_ROTATION"], "exact")
    dpi_in = cfg["INPUT_DPI"]
    dpi = int(round(dpi_in))
    if dpi == dpi_in:
        row("INPUT_DPI", dpi, FIELD_OF["INPUT_DPI"], "exact")
    else:
        msg = (f"INPUT_DPI {dpi_in} is not an integer; Raw Accel's device DPI is an int, exported as {dpi} "
               f"(speed axis scaled by {dpi_in / dpi:.9g})")
        row("INPUT_DPI", dpi, FIELD_OF["INPUT_DPI"], "lossy", msg)
        lossy.append(msg)
    unsupported = [
        "Per-device settings: not exported. INPUT_DPI goes in defaultDeviceConfig, which Raw Accel applies to every "
        "mouse without its own device entry; per-device entries (own DPI, profile, disable) need Windows device IDs.",
        "Timing: not represented. Raw Accel's polling rate, constant-interval lock and per-report time clamp "
        f"({DEFAULT_TIME_MIN:g}-{DEFAULT_TIME_MAX:g} ms) stay at their defaults; maccel has no such settings, and "
        "its own report timing (and 32.32 fixed-point arithmetic) is not modelled by the curve check.",
    ]

    # per-mode fields: all carried; active ones decide the curve
    active = set(MODE_PARAMS[mode])
    for p, field in ARGS_FIELD.items():
        ax[field] = cfg[p]
    ax[K_CAP]["y"] = cfg["OUTPUT_CAP"]
    if cfg["ACCEL"] <= 0:
        # rawaccel-validate.hpp: "acceleration must be positive" (checked in every mode)
        ax["acceleration"] = ACCEL_ARGS_DEFAULT["acceleration"]
    for p in ("ACCEL", "OFFSET", "OUTPUT_CAP", "DECAY_RATE", "LIMIT", "GAMMA", "SMOOTH", "MOTIVITY", "SYNC_SPEED"):
        value = ax[K_CAP]["y"] if p == "OUTPUT_CAP" else ax[ARGS_FIELD[p]]
        state = "active" if p in active else "inactive in this mode"
        if p == "ACCEL" and cfg["ACCEL"] <= 0:
            msg = (f"ACCEL=0 cannot be stored (Raw Accel requires acceleration > 0); field set to Raw Accel default "
                   f"{value} ({state})")
            row(p, value, FIELD_OF[p], "lossy", msg)
            lossy.append(msg)
        else:
            row(p, value, FIELD_OF[p], "exact", None if p in active else state)

    rows.sort(key=lambda r: list(PARAMS).index(r["param"]))
    rawaccel_mode, gain, flat_reason, formula = "noaccel", True, None, None
    if mode == "linear":
        if cfg["ACCEL"] <= 0:
            flat_reason = "linear with ACCEL=0 is flat (1x)"
        else:
            # accel-classic.hpp classic<LEGACY>, cap_mode::out: with exponent 2,
            # accel_raised = acceleration and base_fn = accel*(x-off)^2/x, which is
            # maccel accel/linear.h ("linear in rawaccel is classic with exponent = 2").
            rawaccel_mode, gain = "classic", False
            formula = "classic<LEGACY>, exponent 2, cap mode out (accel-classic.hpp) = maccel 0.5.9 accel/linear.h"
            ax["exponentClassic"] = 2.0
            ax[K_CAP_MODE] = "output"
            notes.append("linear -> Raw Accel Classic, exponent 2, Gain off (legacy sensitivity), cap mode 'output' "
                         "(legacy sensitivity cap; OUTPUT_CAP 0 = uncapped).")
    elif mode == "natural":
        if cfg["LIMIT"] <= 1:
            flat_reason = "natural with LIMIT=1 is flat (1x); Raw Accel's natural would divide by |limit-1| = 0"
        else:
            # maccel accel/natural.h is Raw Accel natural<GAIN> (accel-natural.hpp).
            rawaccel_mode, gain = "natural", True
            formula = "natural<GAIN> (accel-natural.hpp) = maccel 0.5.9 accel/natural.h"
            notes.append("natural -> Raw Accel Natural with Gain on (maccel's natural is the gain form).")
    elif mode == "synchronous":
        # maccel accel/synchronous.h is activation_framework<LEGACY>; the GAIN form is a LUT integral.
        rawaccel_mode, gain = "synchronous", False
        formula = "activation_framework<LEGACY> (accel-synchronous.hpp) = maccel 0.5.9 accel/synchronous.h"
        notes.append("synchronous -> Raw Accel Synchronous with Gain off (maccel's synchronous is the sensitivity form).")
    elif mode == "lookup":
        pts = cfg["LUT_POINTS"]
        rawaccel_mode, gain = "lut", False
        formula = "accel_lut<LEGACY> (accel-lookup.hpp, Sensitivity mode) = mouseaccel accel/lookup.h"
        ax["data"] = [v for pt in pts for v in pt]
        notes.append(
            f"lookup -> Raw Accel LookUpTable with Gain off (Sensitivity mode, {len(pts)} control points, "
            f"flat clamping outside [{pts[0][0]:g}, {pts[-1][0]:g}] counts/ms)."
        )
    else:
        notes.append("no_accel -> Raw Accel No Acceleration (output DPI, Y/X ratio and rotation still apply).")
    if flat_reason:
        notes.append(f"{flat_reason}; exported as Raw Accel No Acceleration, which gives the same 1x curve.")
    if formula is None:
        formula = "noaccel (accel-noaccel.hpp): constant 1x" + (f", because {flat_reason}" if flat_reason else "")
    ax["mode"], ax[K_GAIN] = rawaccel_mode, gain
    # Whole (combined) mode: argsY is unused by the driver; keep it identical so the
    # GUI shows the same curve if someone switches to by-component later.
    prof[K_ARGS_Y] = _copy(ax)
    notes.append("Raw Accel-only features stay at neutral defaults: whole/combined speed, Lp norm 2, no smoothing, "
                 "domain/range weights 1, L/R and U/D ratios 1, no angle snapping, no input speed cap.")
    if mode == "synchronous":
        notes.extend(STUDIO_RULES)

    settings = build_settings(prof, dpi=dpi)
    invalid = validate_settings(settings)
    if invalid:  # would be a mapping bug: never hand writer.exe something it rejects
        raise RawAccelExportError([f"internal: exported settings fail Raw Accel validation: {e}" for e in invalid])
    mapping = {
        "mode": mode, "rawaccel_mode": rawaccel_mode, "gain": gain,
        "formula": formula, "flat": bool(flat_reason),
        "params": rows, "lossy": lossy, "unsupported": unsupported, "notes": notes,
    }
    return settings, mapping


# ----------------------------------------------------------------------------- reverse mapping

def _get(obj, key, where, errors, kind=None):
    if not isinstance(obj, dict) or key not in obj:
        errors.append(f"{where}: missing {key!r}")
        return None
    v = obj[key]
    if kind == "num" and not _is_number(v):
        errors.append(f"{where}.{key} must be a finite number (got {_short(v)})")
        return None
    if kind == "bool" and not isinstance(v, bool):
        errors.append(f"{where}.{key} must be true/false (got {_short(v)})")
        return None
    if kind == "vec" and not (isinstance(v, dict) and _is_number(v.get("x")) and _is_number(v.get("y"))):
        errors.append(f"{where}.{key} must be {{\"x\": number, \"y\": number}} (got {_short(v)})")
        return None
    return v


def _timing_losses(cfg, where):
    """Raw Accel timing/extra-info settings maccel cannot represent, as lossy messages."""
    out = []
    checks = (("setExtraInfo", False, "setExtraInfo"), (K_POLL_LOCK, False, "constant time interval"),
              (K_POLL, 0, "polling rate Hz"), ("minimumTime", DEFAULT_TIME_MIN, "minimum time ms"),
              ("maximumTime", DEFAULT_TIME_MAX, "maximum time ms"))
    for key, default, label in checks:
        if key in cfg and cfg[key] != default:
            out.append(f"{where}: {label} = {_short(cfg[key])} ignored (maccel has no equivalent timing setting)")
    return out


def rawaccel_to_maccel(settings, profile_name=None):
    """Map a Raw Accel v1.7.1 DriverConfig back to a maccel config.

    Returns (cfg, report). Raises RawAccelExportError, listing every reason, if
    the settings are malformed or use something maccel cannot express (power,
    jump, lookup, classic exponent != 2, gain caps, by-component mode,
    smoothing, snapping, weights, speed cap, directional ratios, a disabled
    default device, per-device entries that are disabled or use another DPI
    or profile). Timing settings (polling rate, constant interval, time clamp,
    setExtraInfo) do not change the curve and are reported in `lossy` as
    ignored. Inactive-mode fields outside maccel's range fall back to the app
    default and are reported in `lossy` too.
    """
    errors, lossy, notes = [], [], []
    if not isinstance(settings, dict):
        raise RawAccelExportError("Raw Accel settings must be a JSON object")
    invalid = validate_settings(settings)
    if invalid:
        raise RawAccelExportError([f"Raw Accel would reject these settings: {e}" for e in invalid])
    profiles = _get(settings, "profiles", "settings", errors)
    dev_cfg = _get(settings, "defaultDeviceConfig", "settings", errors)
    devices = settings.get("devices", [])
    if errors:
        raise RawAccelExportError(errors)
    if not isinstance(profiles, list) or not profiles or not all(isinstance(p, dict) for p in profiles):
        raise RawAccelExportError("settings.profiles must be a non-empty list of profiles")
    if profile_name is None:
        if len(profiles) > 1:
            names = ", ".join(_short(p.get("name"), 40) for p in profiles[:10])
            raise RawAccelExportError(f"settings has {len(profiles)} profiles ({names}); choose one with profile_name")
        prof = profiles[0]
    else:
        match = [p for p in profiles if p.get("name") == profile_name]
        if not match:
            raise RawAccelExportError(f"no profile named {_short(profile_name)}")
        prof = match[0]
    version = settings.get("version")
    if version is not None and not (isinstance(version, str) and version.startswith("1.7")):
        notes.append(f"settings version {_short(version)} is not 1.7.x; mapped using the v1.7.1 field layout")

    where = f"profile {_short(prof.get('name'), 40)}"
    ax = _get(prof, K_ARGS_X, where, errors)
    sp = _get(prof, K_SPEED, where, errors)
    out_dpi = _get(prof, K_OUT_DPI, where, errors, "num")
    yx = _get(prof, K_YX, where, errors, "num")
    rot = _get(prof, K_ROT, where, errors, "num")
    for key, neutral in PROFILE_NEUTRAL.items():
        v = _get(prof, key, where, errors, "vec" if isinstance(neutral, dict) else "num")
        if v is not None and v != neutral:
            errors.append(f"unsupported: {where} {key!r} = {_short(v)} (maccel needs {neutral!r})")
    if isinstance(sp, dict):
        for key, neutral in SPEED_ARGS_DEFAULT.items():
            v = _get(sp, key, f"{where}.speed", errors, "bool" if key == K_WHOLE else "num")
            if v is not None and v != neutral:
                errors.append(f"unsupported: {where} speed setting {key!r} = {_short(v)} (maccel needs {neutral!r})")
    elif sp is not None:
        errors.append(f"{where}.{K_SPEED} must be an object")
    # validate_settings() above guarantees the device structure and types.
    dpi = dev_cfg[K_DPI]
    if dev_cfg["disable"]:
        errors.append("unsupported: defaultDeviceConfig.disable is true (Raw Accel off for every mouse)")
    lossy.extend(_timing_losses(dev_cfg, "defaultDeviceConfig"))
    chosen, first = prof["name"], profiles[0]["name"]
    if chosen != first:
        notes.append(f"in Raw Accel, mice without a device entry use the first profile {_short(first)}, "
                     f"not the imported {_short(chosen)}")
    for d in devices:
        c, label = d["config"], f"device {_short(d['id'])}"
        if c["disable"]:
            errors.append(f"unsupported: {label} is disabled in Raw Accel; maccel cannot exempt one mouse")
        if c[K_DPI] != dpi:
            errors.append(f"unsupported: {label} has its own DPI {c[K_DPI]}; maccel has one INPUT_DPI ({dpi})")
        dev_prof = d["profile"] or first
        if dev_prof != chosen:
            errors.append(f"unsupported: {label} uses profile {_short(dev_prof)}, not the imported {_short(chosen)}")
        lossy.extend(_timing_losses(c, label))
    if devices:
        notes.append(f"{len(devices)} per-device entr{'y' if len(devices) == 1 else 'ies'} "
                     f"({', '.join(_short(d['id'], 40) for d in devices[:5])}) folded into the one maccel config: "
                     "same DPI and profile, enabled")
    if not isinstance(ax, dict):
        if ax is not None:
            errors.append(f"{where}.{K_ARGS_X} must be an object")
        raise RawAccelExportError(errors)

    a_where = f"{where}.argsX"
    ra_mode = _get(ax, "mode", a_where, errors)
    gain = _get(ax, K_GAIN, a_where, errors, "bool")
    vals = {f: _get(ax, f, a_where, errors, "num") for f in (*ARGS_FIELD.values(), "exponentClassic")}
    cap = _get(ax, K_CAP, a_where, errors, "vec")
    cap_mode = _get(ax, K_CAP_MODE, a_where, errors)
    if errors:
        raise RawAccelExportError(errors)

    mode = {"classic": "linear", "natural": "natural", "synchronous": "synchronous",
            "noaccel": "no_accel", "lut": "lookup"}.get(ra_mode)
    lut_points = None
    if mode is None:
        errors.append(f"unsupported: Raw Accel mode {_short(ra_mode)} has no maccel equivalent "
                      "(supported: classic, natural, synchronous, lut, noaccel)")
    elif mode == "linear":
        if vals["exponentClassic"] != 2:
            errors.append(f"unsupported: classic exponent {vals['exponentClassic']} (maccel linear is exponent 2)")
        if gain:
            errors.append("unsupported: classic with Gain on (maccel linear is the legacy sensitivity form)")
        if cap_mode != "output":
            errors.append(f"unsupported: classic cap mode {cap_mode!r} (maccel's OUTPUT_CAP is cap mode 'output')")
    elif mode == "natural" and not gain:
        errors.append("unsupported: natural with Gain off (maccel natural is the gain form)")
    elif mode == "synchronous" and gain:
        errors.append("unsupported: synchronous with Gain on (maccel synchronous is the sensitivity form)")
    elif mode == "lookup":
        if gain:
            errors.append("unsupported: lut with Gain on (Velocity mode); Custom curves require Sensitivity mode (Gain off)")
        else:
            raw_data = ax.get("data")
            if not isinstance(raw_data, list) or len(raw_data) % 2 != 0:
                errors.append(f"unsupported: lut data must contain x, y pairs (got {_short(raw_data)})")
            else:
                pairs = [[raw_data[i], raw_data[i + 1]] for i in range(0, len(raw_data), 2)]
                try:
                    lut_points = validate_lut_points(pairs, label="argsX.data")
                except RawAccelExportError as exc:
                    errors.extend(exc.errors)
    if errors:
        raise RawAccelExportError(errors)

    cfg = {"MODE": mode, "SENS_MULT": out_dpi / NORMALIZED_DPI, "YX_RATIO": yx, "ANGLE_ROTATION": rot,
           "INPUT_DPI": dpi if dpi > 0 else NORMALIZED_DPI, "OUTPUT_CAP": cap["y"]}
    if lut_points is not None:
        cfg["LUT_POINTS"] = lut_points
    if dpi <= 0:
        notes.append("device DPI 0 (no normalization) is equivalent to maccel INPUT_DPI 1000")
    for p, f in ARGS_FIELD.items():
        cfg[p] = vals[f]
    active = set(MODE_PARAMS[mode]) | set(SHARED_PARAMS)
    for p, (lo, hi) in PARAMS.items():
        v = cfg[p]
        if lo <= v <= hi:
            continue
        src = FIELD_OF[p]
        if p in active:
            errors.append(f"{p} from {src} = {_short(v)} is outside maccel's range {lo}..{hi}")
        else:
            cfg[p] = DEFAULTS[p]
            lossy.append(f"{p} from {src} = {_short(v)} is outside maccel's range {lo}..{hi}; inactive in {mode}, "
                         f"set to default {DEFAULTS[p]}")
    if errors:
        raise RawAccelExportError(errors)
    return normalize_maccel(cfg), {"profile": prof.get("name"), "rawaccel_mode": ra_mode, "lossy": lossy, "notes": notes}


# ----------------------------------------------------------------------------- curve check

def sample_speeds():
    """Default grid (counts/ms at 1000 DPI): log-spaced 1e-3..1e3 plus the app's 0..40 axis."""
    log_pts = [10 ** (-3 + 6 * i / 240) for i in range(241)]
    axis = [0.05 + 0.25 * i for i in range(161)]
    return sorted(set(log_pts + axis))


def edge_speeds(cfg):
    """Mode-specific samples on and around every speed where a formula branches.

    All modes: the ends of the supported range. Linear/natural: OFFSET. Linear:
    the speed where OUTPUT_CAP starts to bind. Synchronous: SYNC_SPEED (including
    the Studio preview's +-1e-9 window) and the two speeds where the hard clamp
    (SMOOTH < 1/32) reaches 1/MOTIVITY and MOTIVITY. Each point is sampled
    exactly and at relative (1e-12..1e-3) and absolute (5e-10..2e-9) offsets.
    """
    pts = [SPEED_MIN, SPEED_MIN * (1 + 1e-9), 1e-3, 1e3, SPEED_MAX]

    def around(c):
        if not (math.isfinite(c) and c > 0):
            return
        pts.append(c)
        for r in (1e-12, 1e-9, 1e-6, 1e-3):
            pts.extend((c * (1 - r), c * (1 + r)))
        for a in (5e-10, 9.99e-10, 1e-9, 1.001e-9, 2e-9):
            pts.extend((c - a, c + a))

    mode = cfg["MODE"]
    if mode in ("linear", "natural"):
        around(cfg["OFFSET"])
    if mode == "natural":
        # just above the offset, where the natural formula's cancellation is worst
        pts.extend(cfg["OFFSET"] + 10 ** (k / 2) for k in range(-12, 1))
    if mode == "linear" and cfg["ACCEL"] > 0 and cfg["OUTPUT_CAP"] > 0 and cfg["OUTPUT_CAP"] != 1:
        # accel * (x - off)^2 / x = |cap - 1|  ->  accel x^2 - (2 accel off + c) x + accel off^2 = 0
        a, off, c = cfg["ACCEL"], cfg["OFFSET"], abs(cfg["OUTPUT_CAP"] - 1)
        b = 2 * a * off + c
        around((b + math.sqrt(max(0.0, b * b - 4 * a * a * off * off))) / (2 * a))
    if mode == "synchronous":
        sync = cfg["SYNC_SPEED"]
        around(sync)
        edge = math.log(cfg["MOTIVITY"]) / cfg["GAMMA"]  # |log(x / sync)| where |log_space| = 1
        if edge < 700:
            around(sync * math.exp(edge))
            around(sync * math.exp(-edge))
    if mode == "lookup":
        lut = cfg["LUT_POINTS"]
        for i, (kx, _) in enumerate(lut):
            around(kx)
            if i + 1 < len(lut):
                pts.append(0.5 * (kx + lut[i + 1][0]))
        if lut[0][0] > SPEED_MIN:
            pts.append(0.5 * (SPEED_MIN + lut[0][0]))
        pts.extend((lut[-1][0] + 1.0, lut[-1][0] * 1.5))
    return sorted(x for x in set(pts) if SPEED_MIN <= x <= SPEED_MAX)


def _rel(a, b):
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


EXP_ULPS = 2  # assumed max difference between two libms' exp() (each within 1 ulp)


def natural_exp_spread(x, args):
    """How far Raw Accel's natural<GAIN> sensitivity moves if exp() is EXP_ULPS ulps off.

    The formula (accel-natural.hpp; index.html uses the same expression) adds and
    subtracts terms of size |limit-1| / accel, so for small speeds and a small
    decay rate the result cancels catastrophically. Studio runs it on the browser's
    JavaScript exp() and Raw Accel on the MSVC CRT exp(); identical Python ports
    agree with each other but cannot show that those two agree. This returns the
    relative change of the sensitivity under +-1..EXP_ULPS ulp changes of exp(),
    an estimate of how much two correct implementations can disagree at x.
    """
    off, limit = args["inputOffset"], args["limit"] - 1
    if x <= off:
        return 0.0
    accel = args["decayRate"] / abs(limit)
    constant = -limit / accel
    offset_x = off - x
    decay = math.exp(accel * offset_x)

    def sens(e):
        return (limit * (e / accel - offset_x) + constant) / x + 1

    base = sens(decay)
    spread = 0.0
    for direction in (math.inf, -math.inf):
        e = decay
        for _ in range(EXP_ULPS):
            e = math.nextafter(e, direction)
            spread = max(spread, _rel(sens(e), base))
    return spread


def _summarize(r):
    """(equivalent, result) derived only from a curve_check report's own numbers."""
    tol = r["tolerance_relative"]
    e, outside, kern, pipe = (r["max_rel_error"], r["max_rel_error_outside_studio_rules"],
                              r["maccel_formula_max_rel_error"], r["pipeline_max_rel_error"])
    unverified = r["unverified_samples"]
    equivalent = e <= tol and kern <= tol and pipe <= tol and unverified == 0
    if equivalent:
        return True, (f"EQUIVALENT: every sample within {tol:g} relative (Studio preview, maccel formula and "
                      "motion pipeline)")
    parts = []
    if e > tol:
        where = f"max {e:.3g} at {r['worst']['speed']:.12g} counts/ms"
        if outside <= tol and r["studio_rule_samples"]:
            where += (" (only inside the Studio preview's sync-speed shortcut, which returns exactly 1x within "
                      f"1e-9 counts/ms of SYNC_SPEED; elsewhere max {outside:.3g})")
        parts.append(f"Studio preview {where}")
    if kern > tol:
        parts.append(f"maccel formula max {kern:.3g}")
    if pipe > tol:
        parts.append(f"motion pipeline max {pipe:.3g}")
    if unverified:
        parts.append(f"unverifiable at {unverified} of {r['samples']} speeds up to {r['unverified_below']:.3g} "
                     "counts/ms: the natural formula cancels there, and an exp() result 2 ulps off moves the "
                     f"sensitivity by up to {r['max_fp_spread']:.3g}, so the Studio preview (JavaScript) and Raw "
                     "Accel (C++) can disagree beyond tolerance although they compute the same expression")
    head = "NOT EQUIVALENT" if (e > tol or kern > tol or pipe > tol) else "NOT VERIFIED"
    return False, f"{head} within {tol:g}: " + "; ".join(parts)


def _in_studio_rule(x, cfg):
    """True if the Studio preview evaluates speed x with a rule upstream does not have (STUDIO_RULES)."""
    if cfg["MODE"] != "synchronous":
        return False
    smooth = cfg["SMOOTH"]
    sharp = 16.0 if smooth == 0 else 0.5 / smooth
    return x < STUDIO_SPEED_FLOOR or (sharp < 16 and abs(x - cfg["SYNC_SPEED"]) < STUDIO_SYNC_WINDOW)


def _finite(values, what, x):
    if not all(isinstance(v, float) and math.isfinite(v) for v in values):
        raise RawAccelExportError(f"{what} is not finite at speed {x:g} counts/ms")
    return values


def curve_check(cfg, speeds=None, tolerance=DEFAULT_TOLERANCE, settings=None):
    """Measure how closely Raw Accel's formula on the exported settings follows maccel.

    Three comparisons, all as relative error |a - b| / max(|a|, |b|):
      * Studio preview: app_sens (port of index.html maccelEval) vs
        rawaccel_profile_sens, X and Y, at every speed. This is `max_rel_error`.
      * maccel formula: the same without the Studio-only rules (STUDIO_RULES),
        i.e. maccel 0.5.9's kernel expressions in doubles.
      * Motion pipeline: maccel_motion vs rawaccel_motion (rotation, DPI, output
        DPI, Y/X) on 24 directions x 7 speeds.
    Natural mode adds a conditioning check (natural_exp_spread): speeds where a
    2-ulp exp() difference moves the result beyond `tolerance` are counted in
    `unverified_samples`, because there the identical Python ports cannot stand
    in for two different runtimes.
    Speeds: sample_speeds() plus edge_speeds(cfg), or the given list (each within
    SPEED_MIN..SPEED_MAX). `equivalent` is true only if all three stay within
    `tolerance` at every sample and no sample is unverified; `result` says in
    words what failed and where (_summarize).
    """
    cfg = normalize_maccel(cfg)
    if settings is None:
        settings, _ = maccel_to_rawaccel(cfg)
    prof = settings["profiles"][0]
    if speeds is None:
        edges = edge_speeds(cfg)
        speeds = sorted(set(sample_speeds()) | set(edges))
    else:
        speeds, edges = list(speeds), []
        if not speeds or any(not _is_number(x) or not SPEED_MIN <= x <= SPEED_MAX for x in speeds):
            raise RawAccelExportError(f"speeds must be a non-empty list of numbers from {SPEED_MIN:g} to "
                                      f"{SPEED_MAX:g} counts/ms")
        speeds = [float(x) for x in speeds]
    worst = {"rel_error": 0.0, "speed": speeds[0], "axis": "x", "app": None, "rawaccel": None}
    kernel_worst = outside_worst = spread_worst = 0.0
    rule_samples = unverified = 0
    unverified_below = 0.0
    natural = prof[K_ARGS_X]["mode"] == "natural"
    try:
        for x in speeds:
            ra = _finite(rawaccel_profile_sens(x, prof), "Raw Accel sensitivity", x)
            app = _finite(app_sens(x, cfg), "Studio preview sensitivity", x)
            kern = _finite(app_sens(x, cfg, studio_rules=False), "maccel sensitivity", x)
            in_rule = _in_studio_rule(x, cfg)
            rule_samples += in_rule
            if natural:
                sp = natural_exp_spread(x, prof[K_ARGS_X])
                spread_worst = max(spread_worst, sp)
                if sp > tolerance:
                    unverified += 1
                    unverified_below = max(unverified_below, x)
            for i, axis in enumerate("xy"):
                e = _rel(app[i], ra[i])
                if e > worst["rel_error"] or worst["app"] is None:
                    worst = {"rel_error": e, "speed": x, "axis": axis, "app": app[i], "rawaccel": ra[i]}
                if not in_rule:
                    outside_worst = max(outside_worst, e)
                kernel_worst = max(kernel_worst, _rel(kern[i], ra[i]))
        pipe_worst = 0.0
        dpi = cfg["INPUT_DPI"]
        for i in range(24):
            ang = 2 * math.pi * i / 24
            for counts_per_ms in (0.01, 0.3, 1, 3, 8, 20, 60):
                raw = counts_per_ms * dpi / NORMALIZED_DPI
                dx, dy = raw * math.cos(ang), raw * math.sin(ang)
                m = _finite(maccel_motion(dx, dy, 1.0, cfg), "maccel motion", counts_per_ms)
                r = _finite(rawaccel_motion(dx, dy, 1.0, settings), "Raw Accel motion", counts_per_ms)
                for a, b in zip(m, r):
                    if abs(a) > 1e-12 or abs(b) > 1e-12:
                        pipe_worst = max(pipe_worst, _rel(a, b))
    except (OverflowError, ValueError, ZeroDivisionError) as e:
        if isinstance(e, RawAccelExportError):
            raise
        raise RawAccelExportError(f"curve evaluation failed: {e}") from None

    report = {
        "compared": "Raw Accel v1.7.1 formula on the exported settings vs (1) the Studio preview curve "
                    "(index.html maccelEval) and (2) maccel 0.5.9's formula, at the sampled speeds; "
                    "(3) maccel vs Raw Accel motion pipeline on 168 vectors",
        "mode": cfg["MODE"], "rawaccel_mode": prof[K_ARGS_X]["mode"], "gain": prof[K_ARGS_X][K_GAIN],
        "tolerance_relative": tolerance, "samples": len(speeds), "edge_samples": len(edges),
        "speed_range": [min(speeds), max(speeds)],
        "max_rel_error": worst["rel_error"], "worst": worst,
        "studio_rule_samples": rule_samples,
        "max_rel_error_outside_studio_rules": outside_worst,
        "maccel_formula_max_rel_error": kernel_worst,
        "pipeline_max_rel_error": pipe_worst,
        "unverified_samples": unverified, "unverified_below": unverified_below, "max_fp_spread": spread_worst,
        "equivalent": None, "result": None,
    }
    report["equivalent"], report["result"] = _summarize(report)
    return report


# ----------------------------------------------------------------------------- versioned document

DOC_REQUIRED = ("format", "schema_version", "rawaccel_version", "maccel", "mapping", "rawaccel_settings")
DOC_KEYS = set(DOC_REQUIRED) | {"curve_check"}  # curve_check is omitted by export --no-check
CHECK_ERROR_FIELDS = ("max_rel_error", "max_rel_error_outside_studio_rules", "maccel_formula_max_rel_error",
                      "pipeline_max_rel_error", "max_fp_spread", "unverified_below")


def _close(got, want):
    """Equal up to libm rounding differences between platforms."""
    return abs(got - want) <= 1e-12 + 1e-3 * max(abs(got), abs(want))


def _check_report(got, want):
    """Validate an imported curve_check against the one recomputed here.

    Exact: everything that does not depend on floating-point rounding (labels,
    tolerance, sample counts, speed range). Error metrics: non-negative, finite
    and close to the recomputed value (_close). `worst`: right types, a sampled
    speed and the reported maximum. `equivalent` and `result` must follow from the
    report's own numbers (_summarize), so no report can claim a result its
    numbers do not support.
    """
    if not (isinstance(got, dict) and got.keys() == want.keys()):
        return False
    for k, w in want.items():
        g = got[k]
        if k in CHECK_ERROR_FIELDS:
            if not (isinstance(g, float) and math.isfinite(g) and g >= 0 and _close(g, w)):
                return False
        elif k == "worst":
            if not (_same_shape(g, w) and g["axis"] in ("x", "y") and g["rel_error"] == got["max_rel_error"]
                    and got["speed_range"][0] <= g["speed"] <= got["speed_range"][1]):
                return False
        elif k not in ("equivalent", "result") and not _strict_equal(g, w):
            return False
    if (got["unverified_samples"] == 0) != (got["max_fp_spread"] <= got["tolerance_relative"]):
        return False
    return (got["equivalent"], got["result"]) == _summarize(got)


def export_document(cfg, include_check=True):
    """Versioned, deterministic mousetool export (no timestamps, so it round-trips byte for byte).

    `maccel` holds the source parameters (authoritative for import);
    `rawaccel_settings` is the DriverConfig to hand to writer.exe on Windows;
    `mapping` and `curve_check` are derived reports.
    """
    cfg = normalize_maccel(cfg)
    settings, mapping = maccel_to_rawaccel(cfg)
    doc = {
        "format": EXPORT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "rawaccel_version": RAWACCEL_TARGET,
        "maccel": cfg,
        "mapping": mapping,
    }
    if include_check:
        doc["curve_check"] = curve_check(cfg, settings=settings)
    doc["rawaccel_settings"] = settings
    return doc


def _reject_constant(name):
    raise RawAccelExportError(f"invalid JSON number {name}")


def _parse_float(text):
    v = float(text)
    if not math.isfinite(v):
        raise RawAccelExportError(f"JSON number {_short(text, 40)} is out of range for a double")
    return v


def _unique_object(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise RawAccelExportError(f"duplicate JSON key {_short(k, 40)}")
        obj[k] = v
    return obj


def _check_depth(obj):
    stack = [(obj, 1)]
    while stack:
        v, d = stack.pop()
        if d > MAX_JSON_DEPTH:
            raise RawAccelExportError(f"JSON nested deeper than {MAX_JSON_DEPTH} levels")
        if isinstance(v, dict):
            stack.extend((c, d + 1) for c in v.values())
        elif isinstance(v, list):
            stack.extend((c, d + 1) for c in v)
    return obj


def loads(data):
    """Strict, bounded JSON parsing; every failure is a short RawAccelExportError.

    Accepts str or UTF-8 bytes up to MAX_INPUT_BYTES. Rejects invalid UTF-8,
    NaN/Infinity, numbers that overflow a double (1e500), duplicate keys,
    nesting deeper than MAX_JSON_DEPTH and integers Python refuses to parse (over
    4300 digits).
    """
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_INPUT_BYTES:
            raise RawAccelExportError(f"input is larger than {MAX_INPUT_BYTES} bytes")
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as e:
            raise RawAccelExportError(f"input is not valid UTF-8 (byte offset {e.start})") from None
    elif not isinstance(data, str):
        raise RawAccelExportError("expected JSON text")
    if len(data) > MAX_INPUT_BYTES:
        raise RawAccelExportError(f"input is larger than {MAX_INPUT_BYTES} bytes")
    try:
        obj = json.loads(data, parse_constant=_reject_constant, parse_float=_parse_float,
                         object_pairs_hook=_unique_object)
    except RawAccelExportError:
        raise
    except json.JSONDecodeError as e:
        raise RawAccelExportError(f"invalid JSON: {e.msg} at line {e.lineno} column {e.colno}") from None
    except (ValueError, RecursionError) as e:
        raise RawAccelExportError(f"invalid JSON: {str(e)[:100]}") from None
    return _check_depth(obj)


def dumps(obj):
    return json.dumps(obj, indent=2, allow_nan=False) + "\n"


def _strict_equal(a, b):
    """Deep equality that also requires identical JSON types (True != 1, 1000 != 1000.0)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def _same_shape(got, want):
    """Same keys and JSON types as `want`; values may differ (floats must still be finite)."""
    if isinstance(want, float):
        return isinstance(got, float) and math.isfinite(got)
    if type(got) is not type(want):
        return False
    if isinstance(want, dict):
        return got.keys() == want.keys() and all(_same_shape(got[k], want[k]) for k in want)
    if isinstance(want, list):
        return len(got) == len(want) and all(_same_shape(g, w) for g, w in zip(got, want))
    return True


def _import_export_document(obj):
    if obj.get("format") != EXPORT_FORMAT:
        raise RawAccelExportError(f"format must be {EXPORT_FORMAT!r} (got {_short(obj.get('format'))})")
    sv = obj.get("schema_version")
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise RawAccelExportError(f"schema_version must be an integer (got {_short(sv)})")
    if sv > SCHEMA_VERSION:
        raise RawAccelExportError(f"schema_version {_short(sv)} is newer than this mousetool reads ({SCHEMA_VERSION})")
    if sv < 1:
        raise RawAccelExportError(f"schema_version {_short(sv)} is not valid")
    unknown = sorted(str(k) for k in set(obj) - DOC_KEYS)
    if unknown:
        raise RawAccelExportError(f"unknown key(s) in export: {', '.join(_short(k, 40) for k in unknown[:10])}")
    missing = [k for k in DOC_REQUIRED if k not in obj]
    if missing:
        raise RawAccelExportError(f"export is missing {', '.join(repr(k) for k in missing)}")
    if obj["rawaccel_version"] != RAWACCEL_TARGET:
        raise RawAccelExportError(f"rawaccel_version must be {RAWACCEL_TARGET!r} (got {_short(obj['rawaccel_version'])})")
    cfg = normalize_maccel(obj["maccel"])
    settings, mapping = maccel_to_rawaccel(cfg)
    if not _strict_equal(obj["rawaccel_settings"], settings):
        raise RawAccelExportError("rawaccel_settings does not match the maccel parameters in this export "
                                  "(edited by hand?); re-export, or import the Raw Accel settings on their own")
    if not _strict_equal(obj["mapping"], mapping):
        raise RawAccelExportError("mapping report does not match the maccel parameters in this export; re-export")
    if "curve_check" in obj:
        if not _check_report(obj["curve_check"], curve_check(cfg, settings=settings)):
            raise RawAccelExportError("curve_check report is malformed or does not belong to these parameters; "
                                      "re-export")
    return cfg, {"source": "mousetool-export", "schema_version": sv, "lossy": list(mapping["lossy"]),
                 "unsupported": list(mapping["unsupported"]), "notes": []}


def import_document(data, profile_name=None):
    """Read a mousetool export, a bare Raw Accel settings.json, or a maccel config.

    `data` is JSON text (str or UTF-8 bytes, parsed with loads()) or an already
    parsed dict. `profile_name` selects a profile in bare Raw Accel settings and is
    an error for any other kind of document. Returns (cfg, report) where report["source"] is
    "mousetool-export", "rawaccel-settings", "maccel-state" or "maccel-config".
    Export documents are checked strictly: format, schema_version,
    rawaccel_version, all required members, no unknown keys, complete in-range
    parameters, and the embedded rawaccel_settings and mapping must equal (JSON
    types included) what these parameters map to; an optional curve_check must
    have the shape and fixed values this version generates.
    """
    obj = loads(data) if isinstance(data, (str, bytes, bytearray)) else _check_depth(data)
    if not isinstance(obj, dict):
        raise RawAccelExportError("expected a JSON object")
    # Classified once; mousetool's versioned markers take precedence over everything else.
    if "format" in obj or "schema_version" in obj:
        if profile_name is not None:
            raise RawAccelExportError("--profile only applies to bare Raw Accel settings, not a mousetool export")
        return _import_export_document(obj)
    if "profiles" in obj:
        cfg, rep = rawaccel_to_maccel(obj, profile_name)
        return cfg, {"source": "rawaccel-settings", **rep}
    if profile_name is not None:
        raise RawAccelExportError("--profile only applies to bare Raw Accel settings (a file with 'profiles')")
    if "params" in obj and "mode" in obj:  # mousetool.py maccel_state() / GET /api/maccel
        if not isinstance(obj["params"], dict):
            raise RawAccelExportError("maccel state 'params' must be an object")
        merged = {"MODE": obj["mode"], **obj["params"]}
        if obj.get("lut_points") is not None:
            merged["LUT_POINTS"] = obj["lut_points"]
        cfg = normalize_maccel(merged)
        return cfg, {"source": "maccel-state", "lossy": [], "notes": []}
    merged, defaulted = with_defaults(obj)
    cfg = normalize_maccel(merged)
    notes = [f"not in input, used app default: {', '.join(defaulted)}"] if defaulted else []
    return cfg, {"source": "maccel-config", "lossy": [], "notes": notes, "defaulted": defaulted}


# ----------------------------------------------------------------------------- CLI

def parse_set(items):
    """Parse KEY=VAL overrides (MODE stays a string, LUT_POINTS parses pairs, others become floats)."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise RawAccelExportError(f"expected KEY=VAL, got {_short(item)}")
        k, v = (s.strip() for s in item.split("=", 1))
        k = k.upper()
        if k == "MODE":
            out[k] = v
            continue
        if k == "LUT_POINTS":
            out[k] = parse_lut_points_arg(v)
            continue
        try:
            out[k] = float(v)
        except ValueError:
            raise RawAccelExportError(f"{_short(k, 40)} must be a number (got {_short(v)})") from None
    return out


def format_preview(cfg, settings, mapping, check):
    lines = [f"maccel {cfg['MODE']} -> Raw Accel {mapping['rawaccel_mode']} (Gain {'on' if mapping['gain'] else 'off'})",
             f"Formula: {mapping['formula']}", ""]
    w = max(len(r["param"]) for r in mapping["params"])
    for r in mapping["params"]:
        lines.append(f"  {r['param']:<{w}}  {r['value']:<10g} -> {r['field']} = {r['rawaccel_value']:g}  "
                     f"[{r['status']}]" + (f"  {r['note']}" if r.get("note") else ""))
    if cfg.get("MODE") == "lookup" and "LUT_POINTS" in cfg:
        pts = cfg["LUT_POINTS"]
        lines.append(f"  {'LUT_POINTS':<{w}}  {len(pts)} pts      -> argsX.data ({len(pts) * 2} floats)  [exact]")
    if mapping["lossy"]:
        lines += ["", "Lossy:"] + [f"  - {s}" for s in mapping["lossy"]]
    lines += ["", "Unsupported (not exported):"] + [f"  - {s}" for s in mapping["unsupported"]]
    lines += ["", "Notes:"] + [f"  - {s}" for s in mapping["notes"]]
    lines += ["", f"Curve check: {check['samples']} speeds ({check['edge_samples']} at formula edges) "
                  f"{check['speed_range'][0]:g}..{check['speed_range'][1]:g} counts/ms, "
                  f"tolerance {check['tolerance_relative']:g} relative",
              f"  max relative error vs Raw Accel: Studio preview {check['max_rel_error']:.3g}, "
              f"maccel formula {check['maccel_formula_max_rel_error']:.3g}, "
              f"motion pipeline {check['pipeline_max_rel_error']:.3g}",
              f"  {check['result']}", "",
              "  speed     app x     Raw Accel x  app y     Raw Accel y"]
    prof = settings["profiles"][0]
    for x in (0.5, 1, 2, 5, 10, 20, 40):
        a, r = app_sens(x, cfg), rawaccel_profile_sens(x, prof)
        lines.append(f"  {x:<8g}  {a[0]:<8.5f}  {r[0]:<11.5f}  {a[1]:<8.5f}  {r[1]:.5f}")
    return "\n".join(lines) + "\n"


def build_parser(prog="rawaccel_export.py"):
    ap = argparse.ArgumentParser(prog=prog, description="Offline Raw Accel v1.7.1 preview/export of maccel curves. "
                                 "Never applies anything: hand the exported settings to Raw Accel's writer.exe on Windows yourself.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("export", "write a versioned export (or bare Raw Accel settings with --settings-only)"),
                      ("preview", "show the parameter mapping and curve check")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--input", "-i", help="mousetool export, Raw Accel settings.json, /api/maccel state or "
                                              "{MODE: ..., PARAM: ...} JSON (default: app defaults)")
        p.add_argument("--live", action="store_true", help="start from the loaded maccel module (mousetool.py only)")
        p.add_argument("--set", nargs="+", metavar="KEY=VAL", help="override parameters, e.g. MODE=natural LIMIT=1.5")
        if name == "preview":
            p.add_argument("--json", action="store_true", help="JSON output")
        else:
            p.add_argument("--settings-only", action="store_true", help="write only the Raw Accel settings.json")
            p.add_argument("--no-check", action="store_true", help="omit the curve_check report")
            p.add_argument("--output", "-o", help="output file (default: stdout); never overwrites without --force")
            p.add_argument("--force", action="store_true", help="allow overwriting --output")
    imp = sub.add_parser("import", help="read an export or Raw Accel settings.json and print the maccel parameters")
    imp.add_argument("file")
    imp.add_argument("--profile", help="Raw Accel profile name when the settings hold several")
    return ap


def _read(path):
    """Read at most MAX_INPUT_BYTES (+1 to detect larger files) as bytes; loads() decodes and bounds them."""
    with open(path, "rb") as f:
        data = f.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise RawAccelExportError(f"{path} is larger than {MAX_INPUT_BYTES} bytes")
    return data


def _warn(prefix, messages):
    for m in messages:
        print(f"{prefix}: {m}", file=sys.stderr)


def resolve_config(args, live_loader=None):
    notes = []
    if args.live and args.input:
        raise RawAccelExportError("use either --live or --input, not both")
    if args.live:
        if live_loader is None:
            raise RawAccelExportError("--live needs the Linux helper: run `mousetool.py rawaccel ... --live`")
        cfg = normalize_maccel(live_loader())
    elif args.input:
        cfg, rep = import_document(_read(args.input))
        _warn("lossy (input)", rep.get("lossy", []))
        notes += rep.get("notes", [])
    else:
        cfg = dict(DEFAULTS)
        notes.append("no --input/--live given: parameters not set with --set use the app defaults")
    over = parse_set(args.set)
    merged = {**cfg, **over}
    if merged.get("MODE") == "lookup" and "LUT_POINTS" not in merged:
        merged["LUT_POINTS"] = default_lut_points()
        notes.append("MODE=lookup without LUT_POINTS: used default flat 1.0x table")
    return normalize_maccel(merged), notes


def main(argv=None, live_loader=None, stdout=None, prog="rawaccel_export.py"):
    """CLI entry point. Returns a process exit code (0 ok, 2 bad input, 1 internal error).

    Every export path (versioned or --settings-only, stdout or --output) prints
    the mapping's lossy and unsupported items and the curve check result on
    stderr, so a lossy mapping is never silent even when stdout is the JSON.
    """
    out = stdout or sys.stdout
    args = build_parser(prog).parse_args(argv)
    try:
        if args.cmd == "import":
            cfg, rep = import_document(_read(args.file), profile_name=args.profile)
            _warn("lossy", rep.get("lossy", []))
            out.write(dumps({"maccel": cfg, "import": rep}))
            return 0
        cfg, notes = resolve_config(args, live_loader)
        _warn("note", notes)
        if args.cmd == "preview":
            settings, mapping = maccel_to_rawaccel(cfg)
            check = curve_check(cfg, settings=settings)
            if args.json:
                out.write(dumps({"maccel": cfg, "mapping": mapping, "curve_check": check}))
            else:
                out.write(format_preview(cfg, settings, mapping, check))
            return 0
        doc = export_document(cfg, include_check=not args.no_check)
        text = dumps(doc["rawaccel_settings"] if args.settings_only else doc)
        if args.output:
            mode = "w" if args.force else "x"
            try:
                with open(args.output, mode, encoding="utf-8") as f:
                    f.write(text)
            except FileExistsError:
                raise RawAccelExportError(f"{args.output} exists; pass --force to overwrite") from None
        else:
            out.write(text)
            out.flush()
        _warn("lossy", doc["mapping"]["lossy"])
        _warn("unsupported", doc["mapping"]["unsupported"])
        if "curve_check" in doc:
            print(f"curve check: {doc['curve_check']['result']}", file=sys.stderr)
        else:
            print("curve check: skipped (--no-check)", file=sys.stderr)
        if args.output:
            print(f"wrote {args.output}", file=sys.stderr)
        return 0
    except RawAccelExportError as e:
        for msg in e.errors[:20]:
            print(f"error: {msg}", file=sys.stderr)
        if len(e.errors) > 20:
            print(f"error: ... and {len(e.errors) - 20} more", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # never a traceback for bad input; this would be a bug
        print(f"error: internal error ({type(e).__name__}: {str(e)[:200]}); please report it", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
