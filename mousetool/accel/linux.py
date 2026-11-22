"""Linux kernel acceleration backend (mouseaccel, or legacy maccel)."""
import glob
import math
import os
import shutil
import subprocess

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


UDEV_RULES_DIRS = ("/etc/udev/rules.d", "/usr/lib/udev/rules.d", "/lib/udev/rules.d")
SYSTEMD_USER_UNIT = "~/.config/systemd/user/mousetool.service"


def _has_udev_rule(filename, udev_dirs=None):
    dirs = UDEV_RULES_DIRS if udev_dirs is None else udev_dirs
    return any(os.path.isfile(os.path.join(d, filename)) for d in dirs)


def _check_systemd_service_active():
    """Read-only check whether the user mousetool.service unit is active."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        res = subprocess.run(
            [systemctl, "--user", "is-active", "mousetool.service"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return None


def linux_setup_status(state=None, udev_dirs=None, service_unit_path=None, service_active=None):
    """Read-only Linux setup diagnostics for the Acceleration guided setup card.

    Never invokes sudo, modprobe, dkms, or any mutating command.
    """
    st = state if state is not None else maccel_state()
    b = st.get("backend") or {}
    mod_name = b.get("name") or b.get("candidate") or "mouseaccel"
    if mod_name not in ("mouseaccel", "maccel"):
        mod_name = "mouseaccel"
    mod_label = b.get("label") or ("maccel (legacy)" if mod_name == "maccel" else mod_name)

    loaded = bool(st.get("loaded"))
    installed = bool(st.get("installed"))
    params_ok = bool(st.get("parameters_available"))
    writable = bool(st.get("writable"))
    also_loaded = [n for n in (b.get("also_loaded") or []) if n in ("mouseaccel", "maccel")]
    group = st.get("group") or mod_name
    if group not in ("mouseaccel", "maccel"):
        group = "mouseaccel"
    in_group = bool(st.get("in_group"))

    mousetool_udev = _has_udev_rule("60-mousetool.rules", udev_dirs)
    if mod_name == "mouseaccel":
        accel_udev = _has_udev_rule("99-mouseaccel.rules", udev_dirs)
    else:
        accel_udev = _has_udev_rule("99-maccel.rules", udev_dirs) or bool(st.get("group_exists"))

    unit_path = os.path.expanduser(service_unit_path or SYSTEMD_USER_UNIT)
    service_installed = os.path.isfile(unit_path)
    svc_active = _check_systemd_service_active() if service_active is None else service_active

    steps = []

    # Step 1: Kernel acceleration driver
    if loaded and params_ok and not also_loaded:
        steps.append({
            "id": "driver",
            "title": f"{mod_label} driver",
            "status": "installed",
            "description": "Running and ready for custom sensitivity curves.",
            "command": None,
            "url": None,
            "url_label": None,
        })
    elif loaded and also_loaded:
        steps.append({
            "id": "driver",
            "title": "Unload conflicting maccel driver",
            "status": "needs_setup",
            "description": "Both mouseaccel and maccel are loaded, so changes are ignored. Unload maccel and reload mouseaccel:",
            "command": "sudo rmmod maccel && sudo modprobe -r mouseaccel && sudo modprobe mouseaccel",
            "url": None,
            "url_label": None,
        })
    elif loaded and not params_ok:
        steps.append({
            "id": "driver",
            "title": f"Restore {mod_name} read access",
            "status": "needs_setup",
            "description": "The driver is loaded, but its settings could not be read. Re-apply permissions and reload it:",
            "command": f"./install.sh --mouseaccel-perms && sudo modprobe -r {mod_name} && sudo modprobe {mod_name}",
            "url": None,
            "url_label": None,
        })
    elif installed:
        steps.append({
            "id": "driver",
            "title": f"Load the {mod_name} driver",
            "status": "needs_setup",
            "description": f"{mod_name} is installed on this PC, but not loaded yet. Load it with:",
            "command": f"sudo modprobe {mod_name}",
            "url": None,
            "url_label": None,
        })
    else:
        steps.append({
            "id": "driver",
            "title": "Install the mouseaccel driver",
            "status": "needs_setup",
            "description": "Install the Linux mouseaccel driver (recommended) or legacy maccel, then load it:",
            "command": "sudo modprobe mouseaccel",
            "url": "https://www.maccel.org",
            "url_label": "maccel guide (legacy)",
        })

    # Step 2: Driver write permissions & udev rules
    if loaded and writable:
        steps.append({
            "id": "permissions",
            "title": "Save permissions",
            "status": "installed",
            "description": "Mouse Tool can save acceleration changes directly.",
            "command": None,
            "url": None,
            "url_label": None,
        })
    elif accel_udev and not in_group:
        steps.append({
            "id": "permissions",
            "title": f"Join the {group} group",
            "status": "needs_setup",
            "description": "Allow Mouse Tool to save changes without root, then log out and back in:",
            "command": f'sudo usermod -aG {group} "$USER"',
            "url": None,
            "url_label": None,
        })
    elif mod_name == "maccel":
        steps.append({
            "id": "permissions",
            "title": "Join the maccel group",
            "status": "needs_setup",
            "description": "Allow Mouse Tool to save changes without root, then log out and back in:",
            "command": 'sudo usermod -aG maccel "$USER"',
            "url": None,
            "url_label": None,
        })
    else:
        steps.append({
            "id": "permissions",
            "title": "Allow saving without root",
            "status": "needs_setup",
            "description": "Install the mouseaccel permission rule and add your user to the group (then log out and back in):",
            "command": './install.sh --mouseaccel-perms && sudo usermod -aG mouseaccel "$USER"',
            "url": None,
            "url_label": None,
        })

    # Step 3: Mouse USB rules & background helper service
    if mousetool_udev and (svc_active or service_installed):
        steps.append({
            "id": "helper",
            "title": "Mouse USB access & helper service",
            "status": "installed",
            "description": "Browser mouse permissions and the background helper are set up.",
            "command": None,
            "url": None,
            "url_label": None,
        })
    elif not mousetool_udev:
        steps.append({
            "id": "helper",
            "title": "Enable browser mouse access",
            "status": "needs_setup",
            "description": "Run once to install USB permission rules and start the helper at login:",
            "command": "./install.sh --udev",
            "url": None,
            "url_label": None,
        })
    else:
        steps.append({
            "id": "helper",
            "title": "Start helper automatically at login",
            "status": "needs_setup",
            "description": "Optional: enable the background service so Mouse Tool is always ready:",
            "command": "./install.sh",
            "url": None,
            "url_label": None,
        })

    ready = bool(loaded and params_ok and writable and not also_loaded)
    if ready:
        summary = "Ready"
    elif loaded and also_loaded:
        summary = "Driver conflict detected"
    elif loaded and not params_ok:
        summary = "Driver needs permission repair"
    elif loaded and not writable:
        summary = "Needs save permission"
    elif installed:
        summary = "Driver not loaded"
    else:
        summary = "Needs a quick setup"

    return {
        "platform": "linux",
        "ready": ready,
        "summary": summary,
        "driver": {
            "name": mod_name,
            "label": mod_label,
            "installed": installed,
            "loaded": loaded,
            "writable": writable,
            "status": "installed" if (loaded and params_ok and not also_loaded) else "needs_setup",
        },
        "udev": {
            "mousetool_rules": mousetool_udev,
            "accel_rules": accel_udev,
        },
        "service": {
            "unit_installed": service_installed,
            "active": svc_active,
        },
        "steps": steps,
    }
