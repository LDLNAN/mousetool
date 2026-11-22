import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from mousetool.server import Api
from mousetool.accel import rawaccel_export as curves
from mousetool.accel.windows import OFF, WindowsAccel, WindowsRoutes, restore_saved


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
        self.active = json.loads(path.read_text())

    def test_read_apply_reset(self):
        self.assertEqual(self.backend.state()["mode"], "no_accel")
        self.assertTrue(self.backend.state()["lut_supported"])
        state = self.backend.apply(self.linear)
        self.assertEqual(state["mode"], "linear")
        self.assertEqual(state["params"]["INPUT_DPI"], 3200)
        self.assertEqual(state["saved_state"], "Matches saved curve")
        self.assertEqual(self.backend.apply({"reset": True})["mode"], "no_accel")
        self.assertEqual(self.calls, ["pending.json"] * 2)

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
        path.write_text(json.dumps(expected))
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
                    ("/rawaccel/writer.exe", {}, 404),
                    ("/api/status", {"Host": "unrelated.test"}, 403),
                    ("/api/status", {"Origin": "null"}, 403),
                    ("/api/motion", {}, 501),
                    ("/api/desktop", {}, 501),
                ]:
                    with self.subTest(path=path, headers=headers):
                        conn = http.client.HTTPConnection(*server.server_address)
                        conn.request("GET", path, headers=headers)
                        self.assertEqual(conn.getresponse().status, status)
                        conn.close()
                for payload, headers, status in [
                    ('{"reset":true}', {}, 403),
                    ('{"reset":true}', {"X-Mouse-Tool": "1", "Origin": "https://foreign.test"}, 403),
                    ('{"reset":true,"reset":false}', {"X-Mouse-Tool": "1"}, 400),
                    (" " * 4097, {"X-Mouse-Tool": "1"}, 400),
                    (json.dumps(self.linear), {"X-Mouse-Tool": "1"}, 200),
                ]:
                    conn = http.client.HTTPConnection(*server.server_address)
                    conn.request("POST", "/api/accel", payload, headers)
                    self.assertEqual(conn.getresponse().status, status)
                    conn.close()
                self.assertEqual(self.calls, ["pending.json"])
            finally:
                server.shutdown()
                thread.join()
