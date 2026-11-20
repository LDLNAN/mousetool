#!/usr/bin/env python3
import http.client
import json
import os
import re
import struct
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import mousetool


class TestRazerProtocol(unittest.TestCase):
    def test_build_packet_and_crc(self):
        pkt = mousetool._build(0x04, 0x05, 0x07, [0x01, 0x04, 0xB0, 0x04, 0xB0, 0x00, 0x00])
        self.assertEqual(len(pkt), 90)
        self.assertEqual(pkt[1], 0x1F)
        self.assertEqual(pkt[5], 0x07)
        self.assertEqual(pkt[6], 0x04)
        self.assertEqual(pkt[7], 0x05)
        self.assertEqual(pkt[8:15], bytearray([0x01, 0x04, 0xB0, 0x04, 0xB0, 0x00, 0x00]))
        expected_crc = 0
        for b in pkt[2:88]:
            expected_crc ^= b
        self.assertEqual(pkt[88], expected_crc)

    def test_viper_v2_pro_pids_and_priority(self):
        # 0x00A5 = USB cable (prio 0), 0x00B3 = HyperPolling (prio 1), 0x00A6 = HyperSpeed (prio 2)
        self.assertEqual(mousetool.DEVICES[0x00A5][1], "USB cable")
        self.assertEqual(mousetool.DEVICES[0x00A6][1], "HyperSpeed dongle")
        self.assertLess(mousetool.DEVICES[0x00A5][2], mousetool.DEVICES[0x00B3][2])
        self.assertLess(mousetool.DEVICES[0x00B3][2], mousetool.DEVICES[0x00A6][2])

    def test_device_lists_consistent_across_repo(self):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(repo_dir, "60-mousetool.rules")) as f:
            rules_txt = f.read()
        rules_vp = {
            (int(v, 16), int(p, 16))
            for v, p in re.findall(
                r'ATTRS\{idVendor\}=="([0-9a-fA-F]{4})".*?ATTRS\{idProduct\}=="([0-9a-fA-F]{4})"',
                rules_txt,
            )
        }
        with open(os.path.join(repo_dir, "index.html")) as f:
            html = f.read()

        single_vid_tables = [
            ("DEVICES", mousetool.DEVICES, mousetool.RAZER_VID),
            ("EGG_DEVICES", mousetool.EGG_DEVICES, mousetool.EGG_VID),
            ("EGG_WE_DEVICES", mousetool.EGG_WE_DEVICES, mousetool.EGG_VID),
            ("LOGI_DEVICES", mousetool.LOGI_DEVICES, mousetool.LOGI_VID),
            ("PULSAR_XS1_DEVICES", mousetool.PULSAR_XS1_DEVICES, mousetool.PULSAR_VID),
            ("PULSAR_PRO_DEVICES", mousetool.PULSAR_PRO_DEVICES, mousetool.PULSAR_VID),
            ("ARESON_DEVICES", mousetool.ARESON_DEVICES, mousetool.ARESON_VID),
        ]
        all_py_vp = set()
        for js_name, py_map, vid in single_vid_tables:
            m = re.search(rf"const {js_name} = \{{(.*?)\n\}};", html, re.S)
            self.assertIsNotNone(m, f"Missing {js_name} in index.html")
            html_pids = {int(p, 16) for p in re.findall(r"0x([0-9a-fA-F]{4}):", m.group(1))}
            py_pids = set(py_map.keys())
            self.assertEqual(py_pids, html_pids, f"index.html mismatch for {js_name}")
            all_py_vp |= {(vid, pid) for pid in py_pids}

        multi_vid_tables = [
            ("COMPX_DEVICES", mousetool.COMPX_DEVICES),
            ("GLORIOUS_SINOWEALTH", mousetool.GLORIOUS_SINOWEALTH),
            ("GLORIOUS_CORE2", mousetool.GLORIOUS_CORE2),
            ("GLORIOUS_CLASSIC", mousetool.GLORIOUS_CLASSIC),
            ("GLORIOUS_PIXART", mousetool.GLORIOUS_PIXART),
        ]
        for js_name, py_map in multi_vid_tables:
            m = re.search(rf"const {js_name} = \{{(.*?)\n\}};", html, re.S)
            self.assertIsNotNone(m, f"Missing {js_name} in index.html")
            html_vp = {
                (int(v, 16), int(p, 16))
                for v, p in re.findall(r'"([0-9a-fA-F]{4}):([0-9a-fA-F]{4})":', m.group(1))
            }
            py_vp = set(py_map.keys())
            self.assertEqual(py_vp, html_vp, f"index.html mismatch for {js_name}")
            all_py_vp |= py_vp

        self.assertEqual(all_py_vp, rules_vp, "udev rules mismatch vs Python device registries")

        m_atk = re.search(r"const ATK_PRODUCTS = \{(.*?)\n\};", html, re.S)
        self.assertIsNotNone(m_atk, "Missing ATK_PRODUCTS in index.html")
        html_atk = {(int(c), int(mid)) for c, mid in re.findall(r'"(\d+):(\d+)":', m_atk.group(1))}
        self.assertEqual(set(mousetool.ATK_PRODUCTS.keys()), html_atk, "ATK_PRODUCTS mismatch")


