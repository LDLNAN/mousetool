"""Desktop pointer backends. See README for upstream schemas and session limits."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

LOCK = threading.RLock()
LABELS = {"gnome": "GNOME", "plasma": "Plasma", "hyprland": "Hyprland", "cosmic": "COSMIC", "niri": "niri"}
ALIASES = {**{k: k for k in LABELS}, "kde": "plasma", "plasmawayland": "plasma"}


def detect_desktop(env):
    session = ""
    for key in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        if env.get(key):
            session = env[key].lower()
            break
    detected = next((ALIASES[t] for t in session.split(":") if t in ALIASES), session)
    if not detected:
        for var, name in (("NIRI_SOCKET", "niri"), ("HYPRLAND_INSTANCE_SIGNATURE", "hyprland"),
                          ("KDE_FULL_SESSION", "plasma"), ("GNOME_DESKTOP_SESSION_ID", "gnome")):
            if env.get(var):
                detected = name
                break
    return detected


def selection():
    override = os.environ.get("MOUSETOOL_DESKTOP", "").lower()
    if override:
        return ALIASES.get(override, override)
    detected = detect_desktop(os.environ)
    if detected:
        return detected
    # A helper started before the compositor retains its pre-login environment.
    try:
        listing = command("systemctl", "--user", "show-environment")
    except (OSError, UnicodeError):
        return ""
    manager_env = dict(line.split("=", 1) for line in listing.splitlines() if "=" in line)
    return detect_desktop(manager_env)


def command(*args):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=3, check=False)
    except FileNotFoundError as e:
        raise OSError(f"{args[0]} is not installed") from e
    except subprocess.TimeoutExpired as e:
        raise OSError(f"{args[0]} timed out") from e
    if proc.returncode:
        raise OSError(proc.stderr.strip() or f"{args[0]} failed")
    return proc.stdout.strip()


def validate(updates):
    if not isinstance(updates, dict) or not updates or updates.keys() - {"accel_profile", "accel_speed"}:
        raise ValueError("Expected accel_profile or accel_speed")
    if updates.get("accel_profile", "flat") not in ("flat", "adaptive"):
        raise ValueError("Choose flat or adaptive")
    speed = updates.get("accel_speed", 0)
    if type(speed) not in (float, int) or not -1 <= speed <= 1:
        raise ValueError("Pointer speed must be between −1 and 1")


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


NIRI_CONFIG = os.path.expanduser("~/.config/niri/config.kdl")


_COMMENTS_OR_STRING = re.compile(r'//[^\r\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', re.S)
_NIRI_VALUES = {
    "accel_profile": re.compile(r'^[ \t]*accel-profile[ \t]+"(\w+)"', re.M),
    "accel_speed": re.compile(r"^[ \t]*accel-speed[ \t]+([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", re.M),
}


def mask_comments(text, hide_strings=False):
    def mask(m):
        if m[0].startswith('"'):
            return "#" * len(m[0]) if hide_strings else m[0]
        return re.sub(r"[^\r\n]", " ", m[0])
    return _COMMENTS_OR_STRING.sub(mask, text)


def niri_block(cfg):
    masked = mask_comments(cfg, hide_strings=True)
    mouse = re.search(r"^[ \t]*mouse[ \t]*\{", masked, re.M)
    if mouse:
        depth = 1
        for brace in re.finditer(r"[{}]", masked[mouse.end():]):
            depth += 1 if brace[0] == "{" else -1
            if depth == 0:
                return mouse.end(), mouse.end() + brace.start()
    return None


def niri_values(block):
    block = mask_comments(block)
    profile = _NIRI_VALUES["accel_profile"].search(block)
    speed = _NIRI_VALUES["accel_speed"].search(block)
    return dict(accel_profile=profile[1] if profile else "adaptive",
                accel_speed=float(speed[1]) if speed else 0.0)


def niri_state(path=NIRI_CONFIG):
    try:
        cfg = Path(path).read_text()
    except (OSError, UnicodeError):
        return None
    span = niri_block(cfg)
    return niri_values(cfg[slice(*span)] if span else "")


def niri_set(updates, path=NIRI_CONFIG):
    path = Path(path)
    cfg = path.read_bytes().decode()
    span = niri_block(cfg)
    if not span:
        raise ValueError("No mouse block in niri/config.kdl")
    block = cfg[slice(*span)]
    indent = re.search(r"\n([ \t]+)\S", block)
    newline = "\r\n" if "\r\n" in cfg else "\n"
    # Legacy /api/niri ignores unrelated keys and accepts an empty update.
    edits = {k: updates[k] for k in _NIRI_VALUES if k in updates}
    if edits:
        validate(edits)
    for key, value in edits.items():
        if key == "accel_speed":
            value = f"{value:.3f}".rstrip("0").rstrip(".")
            if value == "-0":
                value = "0"
        found = _NIRI_VALUES[key].search(mask_comments(block))
        if found:
            block = block[:found.start(1)] + value + block[found.end(1):]
        else:
            literal = f'"{value}"' if key == "accel_profile" else value
            block = newline + (indent[1] if indent else "        ") + key.replace("_", "-") + " " + literal + block
    atomic_config(path, cfg, cfg[:span[0]] + block + cfg[span[1]:])
    return niri_state(path)


class Niri:
    note = "Saved to niri/config.kdl"

    def __init__(self):
        self.path = config_home() / "niri/config.kdl"

    def read(self):
        cfg = self.path.read_text()
        span = niri_block(cfg)
        if not span:
            raise ValueError("No mouse block in niri/config.kdl")
        values = niri_values(cfg[slice(*span)])
        return values["accel_profile"], values["accel_speed"], file_writable(self.path)

    def write(self, updates):
        niri_set(updates, self.path)


class Gnome:
    schema = "org.gnome.desktop.peripherals.mouse"
    note = "Saved to GNOME mouse preferences"

    def read(self):
        if command("busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                   "org.freedesktop.DBus", "NameHasOwner", "s", "org.gnome.Shell") != "b true":
            raise OSError("GNOME Shell is not running")
        profile = command("gsettings", "get", self.schema, "accel-profile").strip("'")
        speed = float(command("gsettings", "get", self.schema, "speed"))
        writable = all(command("gsettings", "writable", self.schema, k) == "true" for k in ("accel-profile", "speed"))
        return profile, speed, writable

    def write(self, updates):
        for k, v in updates.items():
            command("gsettings", "set", self.schema, "speed" if k == "accel_speed" else "accel-profile", str(v))


class Hyprland:
    note = "Session only · resets when Hyprland reloads"

    def read(self):
        try:
            output = command("hyprctl", "-j", "getoption", "input.accel_profile")
            profile = json.loads(output)["str"]
            self.modern = True
        except (OSError, ValueError, KeyError):
            profile = json.loads(command("hyprctl", "-j", "getoption", "input:accel_profile"))["str"]
            self.modern = False
        key = "input.sensitivity" if self.modern else "input:sensitivity"
        speed = json.loads(command("hyprctl", "-j", "getoption", key))["float"]
        return profile if profile not in ("", "[[EMPTY]]") else "default", speed, True

    def write(self, updates):
        for key, v in updates.items():
            name = "sensitivity" if key == "accel_speed" else "accel_profile"
            if self.modern:
                result = command("hyprctl", "eval", f'hl.config({{ ["input.{name}"] = {json.dumps(v)} }})')
            else:
                result = command("hyprctl", "keyword", "input:" + name, str(v))
            if result != "ok":
                raise OSError(result or "Hyprland rejected this setting")


class Plasma:
    service = "org.kde.KWin"
    interface = "org.kde.KWin.InputDevice"
    note = "KWin mouse preferences"

    def read(self):
        base = "/org/kde/KWin/InputDevice"
        listing = command("busctl", "--user", "--json=short", "get-property", self.service, base,
                          "org.kde.KWin.InputDeviceManager", "devicesSysNames")
        event = os.environ.get("MOUSETOOL_PLASMA_DEVICE")
        devices = []
        for sysname in json.loads(listing)["data"]:
            if not re.fullmatch(r"event\d+", sysname) or (event and event != sysname):
                continue
            path = f"{base}/{sysname}"
            raw = command("busctl", "--user", "--json=short", "call", self.service, path,
                          "org.freedesktop.DBus.Properties", "GetAll", "s", self.interface)
            props = {k: v["data"] for k, v in json.loads(raw)["data"][0].items()}
            if props["pointer"] and not any(props[k] for k in ("touchpad", "tabletTool", "tabletPad", "touch")):
                devices.append((path, props))
        if not devices:
            raise ValueError("No matching KWin mouse")
        if len(devices) != 1:
            raise ValueError("Multiple mice · select an event name with MOUSETOOL_PLASMA_DEVICE")
        self.path, d = devices[0]
        self.note = f"KWin · {d['name']} ({self.path.rsplit('/', 1)[1]})"
        profile = "flat" if d["pointerAccelerationProfileFlat"] else "adaptive" if d["pointerAccelerationProfileAdaptive"] else "default"
        writable = all(d[k] for k in ("supportsPointerAcceleration", "supportsPointerAccelerationProfileFlat",
                                      "supportsPointerAccelerationProfileAdaptive"))
        return profile, d["pointerAcceleration"], writable

    def write(self, updates):
        for key, value in updates.items():
            if key == "accel_speed":
                args = ("pointerAcceleration", "d", str(value))
            else:
                args = ("pointerAccelerationProfile" + value.title(), "b", "true")
            command("busctl", "--user", "set-property", "--", self.service, self.path, self.interface, *args)


def file_writable(path):
    return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK | os.X_OK)


def ron_fields(masked, start, end):
    """Locate a RON struct's immediate field values without rewriting sibling fields."""
    struct = re.fullmatch(r"\s*\((.*)\)\s*", masked[start:end], re.S)
    if not struct:
        raise ValueError("Unsupported COSMIC configuration format")
    start += struct.start(1)
    fields = {}
    stack = []
    field_start = start
    for i, ch in enumerate(struct[1] + ",", start):
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[ch]:
                raise ValueError("Unbalanced COSMIC configuration")
        elif ch == "," and not stack:
            field = masked[field_start:i]
            if field.strip():
                m = re.fullmatch(r"\s*(\w+)\s*:\s*(\S(?:.*?\S)?)\s*", field, re.S)
                if not m or m[1] in fields:
                    raise ValueError("Unsupported COSMIC configuration field")
                fields[m[1]] = (field_start + m.start(2), field_start + m.end(2))
            field_start = i + 1
    if stack:
        raise ValueError("Unbalanced COSMIC configuration")
    return fields


