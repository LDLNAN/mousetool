#!/usr/bin/env python3
"""Tests for the shared devices/ registry, udev rule generator, Razer fallback guard,
wired Viper Mini (1532:008a) Tier 1 read-only slice, and Python/WebHID discovery parity.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mousetool
from mousetool import battery as battery_mod
from mousetool import cli as cli_mod
from mousetool import hid_discovery as hid_mod
from mousetool.drivers import razer as razer_mod
from mousetool.registry import (
    DEFAULT_REGISTRY_PATH,
    DEFAULT_SCHEMA_PATH,
    DeviceRegistry,
    ReadOnlyTierError,
    assert_operation_allowed,
    get_registry,
    is_razer_cmd_allowed,
    load_registry_raw,
    validate_registry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _razer_90b_rd():
    """Minimal HID report descriptor with a 90-byte feature report (Report ID 0)."""
    return bytes([
        0x06, 0x00, 0xFF,  # Usage Page (Vendor 0xFF00)
        0x09, 0x01,        # Usage (0x01)
        0xA1, 0x01,        # Collection (Application)
        0x75, 0x08,        #   Report Size (8)
        0x95, 0x5A,        #   Report Count (90)
        0x09, 0x01,        #   Usage (0x01)
        0xB1, 0x02,        #   Feature (Data, Var, Abs)
        0xC0,              # End Collection
    ])


def _egg8k_rd():
    return bytes([
        0x06, 0x01, 0xFF,  # Usage Page 0xFF01
        0x09, 0x02,        # Usage 0x02
        0xA1, 0x01,
        0x85, 0xA0, 0x75, 0x08, 0x96, 0x10, 0x04, 0x09, 0x01, 0xB1, 0x02,
        0x85, 0xA1, 0x75, 0x08, 0x95, 0x3F, 0x09, 0x02, 0xB1, 0x02,
        0xC0,
    ])


def _logi_hidpp_rd():
    return bytes([
        0x06, 0x00, 0xFF,  # Usage Page 0xFF00
        0x09, 0x01,
        0xA1, 0x01,
        0x85, 0x10, 0x75, 0x08, 0x95, 0x06, 0x09, 0x01, 0x91, 0x02,
        0x85, 0x11, 0x75, 0x08, 0x95, 0x13, 0x09, 0x02, 0x91, 0x02,
        0xC0,
    ])


def _pulsar_pro_rd():
    return bytes([
        0x05, 0x01,        # Usage Page 0x0001
        0x09, 0x00,        # Usage 0x0000
        0xA1, 0x01,
        0x75, 0x08, 0x95, 0x3F, 0x09, 0x01, 0x81, 0x02,
        0x75, 0x08, 0x95, 0x3F, 0x09, 0x02, 0x91, 0x02,
        0xC0,
    ])


def _keyboard_only_rd():
    return bytes([
        0x05, 0x01,        # Generic Desktop
        0x09, 0x06,        # Keyboard
        0xA1, 0x01,
        0x75, 0x08, 0x95, 0x08, 0x09, 0x01, 0x81, 0x02,
        0xC0,
    ])


class TestDeviceRegistrySchemaAndInvariants(unittest.TestCase):
    def test_schema_file_and_registry_valid(self):
        schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        doc = load_registry_raw()
        self.assertEqual(validate_registry(doc), [])
        reg = DeviceRegistry(doc)
        self.assertGreaterEqual(len(reg.models), 70)
        self.assertGreaterEqual(len(reg.matches), 120)

    def test_source_pins_and_distinct_namespaces(self):
        doc = load_registry_raw()
        reg = DeviceRegistry(doc)
        # Every source has https URL + hex commit revision + licence
        for key, src in doc["sources"].items():
            self.assertTrue(src["url"].startswith("https://"), key)
            self.assertRegex(src["revision"], r"^[0-9a-f]{7,40}$", key)
            self.assertTrue(src["licence"].strip(), key)

        # Distinct namespaces: WPID 046d:4079 must never match USB or Bluetooth lookups
        self.assertIsNone(reg.resolve_match(0x046D, 0x4079, bus="usb"))
        self.assertIsNone(reg.resolve_match(0x046D, 0x4079, bus="bluetooth"))
        self.assertIsNone(reg.find_match_any_bus(0x046D, 0x4079))

        # Bluetooth-only MX Master 3 (046d:b023) must not match usb bus
        self.assertIsNone(reg.resolve_match(0x046D, 0xB023, bus="usb"))
        bt_res = reg.resolve_match(0x046D, 0xB023, bus="bluetooth")
        self.assertIsNotNone(bt_res)
        self.assertEqual(bt_res[0]["id_namespace"], "bluetooth_pid")

        # Validator rejects mismatched bus / id_namespace
        bad = copy.deepcopy(doc)
        bad["matches"][0]["bus"] = "receiver_child"
        self.assertTrue(any("logitech_wpid" in e for e in validate_registry(bad)))

    def test_rejects_dangling_profiles_and_silent_legacy_promotion(self):
        doc = load_registry_raw()

        # Verify no legacy migrated profile in repo claims hardware_verified without evidence
        for pid_key, prof in doc["profiles"].items():
            if prof["legacy_migrated"]:
                self.assertNotEqual(
                    prof["evidence_status"],
                    "hardware_verified",
                    f"legacy profile {pid_key} was silently promoted",
                )
            if not prof["legacy_migrated"] and prof["evidence"] is None:
                self.assertLess(prof["tier"], 2)
                self.assertFalse(prof["write_enabled"])

        # Synthetic dangling profile rejected
        bad_dangling = copy.deepcopy(doc)
        bad_dangling["matches"][0]["profile_id"] = "nonexistent_profile"
        self.assertTrue(any("unknown profile_id" in e for e in validate_registry(bad_dangling)))

        # Synthetic silent promotion of legacy entry rejected
        bad_promo = copy.deepcopy(doc)
        bad_promo["profiles"]["razer_legacy_wired"]["evidence_status"] = "hardware_verified"
        bad_promo["profiles"]["razer_legacy_wired"]["evidence"] = None
        self.assertTrue(any("hardware_verified" in e for e in validate_registry(bad_promo)))

        # Synthetic unverified Tier 2 write promotion on Viper Mini rejected
        bad_mini = copy.deepcopy(doc)
        bad_mini["profiles"]["razer_viper_mini_ro"]["tier"] = 2
        bad_mini["profiles"]["razer_viper_mini_ro"]["write_enabled"] = True
        self.assertTrue(any("cannot be Tier 2" in e for e in validate_registry(bad_mini)))

        # Synthetic Tier 1 with write=true rejected
        bad_t1 = copy.deepcopy(doc)
        bad_t1["profiles"]["razer_viper_mini_ro"]["operations"]["dpi"]["write"] = True
        self.assertTrue(any("write=false" in e for e in validate_registry(bad_t1)))


class TestUdevRuleGenerator(unittest.TestCase):
    def test_udev_rules_check_is_byte_identical(self):
        res = subprocess.run(
            [sys.executable, "-B", str(REPO_ROOT / "tools" / "generate_udev_rules.py"), "--check"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr or res.stdout)

    def test_udev_exclusions_and_viper_mini_inclusion(self):
        rules = (REPO_ROOT / "platform" / "linux" / "60-mousetool.rules").read_text(encoding="utf-8")
        # Wired Viper Mini (Tier 1 USB) IS included
        self.assertIn('ATTRS{idVendor}=="1532", ATTRS{idProduct}=="008a"', rules)
        # WPID child (046d:4079) NEVER emits a rule
        self.assertNotIn('idProduct}=="4079"', rules)
        # Bluetooth-only entries (MX Master 3/3S) NEVER emit USB udev rules
        self.assertNotIn('idProduct}=="b023"', rules)
        self.assertNotIn('idProduct}=="b034"', rules)
        # Tier 0 catalog-only entry (DeathAdder Elite 1532:005c) NEVER emits a rule
        self.assertNotIn('idProduct}=="005c"', rules)
        # Keyboard (Wooting 60HE 31e3:1310) NEVER emits a rule
        self.assertNotIn('idVendor}=="31e3"', rules)
        # No blanket vendor-only rules without idProduct
        for line in rules.splitlines():
            if line.startswith("KERNEL=="):
                self.assertIn("ATTRS{idVendor}==", line)
                self.assertIn("ATTRS{idProduct}==", line)


class TestRazerFallbackGuardAndViperMiniSlice(unittest.TestCase):
    def test_unknown_razer_vid_never_selects_razer_driver_and_sends_zero_frames(self):
        rd = _razer_90b_rd()
        # Supported discovery rejects unknown Razer PID even with 90-byte feature report
        entry, split = hid_mod.classify_hid_node(0x1532, 0x9999, rd=rd, name="Unknown Razer", allow_generic=False)
        self.assertIsNone(entry)
        self.assertIsNone(split)

        # Catalog-only Tier 0 DeathAdder Elite (1532:005c) also rejected from Razer driver
        entry_dae, _ = hid_mod.classify_hid_node(0x1532, 0x005C, rd=rd, name="DeathAdder Elite", allow_generic=False)
        self.assertIsNone(entry_dae)

        # Generic fallback yields Tier 0 · Pointer only (write_enabled=False)
        t0, _ = hid_mod.classify_hid_node(0x1532, 0x9999, rd=rd, name="Unknown Razer", allow_generic=True)
        self.assertIsNotNone(t0)
        self.assertEqual(t0["driver"], "generic")
        self.assertEqual(t0["tier"], 0)
        self.assertEqual(t0["tier_label"], "Tier 0 · Pointer only")
        self.assertFalse(t0["write_enabled"])

        # Dispatch guard blocks any vendor frame on Tier 0 with 0 ioctl calls
        fake_fcntl = mock.Mock()
        with mock.patch.object(razer_mod, "fcntl", fake_fcntl, create=True):
            with self.assertRaises(ReadOnlyTierError):
                razer_mod.razer_read_status(99, t0)
            with self.assertRaises(ReadOnlyTierError):
                razer_mod.xfer(99, 0x00, 0x81, 0x02, (), txn=0x1F, meta=t0)
            self.assertEqual(fake_fcntl.ioctl.call_count, 0)

    def test_viper_mini_tier1_getters_only_and_write_blocking_at_dispatch(self):
        rd = _razer_90b_rd()
        meta, _ = hid_mod.classify_hid_node(
            0x1532, 0x008A, rd=rd, name="Razer Viper Mini", devpath="/dev/hidraw7", allow_generic=False
        )
        self.assertIsNotNone(meta)
        self.assertEqual(meta["driver"], "razer")
        self.assertEqual(meta["tier"], 1)
        self.assertEqual(meta["tier_label"], "Tier 1 · Read-only")
        self.assertEqual(meta["profile_id"], "razer_viper_mini_ro")
        self.assertFalse(meta["write_enabled"])
        self.assertFalse(meta["wireless"])
        self.assertFalse(meta["hyper"])
        self.assertEqual(meta["dpi_max"], 8500)

        # Verify command allowlist directly
        self.assertTrue(is_razer_cmd_allowed(meta, 0x00, 0x81, 0xFF))   # firmware getter 0xff
        self.assertTrue(is_razer_cmd_allowed(meta, 0x04, 0x85, 0xFF))   # DPI getter 0xff
        self.assertFalse(is_razer_cmd_allowed(meta, 0x00, 0x81, 0x1F))  # wrong txn byte rejected
        self.assertFalse(is_razer_cmd_allowed(meta, 0x04, 0x05, 0xFF))  # DPI setter rejected
        self.assertFalse(is_razer_cmd_allowed(meta, 0x04, 0x86, 0xFF))  # stages getter rejected
        self.assertFalse(is_razer_cmd_allowed(meta, 0x00, 0x85, 0xFF))  # poll getter rejected
        self.assertFalse(is_razer_cmd_allowed(meta, 0x0B, 0x85, 0xFF))  # LOD getter rejected
        self.assertFalse(is_razer_cmd_allowed(meta, 0x07, 0x80, 0xFF))  # battery getter rejected

        # Fake hidraw ioctl transport verifying ONLY 0xff firmware + 0xff DPI getters are sent
        sent_frames = []
        last_req = {}

        def fake_ioctl(_fd, req_code, arg):
            if req_code == razer_mod.HIDIOCSFEATURE:
                pkt = bytes(arg)[1:]
                sent_frames.append(pkt)
                last_req["cls"] = pkt[6]
                last_req["cmd"] = pkt[7]
                last_req["txn"] = pkt[1]
                last_req["arg0"] = pkt[8]
                return 0
            if req_code == razer_mod.HIDIOCGFEATURE:
                # Populate 91-byte reply buffer: status=2 (OK), echo cls/cmd
                arg[0] = 0
                arg[1] = 2  # status OK
                arg[2] = last_req["txn"]
                arg[7] = last_req["cls"]
                arg[8] = last_req["cmd"]
                if (last_req["cls"], last_req["cmd"]) == (0x00, 0x81):
                    arg[9] = 1   # v1.03
                    arg[10] = 3
                elif (last_req["cls"], last_req["cmd"]) == (0x04, 0x85):
                    arg[9] = 0x00
                    arg[10] = (800 >> 8) & 0xFF
                    arg[11] = 800 & 0xFF
                    arg[12] = (1200 >> 8) & 0xFF
                    arg[13] = 1200 & 0xFF
                return 0
            raise AssertionError(f"unexpected ioctl {req_code:#x}")

        with mock.patch.object(razer_mod, "fcntl", mock.Mock(ioctl=fake_ioctl), create=True), \
             mock.patch.object(razer_mod.time, "sleep"):
            status = razer_mod.razer_read_status(99, meta)

        self.assertEqual(len(sent_frames), 2, "Viper Mini status must send ONLY firmware + DPI frames")
        # Frame 0: firmware getter with txn=0xff
        self.assertEqual((sent_frames[0][1], sent_frames[0][6], sent_frames[0][7]), (0xFF, 0x00, 0x81))
        # Frame 1: DPI getter with txn=0xff and storage selector 0x00
        self.assertEqual((sent_frames[1][1], sent_frames[1][6], sent_frames[1][7], sent_frames[1][8]), (0xFF, 0x04, 0x85, 0x00))
        self.assertEqual(status["firmware"], "v1.3")
        self.assertEqual(status["dpi"], [800, 1200])
        self.assertEqual(status["tier"], 1)
        self.assertFalse(status["write_enabled"])
        for forbidden_key in ("serial", "stages", "polling_hz", "lod", "battery_percent", "idle_seconds", "low_battery_percent"):
            self.assertNotIn(forbidden_key, status)

        # ALL mutating commands and disallowed getters must reject at dispatch with 0 ioctl calls
        fake_fcntl = mock.Mock()
        with mock.patch.object(razer_mod, "fcntl", fake_fcntl, create=True):
            for fn in (
                lambda: razer_mod.set_dpi(99, 1600, 1600, 8500, meta=meta),
                lambda: razer_mod.set_stages(99, 1, [(800, 800)], 8500, meta=meta),
                lambda: razer_mod.set_poll(99, 1000, False, meta=meta),
                lambda: razer_mod.set_lod_preset(99, "low", meta=meta),
                lambda: razer_mod.set_lod_async(99, 3, 2, meta=meta),
                lambda: razer_mod.set_idle(99, 300, meta=meta),
                lambda: razer_mod.set_low(99, 20, meta=meta),
                lambda: razer_mod.get_stages(99, meta=meta),
                lambda: razer_mod.get_poll(99, False, meta=meta),
                lambda: razer_mod.get_lod(99, meta=meta),
                lambda: battery_mod._read_mouse_battery(meta),
            ):
                with self.assertRaises(ReadOnlyTierError):
                    fn()
            self.assertEqual(fake_fcntl.ioctl.call_count, 0)

        # CLI write dispatch must also reject BEFORE opening hidraw
        class _Args:
            device = "1532:008a"
            x = 1600
            y = None
            set = "400,800"
            active = 1
            hz = 1000
            preset = "low"
            index = None
            async_vals = None
            idle = 5
            low = None
            debounce = 4
            motion_sync = "on"
            angle_snapping = None
            ripple_control = None
            slamclick = None
            motion_jitter = None
            glass_mode = None
            angle_tuning = None
            force_max_fps = None
            led_liftoff_disabled = None
            left_handed = None
            spdt = None

        with mock.patch.object(hid_mod, "select_mice", return_value=[meta]), \
             mock.patch.object(os, "open") as m_open:
            for cmd_fn in (cli_mod.dpi_cmd, cli_mod.stages_cmd, cli_mod.poll_cmd, cli_mod.lod_cmd, cli_mod.power_cmd, cli_mod.tune_cmd):
                with self.assertRaises(ReadOnlyTierError):
                    cmd_fn(_Args())
            self.assertEqual(m_open.call_count, 0)


class TestPythonAndWebHidDiscoveryParity(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required for WebHID parity check")
    def test_python_and_webhid_agree_on_descriptor_fixtures(self):
        # 1. Run Python classification on descriptor fixtures
        fixtures = [
            ("unknown_razer", 0x1532, 0x9999, _razer_90b_rd()),
            ("catalog_razer_dae", 0x1532, 0x005C, _razer_90b_rd()),
            ("viper_mini", 0x1532, 0x008A, _razer_90b_rd()),
            ("viper_v2_pro", 0x1532, 0x00A5, _razer_90b_rd()),
            ("egg_op1_8k", 0x3367, 0x1964, _egg8k_rd()),
            ("logi_gpx", 0x046D, 0xC092, _logi_hidpp_rd()),
            ("pulsar_pro", 0x3710, 0x5405, _pulsar_pro_rd()),
            ("wooting_kbd", 0x31E3, 0x1310, _keyboard_only_rd()),
        ]
        py_supported = {}
        py_generic = {}
        for key, vid, pid, rd in fixtures:
            e_sup, _ = hid_mod.classify_hid_node(vid, pid, rd=rd, allow_generic=False)
            e_gen, _ = hid_mod.classify_hid_node(vid, pid, rd=rd, allow_generic=True)
            py_supported[key] = None if e_sup is None else {
                "driver": "egg" if e_sup["driver"] == "egg8k" else e_sup["driver"],
                "tier": e_sup["tier"],
                "writeEnabled": e_sup["write_enabled"],
                "profileId": e_sup["profile_id"],
            }
            py_generic[key] = None if e_gen is None else {
                "driver": "egg" if e_gen["driver"] == "egg8k" else e_gen["driver"],
                "tier": e_gen["tier"],
                "writeEnabled": e_gen["write_enabled"],
                "profileId": e_gen["profile_id"],
            }

        # 2. Run real WebHID JS modules in Node.js on the exact same fixtures + test Viper Mini API dispatch!
        node_script = f"""
