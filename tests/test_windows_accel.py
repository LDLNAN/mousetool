import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mousetool.accel import rawaccel_export as curves
from mousetool.accel import windows as win_accel
from mousetool.accel.windows import OFF, WindowsAccel, WindowsRoutes, restore_saved
from mousetool.server import Api

REPO_ROOT = Path(__file__).resolve().parent.parent


class WindowsAccelTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.active = curves.maccel_to_rawaccel(OFF)[0]
        self.calls = []
        self.backend = WindowsAccel(folder.name, lambda: self.active, self.write, lambda: "missing")
        self.linear = dict(OFF, MODE="linear", ACCEL=.0425, INPUT_DPI=3200, YX_RATIO=1.25)

    def write(self, path):
        self.calls.append(path.name)
        self.active = json.loads(path.read_text(encoding="utf-8"))

    def test_read_apply_reset(self):
        self.assertEqual(self.backend.state()["mode"], "no_accel")
        self.assertTrue(self.backend.state()["lut_supported"])
        state = self.backend.apply(self.linear)
        self.assertEqual(state["mode"], "linear")
        self.assertEqual(state["params"]["INPUT_DPI"], 3200)
        self.assertEqual(state["saved_state"], "Matches saved curve")
        self.assertEqual(self.backend.apply({"reset": True})["mode"], "no_accel")
        self.assertEqual(self.calls, ["pending.json"] * 2)

    def test_all_supported_modes_roundtrip_and_restore_saved(self):
        cases = [
            ("linear", dict(OFF, MODE="linear", ACCEL=0.0425, OFFSET=1.0, OUTPUT_CAP=2.0, INPUT_DPI=3200, YX_RATIO=1.25)),
            ("natural", dict(OFF, MODE="natural", DECAY_RATE=0.1, OFFSET=2.0, LIMIT=1.5, INPUT_DPI=1600)),
            ("synchronous", dict(OFF, MODE="synchronous", GAMMA=1.0, SMOOTH=0.5, MOTIVITY=1.5, SYNC_SPEED=5.0, INPUT_DPI=1600)),
            ("no_accel", dict(OFF, MODE="no_accel", SENS_MULT=0.8, YX_RATIO=1.1, INPUT_DPI=800)),
            ("lookup", dict(OFF, MODE="lookup", INPUT_DPI=1600, YX_RATIO=1.1,
                            LUT_POINTS=[[0.0, 0.9], [5.0, 1.0], [15.0, 1.45], [30.0, 1.7]])),
        ]
        for expected_mode, cfg in cases:
            with self.subTest(mode=expected_mode):
                state = self.backend.apply(cfg)
                self.assertEqual(state["mode"], expected_mode)
                self.assertEqual(state["saved_state"], "Matches saved curve")
                expected = self.active
                self.active = curves.maccel_to_rawaccel(OFF)[0]
                restore_saved(self.backend.saved, self.backend.read, self.write)
                self.assertEqual(self.active, expected)
                self.assertEqual(self.backend.state()["mode"], expected_mode)

    def test_lookup_apply_readback_and_restore_saved_roundtrip(self):
        lut_pts = [[0.0, 0.9], [5.0, 1.0], [15.0, 1.45], [30.0, 1.7]]
        custom = dict(OFF, MODE="lookup", INPUT_DPI=1600, SENS_MULT=1.0, YX_RATIO=1.1, LUT_POINTS=lut_pts)
        state = self.backend.apply(custom)
        self.assertEqual(state["mode"], "lookup")
        self.assertTrue(state["lut_supported"])
        self.assertTrue(state["lut_writable"])
        self.assertEqual(state["lut_points"], lut_pts)
        self.assertEqual(state["saved_state"], "Matches saved curve")
        ax = self.active["profiles"][0][curves.K_ARGS_X]
        self.assertEqual(ax["mode"], "lut")
        self.assertFalse(ax[curves.K_GAIN])
        self.assertEqual(ax["data"], [0.0, 0.9, 5.0, 1.0, 15.0, 1.45, 30.0, 1.7])

        # Replay from saved settings.json restores identical LUT configuration
        expected = self.active
        self.active = curves.maccel_to_rawaccel(OFF)[0]
        restore_saved(self.backend.saved, self.backend.read, self.write)
        self.assertEqual(self.active, expected)
        self.assertEqual(self.backend.state()["lut_points"], lut_pts)

        # Switching back to linear after lookup still round-trips cleanly
        lin_state = self.backend.apply(self.linear)
        self.assertEqual(lin_state["mode"], "linear")
        self.assertIsNone(lin_state["lut_points"])

    def test_saved_state_transitions_and_per_device_rejection(self):
        self.assertEqual(self.backend.state()["saved_state"], "No saved curve")
        self.backend.apply(self.linear)
        self.assertEqual(self.backend.state()["saved_state"], "Matches saved curve")

        # Active config changed externally -> Differs from saved curve
        self.active = curves.maccel_to_rawaccel(OFF)[0]
        self.assertEqual(self.backend.state()["saved_state"], "Differs from saved curve")

        # Corrupted saved file -> Saved curve unreadable
        self.backend.saved.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(self.backend.state()["saved_state"], "Saved curve unreadable")

        # Per-device profiles are read-only and cannot be overwritten without reset
        self.active = curves.maccel_to_rawaccel(OFF)[0]
        self.active["devices"] = [{
            "name": "Mouse A",
            "profile": "mousetool",
            "id": "HID\\VID_1532&PID_00A5",
            "config": self.active["defaultDeviceConfig"],
        }]
        st = self.backend.state()
        self.assertFalse(st["writable"])
        self.assertIn("Per-device", st["message"])
        with self.assertRaisesRegex(ValueError, "Per-device"):
            self.backend.apply(self.linear)

    def test_lookup_64_points_fits_windows_http_post_limit(self):
        class Handler(WindowsRoutes, Api):
            pass

        pts64 = [[round(i * 0.5, 2), round(0.75 + (i / 63.0) * 1.25, 3)] for i in range(64)]
        payload = json.dumps(dict(OFF, MODE="lookup", LUT_POINTS=pts64))
        self.assertLessEqual(len(payload.encode()), 4096)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.acceleration = self.backend
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                conn = http.client.HTTPConnection(*server.server_address)
                conn.request("POST", "/api/accel?backend=rawaccel", payload, {"X-Mouse-Tool": "1"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()
                self.assertEqual(resp.status, 200, body)
                self.assertEqual(body["accel"]["mode"], "lookup")
                self.assertEqual(len(body["accel"]["lut_points"]), 64)
            finally:
                server.shutdown()
                thread.join()

    def test_restore_saved_checks_mapping_and_readback(self):
        path = self.backend.saved
        restore_saved(path, self.backend.read, self.write)
        self.assertEqual(self.calls, [])
        self.backend.apply(self.linear)
        expected = self.active
        self.active = curves.maccel_to_rawaccel(OFF)[0]
        with self.assertRaisesRegex(OSError, "readback"):
            restore_saved(path, self.backend.read, lambda unused: None)
        restore_saved(path, self.backend.read, self.write)
        self.assertEqual(self.active, expected)
        expected["profiles"][0]["name"] = "foreign"
        path.write_text(json.dumps(expected), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configuration"):
            restore_saved(path, self.backend.read, self.write)
        self.assertEqual(self.calls, ["pending.json", "settings.json"])

    def test_mismatch_preserves_saved(self):
        self.backend.apply({"reset": True})
        saved = self.backend.saved.read_bytes()
        self.backend.write = lambda path: None
        with self.assertRaisesRegex(OSError, "readback"):
            self.backend.apply(self.linear)
        self.assertEqual(self.backend.saved.read_bytes(), saved)
        self.assertFalse((Path(self.backend.folder) / "pending.json").exists())

    def test_rejects_unbounded_lossy_and_commands(self):
        for patch in ({"writer": "custom.exe"}, {"INPUT_DPI": 800.5}, {"ACCEL": 1e50}):
            with self.assertRaises(ValueError):
                self.backend.apply(dict(self.linear, **patch))
        self.assertEqual(self.calls, [])

    def test_missing_and_unsupported(self):
        self.active["profiles"] *= 2
        self.assertFalse(self.backend.state()["writable"])
        with self.assertRaisesRegex(ValueError, "profiles"):
            self.backend.apply(self.linear)
        self.backend.read = lambda: self.fail_read()
        self.assertEqual(self.backend.state()["driver"], "missing")

    def fail_read(self):
        raise OSError("readback unavailable")

    def test_driver_status_read_active_write_settings_and_lock(self):
        with mock.patch.dict(os.environ, {"SystemRoot": "C:\\Windows"}):
            with mock.patch.object(win_accel, "run", return_value=mock.Mock(returncode=1060, stdout=b"")):
                self.assertEqual(win_accel.driver_status(), "missing")
            with mock.patch.object(win_accel, "run", return_value=mock.Mock(returncode=0, stdout=b"STATE : 4  RUNNING")):
                self.assertEqual(win_accel.driver_status(), "running")
            with mock.patch.object(win_accel, "run", return_value=mock.Mock(returncode=0, stdout=b"STATE : 1  STOPPED")):
                self.assertEqual(win_accel.driver_status(), "unavailable")

            # read_active handles UTF-8 BOM output and non-zero exit codes
            bom_payload = ("\ufeff" + curves.dumps(self.active)).encode("utf-8")
            with mock.patch.object(win_accel, "run", return_value=mock.Mock(returncode=0, stdout=bom_payload, stderr=b"")):
                self.assertEqual(win_accel.read_active(), self.active)
            with mock.patch.object(win_accel, "run", return_value=mock.Mock(returncode=1, stdout=b"", stderr=b"access denied")):
                with self.assertRaisesRegex(OSError, "access denied"):
                    win_accel.read_active()

        # run() translates TimeoutExpired into OSError
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(["writer.exe"], 15)):
            with self.assertRaisesRegex(OSError, "writer.exe timed out"):
                win_accel.run(["writer.exe", "pending.json"])

        # write_settings invokes writer.exe and waits for Raw Accel delay
        with mock.patch.object(win_accel, "run") as m_run, mock.patch.object(win_accel.time, "sleep") as m_sleep:
            win_accel.write_settings(Path("pending.json"))
            m_run.assert_called_once_with([win_accel.APP / "rawaccel/writer.exe", Path("pending.json")])
            m_sleep.assert_called_once_with(2)

        # writer_lock creates lock file and releases cleanly on both Windows and Linux
        with tempfile.TemporaryDirectory() as lock_dir:
            lock_folder = Path(lock_dir) / "state"
            with win_accel.writer_lock(lock_folder):
                self.assertTrue((lock_folder / "writer.lock").is_file())
            with win_accel.writer_lock(lock_folder):
                self.assertTrue((lock_folder / "writer.lock").is_file())

    def test_windows_http_boundaries(self):
        class Handler(WindowsRoutes, Api):
            pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.acceleration = self.backend
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                for path, headers, status in [
                    ("/", {}, 200),
                    ("/api/status", {}, 200),
                    ("/api/accel", {}, 200),
                    ("/api/maccel", {}, 200),
                    ("/api/devices", {}, 200),
                    ("/rawaccel/writer.exe", {}, 404),
                    ("/api/does-not-exist", {}, 404),
                    ("/api/status", {"Host": "unrelated.test"}, 403),
                    ("/api/status", {"Origin": "null"}, 403),
                    ("/api/motion", {}, 501),
                    ("/api/motion/stream", {}, 501),
                    ("/api/desktop", {}, 501),
                    ("/api/battery", {}, 501),
                    ("/api/niri", {}, 501),
                ]:
                    with self.subTest(path=path, headers=headers):
                        conn = http.client.HTTPConnection(*server.server_address)
                        conn.request("GET", path, headers=headers)
                        self.assertEqual(conn.getresponse().status, status)
                        conn.close()

                conn = http.client.HTTPConnection(*server.server_address)
                conn.request("GET", "/api/devices")
                dev_body = json.loads(conn.getresponse().read())
                conn.close()
                self.assertEqual(dev_body["mice"], [])
                self.assertIn("WebHID", dev_body["message"])

                for path, payload, headers, status in [
                    ("/api/accel", '{"reset":true}', {}, 403),
                    ("/api/accel", '{"reset":true}', {"X-Mouse-Tool": "1", "Origin": "https://foreign.test"}, 403),
                    ("/api/desktop", '{"accel_profile":"flat"}', {"X-Mouse-Tool": "1"}, 501),
                    ("/api/accel?backend=mouseaccel", '{"reset":true}', {"X-Mouse-Tool": "1"}, 400),
                    ("/api/accel", '{"reset":true,"reset":false}', {"X-Mouse-Tool": "1"}, 400),
                    ("/api/accel", " " * 4097, {"X-Mouse-Tool": "1"}, 400),
                    ("/api/accel", json.dumps(self.linear), {"X-Mouse-Tool": "1"}, 200),
                ]:
                    with self.subTest(post_path=path, status=status):
                        conn = http.client.HTTPConnection(*server.server_address)
                        conn.request("POST", path, payload, headers)
                        self.assertEqual(conn.getresponse().status, status)
                        conn.close()
                self.assertEqual(self.calls, ["pending.json"])

                # Driver readback mismatch returns 409 Conflict
                self.backend.write = lambda p: None
                conn = http.client.HTTPConnection(*server.server_address)
                conn.request("POST", "/api/accel", json.dumps({"reset": True}), {"X-Mouse-Tool": "1"})
                resp = conn.getresponse()
                err_body = json.loads(resp.read())
                conn.close()
                self.assertEqual(resp.status, 409)
                self.assertIn("readback", err_body["error"])
            finally:
                server.shutdown()
                thread.join()


class WindowsScriptsContractTests(unittest.TestCase):
    PS1_FILES = (
        REPO_ROOT / "install.ps1",
        REPO_ROOT / "platform" / "windows" / "install.ps1",
        REPO_ROOT / "platform" / "windows" / "read-rawaccel.ps1",
    )

    def test_powershell_scripts_reference_existing_repo_layout(self):
        install_ps1 = (REPO_ROOT / "platform" / "windows" / "install.ps1").read_text(encoding="utf-8")
        read_ps1 = (REPO_ROOT / "platform" / "windows" / "read-rawaccel.ps1").read_text(encoding="utf-8")
        wrapper_ps1 = (REPO_ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn(r"platform\windows\install.ps1", wrapper_ps1)
        for rel in ("mousetool.py", "icon.svg", "install.ps1", "mousetool", "web", "devices"):
            self.assertTrue((REPO_ROOT / rel).exists(), f"missing repo entry {rel}")
            self.assertIn(f"'{rel}'", install_ps1)
        for binary in win_accel.RAWACCEL_BINARIES:
            self.assertIn(f"'{binary}'", install_ps1)
        self.assertIn("-m mousetool.accel.windows", install_ps1)
        self.assertIn("Unblock-File", install_ps1)
        self.assertIn("Remove-Item", install_ps1)
        self.assertIn("[DriverConfig]::GetActive().ToJSON()", read_ps1)
        self.assertEqual(win_accel._read_script_path(), REPO_ROOT / "platform" / "windows" / "read-rawaccel.ps1")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"), "requires Windows PowerShell")
    def test_powershell_scripts_parse_without_syntax_errors(self):
        ps_check = (
            "$ErrorActionPreference = 'Stop'; "
            "foreach ($p in ($env:MOUSETOOL_PS_PATHS | ConvertFrom-Json)) { "
            "  $tokens = $null; $errors = $null; "
            "  [void][System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors); "
            "  if ($errors -and $errors.Count -gt 0) { "
            "    $errors | ForEach-Object { Write-Error ($p + ': ' + $_.Message) }; exit 1 "
            "  } "
            "}"
        )
        env = dict(os.environ, MOUSETOOL_PS_PATHS=json.dumps([str(p) for p in self.PS1_FILES]))
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_check],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)


if __name__ == "__main__":
    unittest.main()
