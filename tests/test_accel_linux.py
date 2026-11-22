#!/usr/bin/env python3
"""Unit tests for Linux mouseaccel Custom LUT (MT-014) and legacy maccel fallback."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import mousetool
from mousetool.accel import linux as linux_accel
from mousetool.server import Api


def _make_linux_sysfs(root, *, mod_name="mouseaccel", with_lut=True, mode=0, lut_raw=None):
    sys_module = os.path.join(root, "sys", "module")
    dev_dir = os.path.join(root, "dev")
    src_dir = os.path.join(root, "usr", "src")
    var_opt = os.path.join(root, "var", "opt")
    params_dir = os.path.join(sys_module, mod_name, "parameters")
    resets_dir = os.path.join(var_opt, mod_name, "resets")
    for d in (params_dir, dev_dir, os.path.join(src_dir, f"{mod_name}-0.2.0"), resets_dir):
        os.makedirs(d, exist_ok=True)
    for name in linux_accel.PARAMS:
        with open(os.path.join(params_dir, name), "w") as f:
            f.write(str(linux_accel.FIX))
    with open(os.path.join(params_dir, "MODE"), "w") as f:
        f.write(str(mode))
    if with_lut:
        default_lut = lut_raw or f"0,{linux_accel.FIX};{10 * linux_accel.FIX},{linux_accel.FIX}"
        with open(os.path.join(params_dir, "LUT_POINTS"), "w") as f:
            f.write(default_lut + "\n")
    return {
        "SYS_MODULE": sys_module,
        "DEV_DIR": dev_dir,
        "SRC_DIR": src_dir,
        "VAR_OPT": var_opt,
        "params_dir": params_dir,
        "resets_dir": resets_dir,
    }


class TestLinuxCustomLut(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _patch_tree(self, **kw):
        paths = _make_linux_sysfs(self.root, **kw)
        for k in ("SYS_MODULE", "DEV_DIR", "SRC_DIR", "VAR_OPT"):
            p = mock.patch.object(linux_accel, k, paths[k])
            p.start()
            self.addCleanup(p.stop)
        for k in ("SYS", "RESETS"):
            p = mock.patch.object(linux_accel, k, None)
            p.start()
            self.addCleanup(p.stop)
        return paths

    def test_encode_and_parse_lut_points_q32_32_roundtrip(self):
        pts = [[0.0, 0.85], [4.5, 1.0], [15.25, 1.45], [30.0, 1.75]]
        encoded = linux_accel.encode_lut_points_sysfs(pts)
        self.assertEqual(
            encoded,
            ";".join(f"{int(round(x * linux_accel.FIX))},{int(round(y * linux_accel.FIX))}" for x, y in pts),
        )
        parsed = linux_accel.parse_lut_points_sysfs(encoded + "\n")
        self.assertEqual(len(parsed), len(pts))
        for (px, py), (ex, ey) in zip(parsed, pts):
            self.assertAlmostEqual(px, ex, delta=1e-9)
            self.assertAlmostEqual(py, ey, delta=1e-9)

        # Max 64 points fits comfortably within MA_LUT_STR_MAX (3072 bytes)
        pts64 = [[round(i * 15.5, 3), round(0.5 + (i / 63.0) * 2.0, 4)] for i in range(64)]
        enc64 = linux_accel.encode_lut_points_sysfs(pts64)
        self.assertLessEqual(len(enc64), linux_accel.LUT_STR_MAX)
        self.assertEqual(len(linux_accel.parse_lut_points_sysfs(enc64)), 64)

    def test_invalid_lut_points_rejected_before_any_sysfs_write(self):
        paths = self._patch_tree(mod_name="mouseaccel", with_lut=True)
        mode_path = Path(paths["params_dir"]) / "MODE"
        lut_path = Path(paths["params_dir"]) / "LUT_POINTS"
        before_mode = mode_path.read_text()
        before_lut = lut_path.read_text()

        bad_tables = [
            [],
            [[0.0, 1.0]],
            [[i, 1.0] for i in range(65)],
            [[5.0, 1.0], [5.0, 1.2]],
            [[5.0, 1.0], [4.0, 1.2]],
            [[1.0, 1.0], [1.0005, 1.2]],
            [[-0.1, 1.0], [10.0, 1.2]],
            [[0.0, 1.0], [1000.1, 1.2]],
            [[0.0, 0.04], [10.0, 1.2]],
            [[0.0, 1.0], [10.0, 10.1]],
            [[0.0, float("nan")], [10.0, 1.2]],
            [[0.0, True], [10.0, 1.2]],
            "not-a-list",
        ]
        for bad in bad_tables:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                linux_accel.maccel_set({"MODE": "lookup", "LUT_POINTS": bad})
        self.assertEqual(mode_path.read_text(), before_mode)
        self.assertEqual(lut_path.read_text(), before_lut)

    def test_write_order_writes_lut_points_before_mode_4_and_skips_unsafe_reset_script(self):
        paths = self._patch_tree(mod_name="mouseaccel", with_lut=True, mode=0)
        pts = [[0.0, 1.0], [8.0, 1.25], [20.0, 1.75]]
        write_order = []
        orig_write = linux_accel._write_param

        def tracked_write(name, raw, backend):
            write_order.append((name, raw))
            return orig_write(name, raw, backend)

        with mock.patch.object(linux_accel, "_write_param", side_effect=tracked_write):
            st = linux_accel.maccel_set({"MODE": "lookup", "SENS_MULT": 1.1, "LUT_POINTS": pts})

        self.assertEqual([name for name, _ in write_order], ["LUT_POINTS", "SENS_MULT", "MODE"])
        self.assertEqual(write_order[-1], ("MODE", 4))
        self.assertEqual(st["mode"], "lookup")
        self.assertTrue(st["lut_supported"])
        self.assertTrue(st["lut_writable"])
        self.assertEqual(len(st["lut_points"]), 3)
        self.assertAlmostEqual(st["lut_points"][2][1], 1.75, delta=1e-9)
        # Reset file written for scalar/MODE, NOT for LUT_POINTS (single-integer udev parser safety)
        self.assertTrue((Path(paths["resets_dir"]) / "set_last_SENS_MULT_value.sh").is_file())
        self.assertTrue((Path(paths["resets_dir"]) / "set_last_MODE_value.sh").is_file())
        self.assertFalse((Path(paths["resets_dir"]) / "set_last_LUT_POINTS_value.sh").exists())

        live = linux_accel.maccel_live_config()
        self.assertEqual(live["MODE"], "lookup")
        self.assertEqual(len(live["LUT_POINTS"]), 3)

    def test_legacy_maccel_and_older_mouseaccel_without_lut_points_keep_other_modes_working(self):
        for mod_name in ("maccel", "mouseaccel"):
            with self.subTest(mod=mod_name):
                self.setUp()
                self._patch_tree(mod_name=mod_name, with_lut=False, mode=0)
                st = linux_accel.maccel_state()
                self.assertTrue(st["loaded"])
                self.assertTrue(st["parameters_available"])
                self.assertTrue(st["writable"])
                self.assertFalse(st["lut_supported"])
                self.assertFalse(st["lut_writable"])
                self.assertIsNone(st["lut_points"])

                # Existing modes still work normally
                st2 = linux_accel.maccel_set({"MODE": "natural", "LIMIT": 1.75})
                self.assertEqual(st2["mode"], "natural")
                self.assertAlmostEqual(st2["params"]["LIMIT"], 1.75)

                # Lookup mode / LUT_POINTS fail cleanly with clear PermissionError
                with self.assertRaisesRegex(PermissionError, "LUT_POINTS parameter not present"):
                    linux_accel.maccel_set({"MODE": "lookup", "LUT_POINTS": [[0, 1], [10, 1.5]]})
                with self.assertRaisesRegex(PermissionError, "LUT_POINTS parameter not present"):
                    linux_accel.maccel_set({"LUT_POINTS": [[0, 1], [10, 1.5]]})

    def test_readonly_lut_points_rejected_clearly(self):
        paths = self._patch_tree(mod_name="mouseaccel", with_lut=True, mode=0)
        lut_path = os.path.join(paths["params_dir"], "LUT_POINTS")
        orig_access = os.access

        def fake_access(path, mode):
            if os.path.abspath(path) == os.path.abspath(lut_path) and (mode & os.W_OK):
                return False
            return orig_access(path, mode)

        with mock.patch.object(os, "access", side_effect=fake_access):
            st = linux_accel.maccel_state()
            self.assertTrue(st["writable"])
            self.assertTrue(st["lut_supported"])
            self.assertFalse(st["lut_writable"])
            with self.assertRaisesRegex(PermissionError, "no write access.*LUT_POINTS"):
                linux_accel.maccel_set({"MODE": "lookup", "LUT_POINTS": [[0, 1], [10, 1.5]]})

    def test_http_api_accel_accepts_64_point_custom_curve_and_rejects_on_legacy(self):
        self._patch_tree(mod_name="mouseaccel", with_lut=True, mode=0)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        pts64 = [[round(i * 0.5, 2), round(0.8 + (i / 63.0) * 0.9, 3)] for i in range(64)]
        payload = json.dumps({
            "MODE": "lookup",
            "SENS_MULT": 1.0, "YX_RATIO": 1.0, "INPUT_DPI": 1600, "ANGLE_ROTATION": 0,
            "ACCEL": 0, "OFFSET": 0, "OUTPUT_CAP": 0,
            "DECAY_RATE": 0.1, "LIMIT": 1.5,
            "GAMMA": 1.0, "SMOOTH": 0.5, "MOTIVITY": 1.5, "SYNC_SPEED": 5.0,
            "LUT_POINTS": pts64,
        })
        self.assertLessEqual(len(payload.encode()), 4096)

        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        conn.request(
            "POST",
            "/api/accel?backend=mouseaccel",
            payload,
            {"Content-Type": "application/json", "X-Mouse-Tool": "1", "Origin": "null"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(body["accel"]["mode"], "lookup")
        self.assertTrue(body["accel"]["lut_supported"])
        self.assertEqual(len(body["accel"]["lut_points"]), 64)


if __name__ == "__main__":
    unittest.main()