import fs from "node:fs";
import {{ setRegistry, getSupportedFilters }} from "{(REPO_ROOT / 'web/drivers/registry.js').as_uri()}";
import {{ classifyDevice, groupHidTargets }} from "{(REPO_ROOT / 'web/drivers/classifier.js').as_uri()}";
import {{ makeRazerApi }} from "{(REPO_ROOT / 'web/drivers/razer.js').as_uri()}";

const doc = JSON.parse(fs.readFileSync({json.dumps(str(DEFAULT_REGISTRY_PATH))}, "utf8"));
setRegistry(doc);

const razerCol = [{{ usagePage: 0xff00, usage: 0x01, inputReports: [], outputReports: [], featureReports: [{{ reportId: 0, items: [{{ reportSize: 8, reportCount: 90 }}] }}] }}];
const eggCol = [{{ usagePage: 0xff01, usage: 0x02, inputReports: [], outputReports: [], featureReports: [{{ reportId: 0xa0, items: [{{ reportSize: 8, reportCount: 1040 }}] }}, {{ reportId: 0xa1, items: [{{ reportSize: 8, reportCount: 63 }}] }}] }}];
const logiCol = [{{ usagePage: 0xff00, usage: 0x01, inputReports: [], outputReports: [{{ reportId: 0x10, items: [{{ reportSize: 8, reportCount: 6 }}] }}, {{ reportId: 0x11, items: [{{ reportSize: 8, reportCount: 19 }}] }}], featureReports: [] }}];
const pulsarProCol = [{{ usagePage: 0x0001, usage: 0x00, inputReports: [{{ reportId: 0, items: [{{ reportSize: 8, reportCount: 63 }}] }}], outputReports: [{{ reportId: 0, items: [{{ reportSize: 8, reportCount: 63 }}] }}], featureReports: [] }}];
const kbdCol = [{{ usagePage: 0x0001, usage: 0x06, inputReports: [{{ reportId: 0, items: [{{ reportSize: 8, reportCount: 8 }}] }}], outputReports: [], featureReports: [] }}];

