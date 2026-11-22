"""Temporary XDG trees and captured-format command responses only."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mousetool import desktop_pointer as pointer


class DesktopFixtures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.dict(os.environ, dict(HOME=tmp.name, XDG_CONFIG_HOME=tmp.name,
                                                     XDG_DATA_DIRS=tmp.name, XDG_DATA_HOME=tmp.name), clear=True))
        self.real_command = pointer.command
        self.cmd = self.enterContext(patch.object(pointer, "command", side_effect=AssertionError("unexpected command")))

    def select(self, desktop):
        os.environ["XDG_CURRENT_DESKTOP"] = desktop

    def exercise(self):
        for key, values in (("accel_profile", ["flat", "adaptive"]), ("accel_speed", [-1, 0.35, 1])):
            for value in values:
                with self.subTest(key=key, value=value):
                    state = pointer.desktop_state({key: value})
                    self.assertTrue(state["writable"])
                    self.assertEqual(state[key], value)

    def test_detection_override_and_absent_session(self):
        self.cmd.side_effect = OSError("no user manager")
        for name, expected in (("", None), ("ubuntu:GNOME", "gnome"), ("KDE", "plasma"), ("XFCE", "xfce")):
            self.select(name)
            self.assertEqual(pointer.selection() or None, expected)
        self.select("")
        for desktop in ("", "xfce"):
            self.select(desktop)
            message = pointer.desktop_state()["message"]
            self.assertIn("Desktop not supported" if desktop else "Desktop not detected", message)
            for name in (*pointer.BACKENDS, "MOUSETOOL_DESKTOP"):
                self.assertIn(name, message)
        self.select("")
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "fixture"
        self.assertEqual(pointer.selection(), "hyprland")
        os.environ["MOUSETOOL_DESKTOP"] = "COSMIC"
        self.assertEqual(pointer.selection(), "cosmic")

    def test_late_niri_environment_import(self):
        self.cmd.side_effect = ["LANG=C", "LANG=C\nNIRI_SOCKET=/run/fixture/niri"]
        path = self.root / "niri/config.kdl"
        path.parent.mkdir()
        path.write_text('input {\n mouse {\n  accel-profile "flat"\n }\n}\n')
        self.assertIsNone(pointer.desktop_state()["backend"])
        state = pointer.desktop_state()
        self.assertEqual((state["backend"], state["accel_profile"], state["writable"]), ("niri", "flat", True))
        self.assertEqual(self.cmd.call_count, 2)
        self.cmd.assert_called_with("systemctl", "--user", "show-environment")

    @unittest.skipUnless(shutil.which("busctl"), "requires busctl for the command-line parser test")
    def test_plasma_negative_speed_real_busctl(self):
        os.environ.update(LC_ALL="C", DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.root}/absent-bus")
        plasma = pointer.Plasma()
        plasma.path = "/org/kde/KWin/InputDevice/event4"
        self.cmd.side_effect = self.real_command
        for speed in (-1, -0.5):
            with self.subTest(speed=speed):
                # The real parser must reach the deliberately absent bus.
                with self.assertRaisesRegex(OSError, "(?i)connect.*bus"):
                    plasma.write({"accel_speed": speed})
                argv = self.cmd.call_args.args
                self.assertLess(argv.index("--"), argv.index(str(speed)))

    def test_missing_desktop_tools(self):
        self.cmd.side_effect = self.real_command
        os.environ["PATH"] = str(self.root)
        for desktop, tool in (("plasma", "busctl"), ("hyprland", "hyprctl")):
            self.select(desktop)
            state = pointer.desktop_state()
            self.assertFalse(state["writable"])
            self.assertEqual(state["message"], f"{tool} is not installed")

    def test_invalid_writes_do_not_call_backends(self):
        for backend in pointer.BACKENDS:
            self.select(backend)
            for bad in [1.01, -1.01, True, "0", None, float("nan"), float("inf")]:
                with self.subTest(backend=backend, bad=bad), self.assertRaises(ValueError):
                    pointer.desktop_state({"accel_speed": bad})
            with self.assertRaises(ValueError):
                pointer.desktop_state({"accel_profile": "other"})
        self.cmd.assert_not_called()

    def test_unavailable_commands_and_missing_niri(self):
        self.cmd.side_effect = OSError("session unavailable")
        for name in ("GNOME", "Plasma", "Hyprland", "niri"):
            self.select(name)
            state = pointer.desktop_state()
            self.assertFalse(state["writable"])
            self.assertIsNone(state["accel_speed"])
            with self.assertRaises(PermissionError):
                pointer.desktop_state({"accel_profile": "flat"})

    def test_niri(self):
        self.select("niri")
        path = self.root / "niri/config.kdl"
        path.parent.mkdir()
        text = 'input {\n    mouse {\n        accel-speed 0 // retain\n        accel-profile "adaptive"\n    }\n}\n'
        path.write_text(text)
        self.exercise()
        self.assertEqual(path.read_text(), text.replace("speed 0", "speed 1"))
        path.write_bytes(b'mouse { // } ignored\r\n  accel-speed   +2.5e-2 // units\r\n}\r\n')
        self.assertEqual(pointer.desktop_state()["accel_speed"], 0.025)
        pointer.desktop_state({"accel_speed": 0.1236})
        self.assertIn(b"speed   0.124 // units\r\n", path.read_bytes())
        path.write_text("// mouse block is missing\n")
        self.assertFalse(pointer.desktop_state()["writable"])

    def test_cosmic(self):
        self.select("COSMIC")
        path = self.root / "cosmic/com.system76.CosmicComp/v1/input_default"
        path.parent.mkdir(parents=True)
        text = ('(\r\n  state: Enabled, // retain )\r\n  map_to_output: Some("a(b)c"),\r\n'
                '  acceleration: Some((speed: 0.0, profile: Some( Adaptive ),)),\r\n)')
        path.write_bytes(text.encode())
        self.exercise()
        self.assertEqual(path.read_bytes(), text.replace("speed: 0.0", "speed: 1.0").encode())
        path.write_text("(malformed")
        with self.assertRaises(PermissionError):
            pointer.desktop_state({"accel_speed": 0})
        self.assertEqual(path.read_text(), "(malformed")

    def test_cosmic_defaults_and_missing_user_file(self):
        self.select("COSMIC")
        self.assertEqual(pointer.desktop_state()["accel_profile"], "default")
        rel = Path("cosmic/com.system76.CosmicComp/v1/input_default")
        system = self.root / "system" / rel
        os.environ["XDG_DATA_DIRS"] = str(self.root / "system")
        system.parent.mkdir(parents=True)
        base = "// defaults\n(state: Enabled, left_handed: Some(true))"
        system.write_text(base)
        self.assertEqual(pointer.desktop_state({"accel_speed": -0.5})["accel_profile"], "default")
        self.assertEqual(system.read_text(), base)
        self.assertIn("left_handed: Some(true)", (self.root / rel).read_text())
        self.exercise()

    def test_gnome(self):
        self.select("ubuntu:GNOME")
        schema = "org.gnome.desktop.peripherals.mouse"
        keys = {"speed": "0.0", "accel-profile": "'default'"}
        locked = False

        def run(binary, *args):
            if binary == "busctl":
                self.assertEqual(args[-2:], ("s", "org.gnome.Shell"))
                return "b true"
            self.assertEqual(binary, "gsettings")
            action, actual, key, *value = args
            self.assertEqual(actual, schema)
            if action == "get":
                return keys[key]
            if action == "writable":
                return "false" if locked else "true"
            self.assertEqual(action, "set")
            keys[key], = value
            return ""

        self.cmd.side_effect = run
        self.assertEqual(pointer.desktop_state()["accel_profile"], "default")
        self.exercise()
        def reject_profile(binary, *args):
            if args[:3] == ("set", schema, "accel-profile"):
                raise OSError("write rejected")
            return run(binary, *args)
        self.cmd.side_effect = reject_profile
        with self.assertRaisesRegex(PermissionError, "write rejected"):
            pointer.desktop_state({"accel_profile": "flat", "accel_speed": -0.2})
        self.assertEqual(float(keys["speed"]), 1.0)
        locked = True
        self.assertFalse(pointer.desktop_state()["writable"])
        with self.assertRaises(PermissionError):
            pointer.desktop_state({"accel_speed": 0})
        self.cmd.side_effect = lambda *args: "b false"
        self.assertIn("not running", pointer.desktop_state()["message"])

    def test_hyprland_modern_and_legacy(self):
        self.select("Hyprland")
        for modern in (False, True):
            values = {"accel_profile": "[[EMPTY]]", "sensitivity": 0.0}

            def run(binary, *args):
                self.assertEqual(binary, "hyprctl")
                if args[:2] == ("-j", "getoption"):
                    key = args[2].removeprefix("input." if modern else "input:")
                    if key not in values:
                        return "no such option"
                    return json.dumps({"option": args[2], "str" if key == "accel_profile" else "float": values[key], "set": True})
                if modern:
                    self.assertEqual(args[0], "eval")
                    self.assertTrue(args[1].startswith('hl.config({ ["input.'))
                    key, val = args[1][len('hl.config({ ["input.'):-len(" })")].split('"] = ')
                    values[key] = json.loads(val)
                else:
                    action, key, val = args
                    self.assertEqual(action, "keyword")
                    key = key.removeprefix("input:")
                    values[key] = float(val) if key == "sensitivity" else val
                return "ok"

            self.cmd.side_effect = run
            self.assertEqual(pointer.desktop_state()["accel_profile"], "default")
            self.exercise()

    def test_plasma(self):
        self.select("KDE")
        path = "/org/kde/KWin/InputDevice/event4"
        props = dict(name="Fixture mouse", pointer=True, touch=False, touchpad=False, tabletTool=False, tabletPad=False,
                     supportsPointerAcceleration=True, supportsPointerAccelerationProfileFlat=True,
                     supportsPointerAccelerationProfileAdaptive=True, pointerAcceleration=0.5,
                     pointerAccelerationProfileFlat=False, pointerAccelerationProfileAdaptive=True)
        paths = [path, path.replace("4", "5")]

        def run(binary, *args):
            self.assertEqual(binary, "busctl")
            if "get-property" in args:
                self.assertEqual(args[-2:], ("org.kde.KWin.InputDeviceManager", "devicesSysNames"))
                return json.dumps({"type": "as", "data": [path.rsplit("/", 1)[1] for path in paths]})
            if "GetAll" in args:
                other = {**props, "touchpad": True} if args[4].endswith("5") else props
                return json.dumps({"type": "a{sv}", "data": [{k: {"type": "b" if type(v) is bool else "s" if type(v) is str else "d",
                                                                    "data": v} for k, v in other.items()}]})
            self.assertEqual(args[:6], ("--user", "set-property", "--", "org.kde.KWin", path, "org.kde.KWin.InputDevice"))
            key, typ, value = args[-3:]
            if typ == "b":
                self.assertEqual(value, "true")
                for prof in ("Flat", "Adaptive"):
                    prop = "pointerAccelerationProfile" + prof
                    props[prop] = prop == key
            else:
                self.assertEqual((key, typ), ("pointerAcceleration", "d"))
                props[key] = float(value)
            return ""

        self.cmd.side_effect = run
        self.assertEqual(pointer.desktop_state()["accel_speed"], 0.5)
        self.exercise()
        paths.append(path.replace("4", "6"))
        self.assertFalse(pointer.desktop_state()["writable"])
        os.environ["MOUSETOOL_PLASMA_DEVICE"] = "event4"
        self.assertTrue(pointer.desktop_state()["writable"])
        props["supportsPointerAcceleration"] = False
        self.assertFalse(pointer.desktop_state()["writable"])

    def test_file_read_denied(self):
        for backend in ("COSMIC", "niri"):
            self.select(backend)
            with patch.object(Path, "exists", return_value=True), patch.object(Path, "read_text", side_effect=PermissionError), \
                 patch.object(Path, "read_bytes", side_effect=PermissionError):
                self.assertFalse(pointer.desktop_state()["writable"])