# Accept cosmic-config's explicit Some(...) RON serialization. Mask comments
# and strings before locating fields, retaining offsets into the original text.
class Cosmic:
    note = "COSMIC mouse defaults · individual devices may override"

    def read(self):
        rel = Path("cosmic/com.system76.CosmicComp/v1/input_default")
        self.path = config_home() / rel
        self.local = self.path.exists()
        if self.local:
            self.text = self.path.read_bytes().decode()
        else:
            dirs = [os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share",
                    *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
            source = next((Path(d) / rel for d in dirs if (Path(d) / rel).is_file()), None)
            self.text = source.read_bytes().decode() if source else "(state: Enabled,)\n"
        masked = mask_comments(self.text, hide_strings=True)
        if "*/" in masked or "/*" in masked:
            raise ValueError("Unsupported COSMIC comment syntax")
        self.root_start = len(masked) - len(masked.lstrip())
        fields = ron_fields(masked, 0, len(masked))
        if "state" not in fields:
            raise ValueError("Unsupported COSMIC input configuration")
        self.entry = fields.get("acceleration")
        self.spans = {}
        profile, speed = "default", 0.0
        if self.entry and masked[slice(*self.entry)] != "None":
            a, b = self.entry
            wrap = re.fullmatch(r"Some\s*\(\s*(\(.*\))\s*\)", masked[a:b], re.S)
            if not wrap:
                raise ValueError("Unsupported COSMIC acceleration format")
            fields = ron_fields(masked, a + wrap.start(1), a + wrap.end(1))
            a, b = fields["profile"]
            value = masked[a:b]
            if value != "None":
                m = re.fullmatch(r"Some\s*\(\s*(Flat|Adaptive)\s*\)", value)
                if not m:
                    raise ValueError("Unsupported COSMIC acceleration profile")
                self.spans["accel_profile"] = (a + m.start(1), a + m.end(1))
                profile = m[1].lower()
            else:
                self.spans["accel_profile"] = (a, b)
            self.spans["accel_speed"] = fields["speed"]
            speed = float(masked[slice(*fields["speed"])])
        self.profile = profile
        parent = next(p for p in self.path.parents if p.exists())
        writable = file_writable(self.path) if self.local else os.access(parent, os.W_OK | os.X_OK)
        return profile, speed, writable

    def write(self, updates):
        text = self.text
        if self.spans:
            for key in sorted(updates, key=self.spans.get, reverse=True):
                a, b = self.spans[key]
                if key == "accel_profile":
                    profile = updates[key].title()
                    value = f"Some({profile})" if self.profile == "default" else profile
                else:
                    value = str(float(updates[key]))
                text = text[:a] + value + text[b:]
        else:
            profile = updates.get("accel_profile")
            profile = f"Some({profile.title()})" if profile else "None"
            speed = float(updates.get("accel_speed", 0))
            value = f"Some((profile: {profile}, speed: {speed}))"
            if self.entry:
                a, b = self.entry
            else:
                # Insert before the first field so the existing trailing comma is immaterial.
                a = b = self.root_start + 1
                value = "acceleration: " + value + ", "
            text = text[:a] + value + text[b:]
        atomic_config(self.path, self.text if self.local else None, text)


def atomic_config(path, original, text):
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".atomicwrite", delete=False) as tmp:
        try:
            if original is not None:
                os.fchmod(tmp.fileno(), target.stat().st_mode & 0o777)
            tmp.write(text.encode())
            tmp.flush()
            if original is None:
                os.link(tmp.name, target)  # Fail if another writer created it.
            elif target.read_bytes() == original.encode():
                os.replace(tmp.name, target)
            else:
                raise OSError("Desktop settings changed during edit")
        finally:
            Path(tmp.name).unlink(missing_ok=True)