const fixtures = [
  ["unknown_razer", {{ vendorId: 0x1532, productId: 0x9999, productName: "Unknown Razer", collections: razerCol }}],
  ["catalog_razer_dae", {{ vendorId: 0x1532, productId: 0x005c, productName: "DeathAdder Elite", collections: razerCol }}],
  ["viper_mini", {{ vendorId: 0x1532, productId: 0x008a, productName: "Razer Viper Mini", collections: razerCol }}],
  ["viper_v2_pro", {{ vendorId: 0x1532, productId: 0x00a5, productName: "Razer Viper V2 Pro", collections: razerCol }}],
  ["egg_op1_8k", {{ vendorId: 0x3367, productId: 0x1964, productName: "OP1 8k", collections: eggCol }}],
  ["logi_gpx", {{ vendorId: 0x046d, productId: 0xc092, productName: "G Pro X Superlight", collections: logiCol }}],
  ["pulsar_pro", {{ vendorId: 0x3710, productId: 0x5405, productName: "Pulsar Pro", collections: pulsarProCol }}],
  ["wooting_kbd", {{ vendorId: 0x31e3, productId: 0x1310, productName: "Wooting 60HE", collections: kbdCol }}],
];

const simplify = (s) => s ? ({{ driver: s.driver, tier: s.tier, writeEnabled: s.writeEnabled, profileId: s.profileId }}) : null;
const jsSupported = {{}};
const jsGeneric = {{}};
for (const [k, d] of fixtures) {{
  jsSupported[k] = simplify(classifyDevice(d, false));
  jsGeneric[k] = simplify(classifyDevice(d, true));
}}