class TestEndgameGearProtocol(unittest.TestCase):
    def _sample_egg_blob(self):
        b = bytearray(mousetool.EGG_CFG_LEN)
        b[0] = 0xA0
        b[1] = 0x01
        b[21] = 8        # 8000 / 8 = 1000 Hz
        b[22] = 0x01     # slamclick on, motion_jitter off
        b[24] = 1        # LED active
        b[25] = 1        # 1 mm
        b[26] = 0        # angle snapping off
        b[27] = 1        # ripple control on
        b[28] = 1        # motion sync on
        b[29] = 1        # active stage 0-indexed -> stage 2
        b[30] = 4        # 4 stages
        for idx, cpi in enumerate((400, 800, 1600, 3200)):
            o = 51 + idx * 5
            b[o] = 0
            b[o + 1] = cpi & 0xFF
            b[o + 2] = (cpi >> 8) & 0xFF
            b[o + 3] = cpi & 0xFF
            b[o + 4] = (cpi >> 8) & 0xFF
        # Right-handed Left slot (71..76) = Left click [0x00, 0x01, 0, 0, 0, 0]
        b[71:77] = bytes([0x00, 0x01, 0, 0, 0, 0])
        # Button 0 (Right) ctl=0xF1 (gx_speed), type=0, param1=0x02 (Right click)
        b[77:84] = bytes([0xF1, 0x00, 0x02, 0, 0, 0, 0])
        # Button 1 (Middle) ctl=0xF1 (gx_speed), type=0, param1=0x04
        b[84:91] = bytes([0xF1, 0x00, 0x04, 0, 0, 0, 0])
        # Preserve sentinel bytes
        b[16:21] = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE])
        b[31:35] = bytes([0x11, 0x22, 0x33, 0x44])
        b[130] = 0x5A
        return b

    def test_egg_clamp_cpi_v1_and_v2(self):
        v1 = mousetool.EGG_DEVICES[0x1964]  # OP1 8k
        self.assertEqual(mousetool.egg_clamp_cpi(v1, 20), 50)
        self.assertEqual(mousetool.egg_clamp_cpi(v1, 826), 850)
        self.assertEqual(mousetool.egg_clamp_cpi(v1, 12020), 12000)
        self.assertEqual(mousetool.egg_clamp_cpi(v1, 12040), 12050)
        self.assertEqual(mousetool.egg_clamp_cpi(v1, 35000), 26000)

        v2 = mousetool.EGG_DEVICES[0x1978]  # OP1 8k v2
        self.assertEqual(mousetool.egg_clamp_cpi(v2, 824), 820)
        self.assertEqual(mousetool.egg_clamp_cpi(v2, 12030), 12050)
        self.assertEqual(mousetool.egg_clamp_cpi(v2, 35000), 30000)

    def test_egg_format_version_bcd(self):
        buf = bytearray(64)
        buf[17] = 0x23
        buf[18] = 0x01
        self.assertEqual(mousetool.egg_format_version(buf), "v1.23")
        buf[17] = 0x07
        buf[18] = 0x01
        self.assertEqual(mousetool.egg_format_version(buf), "v1.07")

    def test_egg_parse_and_serialize_preserves_and_left_handed(self):
        v2 = mousetool.EGG_DEVICES[0x1978]
        blob = self._sample_egg_blob()
        self.assertTrue(mousetool.egg_blob_ok(blob))
        parsed = mousetool.egg_parse_config(blob, v2)
        self.assertEqual(parsed["polling_hz"], 1000)
        self.assertEqual(parsed["active"], 2)
        self.assertEqual(parsed["dpi"], (800, 800))
        self.assertEqual(parsed["stages"], [(400, 400), (800, 800), (1600, 1600), (3200, 3200)])
        self.assertTrue(parsed["slamclick"])
        self.assertFalse(parsed["motion_jitter"])
        self.assertTrue(parsed["ripple_control"])
        self.assertTrue(parsed["motion_sync"])
        self.assertFalse(parsed["left_handed"])
        self.assertEqual(parsed["buttons"][0]["spdt"], "gx_speed")

        out = mousetool.egg_serialize_config(blob, {
            "polling_hz": 4000,
            "dpi": (1625, 1625),
            "spdt": "gx_safe",
            "left_handed": True,
            "glass_mode": True,
            "lod_index": 1,
            "angle_tuning": -5,
            "force_max_fps": True,
            "led_liftoff_disabled": True,
        }, v2)
        self.assertEqual(out[0], 0xA0)
        self.assertEqual(out[1], 0x11)
        # Reserved/unchanged bytes preserved
        self.assertEqual(bytes(out[16:21]), bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE]))
        self.assertEqual(bytes(out[31:35]), bytes([0x11, 0x22, 0x33, 0x44]))
        self.assertEqual(out[130], 0x5A)

        parsed2 = mousetool.egg_parse_config(out, v2)
        self.assertEqual(parsed2["polling_hz"], 4000)
        self.assertEqual(parsed2["dpi"], (1630, 1630))
        self.assertEqual(parsed2["buttons"][0]["spdt"], "gx_safe")
        self.assertTrue(parsed2["left_handed"])
        self.assertTrue(parsed2["glass_mode"])
        self.assertEqual(parsed2["lod"]["label"], "2.0 mm")
        self.assertEqual(parsed2["angle_tuning"], -5)
        self.assertTrue(parsed2["force_max_fps"])
        self.assertTrue(parsed2["led_liftoff_disabled"])
        self.assertEqual(bytes(out[71:77]), bytes([0x00, 0x02, 0, 0, 0, 0]))
        self.assertEqual(bytes(out[78:84]), bytes([0x00, 0x01, 0, 0, 0, 0]))

        # Revert left-handed mode cleanly
        out3 = mousetool.egg_serialize_config(out, {"left_handed": False}, v2)
        self.assertFalse(mousetool.egg_parse_config(out3, v2)["left_handed"])
        self.assertEqual(bytes(out3[71:77]), bytes([0x00, 0x01, 0, 0, 0, 0]))

        with self.assertRaises(ValueError):
            mousetool.egg_serialize_config(blob, {"polling_hz": 3333}, v2)


