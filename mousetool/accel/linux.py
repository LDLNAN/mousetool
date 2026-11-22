"""Linux kernel acceleration backend (mouseaccel, or legacy maccel)."""
import glob
import math
import os

# mouseaccel is a maccel 0.5.9 fork with the same parameter names and value formats. It refuses to load next to
# maccel. mousetool prefers mouseaccel when its module directory exists and falls back to maccel (legacy).
SYS_MODULE = "/sys/module"         # where loaded modules appear
DEV_DIR = "/dev"                   # speed device lives at DEV_DIR/<module>
SRC_DIR = "/usr/src"               # DKMS sources: SRC_DIR/<module>-<version>
VAR_OPT = "/var/opt"               # reset scripts: VAR_OPT/<module>/resets
ACCEL_BACKENDS = (                 # detection order = preference
    {"name": "mouseaccel", "label": "mouseaccel", "legacy": False},
    {"name": "maccel", "label": "maccel (legacy)", "legacy": True},
)
SYS = None                         # override for the parameters directory (None: the detected module's)
RESETS = None                      # override for the reset-scripts directory (None: the detected module's)
FIX = 1 << 32                      # both modules are built with 64-bit fixed point on x86_64: value * 2^32
MODES = ["linear", "natural", "synchronous", "no_accel"]
PARAMS = {
    "SENS_MULT": (0.01, 50), "YX_RATIO": (0.01, 50), "INPUT_DPI": (50, 50000), "ANGLE_ROTATION": (-180, 180),
    "ACCEL": (0, 50), "OFFSET": (0, 1000), "OUTPUT_CAP": (0, 100),
    "DECAY_RATE": (0.0001, 100), "LIMIT": (1, 100),
    "GAMMA": (0.0001, 100), "SMOOTH": (0, 1), "MOTIVITY": (1.0001, 100), "SYNC_SPEED": (0.0001, 1000),
}


def _backend_info(b, sys_module, dev_dir, src_dir, var_opt):
    name = b["name"]
    return {
        "name": name, "label": b["label"], "legacy": b["legacy"],
        "module_path": os.path.join(sys_module, name),
        "params_path": os.path.join(sys_module, name, "parameters"),
        "speed_device": os.path.join(dev_dir, name),
        "resets_path": os.path.join(var_opt, name, "resets"),
        "group": name,
        "installed": os.path.isdir(src_dir) and bool(glob.glob(os.path.join(glob.escape(src_dir), f"{name}-*"))),
    }


def detect_accel_backend(sys_module=None, dev_dir=None, src_dir=None, var_opt=None):
    """Pick the kernel acceleration module mousetool talks to.

    mouseaccel if SYS_MODULE/mouseaccel exists, else maccel if SYS_MODULE/maccel exists, else none. Returns
    {"name", "label", "legacy", "loaded", "module_path", "params_path", "speed_device", "resets_path", "group",
    "installed", "installed_backends", "also_loaded"}. With nothing loaded, name/label/paths are None and
    "candidate" names the module the not-loaded hint should mention (an installed one, mouseaccel first).
    """
    sys_module = sys_module or SYS_MODULE
    dev_dir = dev_dir or DEV_DIR
    src_dir = src_dir or SRC_DIR
    var_opt = var_opt or VAR_OPT
    infos = [_backend_info(b, sys_module, dev_dir, src_dir, var_opt) for b in ACCEL_BACKENDS]
    loaded = [i for i in infos if os.path.isdir(i["module_path"])]
    installed = [i["name"] for i in infos if i["installed"]]
    if loaded:
        out = dict(loaded[0], loaded=True)
        # mouseaccel switches to pass-through if maccel loads after it; report it so the UI can warn.
        out["also_loaded"] = [i["name"] for i in loaded[1:]]
    else:
        cand = next((i for i in infos if i["installed"]), None)
        out = {"name": None, "label": None, "legacy": False, "loaded": False, "module_path": None,
               "params_path": None, "speed_device": None, "resets_path": None,
               "group": cand["group"] if cand else None, "installed": bool(cand),
               "candidate": cand["name"] if cand else None, "also_loaded": []}
    out["installed_backends"] = installed
    return out


def _params_dir(backend):
    return SYS if SYS is not None else backend.get("params_path")