// Startup auto-attach / reconnect simulation: groupHidTargets(..., true) on unknown Razer + Viper Mini
const sentMini = [];
let lastReq = null;
const fakeMiniDev = {{
  vendorId: 0x1532, productId: 0x008a, productName: "Razer Viper Mini", serialNumber: "MINI-FIXTURE", opened: true,
  collections: razerCol,
  async sendFeatureReport(rid, data) {{
    const u = new Uint8Array(data);
    sentMini.push([rid, u[1], u[6], u[7], u[8]]);
    lastReq = u;
  }},
  async receiveFeatureReport() {{
    const rep = new Uint8Array(90);
    rep[0] = 2; // OK
    rep[1] = lastReq[1];
    rep[6] = lastReq[6];
    rep[7] = lastReq[7];
    if (lastReq[6] === 0x00 && lastReq[7] === 0x81) {{ rep[8] = 1; rep[9] = 3; }}
    if (lastReq[6] === 0x04 && lastReq[7] === 0x85) {{
      rep[8] = 0; rep[9] = 800 >> 8; rep[10] = 800 & 0xff; rep[11] = 1200 >> 8; rep[12] = 1200 & 0xff;
    }}
    return new DataView(rep.buffer);
  }},
}};
const miniSpec = classifyDevice(fakeMiniDev, false);
const miniSess = {{
  id: "mini", dev: fakeMiniDev, vid: 0x1532, pid: 0x008a, shortName: "Viper Mini", name: miniSpec.info.name,
  tier: miniSpec.tier, tierLabel: miniSpec.tierLabel, writeEnabled: miniSpec.writeEnabled,
  operations: miniSpec.operations, caps: miniSpec.caps, info: miniSpec.info,
  btnLayer: 0, queue: Promise.resolve(),
}};
const miniApi = makeRazerApi(miniSess, {{ isConnected: () => true, log: () => {{}} }});
const fw = await miniApi.firmware();
const dpi = await miniApi.getDpi();
const serial = await miniApi.serial();
const stages = await miniApi.getStages();
const poll = await miniApi.getPoll();
const lod = await miniApi.getLod();
const batt = await miniApi.battery();