class TestMultiMouseDiscovery(unittest.TestCase):
    def test_select_mice_selectors(self):
        fake = [
            {
                "id": "razer:00a5:/dev/hidraw5",
                "driver": "razer",
                "hidraw": "/dev/hidraw5",
                "vid": 0x1532,
                "pid": 0x00A5,
                "name": "Razer Viper V2 Pro",
                "link": "USB cable",
            },
            {
                "id": "egg:1964:/dev/hidraw11",
                "driver": "egg",
                "hidraw": "/dev/hidraw11",
                "vid": 0x3367,
                "pid": 0x1964,
                "name": "Endgame Gear OP1 8k",
                "link": "USB 8k",
            },
            {
                "id": "logitech:c094:/dev/hidraw9",
                "driver": "logitech",
                "hidraw": "/dev/hidraw9",
                "vid": 0x046D,
                "pid": 0xC094,
                "name": "Logitech G PRO X SUPERLIGHT",
                "link": "USB cable",
            },
        ]
        with mock.patch.object(mousetool, "discover_mice", return_value=fake):
            self.assertEqual(mousetool.select_mice()[0]["driver"], "razer")
            self.assertEqual(len(mousetool.select_mice(all_devices=True)), 3)
            self.assertEqual(mousetool.select_mice("2")[0]["pid"], 0x1964)
            self.assertEqual(mousetool.select_mice("3367:1964")[0]["hidraw"], "/dev/hidraw11")
            self.assertEqual(mousetool.select_mice("hidraw11")[0]["driver"], "egg")
            self.assertEqual(mousetool.select_mice("/dev/hidraw9")[0]["driver"], "logitech")
            self.assertEqual(mousetool.select_mice("op1")[0]["pid"], 0x1964)
            self.assertEqual(mousetool.select_mice(driver="egg")[0]["pid"], 0x1964)
            with self.assertRaises(FileNotFoundError):
                mousetool.select_mice("9")
            with self.assertRaises(FileNotFoundError):
                mousetool.select_mice("nonexistent")

    def test_discover_system_mice_merges_generic_inputs(self):
        fake_cfg = [{
            "id": "razer:00a5:/dev/hidraw5",
            "driver": "razer",
            "hidraw": "/dev/hidraw5",
            "vid": 0x1532,
            "pid": 0x00A5,
            "name": "Razer Viper V2 Pro",
            "link": "USB cable",
            "wireless": True,
            "hyper": False,
            "dpi_max": 30000,
            "phys": "usb-0000:00:14.0-1",
            "battery_percent": None,
            "charging": None,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            # 1) Mouse matching configurable Razer device (should NOT duplicate)
            m1 = os.path.join(tmp, "mouse0")
            os.makedirs(os.path.join(m1, "device"))
            with open(os.path.join(m1, "device", "name"), "w") as f:
                f.write("Razer Viper V2 Pro\n")
            with open(os.path.join(m1, "device", "uevent"), "w") as f:
                f.write("PRODUCT=3/1532/a5/111\nPHYS=usb-0000:00:14.0-1/input0\n")

            # 2) Generic third-party USB mouse (should appear as generic)
            m2 = os.path.join(tmp, "mouse1")
            os.makedirs(os.path.join(m2, "device"))
            with open(os.path.join(m2, "device", "name"), "w") as f:
                f.write("Generic Office Mouse\n")
            with open(os.path.join(m2, "device", "uevent"), "w") as f:
                f.write("PRODUCT=3/461/4e66/110\nPHYS=usb-0000:00:14.0-2/input0\n")

            with mock.patch.object(mousetool, "discover_mice", return_value=fake_cfg), \
                 mock.patch.object(mousetool, "_sysfs_batteries", return_value={}), \
                 mock.patch.object(mousetool.glob, "glob", return_value=[m1, m2]):
                res = mousetool.discover_system_mice()
            self.assertEqual(len(res), 2)
            self.assertEqual(res[0]["driver"], "razer")
            self.assertTrue(res[0]["configurable"])
            self.assertEqual(res[1]["driver"], "generic")
            self.assertFalse(res[1]["configurable"])
            self.assertEqual(res[1]["name"], "Generic Office Mouse")


class TestMaccelAndNiri(unittest.TestCase):
    def test_maccel_set_fixed_point_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            sys_dir = os.path.join(tmp, "sys")
            resets_dir = os.path.join(tmp, "resets")
            os.makedirs(sys_dir)
            os.makedirs(resets_dir)
            for name in mousetool.PARAMS:
                with open(os.path.join(sys_dir, name), "w") as f:
                    f.write(str(mousetool.FIX))
            with open(os.path.join(sys_dir, "MODE"), "w") as f:
                f.write("0")

            with mock.patch.object(mousetool, "SYS", sys_dir), mock.patch.object(mousetool, "RESETS", resets_dir):
                st = mousetool.maccel_set({"MODE": "natural", "LIMIT": 1.5, "DECAY_RATE": 0.25})
                self.assertEqual(st["mode"], "natural")
                self.assertAlmostEqual(st["params"]["LIMIT"], 1.5)
                self.assertAlmostEqual(st["params"]["DECAY_RATE"], 0.25)

                with open(os.path.join(sys_dir, "MODE")) as f:
                    self.assertEqual(f.read().strip(), "1")
                with open(os.path.join(sys_dir, "LIMIT")) as f:
                    self.assertEqual(int(f.read().strip()), int(round(1.5 * mousetool.FIX)))
                with open(os.path.join(resets_dir, "set_last_LIMIT_value.sh")) as f:
                    self.assertIn(str(int(round(1.5 * mousetool.FIX))), f.read())

                with self.assertRaises(ValueError):
                    mousetool.maccel_set({"MODE": "bogus"})
                with self.assertRaises(ValueError):
                    mousetool.maccel_set({"ACCEL": True})
                with self.assertRaises(ValueError):
                    mousetool.maccel_set({"LIMIT": 0.5})

    def test_niri_state_and_set_preserves_kdl(self):
        sample_kdl = (
            "input {\n"
            "    touchpad {\n"
            "        tap\n"
            "    }\n\n"
            "    mouse {\n"
            '        accel-profile "flat"\n'
            "    }\n\n"
            "    trackpoint {\n"
            "        // off\n"
            "    }\n"
            "}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.kdl")
            with open(cfg, "w") as f:
                f.write(sample_kdl)

            self.assertEqual(mousetool.niri_state(cfg), {"accel_profile": "flat", "accel_speed": 0.0})

            updated = mousetool.niri_set({"accel_profile": "adaptive", "accel_speed": -0.25}, cfg)
            self.assertEqual(updated, {"accel_profile": "adaptive", "accel_speed": -0.25})

            with open(cfg) as f:
                text = f.read()
            self.assertIn("touchpad {", text)
            self.assertIn("// off", text)
            self.assertIn('accel-profile "adaptive"', text)
            self.assertIn("accel-speed -0.25", text)

            with self.assertRaises(ValueError):
                mousetool.niri_set({"accel_profile": "other"}, cfg)
            with self.assertRaises(ValueError):
                mousetool.niri_set({"accel_speed": 2.0}, cfg)


class TestLocalApi(unittest.TestCase):
    def test_http_endpoints_and_cors_guards(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mousetool.Api)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        fake_sys_mice = [{"id": "egg:1964:/dev/hidraw11", "driver": "egg", "name": "Endgame Gear OP1 8k", "configurable": True}]
        try:
            with mock.patch.dict(os.environ, {"MOUSETOOL_DESKTOP": "xfce"}), \
                 mock.patch.object(mousetool, "maccel_state", return_value={"loaded": False}), \
                 mock.patch.object(mousetool, "niri_state", return_value={"accel_profile": "flat", "accel_speed": 0.0}), \
                 mock.patch.object(mousetool, "discover_system_mice", return_value=fake_sys_mice):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/status", headers={"Origin": "null"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "null")
                body = json.loads(r.read())
                self.assertEqual(body["version"], 2)
                self.assertEqual(body["niri"]["accel_profile"], "flat")
                self.assertEqual(body["system_mice"][0]["driver"], "egg")

                c.request("GET", "/api/devices", headers={"Origin": "null"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                devs = json.loads(r.read())
                self.assertEqual(devs["mice"][0]["name"], "Endgame Gear OP1 8k")

                c.request("GET", "/api/desktop")
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                desktop = json.loads(r.read())["desktop"]
                self.assertEqual(desktop, body["desktop"])
                self.assertFalse(desktop["writable"])
                self.assertEqual(desktop["backend"], "xfce")
                for update, status in (({"accel_speed": 1.1}, 400), ({"accel_speed": 1}, 409)):
                    c.request("POST", "/api/desktop", body=json.dumps(update), headers={"X-Mouse-Tool": "1"})
                    r = c.getresponse()
                    self.assertEqual(r.status, status)
                    self.assertIn("error", json.loads(r.read()))

                with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
                    "MOUSETOOL_DESKTOP": "cosmic", "XDG_CONFIG_HOME": tmp,
                    "XDG_DATA_HOME": tmp, "XDG_DATA_DIRS": tmp,
                }):
                    c.request("POST", "/api/desktop", body='{"accel_profile":"flat","accel_speed":-0.25}',
                              headers={"X-Mouse-Tool": "1"})
                    r = c.getresponse()
                    self.assertEqual(r.status, 200)
                    saved = json.loads(r.read())["desktop"]
                    self.assertEqual((saved["accel_profile"], saved["accel_speed"]), ("flat", -0.25))
                    c.request("GET", "/api/desktop")
                    self.assertEqual(json.loads(c.getresponse().read())["desktop"], saved)

                # Evil origin blocked
                c.request("GET", "/api/status", headers={"Origin": "https://evil.example"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 403)

                # Missing custom header blocked
                c.request("POST", "/api/maccel", body=b"{}", headers={"Origin": "null", "Content-Type": "application/json"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 403)

                # Valid POST to /api/niri
                with mock.patch.object(mousetool, "niri_set", return_value={"accel_profile": "adaptive", "accel_speed": 0.1}) as m_niri:
                    c.request(
                        "POST",
                        "/api/niri",
                        body=json.dumps({"accel_profile": "adaptive"}).encode(),
                        headers={"Origin": "null", "Content-Type": "application/json", "X-Mouse-Tool": "1"},
                    )
                    r = c.getresponse()
                    self.assertEqual(r.status, 200)
                    self.assertEqual(json.loads(r.read())["niri"]["accel_profile"], "adaptive")
                    m_niri.assert_called_once_with({"accel_profile": "adaptive"})
                c.close()
        finally:
            srv.shutdown()
            srv.server_close()


class TestUniversalProtocols(unittest.TestCase):
    def test_hid_collection_parser(self):
        # CompX 0xFF02 usage 0x0002 with Input 0x08 (16B) + Output 0x08 (16B)
        rd_compx = bytes([
            0x06, 0x02, 0xFF, 0x09, 0x02, 0xA1, 0x01,
            0x85, 0x08, 0x75, 0x08, 0x95, 0x10, 0x09, 0x01, 0x81, 0x02,
            0x09, 0x02, 0x91, 0x02, 0xC0,
        ])
        cols = mousetool._parse_hid_collections(rd_compx)
        self.assertIsNotNone(mousetool._cols_has(cols, page=0xFF02, usage=0x0002, in_id=0x08, out_id=0x08))
        self.assertEqual(cols[0]["in_lens"][0x08], 16)
        self.assertEqual(cols[0]["out_lens"][0x08], 16)

        # Pulsar XS-1 0xFFFF:0x0001 Feature 0 (64B)
        rd_xs1 = bytes([
            0x06, 0xFF, 0xFF, 0x09, 0x01, 0xA1, 0x01,
            0x75, 0x08, 0x95, 0x40, 0x09, 0x01, 0xB1, 0x02, 0xC0,
        ])
        c_xs1 = mousetool._cols_has(mousetool._parse_hid_collections(rd_xs1), page=0xFFFF, usage=0x0001, feat_id=0)
        self.assertIsNotNone(c_xs1)
        self.assertEqual(c_xs1["feat_lens"][0], 64)

        # Pulsar Pro 0x0001:0x0000 Input 0 + Output 0
        rd_pro = bytes([
            0x05, 0x01, 0x09, 0x00, 0xA1, 0x01,
            0x75, 0x08, 0x95, 0x3F, 0x09, 0x01, 0x81, 0x02,
            0x09, 0x02, 0x91, 0x02, 0xC0,
        ])
        self.assertIsNotNone(
            mousetool._cols_has(mousetool._parse_hid_collections(rd_pro), page=0x0001, usage=0x0000, in_id=0, out_id=0)
        )

        # Sinowealth 0xFF00 Feature 0x05 (6B) + Feature 0x04 (166B)
        rd_sino = bytes([
            0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,
            0x85, 0x05, 0x75, 0x08, 0x95, 0x06, 0x09, 0x01, 0xB1, 0x02,
            0x85, 0x04, 0x95, 0xA6, 0x09, 0x02, 0xB1, 0x02, 0xC0,
        ])
        c_sino = mousetool._cols_has(mousetool._parse_hid_collections(rd_sino), page=0xFF00, feat_id=0x05)
        self.assertIsNotNone(c_sino)
        self.assertEqual(c_sino["feat_lens"][0x05], 6)
        self.assertEqual(c_sino["feat_lens"][0x04], 166)

    def test_compx_checksum_and_dpi_codecs(self):
        pkt = mousetool._compx_build(0x07, [0x00, 0x00, 0xA9, 0x02, 0x04, 0x51])
        self.assertEqual(len(pkt), 16)
        self.assertEqual(pkt[0], 0x07)
        self.assertEqual(pkt[15], (0x55 - (0x08 + sum(pkt[:15]))) & 0xFF)

        # byte50 (EGG -we)
        b = mousetool.compx_encode_dpi4(1600, 1600, "byte50")
        self.assertEqual(mousetool.compx_decode_dpi4(b, "byte50"), (1600, 1600))
        self.assertEqual(b[3], (0x55 - sum(b[:3])) & 0xFF)

        # flat50 split X/Y up to 30000
        b_flat = mousetool.compx_encode_dpi4(1600, 26000, "flat50")
        self.assertEqual(mousetool.compx_decode_dpi4(b_flat, "flat50"), (1600, 26000))

        # pulsar 3-tier encoding (10 / 50 / 100 steps)
        for dpi in (450, 1600, 12550, 31900):
            bp = mousetool.compx_encode_dpi4(dpi, dpi, "pulsar")
            self.assertEqual(mousetool.compx_decode_dpi4(bp, "pulsar"), (dpi, dpi))

        # paw3950 2-tier encoding (50 <=30000, 100 >30000)
        for dpi in (800, 26000, 41500):
            bt = mousetool.compx_encode_dpi4(dpi, dpi, "paw3950")
            self.assertEqual(mousetool.compx_decode_dpi4(bt, "paw3950"), (dpi, dpi))

        # 6-byte DPI encoding (Scyrox V8S / ATK A9 Mini+)
        b6 = mousetool.compx_encode_dpi6(800, 48000)
        self.assertEqual(len(b6), 6)
        self.assertEqual(mousetool.compx_decode_dpi6(b6), (800, 48000))
        self.assertEqual(b6[5], (0x55 - sum(b6[:5])) & 0xFF)

    def test_sinowealth_parser_and_safety_gate(self):
        blk = bytearray(130)
        blk[9] = 0x06          # PMW3360 -> offset1=True, dpi_max=12000
        blk[0x0A] = 0x84       # xy_mode=True, poll=1000 Hz (4)
        blk[0x0B] = 2 << 4     # active stage 2
        blk[0x0C] = 0b11111000 # stages 0..2 enabled
        # offset1=True => (raw + 1) * 100
        blk[0x0D:0x13] = bytes([3, 3, 7, 15, 31, 31])  # 400x400, 800x1600, 3200x3200
        parsed = mousetool.sino_parse_block(blk)
        self.assertTrue(parsed["sensor_writable"])
        self.assertEqual(parsed["sensor"], "PMW3360")
        self.assertEqual(parsed["polling_hz"], 1000)
        self.assertEqual(parsed["stages"], [[400, 400], [800, 1600], [3200, 3200]])
        self.assertEqual(parsed["dpi"], [800, 1600])

        blk[9] = 0x99          # unknown sensor -> read-only safety gate
        self.assertFalse(mousetool.sino_parse_block(blk)["sensor_writable"])

    def test_glorious_classic_safety_gates(self):
        bad_len_meta = {
            "id": "glorious_classic-258a:2011-hidraw0",
            "driver": "glorious_classic",
            "vid": 0x258A, "pid": 0x2011,
            "name": "Glorious Model O Wireless",
            "link": "USB cable",
            "wireless": True,
            "gen": "legacy", "feat_rid": 2, "feat_len": 32,
            "hidraw": "/dev/hidraw0",
        }
        core2_meta = dict(bad_len_meta, vid=0x3794, pid=0xA300, gen="core2", feat_len=64)

        class _Args:
            device = None
            x = 1600
            y = None
            debounce = 6
            motion_sync = angle_snapping = ripple_control = slamclick = None
            motion_jitter = glass_mode = force_max_fps = led_liftoff_disabled = None
            left_handed = angle_tuning = spdt = None

        with mock.patch.object(mousetool, "select_mice", return_value=[bad_len_meta]), \
             mock.patch.object(mousetool.os, "open", return_value=99), \
             mock.patch.object(mousetool.os, "close"):
            with self.assertRaises(OSError):
                mousetool.dpi_cmd(_Args())
            with self.assertRaises(OSError):
                mousetool.tune_cmd(_Args())

        with mock.patch.object(mousetool, "select_mice", return_value=[core2_meta]), \
             mock.patch.object(mousetool.os, "open", return_value=99), \
             mock.patch.object(mousetool.os, "close"), \
             mock.patch.object(mousetool.fcntl, "ioctl") as m_ioctl, \
             mock.patch.object(mousetool, "_save_device_cache"), \
             mock.patch.object(mousetool, "classic_read_status", return_value={"debounce_ms": 6}), \
             mock.patch("sys.stdout"):
            with self.assertRaises(OSError):
                mousetool.dpi_cmd(_Args())
            m_ioctl.assert_not_called()
            # CORE 2 Classic DOES allow debounce when feat_len == 64
            mousetool.tune_cmd(_Args())
            self.assertEqual(m_ioctl.call_count, 1)

    def test_discover_identical_split_mice_and_0xa1_exclusion(self):
        rd_egg_v2_a1 = bytes([
            0x06, 0x01, 0xFF, 0x09, 0x01, 0xA1, 0x01,
            0x85, 0xA0, 0x75, 0x08, 0x95, 0x08, 0x09, 0x01, 0xB1, 0x02,
            0x85, 0xA1, 0x75, 0x08, 0x95, 0x08, 0x09, 0x02, 0xB1, 0x02, 0xC0,
        ])
        rd_we_cmd = bytes([
            0x06, 0x02, 0xFF, 0x09, 0x02, 0xA1, 0x01,
            0x85, 0x08, 0x75, 0x08, 0x95, 0x10, 0x09, 0x01, 0xB1, 0x02, 0xC0,
        ])
        rd_we_notify = bytes([
            0x06, 0x01, 0xFF, 0x09, 0x01, 0xA1, 0x01,
            0x85, 0x09, 0x75, 0x08, 0x95, 0x10, 0x09, 0x01, 0x81, 0x02, 0xC0,
        ])

        def _mk_hidraw(base, name, hid_id, phys, rd):
            d = os.path.join(base, name)
            os.makedirs(os.path.join(d, "device"), exist_ok=True)
            with open(os.path.join(d, "device", "uevent"), "w") as f:
                f.write(f"HID_ID={hid_id}\nHID_NAME=Test Mouse\nHID_PHYS={phys}\n")
            with open(os.path.join(d, "device", "report_descriptor"), "wb") as f:
                f.write(rd)
            return d

        def _mk_mouse(base, name, prod, phys):
            d = os.path.join(base, name)
            os.makedirs(os.path.join(d, "device"), exist_ok=True)
            with open(os.path.join(d, "device", "name"), "w") as f:
                f.write("Endgame Gear OP1we\n")
            with open(os.path.join(d, "device", "uevent"), "w") as f:
                f.write(f"PRODUCT={prod}\nPHYS={phys}\n")
            return d

        with tempfile.TemporaryDirectory() as tmp:
            # 1) 0x3367:0x1970 with Feature 0xA1 must NOT be classified as egg_we
            h0 = _mk_hidraw(tmp, "hidraw0", "0003:00003367:00001970", "usb-0000:00:14.0-9/input1", rd_egg_v2_a1)
            # 2) Two identical 0x3367:0x1961 OP1we mice on different USB ports with split cmd/notify nodes
            h1 = _mk_hidraw(tmp, "hidraw1", "0003:00003367:00001961", "usb-0000:00:14.0-1:1.1/input1", rd_we_cmd)
            h2 = _mk_hidraw(tmp, "hidraw2", "0003:00003367:00001961", "usb-0000:00:14.0-1:1.2/input2", rd_we_notify)
            h3 = _mk_hidraw(tmp, "hidraw3", "0003:00003367:00001961", "usb-0000:00:14.0-2:1.2/input2", rd_we_notify)
            h4 = _mk_hidraw(tmp, "hidraw4", "0003:00003367:00001961", "usb-0000:00:14.0-2:1.1/input1", rd_we_cmd)

            m0 = _mk_mouse(tmp, "mouse0", "3/3367/1961/111", "usb-0000:00:14.0-1:1.0/input0")
            m1 = _mk_mouse(tmp, "mouse1", "3/3367/1961/111", "usb-0000:00:14.0-2:1.0/input0")

            def _fake_glob(pattern):
                if "hidraw" in pattern:
                    return [h0, h1, h2, h3, h4]
                if "input/mouse" in pattern:
                    return [m0, m1]
                return []

            with mock.patch.object(mousetool.glob, "glob", side_effect=_fake_glob), \
                 mock.patch.object(mousetool, "_sysfs_batteries", return_value=[]):
                mice = mousetool.discover_mice()
                self.assertEqual([m["driver"] for m in mice], ["egg8k", "egg_we", "egg_we"])
                we_mice = [m for m in mice if m["driver"] == "egg_we"]
                self.assertEqual(len(we_mice), 2)
                self.assertEqual(we_mice[0]["hidraw"], "/dev/hidraw1")
                self.assertEqual(we_mice[0]["notify_hidraw"], "/dev/hidraw2")
                self.assertEqual(we_mice[1]["hidraw"], "/dev/hidraw4")
                self.assertEqual(we_mice[1]["notify_hidraw"], "/dev/hidraw3")

                sys_mice = mousetool.discover_system_mice()
                sys_we = [m for m in sys_mice if m["driver"] == "egg_we"]
                self.assertEqual(len(sys_we), 2)
                self.assertNotEqual(sys_we[0]["id"], sys_we[1]["id"])


class TestMotionTelemetry(unittest.TestCase):
    @staticmethod
    def _pack_ev(sec, usec, ev_type, code, value):
        return mousetool.INPUT_EVENT_STRUCT.pack(sec, usec, ev_type, code, value)

    def test_parse_evdev_events_and_chunking(self):
        # Frame 1: REL_X=12, REL_Y=-5, ignored REL_WHEEL=1, SYN_REPORT at 100.001s
        raw = (
            self._pack_ev(100, 1000, mousetool.EV_REL, mousetool.REL_X, 12)
            + self._pack_ev(100, 1000, mousetool.EV_REL, mousetool.REL_Y, -5)
            + self._pack_ev(100, 1000, mousetool.EV_REL, 8, 1)  # REL_WHEEL ignored
            + self._pack_ev(100, 1000, mousetool.EV_SYN, mousetool.SYN_REPORT, 0)
            # Empty SYN_REPORT should emit nothing
            + self._pack_ev(100, 2000, mousetool.EV_SYN, mousetool.SYN_REPORT, 0)
            # Frame 2 split across chunks
            + self._pack_ev(100, 3000, mousetool.EV_REL, mousetool.REL_X, 6)
            + self._pack_ev(100, 3000, mousetool.EV_REL, mousetool.REL_Y, 8)
            + self._pack_ev(100, 3000, mousetool.EV_SYN, mousetool.SYN_REPORT, 0)
        )
        # Split mid-struct to test partial byte buffering
        cut = mousetool.INPUT_EVENT_STRUCT.size * 5 + 7
        pending = {"dx": 0, "dy": 0, "buf": b""}
        f1, pending = mousetool.parse_evdev_events(raw[:cut], pending)
        f2, pending = mousetool.parse_evdev_events(raw[cut:], pending)
        self.assertEqual(len(f1), 1)
        self.assertAlmostEqual(f1[0][0], 100.001, places=6)
        self.assertEqual((f1[0][1], f1[0][2]), (12, -5))
        self.assertEqual(len(f2), 1)
        self.assertAlmostEqual(f2[0][0], 100.003, places=6)
        self.assertEqual((f2[0][1], f2[0][2]), (6, 8))
        self.assertEqual(pending["buf"], b"")

    def test_discover_evdev_mice_sysfs_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            def _mk_ev(name, ev_hex, rel_hex):
                cap = os.path.join(tmp, name, "device", "capabilities")
                os.makedirs(cap)
                with open(os.path.join(cap, "ev"), "w") as f:
                    f.write(ev_hex + "\n")
                with open(os.path.join(cap, "rel"), "w") as f:
                    f.write(rel_hex + "\n")

            _mk_ev("event2", "120013", "0")           # keyboard (no EV_REL)
            _mk_ev("event5", "17", "1943")            # standard mouse (EV_REL + REL_X + REL_Y)
            _mk_ev("event9", "4", "100")              # scroll-only dial (no REL_X/REL_Y)
            _mk_ev("event11", "0 17", "0 3")          # multi-word capability bitmap mouse

            found = mousetool.discover_evdev_mice(
                sys_input_glob=os.path.join(tmp, "event*"),
                dev_dir="/dev/input",
            )
            self.assertEqual(found, ["/dev/input/event11", "/dev/input/event5"])

    def test_speed_and_dpi_normalization(self):
        # 3-4-5 right triangle: distance=5 counts in 1 ms at 1000 DPI => 5.0 counts/ms
        self.assertAlmostEqual(mousetool.compute_normalized_speed(3, 4, 1.0, 1000.0), 5.0, places=6)
        # At 2000 DPI, 1000/2000 = 0.5x => 2.5 counts/ms
        self.assertAlmostEqual(mousetool.compute_normalized_speed(3, 4, 1.0, 2000.0), 2.5, places=6)
        # At 3200 DPI, 32 counts in 1 ms => 10.0 counts/ms
        self.assertAlmostEqual(mousetool.compute_normalized_speed(32, 0, 1.0, 3200.0), 10.0, places=6)
        # Invalid dt or DPI safely returns 0.0
        self.assertEqual(mousetool.compute_normalized_speed(10, 0, 0.0, 1000.0), 0.0)
        self.assertEqual(mousetool.compute_normalized_speed(10, 0, -1.0, 1000.0), 0.0)
        self.assertEqual(mousetool.compute_normalized_speed(10, 0, 1.0, 0.0), 0.0)
        self.assertEqual(mousetool.compute_normalized_speed(10, 0, float("nan"), 1000.0), 0.0)

        # /dev/maccel big-endian Q32.32 fixed-point decoding
        with tempfile.NamedTemporaryFile() as tf:
            raw_fixed = int(round(18.5 * mousetool.FIX))
            tf.write(struct.pack(">q", raw_fixed))
            tf.flush()
            res = mousetool.read_maccel_speed(tf.name)
            self.assertIsNotNone(res)
            self.assertEqual(res[0], raw_fixed)
            self.assertAlmostEqual(res[1], 18.5, places=6)

    def test_motion_tracker_swipe_peak_decay_and_dpi_rescale(self):
        tr = mousetool.MotionTracker(input_dpi=3200.0, peak_hold_s=2.5)
        s0 = tr.snapshot(now=10.0)
        self.assertEqual(s0["speed"], 0.0)
        self.assertEqual(s0["peak"], 0.0)
        self.assertFalse(s0["active"])
        self.assertEqual(s0["dpi"], 3200.0)

        # Start swipe: 32 counts/ms at 3200 DPI => 10.0 normalized c/ms
        tr.record_delta(32, 0, 1.0, now=10.0)
        s1 = tr.snapshot(now=10.005)
        self.assertTrue(s1["active"])
        self.assertAlmostEqual(s1["speed"], 10.0, places=2)
        self.assertAlmostEqual(s1["peak"], 10.0, places=2)
        self.assertEqual(s1["swipe_id"], 1)

        # Ramp up swipe to 30 c/ms over several 10ms steps
        for step in range(1, 8):
            tr.record_delta(960, 0, 10.0, now=10.0 + step * 0.01)
        s2 = tr.snapshot(now=10.08)
        self.assertTrue(s2["active"])
        self.assertGreater(s2["peak"], 28.0)
        self.assertAlmostEqual(s2["swipe_min"], 10.0, places=1)
        self.assertEqual(s2["swipe_max"], s2["peak"])
        self.assertEqual(s2["swipe_id"], 1)

        # Draft DPI override: 1600 DPI should double normalized speed/peak vs 3200 DPI
        s2_1600 = tr.snapshot(now=10.08, dpi=1600.0)
        self.assertEqual(s2_1600["dpi"], 1600.0)
        self.assertAlmostEqual(s2_1600["speed"], s2["speed"] * 2.0, places=1)
        self.assertAlmostEqual(s2_1600["peak"], s2["peak"] * 2.0, places=1)

        # After 400ms idle: speed decays to 0 while peak remains held
        s3 = tr.snapshot(now=10.50)
        self.assertFalse(s3["active"])
        self.assertEqual(s3["speed"], 0.0)
        self.assertAlmostEqual(s3["peak"], s2["peak"], places=2)
        self.assertGreater(s3["peak_age_ms"], 300)

        # New swipe after swipe_gap_s replaces peak and increments swipe_id
        tr.record_delta(16, 0, 1.0, now=11.0)  # 5.0 c/ms
        s4 = tr.snapshot(now=11.01)
        self.assertEqual(s4["swipe_id"], 2)
        self.assertAlmostEqual(s4["peak"], 5.0, places=2)
        self.assertAlmostEqual(s4["swipe_min"], 5.0, places=2)

        # After peak_hold_s elapses without motion, peak clears cleanly
        s5 = tr.snapshot(now=14.0)
        self.assertEqual(s5["speed"], 0.0)
        self.assertEqual(s5["peak"], 0.0)
        self.assertEqual(s5["swipe_max"], 0.0)

    def test_reconnect_waits_for_reader_ownership(self):
        entered = threading.Event()
        release = threading.Event()
        replacement = threading.Event()

        def discover():
            if entered.is_set():
                replacement.set()
            else:
                entered.set()
                release.wait(3)
            return []

        hub = mousetool.MotionHub(discover, None, lambda: 1000)
        hub.subscribe()
        self.assertTrue(entered.wait(1))
        closing = threading.Thread(target=hub.unsubscribe)
        closing.start()
        self.assertTrue(hub._stop.wait(1))
        joining = threading.Thread(target=hub.subscribe)
        joining.start()
        release.set()
        closing.join(2)
        joining.join(2)
        try:
            self.assertTrue(replacement.wait(1))
            self.assertTrue(hub.running)
            self.assertEqual(hub.subscriber_count, 1)
        finally:
            hub.unsubscribe()
        self.assertFalse(hub.running)

    def test_dpi_change_preserves_history_and_smoothing_at_fixed_draft(self):
        tracker = mousetool.MotionTracker(input_dpi=2000)
        tracker.record_speed(20, now=10)
        before = tracker.snapshot(now=10, dpi=2000)
        tracker.set_input_dpi(1000)
        self.assertEqual(tracker.snapshot(now=10, dpi=2000), before)
        tracker.record_speed(40, dt_ms=2, now=10.002)
        self.assertEqual(tracker.snapshot(now=10.002, dpi=2000)["speed"], 20)

    def test_maccel_only_stable_value_has_unknown_freshness(self):
        with tempfile.NamedTemporaryFile() as dev:
            hub = mousetool.MotionHub(lambda: [], dev.name, lambda: 2000)
            with mock.patch.object(mousetool, "_read_maccel_fd", return_value=(123, 20.0)):
                hub.subscribe()
                try:
                    deadline = time.monotonic() + 1
                    while hub.snapshot().get("last_speed") is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertEqual(hub.snapshot(dpi=1000)["last_speed"], 40)
                    time.sleep(.4)
                    snap = hub.snapshot()
                    self.assertEqual(snap["last_speed"], 20)
                    self.assertFalse(snap["active"])
                    self.assertEqual(snap["peak"], 0)
                finally:
                    hub.unsubscribe()

    def test_motion_hub_lazy_lifecycle_and_evdev_fifo(self):
        self._check_evdev_fifo(raw=True)

    def test_loaded_module_without_speed_device_excludes_evdev_output(self):
        self._check_evdev_fifo(raw=False)

    def test_short_kernel_read_excludes_evdev_batch(self):
        with tempfile.NamedTemporaryFile() as dev:
            dev.write(b"x")
            dev.flush()
            self._check_evdev_fifo(raw=False, kernel_device=dev.name)

    def _check_evdev_fifo(self, raw, kernel_device=None):
        with tempfile.TemporaryDirectory() as tmp:
            fifo = os.path.join(tmp, "event99")
            os.mkfifo(fifo)
            # Open write end non-blocking only after reader opens; use O_RDWR to keep FIFO open cleanly
            wfd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            hub = mousetool.MotionHub(
                evdev_discover=lambda: [fifo],
                maccel_dev=kernel_device,
                raw_evdev_available=lambda: raw,
                dpi_provider=lambda: 1000.0,
            )
            try:
                self.assertFalse(hub.running)
                self.assertEqual(hub.subscriber_count, 0)

                hub.subscribe()
                self.assertTrue(hub.running)
                self.assertEqual(hub.subscriber_count, 1)

                # Wait briefly for initial device scan
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline and hub.snapshot()["devices"] != 1:
                    time.sleep(0.01)
                initial_source = "maccel+evdev" if kernel_device else ("evdev" if raw else "none")
                self.assertEqual(hub.snapshot()["source"], initial_source)
                self.assertEqual(hub.snapshot()["devices"], 1)

                # Inject two EV_REL + SYN_REPORT frames separated by 2 ms (dx=40 => 20 c/ms at 1000 DPI)
                pkt = (
                    self._pack_ev(200, 0, mousetool.EV_REL, mousetool.REL_X, 1)
                    + self._pack_ev(200, 0, mousetool.EV_SYN, mousetool.SYN_REPORT, 0)
                    + self._pack_ev(200, 2000, mousetool.EV_REL, mousetool.REL_X, 40)
                    + self._pack_ev(200, 2000, mousetool.EV_SYN, mousetool.SYN_REPORT, 0)
                )
                os.write(wfd, pkt)

                deadline = time.monotonic() + 1.0
                snap = hub.snapshot()
                while time.monotonic() < deadline and snap["peak"] == 0:
                    time.sleep(0.01)
                    snap = hub.snapshot()
                self.assertAlmostEqual(snap["peak"], 20.0 if raw else 0.0, places=1)
                self.assertEqual(snap["source"], "evdev" if raw else "none")

                hub.unsubscribe()
                self.assertEqual(hub.subscriber_count, 0)
                self.assertFalse(hub.running)
            finally:
                os.close(wfd)

    def test_motion_sse_endpoint_cors_framing_and_dpi_param(self):
        hub = mousetool.MotionHub(
            evdev_discover=lambda: [],
            maccel_dev=None,
            dpi_provider=lambda: 2000.0,
        )
        # Pre-seed tracker with a known swipe so the initial SSE frame carries live values
        now = time.monotonic()
        hub._tracker.set_input_dpi(2000.0)
        hub._tracker.record_speed(15.0, dt_ms=10.0, now=now)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), mousetool.Api)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(mousetool, "MOTION_HUB", hub):
                # 1) Evil origin rejected
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/motion", headers={"Origin": "https://evil.example"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 403)
                c.close()

                # 2) Invalid dpi rejected with 400
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/motion?dpi=999999", headers={"Origin": "null"})
                r = c.getresponse()
                r.read()
                self.assertEqual(r.status, 400)
                c.close()

                # 3) Valid SSE from file:// (Origin: null) with ?dpi=1000 (2x rescale vs 2000 DPI)
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/motion?dpi=1000", headers={"Origin": "null"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "null")
                self.assertIn("text/event-stream", r.getheader("Content-Type", ""))
                self.assertIn("no-cache", r.getheader("Cache-Control", ""))
                line = r.fp.readline().decode().strip()
                blank = r.fp.readline()
                self.assertTrue(line.startswith("data: "), f"unexpected SSE line: {line!r}")
                self.assertEqual(blank, b"\n")
                frame = json.loads(line[6:])
                self.assertEqual(frame["dpi"], 1000.0)
                self.assertAlmostEqual(frame["peak"], 30.0, places=1)
                self.assertIn("speed", frame)
                self.assertIn("swipe_min", frame)
                self.assertIn("swipe_max", frame)
                r.close()
                c.close()

                # 4) Alias /api/motion/stream with localhost origin
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                c.request("GET", "/api/motion/stream", headers={"Origin": "http://127.0.0.1:8766"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "http://127.0.0.1:8766")
                line = r.fp.readline().decode().strip()
                self.assertTrue(line.startswith("data: "))
                r.close()
                c.close()

                # Wait briefly for handler threads to observe closed sockets and unsubscribe
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and hub.subscriber_count > 0:
                    time.sleep(0.02)
                self.assertEqual(hub.subscriber_count, 0)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