BACKENDS = {"niri": Niri, "gnome": Gnome, "plasma": Plasma, "cosmic": Cosmic, "hyprland": Hyprland}


def desktop_state(updates=None):
    name = selection()
    state = dict(backend=name or None, label=LABELS.get(name, name or "Not detected"), writable=False,
                 accel_profile=None, accel_speed=None, message="")
    if updates is not None:
        validate(updates)
    try:
        if name not in LABELS:
            reason = "Desktop not supported" if name else "Desktop not detected"
            raise ValueError(f"{reason}. Set MOUSETOOL_DESKTOP to niri, gnome, plasma, hyprland or cosmic.")
        impl = BACKENDS[name]()
        with LOCK:
            profile, speed, writable = impl.read()
            validate({"accel_speed": speed})
            if updates is not None:
                if not writable:
                    raise ValueError("Desktop settings are read-only")
                if len(updates) == 2 and isinstance(impl, (Gnome, Hyprland, Plasma)):
                    # Command APIs apply individual properties. Speed is always
                    # restorable, including when the original profile is custom.
                    impl.write({"accel_speed": updates["accel_speed"]})
                    try:
                        impl.write({"accel_profile": updates["accel_profile"]})
                    except OSError:
                        impl.write({"accel_speed": speed})
                        raise
                else:
                    impl.write(updates)
                profile, speed, writable = impl.read()
                validate({"accel_speed": speed})
                for k, v in updates.items():
                    if (k == "accel_profile" and profile != v) or (k == "accel_speed" and abs(speed - v) > 0.00051):
                        raise OSError("Desktop did not apply the requested setting")
            state.update(accel_profile=profile, accel_speed=speed, writable=writable,
                         message=impl.note if writable else f"Read-only · {impl.note}")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
        if updates is not None:
            raise PermissionError(str(e)) from e
        state["message"] = str(e) or "Desktop settings are unavailable"
    return state