const blockedResults = [];
const writeAttempts = [
  ["setDpi", () => miniApi.setDpi(1600, 1600)],
  ["setStages", () => miniApi.setStages(1, [{{ x: 800, y: 800 }}])],
  ["setPoll", () => miniApi.setPoll(1000)],
  ["setLodPreset", () => miniApi.setLodPreset(0)],
  ["setLodAsync", () => miniApi.setLodAsync(3, 2)],
  ["setButton", () => miniApi.setButton(2, {{ fn: 1, size: 1, data: [2] }})],
  ["setIdle", () => miniApi.setIdle(300)],
  ["setLow", () => miniApi.setLow(50)],
  ["setDongleLed", () => miniApi.setDongleLed(1)],
  ["pair", () => miniApi.pair(0x00a6)],
  ["unpair", () => miniApi.unpair(0x00a6)],
];
for (const [name, fn] of writeAttempts) {{
  try {{
    await fn();
    blockedResults.push([name, false]);
  }} catch (e) {{
    blockedResults.push([name, e.kind === "readonly"]);
  }}
}}

// Check reconnect grouping on unknown Razer + Viper Mini
const reconnectTargets = groupHidTargets([
  {{ vendorId: 0x1532, productId: 0x9999, productName: "Unknown Razer", collections: razerCol }},
  fakeMiniDev,
], true).map((t) => ({{ pid: t.dev.productId, driver: t.spec.driver, tier: t.spec.tier, writeEnabled: t.spec.writeEnabled }}));