def _resets_dir(backend):
    return RESETS if RESETS is not None else backend.get("resets_path")


def maccel_state(backend=None):
    """State of the detected kernel acceleration module (mouseaccel, or maccel legacy).

    The historical name is kept for the CLI, the /api/maccel route and rawaccel --live.
    """
    b = backend or detect_accel_backend()
    sys_dir = _params_dir(b)
    loaded = b["loaded"] if SYS is None else os.path.isdir(SYS)
    st = {"installed": b["installed"] or loaded, "parameters_available": False,
          "loaded": loaded, "writable": False, "params": {}, "mode": None,
          "in_group": False, "group_exists": False, "group": b.get("group"),
          "backend": {k: b.get(k) for k in ("name", "label", "legacy", "loaded", "params_path", "speed_device",
                                             "also_loaded", "installed_backends")}}
    if SYS is not None:
        st["backend"]["params_path"] = SYS
    if b.get("candidate"):
        st["backend"]["candidate"] = b["candidate"]
    if b.get("group"):
        try:
            import grp
            g = grp.getgrnam(b["group"])
            st["group_exists"] = True
            st["in_group"] = g.gr_gid in os.getgroups()
        except (KeyError, ImportError):
            pass
    if not st["loaded"]:
        return st
    for name in PARAMS:
        try:
            with open(f"{sys_dir}/{name}") as f:
                st["params"][name] = int(f.read().strip()) / FIX
        except (OSError, ValueError):
            pass
    try:
        with open(f"{sys_dir}/MODE") as f:
            m = int(f.read().strip())
        st["mode"] = MODES[m % len(MODES)]
    except (OSError, ValueError):
        pass
    st["parameters_available"] = st["mode"] is not None and st["params"].keys() == PARAMS.keys()
    st["writable"] = st["parameters_available"] and all(
        os.access(f"{sys_dir}/{name}", os.W_OK) for name in ("MODE", *PARAMS))
    return st


accel_state = maccel_state


def _write_param(name, raw, backend):
    sys_dir = _params_dir(backend)
    with open(f"{sys_dir}/{name}", "w") as f:
        f.write(str(raw))
    resets = _resets_dir(backend)
    if not resets:
        return
    try:
        with open(f"{resets}/set_last_{name}_value.sh", "w") as f:
            f.write(f"echo {raw} > {sys_dir}/{name};")
    except OSError:
        pass


def maccel_set(updates, expected_backend=None):
    b = detect_accel_backend()
    if expected_backend is not None and expected_backend != b["name"]:
        raise PermissionError("Acceleration backend changed. Review the current curve before saving.")
    st = maccel_state(b)
    label = b.get("label") or "kernel acceleration"
    if not st["loaded"]:
        raise PermissionError("no kernel acceleration module is loaded (mouseaccel, or maccel legacy)")
    if not st["parameters_available"]:
        raise PermissionError(f"{label} is loaded, but its curve parameters could not all be read")
    if not st["writable"]:
        grp_name = b.get("group") or "mouseaccel"
        raise PermissionError(f"no write access to {label} — add yourself to the {grp_name} group and log in again")
    todo = []
    for k, v in updates.items():
        if k == "MODE":
            if v not in MODES:
                raise ValueError(f"unknown mode {v!r}")
            todo.append(("MODE", MODES.index(v)))
            continue
        if k not in PARAMS:
            raise ValueError(f"unknown parameter {k!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{k} must be a number")
        lo, hi = PARAMS[k]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
        todo.append((k, int(round(v * FIX))))
    for k, raw in todo:
        _write_param(k, raw, b)
    return maccel_state(b)


accel_set = maccel_set


def maccel_live_config():
    """Read-only snapshot of the loaded module as {"MODE": ..., PARAM: value} (for rawaccel --live)."""
    st = maccel_state()
    if not st["loaded"]:
        raise PermissionError("the kernel acceleration module isn't loaded (neither mouseaccel nor maccel); "
                              "use --input FILE or --set KEY=VAL instead of --live")
    missing = [k for k in PARAMS if k not in st["params"]]
    if st["mode"] is None or missing:
        where = (st.get("backend") or {}).get("params_path") or SYS
        raise OSError(f"could not read MODE/{', '.join(missing) or 'params'} from {where}")
    return {"MODE": st["mode"], **st["params"]}