const filters = getSupportedFilters().map((f) => `${{f.vendorId.toString(16).padStart(4, "0")}}:${{f.productId.toString(16).padStart(4, "0")}}`).sort();

console.log(JSON.stringify({{
  jsSupported, jsGeneric,
  mini: {{ fastKeys: miniApi.fastKeys, slowKeys: miniApi.slowKeys, fw, dpi, serial, stages, poll, lod, batt, sentMini, blockedResults }},
  reconnectTargets,
  filters,
}}));
"""
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        js_out = json.loads(proc.stdout)

        # Python and WebHID must agree 100% on every descriptor fixture!
        self.assertEqual(py_supported, js_out["jsSupported"])
        self.assertEqual(py_generic, js_out["jsGeneric"])

        # Viper Mini WebHID API sent ONLY 2 frames (both 0xff) and blocked all 11 setters
        mini = js_out["mini"]
        self.assertEqual(mini["fastKeys"], ["dpi"])
        self.assertEqual(mini["slowKeys"], [])
        self.assertEqual(mini["fw"], "v1.3")
        self.assertEqual(mini["dpi"], [800, 1200])
        self.assertEqual(mini["sentMini"], [
            [0, 0xFF, 0x00, 0x81, 0x00],
            [0, 0xFF, 0x04, 0x85, 0x00],
        ])
        for name, ok in mini["blockedResults"]:
            self.assertTrue(ok, f"WebHID Viper Mini {name} was not blocked with readonly error")

        # Reconnect path preserves Tier 1 for Viper Mini and Tier 0 for unknown Razer
        by_pid = {t["pid"]: t for t in js_out["reconnectTargets"]}
        self.assertEqual(by_pid[0x008A], {"pid": 0x008A, "driver": "razer", "tier": 1, "writeEnabled": False})
        self.assertEqual(by_pid[0x9999], {"pid": 0x9999, "driver": "generic", "tier": 0, "writeEnabled": False})

        # Supported WebHID filters match udev-eligible USB VID:PIDs exactly
        reg = get_registry()
        expected_usb = sorted(f"{m['vid']}:{m['pid']}" for m, _, _ in reg.udev_usb_matches())
        self.assertEqual(js_out["filters"], expected_usb)
        self.assertIn("1532:008a", js_out["filters"])
        self.assertNotIn("046d:b023", js_out["filters"])
        self.assertNotIn("046d:b034", js_out["filters"])
        self.assertNotIn("1532:005c", js_out["filters"])
        self.assertNotIn("31e3:1310", js_out["filters"])


if __name__ == "__main__":
    unittest.main()
