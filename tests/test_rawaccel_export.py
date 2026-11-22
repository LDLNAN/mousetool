#!/usr/bin/env python3
"""Tests for rawaccel_export (MT-003: offline Raw Accel v1.7.1 preview/export).

Nothing here touches devices, sysfs, the running helper service or Windows.
Run: python3 -m unittest -v test_rawaccel_export.py
"""
import copy
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from mousetool.accel import rawaccel_export as rx

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOL = rx.DEFAULT_TOLERANCE


def cfg(**kw):
    out = {**rx.DEFAULTS, **kw}
    if out.get("MODE") == "lookup" and "LUT_POINTS" not in kw:
        out["LUT_POINTS"] = [[0.0, 1.0], [5.0, 1.0], [15.0, 1.4], [30.0, 1.6]]
    return out


# One fixture per behaviour that matters for the mapping, all five modes covered.
FIXTURES = {
    "linear_uncapped": cfg(MODE="linear", ACCEL=0.05, OFFSET=2, OUTPUT_CAP=0),
    "linear_capped": cfg(MODE="linear", ACCEL=0.1, OFFSET=1.5, OUTPUT_CAP=2.5, SENS_MULT=0.8, YX_RATIO=1.2,
                         INPUT_DPI=1600, ANGLE_ROTATION=-3),
    "linear_cap_below_1": cfg(MODE="linear", ACCEL=0.2, OFFSET=0, OUTPUT_CAP=0.6),
    "linear_accel_zero": cfg(MODE="linear", ACCEL=0),
    "natural": cfg(MODE="natural", DECAY_RATE=0.1, OFFSET=3, LIMIT=1.5, SENS_MULT=1.1, INPUT_DPI=800),
    "natural_steep": cfg(MODE="natural", DECAY_RATE=2, OFFSET=0, LIMIT=10, ACCEL=0.03),
    "natural_limit_1": cfg(MODE="natural", LIMIT=1),
    "synchronous": cfg(MODE="synchronous", GAMMA=1, SMOOTH=0.5, MOTIVITY=1.5, SYNC_SPEED=5),
    "synchronous_clamp": cfg(MODE="synchronous", GAMMA=2, SMOOTH=0, MOTIVITY=3, SYNC_SPEED=10, YX_RATIO=0.9),
    "synchronous_sharp_edge": cfg(MODE="synchronous", GAMMA=0.5, SMOOTH=1 / 32, MOTIVITY=2, SYNC_SPEED=4),
    "synchronous_smooth_1": cfg(MODE="synchronous", GAMMA=0.3, SMOOTH=1, MOTIVITY=10, SYNC_SPEED=20),
    "no_accel": cfg(MODE="no_accel", SENS_MULT=0.5, YX_RATIO=2, ANGLE_ROTATION=45, INPUT_DPI=3200),
    "lookup_ramp": cfg(MODE="lookup", ACCEL=0.005, SENS_MULT=0.9, YX_RATIO=1.15, INPUT_DPI=1600, ANGLE_ROTATION=2.5,
                       LUT_POINTS=[[0.0, 1.0], [5.0, 1.0], [15.0, 1.4], [30.0, 1.6]]),
    "lookup_scurve": cfg(MODE="lookup", ACCEL=0.005,
                         LUT_POINTS=[[1.5, 0.7], [6.0, 0.9], [12.5, 1.35], [22.0, 1.8], [40.0, 1.8]]),
}


def assert_rawaccel_matches_maccel(tc, chk, exact=1e-12):
    """Raw Accel == maccel formula (and pipeline) everywhere; any Studio-preview gap must be flagged as such."""
    tc.assertLessEqual(chk["maccel_formula_max_rel_error"], exact, chk)
    tc.assertLessEqual(chk["max_rel_error_outside_studio_rules"], exact, chk)
    tc.assertLessEqual(chk["pipeline_max_rel_error"], exact, chk)
    tc.assertEqual(chk["equivalent"], chk["max_rel_error"] <= TOL and chk["unverified_samples"] == 0, chk)
    if chk["equivalent"]:
        tc.assertTrue(chk["result"].startswith("EQUIVALENT"), chk)
        return
    if chk["max_rel_error"] > TOL:  # only ever the Studio sync-speed shortcut
        tc.assertGreater(chk["studio_rule_samples"], 0, chk)
        tc.assertIn("only inside the Studio preview's sync-speed shortcut", chk["result"])
    if chk["unverified_samples"]:  # only ever natural-mode cancellation
        tc.assertEqual(chk["rawaccel_mode"], "natural", chk)
        tc.assertGreater(chk["max_fp_spread"], TOL, chk)
        tc.assertIn("unverifiable at", chk["result"])


def prof_of(settings):
    return settings["profiles"][0]


def args_x(settings):
    return prof_of(settings)[rx.K_ARGS_X]


class TestPurity(unittest.TestCase):
    def test_imports_without_linux_only_modules(self):
        code = ("import sys\n"
                "for m in ('fcntl', 'grp', 'select', 'socket', 'termios'): sys.modules[m] = None\n"
                f"sys.path.insert(0, {REPO!r})\n"
                "from mousetool.accel import rawaccel_export as r\n"
                "print(r.export_document(r.DEFAULTS)['schema_version'])\n")
        res = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "1")

    def test_constants_match_mousetool_and_ui(self):
        import mousetool
        self.assertEqual(rx.MODES, mousetool.MODES)
        self.assertEqual(rx.PARAMS, mousetool.PARAMS)
        with open(os.path.join(REPO, "web", "accel_graph.js")) as f:
            js_src = f.read()
        block = re.search(r"const MACCEL_DEFAULTS = \{(.*?)\};", js_src, re.S).group(1)
        ui = dict(re.findall(r"(\w+):\s*(\"?[\w.]+\"?)", block))
        ui = {k: (v.strip('"') if v.startswith('"') else float(v)) for k, v in ui.items()}
        self.assertEqual(ui, rx.DEFAULTS)
        self.assertEqual(set(rx.DEFAULTS) - {"MODE"}, set(rx.PARAMS))
        self.assertEqual(set().union(*rx.MODE_PARAMS.values()) | set(rx.SHARED_PARAMS), set(rx.PARAMS))


class TestForwardMapping(unittest.TestCase):
    def test_every_fixture_maps_validates_and_reports_every_param(self):
        for name, c in FIXTURES.items():
            with self.subTest(name):
                settings, mapping = rx.maccel_to_rawaccel(c)
                self.assertEqual(rx.validate_settings(settings), [])
                self.assertEqual([r["param"] for r in mapping["params"]], list(rx.PARAMS))
                for r in mapping["params"]:
                    self.assertIn(r["status"], ("exact", "lossy"))
                    if r["status"] == "lossy":
                        self.assertTrue(any(r["note"] == s for s in mapping["lossy"]))
                self.assertEqual(prof_of(settings)[rx.K_ARGS_Y], args_x(settings))
                self.assertEqual(settings["devices"], [])
                self.assertEqual(settings["version"], "1.7.0")

    def test_linear_is_classic_exponent_2_legacy_output_cap(self):
        s, m = rx.maccel_to_rawaccel(FIXTURES["linear_capped"])
        a, p = args_x(s), prof_of(s)
        self.assertEqual((a["mode"], a[rx.K_GAIN], a["exponentClassic"], a[rx.K_CAP_MODE]), ("classic", False, 2.0, "output"))
        self.assertEqual((a["acceleration"], a["inputOffset"], a[rx.K_CAP]["y"]), (0.1, 1.5, 2.5))
        self.assertEqual((p[rx.K_OUT_DPI], p[rx.K_YX], p[rx.K_ROT]), (800.0, 1.2, -3.0))
        self.assertEqual(s["defaultDeviceConfig"][rx.K_DPI], 1600)
        self.assertIsInstance(s["defaultDeviceConfig"][rx.K_DPI], int)
        self.assertEqual(m["lossy"], [])
        self.assertFalse(m["flat"])
        self.assertIn("accel/linear.h", m["formula"])

    def test_full_profile_shape_matches_v171_json_names(self):
        s, _ = rx.maccel_to_rawaccel(FIXTURES["natural"])
        expected_args = {
            "mode": "natural", "Gain / Velocity": True, "inputOffset": 3.0, "outputOffset": 0.0,
            "acceleration": 0.005, "decayRate": 0.1, "gamma": 1.0, "motivity": 1.5, "exponentClassic": 2.0,
            "scale": 1.0, "exponentPower": 0.05, "limit": 1.5, "syncSpeed": 5.0, "smooth": 0.5,
            "Cap / Jump": {"x": 15.0, "y": 0.0}, "Cap mode": "output", "data": [],
        }
        expected = {
            "name": "mousetool",
            "Stretches domain for horizontal vs vertical inputs": {"x": 1.0, "y": 1.0},
            "Stretches accel range for horizontal vs vertical inputs": {"x": 1.0, "y": 1.0},
            "Whole or horizontal accel parameters": expected_args,
            "Vertical accel parameters": expected_args,
            "Input speed calculation parameters": {
                "Whole/combined accel (set false for 'by component' mode)": True, "lpNorm": 2.0,
                "Time in ms after which an input is weighted at half its original value.": 0.0,
                "Time in ms after which scale is weighted at half its original value.": 0.0,
                "Time in ms after which an output is weighted at half its original value.": 0.0,
            },
            "Output DPI": 1100.0, "Y/X output DPI ratio (vertical sens multiplier)": 1.0,
            "L/R output DPI ratio (left sens multiplier)": 1.0, "U/D output DPI ratio (up sens multiplier)": 1.0,
            "Degrees of rotation": 0.0, "Degrees of angle snapping": 0.0, "Input Speed Cap": 0.0,
        }
        self.assertEqual(prof_of(s), expected)
        self.assertEqual(s["defaultDeviceConfig"], {
            "disable": False, "Use constant time interval based on polling rate": False,
            "DPI (normalizes input speed unit: counts/ms -> in/s)": 800,
            "Polling rate Hz (keep at 0 for automatic adjustment)": 0})
        self.assertEqual(list(s), ["### Accel modes ###", "### Cap modes ###", "version", "defaultDeviceConfig",
                                   "profiles", "devices"])

    def test_natural_is_gain_form_and_synchronous_is_sensitivity_form(self):
        a = args_x(rx.maccel_to_rawaccel(FIXTURES["natural_steep"])[0])
        self.assertEqual((a["mode"], a[rx.K_GAIN], a["decayRate"], a["limit"]), ("natural", True, 2.0, 10.0))
        a = args_x(rx.maccel_to_rawaccel(FIXTURES["synchronous_clamp"])[0])
        self.assertEqual((a["mode"], a[rx.K_GAIN]), ("synchronous", False))
        self.assertEqual((a["gamma"], a["smooth"], a["motivity"], a["syncSpeed"]), (2.0, 0.0, 3.0, 10.0))

    def test_no_accel(self):
        s, m = rx.maccel_to_rawaccel(FIXTURES["no_accel"])
        self.assertEqual(args_x(s)["mode"], "noaccel")
        self.assertEqual((prof_of(s)[rx.K_OUT_DPI], prof_of(s)[rx.K_YX], prof_of(s)[rx.K_ROT]), (500.0, 2.0, 45.0))
        self.assertEqual(s["defaultDeviceConfig"][rx.K_DPI], 3200)
        self.assertEqual(len(m["lossy"]), 1)  # only the app-default ACCEL=0, inactive here
        self.assertIn("ACCEL=0", m["lossy"][0])

    def test_accel_zero_is_reported_lossy_never_silently_changed(self):
        for mode in rx.MODES:
            with self.subTest(mode):
                s, m = rx.maccel_to_rawaccel(cfg(MODE=mode, ACCEL=0))
                self.assertEqual(args_x(s)["acceleration"], 0.005)
                row = next(r for r in m["params"] if r["param"] == "ACCEL")
                self.assertEqual(row["status"], "lossy")
                self.assertEqual(len(m["lossy"]), 1)
                self.assertIn("ACCEL=0", m["lossy"][0])
        s, m = rx.maccel_to_rawaccel(FIXTURES["linear_accel_zero"])
        self.assertEqual((args_x(s)["mode"], m["flat"]), ("noaccel", True))
        self.assertTrue(any("ACCEL=0 is flat" in n for n in m["notes"]))

    def test_natural_limit_1_exports_noaccel_with_note(self):
        s, m = rx.maccel_to_rawaccel(FIXTURES["natural_limit_1"])
        self.assertEqual((args_x(s)["mode"], args_x(s)["limit"], m["flat"]), ("noaccel", 1.0, True))
        self.assertTrue(any("LIMIT=1" in n for n in m["notes"]))

    def test_non_integer_dpi_is_lossy(self):
        s, m = rx.maccel_to_rawaccel(cfg(INPUT_DPI=1234.5))
        self.assertEqual(s["defaultDeviceConfig"][rx.K_DPI], 1234)  # round-half-even
        self.assertEqual(len(m["lossy"]), 2)  # ACCEL=0 default + DPI
        self.assertTrue(any("INPUT_DPI 1234.5" in x for x in m["lossy"]))

    def test_inactive_params_are_carried_and_labelled(self):
        _, m = rx.maccel_to_rawaccel(cfg(MODE="natural", ACCEL=0.2, GAMMA=3))
        rows = {r["param"]: r for r in m["params"]}
        self.assertEqual(rows["GAMMA"]["rawaccel_value"], 3.0)
        self.assertEqual(rows["GAMMA"]["note"], "inactive in this mode")
        self.assertNotIn("note", rows["LIMIT"])

    def test_boundary_values_for_every_param_and_mode(self):
        for mode in rx.MODES:
            for p, (lo, hi) in rx.PARAMS.items():
                for v in (lo, hi):
                    c = cfg(MODE=mode, **{p: v})
                    if mode == "linear" and p != "ACCEL":
                        c["ACCEL"] = 0.05
                    with self.subTest(mode=mode, param=p, value=v):
                        doc = rx.export_document(c)
                        self.assertEqual(rx.validate_settings(doc["rawaccel_settings"]), [])
                        chk = doc["curve_check"]
                        assert_rawaccel_matches_maccel(self, chk, exact=1e-9)
                        # Only the Studio preview's +-1e-9 SYNC_SPEED shortcut (when 1e-9 is a large step at
                        # SYNC_SPEED=1e-4) or natural-mode cancellation (steep LIMIT / tiny DECAY_RATE near the
                        # lowest speeds) may keep a boundary config from being equivalent.
                        if not chk["equivalent"]:
                            self.assertIn((mode, p, v), {("synchronous", "SYNC_SPEED", 0.0001),
                                                         ("natural", "LIMIT", 100), ("natural", "DECAY_RATE", 0.0001)})
                        self.assertEqual(rx.import_document(rx.dumps(doc))[0], rx.normalize_maccel(c))

    def test_out_of_range_and_bad_types_rejected_with_clear_errors(self):
        cases = []
        for p, (lo, hi) in rx.PARAMS.items():
            span = (hi - lo) or 1
            cases += [(p, lo - span * 1e-6), (p, hi + span * 1e-6)]
        cases += [("ACCEL", float("nan")), ("ACCEL", float("inf")), ("LIMIT", True), ("GAMMA", "1"),
                  ("SMOOTH", None), ("MODE", "jump"), ("MODE", 0)]
        for k, v in cases:
            with self.subTest(param=k, value=v):
                with self.assertRaises(rx.RawAccelExportError) as cm:
                    rx.maccel_to_rawaccel(cfg(**{k: v}))
                self.assertIn(k, str(cm.exception))
        with self.assertRaisesRegex(rx.RawAccelExportError, "unknown maccel parameter.*BOGUS"):
            rx.normalize_maccel(cfg(BOGUS=1))
        c = cfg()
        del c["LIMIT"], c["MODE"]
        with self.assertRaisesRegex(rx.RawAccelExportError, "missing maccel parameter.*MODE, LIMIT"):
            rx.normalize_maccel(c)
        with self.assertRaises(rx.RawAccelExportError) as cm:
            rx.normalize_maccel(cfg(ACCEL=-1, LIMIT=0.5))
        self.assertEqual(len(cm.exception.errors), 2)
        with self.assertRaises(rx.RawAccelExportError):
            rx.normalize_maccel([1, 2])


class TestCurveFormulas(unittest.TestCase):
    """Hand-computed values, independent of both ports."""

    def test_linear(self):
        c = cfg(MODE="linear", ACCEL=0.1, OFFSET=0, OUTPUT_CAP=0)
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        self.assertAlmostEqual(rx.rawaccel_profile_sens(10, p)[0], 2.0, places=14)  # 1 + 0.1*100/10
        c["OUTPUT_CAP"] = 1.5
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        self.assertAlmostEqual(rx.rawaccel_profile_sens(10, p)[0], 1.5, places=14)
        c["OUTPUT_CAP"] = 0.6  # cap below 1 decelerates down to 0.6
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        self.assertAlmostEqual(rx.rawaccel_profile_sens(10, p)[0], 0.6, places=14)
        self.assertAlmostEqual(rx.rawaccel_profile_sens(1, p)[0], 0.9, places=14)

    def test_natural(self):
        c = cfg(MODE="natural", DECAY_RATE=0.1, OFFSET=0, LIMIT=1.5)
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        a = 0.1 / 0.5
        x = 10
        expected = 0.5 * (math.exp(-a * x) / a + x - 1 / a) / x + 1
        self.assertAlmostEqual(rx.rawaccel_profile_sens(x, p)[0], expected, places=14)
        self.assertLess(rx.rawaccel_profile_sens(1e6, p)[0], 1.5)
        self.assertAlmostEqual(rx.rawaccel_profile_sens(1e6, p)[0], 1.5, places=5)

    def test_synchronous(self):
        c = cfg(MODE="synchronous", GAMMA=1, SMOOTH=0.5, MOTIVITY=1.5, SYNC_SPEED=5)
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        self.assertEqual(rx.rawaccel_profile_sens(5, p)[0], 1.0)
        self.assertAlmostEqual(rx.rawaccel_profile_sens(1e9, p)[0], 1.5, places=9)
        self.assertAlmostEqual(rx.rawaccel_profile_sens(1e-9, p)[0], 1 / 1.5, places=9)
        c["SMOOTH"] = 0
        p = prof_of(rx.maccel_to_rawaccel(c)[0])
        self.assertAlmostEqual(rx.rawaccel_profile_sens(5 * 1.5, p)[0], 1.5, places=12)  # log-space edge = +1
        self.assertAlmostEqual(rx.rawaccel_profile_sens(5 * 1.5 ** 0.5, p)[0], 1.5 ** 0.5, places=12)

    def test_shared_params_in_motion_pipeline(self):
        c = cfg(MODE="no_accel", SENS_MULT=0.5, YX_RATIO=2, ANGLE_ROTATION=90, INPUT_DPI=2000)
        s = rx.maccel_to_rawaccel(c)[0]
        for fn in (lambda: rx.maccel_motion(4, 0, 1, c), lambda: rx.rawaccel_motion(4, 0, 1, s)):
            ox, oy = fn()
            # rotate (4,0) by +90 -> (0,4); x1000/2000 -> (0,2); x0.5 -> (0,1); Y x2 -> (0,2)
            self.assertAlmostEqual(ox, 0, places=12)
            self.assertAlmostEqual(oy, 2, places=12)

    def test_unported_rawaccel_modes_raise(self):
        a = copy.deepcopy(rx.ACCEL_ARGS_DEFAULT)
        for mode, gain in (("power", True), ("jump", False), ("lut", True), ("classic", True), ("synchronous", True)):
            a["mode"], a[rx.K_GAIN] = mode, gain
            with self.subTest(mode=mode, gain=gain), self.assertRaises(rx.RawAccelExportError):
                rx.rawaccel_sens(5, a)


class TestCurveCheck(unittest.TestCase):
    def test_each_fixture_equivalent_within_tolerance(self):
        for name, c in FIXTURES.items():
            with self.subTest(name):
                chk = rx.curve_check(c)
                self.assertTrue(chk["equivalent"], chk)
                assert_rawaccel_matches_maccel(self, chk)
                self.assertEqual(chk["tolerance_relative"], TOL)
                self.assertLessEqual(chk["max_rel_error"], 1e-9)
                self.assertGreater(chk["samples"], 400)
                self.assertGreaterEqual(chk["edge_samples"], 5)

    def test_randomised_configs_rawaccel_matches_maccel(self):
        rng = random.Random(20261120)
        worst, flagged = 0.0, 0
        for i in range(400):
            c = {"MODE": rx.MODES[i % 4]}
            for p, (lo, hi) in rx.PARAMS.items():
                c[p] = rng.uniform(lo, hi) if p not in ("INPUT_DPI",) else float(rng.randrange(100, 32000, 50))
            c["OFFSET"] = rng.uniform(0, 40)
            c["ACCEL"] = rng.uniform(0.0001, 2)
            chk = rx.curve_check(c)
            assert_rawaccel_matches_maccel(self, chk, exact=1e-9)
            worst = max(worst, chk["maccel_formula_max_rel_error"], chk["max_rel_error_outside_studio_rules"],
                        chk["pipeline_max_rel_error"])
            flagged += not chk["equivalent"]
            if c["MODE"] != "synchronous":
                self.assertTrue(chk["equivalent"], chk)
        self.assertLessEqual(worst, 1e-9)
        self.assertLess(flagged, 100)  # only synchronous configs, only via the Studio sync window

    def test_check_detects_a_wrong_mapping(self):
        c = FIXTURES["linear_capped"]
        s = copy.deepcopy(rx.maccel_to_rawaccel(c)[0])
        args_x(s)["acceleration"] *= 1.001
        chk = rx.curve_check(c, settings=s)
        self.assertFalse(chk["equivalent"])
        self.assertGreater(chk["max_rel_error"], TOL)
        s = copy.deepcopy(rx.maccel_to_rawaccel(c)[0])
        s["defaultDeviceConfig"][rx.K_DPI] = 800  # wrong DPI only shows up in the motion pipeline
        chk = rx.curve_check(c, settings=s)
        self.assertFalse(chk["equivalent"])
        self.assertLessEqual(chk["max_rel_error"], TOL)
        self.assertGreater(chk["pipeline_max_rel_error"], TOL)

    def test_bad_speeds_rejected(self):
        for speeds in ([], [0], [-1], [float("nan")], ["1"], [1e200], [1e7], [1e-7], [True], [10 ** 400]):
            with self.subTest(speeds), self.assertRaises(rx.RawAccelExportError):
                rx.curve_check(rx.DEFAULTS, speeds=speeds)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestAgainstAppJavaScript(unittest.TestCase):
    """Runs index.html's own maccelEval under node and compares it with both Python ports."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO, "web", "accel_graph.js")) as f:
            src = f.read()
        m = re.search(r"^(?:export )?function maccelEval\(x, cfg\) \{.*?^\}$", src, re.S | re.M)
        assert m, "maccelEval not found in web/accel_graph.js"
        cls.js = m.group(0).removeprefix("export ")

    def run_js(self, cases):
        script = (self.js + "\nlet d='';process.stdin.on('data',c=>d+=c).on('end',()=>{"
                  "const out=JSON.parse(d).map(([x,c])=>maccelEval(x,c));process.stdout.write(JSON.stringify(out));});")
        res = subprocess.run(["node", "-e", script], input=json.dumps(cases), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)

    def test_app_curve_matches_rawaccel_formula(self):
        rng = random.Random(7)
        cfgs = list(FIXTURES.values())
        for i in range(80):
            c = {"MODE": rx.MODES[i % 4]}
            for p, (lo, hi) in rx.PARAMS.items():
                c[p] = rng.uniform(lo, hi)
            c["INPUT_DPI"] = 1600.0
            c["OFFSET"] = rng.uniform(0, 40)
            c["ACCEL"] = rng.uniform(0.0001, 2)
            cfgs.append(c)
        speeds = rx.sample_speeds()[::3]
        cases = [(x, c) for c in cfgs for x in speeds]
        js = self.run_js(cases)
        worst_port, worst_ra = 0.0, 0.0
        for (x, c), (jx, jy) in zip(cases, js):
            px, py = rx.app_sens(x, c)
            rax, ray = rx.rawaccel_profile_sens(x, prof_of(rx.maccel_to_rawaccel(c)[0]))
            worst_port = max(worst_port, rx._rel(jx, px), rx._rel(jy, py))
            worst_ra = max(worst_ra, rx._rel(jx, rax), rx._rel(jy, ray))
        self.assertLessEqual(worst_port, 1e-12, "Python app_sens drifted from index.html maccelEval")
        self.assertLessEqual(worst_ra, TOL, f"app vs Raw Accel max rel error {worst_ra}")
        print(f"\n  JS maccelEval vs Raw Accel port: {len(cases)} samples, max rel error {worst_ra:.3g} "
              f"(port drift {worst_port:.3g})", file=sys.stderr)


class TestVersionedRoundTrip(unittest.TestCase):
    def test_export_import_export_is_lossless_and_byte_identical(self):
        rng = random.Random(3)
        configs = list(FIXTURES.values())
        for i in range(100):
            c = {"MODE": rng.choice(rx.MODES)}
            for p, (lo, hi) in rx.PARAMS.items():
                c[p] = rng.uniform(lo, hi)
            if c["MODE"] == "lookup":
                n_pts = rng.randint(2, 8)
                xs = sorted(rng.sample([round(k * 0.25, 2) for k in range(400)], n_pts))
                c["LUT_POINTS"] = [[float(x), round(rng.uniform(0.2, 4.0), 4)] for x in xs]
            configs.append(c)
        for c in configs:
            for include_check in (True, False):
                text = rx.dumps(rx.export_document(c, include_check=include_check))
                back, rep = rx.import_document(text)
                self.assertEqual(rep["source"], "mousetool-export")
                self.assertEqual(back, rx.normalize_maccel(c))
                for k in c:
                    self.assertEqual(back[k], c[k])  # bit-exact floats
                self.assertEqual(rx.dumps(rx.export_document(back, include_check=include_check)), text)

    def test_document_header(self):
        doc = rx.export_document(FIXTURES["natural"])
        self.assertEqual((doc["format"], doc["schema_version"], doc["rawaccel_version"]),
                         ("mousetool.rawaccel-export", 1, "1.7.1"))
        self.assertEqual(set(doc), rx.DOC_KEYS)
        self.assertEqual(list(doc)[-1], "rawaccel_settings")

    def test_import_rejections(self):
        good = rx.export_document(FIXTURES["synchronous"])

        def mutate(fn):
            d = copy.deepcopy(good)
            fn(d)
            return d

        bad = {
            "wrong format": (mutate(lambda d: d.update(format="other")), "format must be"),
            "newer schema": (mutate(lambda d: d.update(schema_version=2)), "newer than this mousetool reads"),
            "string schema": (mutate(lambda d: d.update(schema_version="1")), "must be an integer"),
            "bool schema": (mutate(lambda d: d.update(schema_version=True)), "must be an integer"),
            "zero schema": (mutate(lambda d: d.update(schema_version=0)), "not valid"),
            "missing schema": (mutate(lambda d: d.pop("schema_version")), "must be an integer"),
            "unknown key": (mutate(lambda d: d.update(extra=1)), "unknown key"),
            "missing maccel": (mutate(lambda d: d.pop("maccel")), "missing 'maccel'"),
            "out of range": (mutate(lambda d: d["maccel"].update(MOTIVITY=1.0)), "MOTIVITY must be between"),
            "missing param": (mutate(lambda d: d["maccel"].pop("GAMMA")), "missing maccel parameter"),
            "tampered settings": (mutate(lambda d: args_x(d["rawaccel_settings"]).update(motivity=2.0)),
                                  "does not match"),
        }
        for name, (doc, msg) in bad.items():
            with self.subTest(name), self.assertRaisesRegex(rx.RawAccelExportError, msg):
                rx.import_document(doc)
        for text, msg in (('{"format": "mousetool.rawaccel-export", "schema_version": 1, "maccel": {"ACCEL": NaN}}',
                           "invalid JSON number NaN"), ("{nope", "invalid JSON"), ("[1]", "expected a JSON object"),
                          (b'{"ACCEL": Infinity}', "invalid JSON number Infinity")):
            with self.subTest(text), self.assertRaisesRegex(rx.RawAccelExportError, msg):
                rx.import_document(text)
        with self.assertRaises(ValueError):
            rx.dumps({"x": float("nan")})

    def test_import_maccel_state_and_partial_config(self):
        state = {"installed": True, "loaded": True, "mode": "natural", "params": dict(
            (k, v) for k, v in rx.DEFAULTS.items() if k != "MODE")}
        c, rep = rx.import_document(state)
        self.assertEqual((rep["source"], c["MODE"]), ("maccel-state", "natural"))
        c, rep = rx.import_document('{"MODE": "synchronous", "GAMMA": 2}')
        self.assertEqual((rep["source"], c["GAMMA"], c["LIMIT"]), ("maccel-config", 2.0, 1.5))
        self.assertIn("LIMIT", rep["defaulted"])
        self.assertTrue(rep["notes"])


class TestReverseMapping(unittest.TestCase):
    def test_rawaccel_settings_back_to_maccel(self):
        for name, c in FIXTURES.items():
            with self.subTest(name):
                s, m = rx.maccel_to_rawaccel(c)
                back, rep = rx.import_document(json.loads(rx.dumps(s)))
                self.assertEqual(rep["source"], "rawaccel-settings")
                for k in rx.PARAMS:
                    if k == "ACCEL" and c["ACCEL"] == 0:
                        self.assertEqual(back[k], 0.005)  # reported lossy on export
                    else:
                        self.assertAlmostEqual(back[k], c[k], delta=abs(c[k]) * 1e-15, msg=k)
                flat = m["flat"]
                self.assertEqual(back["MODE"], "no_accel" if flat else c["MODE"])
                self.assertTrue(rx.curve_check(back, settings=s)["equivalent"])

    def test_dpi_zero_means_1000(self):
        s, _ = rx.maccel_to_rawaccel(cfg())
        s["defaultDeviceConfig"][rx.K_DPI] = 0
        back, rep = rx.rawaccel_to_maccel(s)
        self.assertEqual(back["INPUT_DPI"], 1000.0)
        self.assertTrue(any("DPI 0" in n for n in rep["notes"]))

    def test_unsupported_rawaccel_features_rejected(self):
        base = rx.maccel_to_rawaccel(FIXTURES["linear_capped"])[0]

        def mutate(fn):
            s = copy.deepcopy(base)
            fn(s)
            return s

        def ax(s):
            return args_x(s)

        cases = {
            "power": (lambda s: ax(s).update(mode="power"), "mode 'power'"),
            "jump": (lambda s: ax(s).update(mode="jump"), "mode 'jump'"),
            "exponent 3": (lambda s: ax(s).update(exponentClassic=3.0), "classic exponent"),
            "gain classic": (lambda s: ax(s).update({rx.K_GAIN: True}), "classic with Gain on"),
            "cap mode input": (lambda s: ax(s).update({rx.K_CAP_MODE: "input"}), "cap mode 'input'"),
            "natural legacy": (lambda s: ax(s).update({"mode": "natural", rx.K_GAIN: False}), "natural with Gain off"),
            "sync gain": (lambda s: ax(s).update({"mode": "synchronous", rx.K_GAIN: True}), "synchronous with Gain on"),
            "by component": (lambda s: prof_of(s)[rx.K_SPEED].update({rx.K_WHOLE: False}), "by component"),
            "smoothing": (lambda s: prof_of(s)[rx.K_SPEED].update({rx.K_IN_HALF: 5.0}), "weighted at half"),
            "snap": (lambda s: prof_of(s).update({rx.K_SNAP: 5.0}), "angle snapping"),
            "speed cap": (lambda s: prof_of(s).update({rx.K_SPEED_CAP: 50.0}), "Input Speed Cap"),
            "lr ratio": (lambda s: prof_of(s).update({rx.K_LR: 1.1}), "L/R"),
            "range weights": (lambda s: prof_of(s)[rx.K_RANGE].update(y=0.5), "accel range"),
            "device dpi": (lambda s: s["devices"].append({"name": "m", "profile": "", "id": "HID#1", "config": {
                **rx.DEVICE_CONFIG_DEFAULT, rx.K_DPI: 400}}), "own DPI"),
            "disabled": (lambda s: s["defaultDeviceConfig"].update(disable=True), "disable"),
            "active out of range": (lambda s: ax(s).update(acceleration=60.0), "ACCEL .* outside maccel's range"),
            "upstream invalid": (lambda s: ax(s).update(motivity=1.0), "would reject.*motivity"),
            "no profiles": (lambda s: s.update(profiles=[]), "non-empty list"),
            "two profiles": (lambda s: s["profiles"].append(dict(prof_of(s), name="b")), "choose one"),
            "missing field": (lambda s: prof_of(s).pop(rx.K_OUT_DPI), "missing 'Output DPI'"),
        }
        for name, (fn, msg) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(rx.RawAccelExportError, msg):
                rx.rawaccel_to_maccel(mutate(fn))
        two = mutate(cases["two profiles"][0])
        self.assertEqual(rx.rawaccel_to_maccel(two, "b")[0]["ACCEL"], 0.1)
        with self.assertRaisesRegex(rx.RawAccelExportError, "no profile named"):
            rx.rawaccel_to_maccel(two, "zzz")

    def test_inactive_out_of_range_falls_back_with_lossy_report(self):
        s = rx.maccel_to_rawaccel(FIXTURES["linear_capped"])[0]
        args_x(s)["limit"] = 0.5  # valid in Raw Accel, below maccel's LIMIT range, unused by classic
        back, rep = rx.rawaccel_to_maccel(s)
        self.assertEqual(back["LIMIT"], 1.5)
        self.assertEqual(len(rep["lossy"]), 1)
        self.assertIn("LIMIT", rep["lossy"][0])

    def test_validate_settings_catches_upstream_errors(self):
        s = rx.maccel_to_rawaccel(FIXTURES["natural"])[0]
        for fn, msg in ((lambda s: prof_of(s).update({rx.K_OUT_DPI: 0.0}), "output DPI is 0"),
                        (lambda s: args_x(s).update(smooth=1.5), "smooth"),
                        (lambda s: args_x(s).update(mode="quadratic"), "mode must be"),
                        (lambda s: s.pop("version"), "missing 'version'"),
                        (lambda s: s["defaultDeviceConfig"].update({rx.K_DPI: 800.5}), "dpi must be"),
                        (lambda s: prof_of(s).update(name=""), "name can not be empty")):
            t = copy.deepcopy(s)
            fn(t)
            with self.subTest(msg):
                self.assertTrue(any(msg in e for e in rx.validate_settings(t)), rx.validate_settings(t))


class TestCli(unittest.TestCase):
    def run_main(self, argv, live=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = rx.main(argv, live_loader=live, stdout=out)
        return code, out.getvalue(), err.getvalue()

    def test_export_settings_only_and_import(self):
        with tempfile.TemporaryDirectory() as td:
            doc_path, set_path = os.path.join(td, "export.json"), os.path.join(td, "settings.json")
            code, _, err = self.run_main(["export", "--set", "MODE=natural", "LIMIT=2", "-o", doc_path])
            self.assertEqual(code, 0, err)
            code, _, err = self.run_main(["export", "--input", doc_path, "--settings-only", "-o", set_path])
            self.assertEqual(code, 0, err)
            with open(doc_path) as f:
                doc = json.load(f)
            with open(set_path) as f:
                settings = json.load(f)
            self.assertEqual(doc["rawaccel_settings"], settings)
            self.assertEqual(doc["maccel"]["LIMIT"], 2.0)
            code, _, err = self.run_main(["export", "-o", doc_path])
            self.assertEqual(code, 2)
            self.assertIn("exists; pass --force", err)
            self.assertEqual(self.run_main(["export", "-o", doc_path, "--force", "--no-check"])[0], 0)
            with open(doc_path) as f:
                self.assertNotIn("curve_check", json.load(f))
            for path, source in ((doc_path, "mousetool-export"), (set_path, "rawaccel-settings")):
                code, out, err = self.run_main(["import", path])
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["import"]["source"], source)

    def test_preview_and_errors(self):
        code, out, _ = self.run_main(["preview", "--set", "MODE=synchronous", "SMOOTH=0"])
        self.assertEqual(code, 0)
        self.assertIn("EQUIVALENT: every sample within 1e-06", out)
        self.assertIn("Unsupported (not exported):", out)
        self.assertIn("SYNC_SPEED", out)
        code, out, _ = self.run_main(["preview", "--json", "--set", "MODE=linear", "ACCEL=0.1"])
        self.assertTrue(json.loads(out)["curve_check"]["equivalent"])
        for argv, msg in ((["preview", "--set", "LIMIT=abc"], "must be a number"),
                          (["preview", "--set", "LIMIT"], "expected KEY=VAL"),
                          (["preview", "--set", "MODE=nope"], "MODE must be one of"),
                          (["preview", "--live"], "--live needs the Linux helper"),
                          (["import", "/nonexistent/x.json"], "No such file")):
            with self.subTest(argv):
                code, _, err = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn(msg, err)

    def test_live_loader_is_used_read_only(self):
        live = mock.Mock(return_value=cfg(MODE="natural", LIMIT=3))
        code, out, _ = self.run_main(["export", "--live", "--settings-only"], live=live)
        self.assertEqual(code, 0)
        live.assert_called_once_with()
        self.assertEqual(args_x(json.loads(out))["limit"], 3.0)
        code, _, err = self.run_main(["export", "--live", "--input", "x.json"], live=live)
        self.assertEqual(code, 2)

    def test_mousetool_subcommand(self):
        env = dict(os.environ, HOME=tempfile.mkdtemp(), XDG_CONFIG_HOME=tempfile.mkdtemp())
        res = subprocess.run([sys.executable, os.path.join(REPO, "mousetool.py"), "rawaccel", "export",
                              "--settings-only", "--set", "MODE=synchronous"], capture_output=True, text=True,
                             timeout=60, env=env, cwd=env["HOME"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(args_x(json.loads(res.stdout))["mode"], "synchronous")
        res = subprocess.run([sys.executable, os.path.join(REPO, "mousetool.py"), "rawaccel", "--help"],
                             capture_output=True, text=True, timeout=60, env=env, cwd=env["HOME"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("mousetool.py rawaccel", res.stdout)
        res = subprocess.run([sys.executable, os.path.join(REPO, "mousetool.py"), "rawaccel", "preview",
                              "--set", "GAMMA=0"], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(res.returncode, 2)
        self.assertIn("GAMMA must be between", res.stderr)

    def test_maccel_live_config_reads_state_only(self):
        from mousetool.accel import linux as linux_accel
        st = {"loaded": True, "mode": "linear", "params": {k: v for k, v in rx.DEFAULTS.items() if k != "MODE"}}
        with mock.patch.object(linux_accel, "maccel_state", return_value=st), \
                mock.patch.object(linux_accel, "_write_param") as w:
            self.assertEqual(linux_accel.maccel_live_config(), rx.DEFAULTS)
            w.assert_not_called()
        with mock.patch.object(linux_accel, "maccel_state", return_value={"loaded": False, "mode": None, "params": {}}):
            with self.assertRaisesRegex(PermissionError, "isn't loaded"):
                linux_accel.maccel_live_config()
        st2 = {"loaded": True, "mode": "linear", "params": {"ACCEL": 0.1}}
        with mock.patch.object(linux_accel, "maccel_state", return_value=st2):
            with self.assertRaisesRegex(OSError, "could not read"):
                linux_accel.maccel_live_config()


class ScratchCli:
    """Mixin: run either CLI in a throwaway HOME/cwd and write input files there."""

    def cli(self, argv, prog="rawaccel_export.py"):
        if prog == "mousetool.py":
            cmd = [sys.executable, os.path.join(REPO, "mousetool.py"), "rawaccel"] + argv
        else:
            cmd = [sys.executable, "-m", "mousetool.accel.rawaccel_export"] + argv
        env = dict(os.environ, HOME=self.home, XDG_CONFIG_HOME=self.home, PYTHONPATH=REPO)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env, cwd=self.home)

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.home = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def write(self, name, content):
        path = os.path.join(self.home, name)
        with open(path, "wb") as f:
            f.write(content if isinstance(content, bytes) else content.encode())
        return path


class TestVerifyRound1Regressions(ScratchCli, unittest.TestCase):
    """Cases from the MT-003 round-1 independent verification (MT-003-R1-01..05)."""

    # R1-01: losses are never silent, whatever the output route
    def test_lossy_export_warns_on_stderr_for_every_route(self):
        lossy = ["--set", "MODE=no_accel", "INPUT_DPI=50.49", "ACCEL=0"]
        routes = [["export", "--settings-only"], ["export"], ["export", "--no-check"],
                  ["export", "--settings-only", "-o", "s.json"], ["export", "-o", "d.json"]]
        for prog in ("rawaccel_export.py", "mousetool.py"):
            for route in routes:
                with self.subTest(prog=prog, route=route):
                    for f in ("s.json", "d.json"):
                        if os.path.exists(os.path.join(self.home, f)):
                            os.unlink(os.path.join(self.home, f))
                    res = self.cli(route + lossy, prog)
                    self.assertEqual(res.returncode, 0, res.stderr)
                    self.assertIn("lossy: INPUT_DPI 50.49 is not an integer", res.stderr)
                    self.assertIn("lossy: ACCEL=0 cannot be stored", res.stderr)
                    self.assertIn("unsupported: Per-device settings", res.stderr)
                    self.assertIn("unsupported: Timing", res.stderr)
                    self.assertIn("curve check:", res.stderr)
                    if "-o" not in route:
                        json.loads(res.stdout)  # stdout stays pure JSON

    # R1-02: malformed input -> concise error, exit 2, never a traceback
    def test_malformed_inputs_fail_cleanly(self):
        doc = rx.export_document(rx.with_defaults({"MODE": "synchronous"})[0])
        settings = doc["rawaccel_settings"]

        def doc_with(fn):
            d = copy.deepcopy(doc)
            fn(d)
            return json.dumps(d)

        def settings_with(fn):
            d = copy.deepcopy(settings)
            fn(d)
            return json.dumps(d)

        dev = lambda d: d["defaultDeviceConfig"]  # noqa: E731
        ax = lambda d: prof_of(d)[rx.K_ARGS_X]  # noqa: E731
        ems = lambda d: d["rawaccel_settings"]  # noqa: E731
        cases = {
            "truncated": ('{"SENS_MULT":', "invalid JSON"),
            "nan": ('{"SENS_MULT":NaN}', "invalid JSON number NaN"),
            "infinity": ('{"SENS_MULT":Infinity}', "invalid JSON number Infinity"),
            "overflow-exponent": ('{"SENS_MULT":1e400}', "JSON number '1e400' is out of range for a double"),
            "huge-int": ('{"SENS_MULT":1' + "0" * 400 + "}", "SENS_MULT must be a finite number (got <integer"),
            "5001-digit-int": ('{"SENS_MULT":1' + "0" * 5000 + "}", "invalid JSON: Exceeds the limit"),
            "deep": ('{"SENS_MULT":' + "[" * 30000 + "0" + "]" * 30000 + "}", "nested deeper than 32"),
            "bad-utf8": (b'{"SENS_MULT":"\xff"}', "not valid UTF-8"),
            "wrong-bool": ('{"SENS_MULT":true}', "SENS_MULT must be a finite number (got True)"),
            "duplicate": ('{"SENS_MULT":"bad","SENS_MULT":1}', "duplicate JSON key 'SENS_MULT'"),
            "oversize": (" " * (rx.MAX_INPUT_BYTES + 1) + '{"MODE":"no_accel"}', "larger than 1048576 bytes"),
            "missing-required": (doc_with(lambda d: [d.pop(k) for k in ("rawaccel_settings", "rawaccel_version",
                                                                          "mapping", "curve_check")]),
                                 "export is missing 'rawaccel_version', 'mapping', 'rawaccel_settings'"),
            "missing-settings-only": (doc_with(lambda d: d.pop("rawaccel_settings")), "missing 'rawaccel_settings'"),
            "version-list": (doc_with(lambda d: d.update(rawaccel_version=["999"])), "rawaccel_version must be '1.7.1'"),
            "mapping-list": (doc_with(lambda d: d.update(mapping=[])), "mapping report does not match"),
            "mapping-edited": (doc_with(lambda d: d["mapping"]["lossy"].clear()), "mapping report does not match"),
            "check-types": (doc_with(lambda d: d.update(curve_check={"equivalent": "false", "max_rel_error": "n/a"})),
                            "curve_check report is malformed"),
            "check-bool-type": (doc_with(lambda d: d["curve_check"].update(equivalent="false")),
                                "curve_check report is malformed"),
            "check-samples": (doc_with(lambda d: d["curve_check"].update(samples=3)), "curve_check report is malformed"),
            "embedded-dpi-bool": (doc_with(lambda d: dev(ems(d)).update({rx.K_DPI: True})), "does not match"),
            "embedded-dpi-float": (doc_with(lambda d: dev(ems(d)).update({rx.K_DPI: 1000.0})), "does not match"),
            "embedded-disable-0": (doc_with(lambda d: dev(ems(d)).update(disable=0)), "does not match"),
            "embedded-gain-0": (doc_with(lambda d: ax(ems(d)).update({rx.K_GAIN: 0})), "does not match"),
            "lut-int-data": (settings_with(lambda d: ax(d).update(mode="lut", data=5)), "data must be an array"),
            "raw-disable-string": (settings_with(lambda d: dev(d).update(disable="true")), "disable must be true/false"),
            "raw-null-y-data": (settings_with(lambda d: prof_of(d)[rx.K_ARGS_Y].update(data=None)),
                                "argsY.data must be an array"),
            "raw-missing-y-field": (settings_with(lambda d: prof_of(d)[rx.K_ARGS_Y].pop("motivity")),
                                    "argsY: missing 'motivity'"),
            "raw-version-object": (settings_with(lambda d: d.update(version={})), "version must be a string"),
            "raw-dpi-float": (settings_with(lambda d: dev(d).update({rx.K_DPI: 1000.0})), "dpi must be a non-negative"),
            "raw-device-shape": (settings_with(lambda d: d.update(devices=[5])), "devices[0] must be an object"),
        }
        for name, (content, msg) in cases.items():
            path = self.write(f"{name}.json", content)
            for prog in ("rawaccel_export.py", "mousetool.py"):
                if prog == "mousetool.py" and name not in ("huge-int", "5001-digit-int", "deep", "bad-utf8",
                                                           "embedded-dpi-float", "raw-null-y-data"):
                    continue
                with self.subTest(case=name, prog=prog):
                    res = self.cli(["import", path], prog)
                    self.assertEqual(res.returncode, 2, (res.stdout[:200], res.stderr[-500:]))
                    self.assertNotIn("Traceback", res.stderr)
                    self.assertIn(msg, res.stderr)
                    self.assertLess(len(res.stderr), 2000)
                    self.assertEqual(res.stdout, "")

    def test_import_document_api_raises_only_export_errors(self):
        for data in ('{"a":' + "[" * 100 + "]" * 100 + "}", b"\xff\xfe", 12, None, [1], '{"SENS_MULT": 1' + "0" * 400 + "}"):
            with self.subTest(data=str(data)[:30]), self.assertRaises(rx.RawAccelExportError):
                rx.import_document(data)

    # R1-03: synchronous edges around SYNC_SPEED
    def test_synchronous_sync_window_case_is_flagged_not_called_equivalent(self):
        c = cfg(MODE="synchronous", SYNC_SPEED=0.051, GAMMA=100, SMOOTH=1, MOTIVITY=100, SENS_MULT=1.7)
        prof = prof_of(rx.maccel_to_rawaccel(c)[0])
        x = 0.0509999991
        self.assertEqual(rx.app_sens(x, c)[0], 1.7)  # Studio shortcut
        # the verifier's value from the unmodified upstream C++ modifier at this speed
        self.assertAlmostEqual(rx.rawaccel_profile_sens(x, prof)[0], 1.6999970000033962, delta=1e-12)
        self.assertAlmostEqual(rx.app_sens(x, c, studio_rules=False)[0], 1.6999970000033962, delta=1e-12)
        chk = rx.curve_check(c)
        self.assertFalse(chk["equivalent"])
        self.assertGreater(chk["max_rel_error"], 1.7e-6)
        self.assertLess(chk["max_rel_error"], 2.5e-6)
        assert_rawaccel_matches_maccel(self, chk)
        self.assertIn("NOT EQUIVALENT", chk["result"])
        spot = rx.curve_check(c, speeds=[x])
        self.assertAlmostEqual(spot["max_rel_error"], 1.7647038845900073e-06, delta=1e-12)

    def test_synchronous_preview_labels_are_consistent(self):
        res = self.cli(["preview", "--set", "MODE=synchronous", "SYNC_SPEED=0.0500000009", "GAMMA=100", "SMOOTH=1",
                        "MOTIVITY=100", "SENS_MULT=1.7"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("formula-equivalent", res.stdout)
        self.assertNotIn("EQUIVALENT:", res.stdout)
        self.assertIn("NOT EQUIVALENT within 1e-06: Studio preview max", res.stdout)
        self.assertIn("only inside the Studio preview's sync-speed shortcut", res.stdout)
        self.assertIn("within 1e-9 counts/ms of SYNC_SPEED", res.stdout)

    def test_edge_speeds_cover_branch_points(self):
        sync = cfg(MODE="synchronous", SYNC_SPEED=0.051, GAMMA=2, MOTIVITY=3, SMOOTH=0)
        e = rx.edge_speeds(sync)
        for target in (0.051, 0.051 - 9.99e-10, 0.051 + 1.001e-9, 0.051 * 3 ** 0.5, 0.051 / 3 ** 0.5,
                       rx.SPEED_MIN, rx.SPEED_MAX):
            self.assertTrue(any(abs(x - target) <= 1e-15 * max(1, target) for x in e), target)
        lin = cfg(MODE="linear", ACCEL=0.1, OFFSET=2, OUTPUT_CAP=1.5)
        cap_x = [x for x in rx.edge_speeds(lin) if abs(0.1 * (x - 2) ** 2 / x - 0.5) < 1e-9]
        self.assertTrue(cap_x)
        self.assertIn(2.0, rx.edge_speeds(lin))
        self.assertTrue(all(rx.SPEED_MIN <= x <= rx.SPEED_MAX for x in e))

    # R1-04: timing and per-device settings are explicit
    def test_export_lists_unsupported_timing_and_devices(self):
        _, m = rx.maccel_to_rawaccel(cfg())
        self.assertEqual(len(m["unsupported"]), 2)
        self.assertTrue(m["unsupported"][0].startswith("Per-device settings"))
        self.assertTrue(m["unsupported"][1].startswith("Timing"))

    def test_rawaccel_timing_and_device_settings_on_import(self):
        base = rx.maccel_to_rawaccel(FIXTURES["synchronous"])[0]
        s = copy.deepcopy(base)
        s["defaultDeviceConfig"].update({rx.K_POLL_LOCK: True, rx.K_POLL: 125, "minimumTime": 1.0,
                                         "maximumTime": 40.0, "setExtraInfo": True})
        back, rep = rx.rawaccel_to_maccel(s)
        self.assertEqual(back, rx.normalize_maccel({**FIXTURES["synchronous"], "ACCEL": 0.005}))  # ACCEL=0 lossy on export
        self.assertEqual(len(rep["lossy"]), 5)
        for label in ("constant time interval", "polling rate Hz = 125", "minimum time ms = 1.0",
                      "maximum time ms = 40.0", "setExtraInfo"):
            self.assertTrue(any(label in x for x in rep["lossy"]), label)
        path = self.write("timing.json", json.dumps(s))
        res = self.cli(["import", path])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("lossy: defaultDeviceConfig: polling rate Hz = 125 ignored", res.stderr)

        def device(**kw):
            d = {"name": "m", "profile": "mousetool", "id": "HID#VID_1&PID_2",
                 "config": dict(base["defaultDeviceConfig"])}
            d["config"].update(kw.pop("config", {}))
            d.update(kw)
            t = copy.deepcopy(base)
            t["devices"] = [d]
            return t

        with self.assertRaisesRegex(rx.RawAccelExportError, "is disabled in Raw Accel"):
            rx.rawaccel_to_maccel(device(config={"disable": True}))
        with self.assertRaisesRegex(rx.RawAccelExportError, "own DPI 1600"):
            rx.rawaccel_to_maccel(device(config={rx.K_DPI: 1600}))
        with self.assertRaisesRegex(rx.RawAccelExportError, "uses profile 'other'"):
            rx.rawaccel_to_maccel(device(profile="other"))
        back, rep = rx.rawaccel_to_maccel(device(config={rx.K_POLL: 1000}))
        self.assertTrue(any("HID#VID_1&PID_2" in n for n in rep["notes"]))
        self.assertTrue(any("device 'HID#VID_1&PID_2': polling rate Hz = 1000" in x for x in rep["lossy"]))
        back, rep = rx.rawaccel_to_maccel(device(profile=""))  # empty profile = first profile
        self.assertEqual(rep["lossy"], [])

    # R1-05: custom speeds are bounded and evaluation can't escape the error contract
    def test_extreme_custom_speeds(self):
        c = cfg(MODE="linear", ACCEL=1)
        with self.assertRaisesRegex(rx.RawAccelExportError, "from 1e-06 to 1e\\+06"):
            rx.curve_check(c, speeds=[1e200])
        for mode in rx.MODES:
            hi = cfg(MODE=mode, ACCEL=50, SENS_MULT=50, YX_RATIO=50, DECAY_RATE=100, LIMIT=100, GAMMA=100,
                     MOTIVITY=100, SMOOTH=1, SYNC_SPEED=0.0001)
            with self.subTest(mode):
                chk = rx.curve_check(hi, speeds=[rx.SPEED_MIN, 1.0, rx.SPEED_MAX])
                self.assertTrue(math.isfinite(chk["max_rel_error"]))


class TestVerifyRound2Regressions(ScratchCli, unittest.TestCase):
    """Cases from the MT-003 round-2 independent verification (MT-003-R2-01..05)."""

    # (LIMIT, DECAY_RATE, speed, native Raw Accel value, actual Studio JavaScript value), OFFSET=0. Recorded by
    # the round-2 verifier: native from a C++ harness over the unmodified v1.7.1 headers, Studio from
    # index.html maccelEval under Node.
    NATURAL_EDGES = [
        (100, 0.0001, 0.00016686066030224922, 1.0, 0.9999106969781442),
        (80, 0.000135, 0.00060960975913815, 1.0, 0.9999877781146295),
        (89, 0.000128, 0.00015818802171687108, 1.0, 0.9999529004755476),
        (20, 0.0002, 7.847677037317504e-05, 1.0000029668734143, 1.0),
    ]

    # R2-01: natural cancellation is flagged, never reported as equivalent
    def test_natural_cancellation_cases_are_not_called_equivalent(self):
        for limit, decay, x, native, studio in self.NATURAL_EDGES:
            c = cfg(MODE="natural", LIMIT=limit, DECAY_RATE=decay, OFFSET=0)
            with self.subTest(limit=limit, decay=decay):
                observed = rx._rel(native, studio)
                self.assertGreater(observed, TOL)
                spot = rx.curve_check(c, speeds=[x])
                self.assertFalse(spot["equivalent"])
                self.assertEqual(spot["unverified_samples"], 1)
                self.assertGreaterEqual(spot["max_fp_spread"], observed)  # the estimate covers the real gap
                self.assertTrue(spot["result"].startswith("NOT VERIFIED within 1e-06"), spot["result"])
                full = rx.curve_check(c)
                self.assertFalse(full["equivalent"])
                self.assertGreater(full["unverified_samples"], 0)
                self.assertGreater(full["unverified_below"], x)

    def test_well_conditioned_natural_still_equivalent(self):
        for c in (FIXTURES["natural"], FIXTURES["natural_steep"], cfg(MODE="natural")):
            chk = rx.curve_check(c)
            self.assertTrue(chk["equivalent"], chk["result"])
            self.assertEqual(chk["unverified_samples"], 0)
            self.assertLessEqual(chk["max_fp_spread"], TOL)

    def test_natural_edge_speeds_include_points_just_above_offset(self):
        e = rx.edge_speeds(cfg(MODE="natural", OFFSET=2))
        for k in range(-12, 1):
            self.assertIn(2 + 10 ** (k / 2), e)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_studio_javascript_really_differs_at_the_verifier_point(self):
        with open(os.path.join(REPO, "web", "accel_graph.js")) as f:
            js = re.search(r"^(?:export )?function maccelEval\(x, cfg\) \{.*?^\}$", f.read(), re.S | re.M).group(0).removeprefix("export ")
        limit, decay, x, native, studio = self.NATURAL_EDGES[0]
        c = cfg(MODE="natural", LIMIT=limit, DECAY_RATE=decay, OFFSET=0)
        res = subprocess.run(["node", "-e", js + f"\nprocess.stdout.write(JSON.stringify(maccelEval({x!r}, "
                                                f"{json.dumps(c)})))"], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)[0]
        self.assertAlmostEqual(got, studio, delta=1e-12)  # this machine's Node reproduces the verifier's value
        self.assertGreater(rx._rel(got, native), TOL)     # ...which really is off from native Raw Accel
        self.assertGreaterEqual(rx.natural_exp_spread(x, args_x(rx.maccel_to_rawaccel(c)[0])), rx._rel(got, native))

    # R2-02: forged or hybrid versioned documents are rejected
    def test_forged_curve_reports_are_rejected(self):
        doc = rx.export_document(FIXTURES["natural"])

        def forged(fn):
            d = copy.deepcopy(doc)
            fn(d["curve_check"])
            return d

        def verifier_forgery(r):
            r.update(equivalent=True, result="EQUIVALENT: trust me", max_rel_error=-4.0, tolerance_relative=-1.0)

        for name, fn in {
            "verifier forgery": verifier_forgery,
            "negative error": lambda r: r.update(max_rel_error=-1e-17),
            "negative tolerance": lambda r: r.update(tolerance_relative=-1.0),
            "inflated error": lambda r: r.update(max_rel_error=0.5),
            "equivalent flipped": lambda r: r.update(equivalent=False),
            "result rewritten": lambda r: r.update(result="EQUIVALENT: every sample within 1e-06 relative"),
            "unverified hidden": lambda r: r.update(max_fp_spread=0.01),
            "worst speed outside range": lambda r: r["worst"].update(speed=1e9),
            "worst not the max": lambda r: r["worst"].update(rel_error=0.0),
        }.items():
            with self.subTest(name), self.assertRaisesRegex(rx.RawAccelExportError, "curve_check report"):
                rx.import_document(rx.dumps(forged(fn)))

        def platform_rounding(r):  # what another libm might legitimately produce
            r["max_rel_error"] = r["worst"]["rel_error"] = r["max_rel_error"] * (1 + 1e-4)
            r["maccel_formula_max_rel_error"] *= 1 + 1e-4
        self.assertEqual(rx.import_document(rx.dumps(forged(platform_rounding)))[0], rx.normalize_maccel(FIXTURES["natural"]))

    def test_hybrid_document_classified_once(self):
        settings = rx.maccel_to_rawaccel(FIXTURES["natural"])[0]
        hybrid = dict(settings, format=rx.EXPORT_FORMAT, schema_version=99)
        path = self.write("hybrid.json", json.dumps(hybrid))
        for prog in ("rawaccel_export.py", "mousetool.py"):
            for extra in ([], ["--profile", "mousetool"]):
                with self.subTest(prog=prog, extra=extra):
                    res = self.cli(["import", path] + extra, prog)
                    self.assertEqual(res.returncode, 2, res.stdout[:200])
                    self.assertIn("error:", res.stderr)
                    self.assertTrue("newer than this mousetool reads" in res.stderr or
                                    "--profile only applies to bare Raw Accel settings" in res.stderr, res.stderr)
        with self.assertRaisesRegex(rx.RawAccelExportError, "--profile only applies"):
            rx.import_document('{"MODE": "natural"}', profile_name="mousetool")
        self.assertEqual(rx.import_document(json.dumps(settings), profile_name="mousetool")[1]["source"],
                         "rawaccel-settings")

    # R2-04: bounded diagnostics, overflowing floats rejected while parsing
    def test_long_schema_version_and_overflowing_float(self):
        cases = {
            "longver": ('{"format": "%s", "schema_version": 1%s}' % (rx.EXPORT_FORMAT, "0" * 3999),
                        "is newer than this mousetool reads"),
            "loaded-overflow": (json.dumps({"loaded": 1, "mode": "linear", "params": {}}).replace(
                '"loaded": 1', '"loaded": 1e500'), "out of range for a double"),
        }
        for name, (content, msg) in cases.items():
            path = self.write(f"{name}.json", content)
            for prog in ("rawaccel_export.py", "mousetool.py"):
                with self.subTest(case=name, prog=prog):
                    res = self.cli(["import", path], prog)
                    self.assertEqual(res.returncode, 2)
                    self.assertIn(msg, res.stderr)
                    self.assertLess(len(res.stderr), 300)

    # R2-05: bare stderr names the Studio shortcut instead of pointing to absent notes
    def test_bare_stderr_names_the_sync_shortcut(self):
        res = self.cli(["export", "--settings-only", "--set", "MODE=synchronous", "SYNC_SPEED=.051", "GAMMA=100",
                        "SMOOTH=1", "MOTIVITY=100", "SENS_MULT=1.7"], "mousetool.py")
        self.assertEqual(res.returncode, 0, res.stderr)
        line = next(x for x in res.stderr.splitlines() if x.startswith("curve check:"))
        self.assertIn("returns exactly 1x within 1e-9 counts/ms of SYNC_SPEED", line)
        self.assertNotIn("see notes", res.stderr)


class TestCustomLutMode(ScratchCli, unittest.TestCase):
    """MT-014: Custom Lookup-Table (MODE='lookup') forward/reverse mapping, validation, edge speeds & JS parity."""

    def test_lookup_forward_reverse_and_curve_check_exact_equivalence(self):
        pts = [[1.0, 0.75], [5.5, 1.0], [12.0, 1.4], [25.0, 1.85], [40.0, 1.6]]
        c = cfg(MODE="lookup", ACCEL=0.005, SENS_MULT=0.85, YX_RATIO=1.2, INPUT_DPI=1600,
                ANGLE_ROTATION=-2.0, LUT_POINTS=pts)
        settings, mapping = rx.maccel_to_rawaccel(c)
        self.assertEqual(rx.validate_settings(settings), [])
        ax = args_x(settings)
        self.assertEqual(ax["mode"], "lut")
        self.assertFalse(ax[rx.K_GAIN])
        self.assertEqual(ax["data"], [1.0, 0.75, 5.5, 1.0, 12.0, 1.4, 25.0, 1.85, 40.0, 1.6])
        self.assertEqual(mapping["lossy"], [])
        self.assertFalse(mapping["flat"])

        # Edge speeds cover every knot plus surrounding epsilons and midpoints
        edges = rx.edge_speeds(c)
        for kx, _ in pts:
            self.assertIn(kx, edges)
            self.assertTrue(any(abs(x - (kx + 1e-6 * kx)) < 1e-12 for x in edges))
        self.assertIn(0.5 * (1.0 + 5.5), edges)

        # Exact curve_check equivalence (< 1e-12)
        chk = rx.curve_check(c, settings=settings)
        self.assertTrue(chk["equivalent"], chk)
        self.assertLessEqual(chk["max_rel_error"], 1e-12)
        self.assertLessEqual(chk["maccel_formula_max_rel_error"], 1e-12)
        self.assertLessEqual(chk["pipeline_max_rel_error"], 1e-12)

        # Reverse mapping round-trip
        back, rep = rx.rawaccel_to_maccel(settings)
        self.assertEqual(back["MODE"], "lookup")
        self.assertEqual(back["LUT_POINTS"], pts)
        self.assertEqual(rep["lossy"], [])
        self.assertEqual(rx.maccel_to_rawaccel(back)[0], settings)

        # Hand-verified clamp & linear interpolation values
        p = prof_of(settings)
        self.assertAlmostEqual(rx.rawaccel_profile_sens(0.2, p)[0], 0.75 * 0.85, places=14)   # below x0
        self.assertAlmostEqual(rx.rawaccel_profile_sens(1.0, p)[0], 0.75 * 0.85, places=14)   # at x0
        self.assertAlmostEqual(rx.rawaccel_profile_sens(3.25, p)[0], 0.875 * 0.85, places=14) # midpoint
        self.assertAlmostEqual(rx.rawaccel_profile_sens(99.0, p)[0], 1.6 * 0.85, places=14)   # above x_last
        self.assertAlmostEqual(rx.rawaccel_profile_sens(99.0, p)[1], 1.6 * 0.85 * 1.2, places=14)

    def test_lookup_rejections_for_missing_velocity_and_malformed_tables(self):
        # Missing LUT_POINTS when MODE == "lookup"
        no_lut = {**rx.DEFAULTS, "MODE": "lookup"}
        with self.assertRaisesRegex(rx.RawAccelExportError, "missing maccel parameter.*LUT_POINTS"):
            rx.normalize_maccel(no_lut)

        # Reject Velocity LUT (Gain = true) on import
        s, _ = rx.maccel_to_rawaccel(FIXTURES["lookup_ramp"])
        vel = copy.deepcopy(s)
        args_x(vel)[rx.K_GAIN] = True
        with self.assertRaisesRegex(rx.RawAccelExportError, "Velocity mode"):
            rx.rawaccel_to_maccel(vel)

        # Malformed / out-of-range / non-monotonic / too-few / too-many points
        bad_tables = [
            ([], "between 2 and 64"),
            ([[0.0, 1.0]], "between 2 and 64"),
            ([[float(i), 1.0] for i in range(65)], "between 2 and 64"),
            ([[5.0, 1.0], [5.0, 1.5]], "strictly greater"),
            ([[5.0, 1.0], [4.0, 1.5]], "strictly greater"),
            ([[1.0, 1.0], [1.0004, 1.5]], "at least 0.001"),
            ([[-0.01, 1.0], [10.0, 1.5]], "between 0 and 1000"),
            ([[0.0, 1.0], [1000.5, 1.5]], "between 0 and 1000"),
            ([[0.0, 0.04], [10.0, 1.5]], "between 0.05 and 10"),
            ([[0.0, 1.0], [10.0, 10.01]], "between 0.05 and 10"),
            ([[0.0, float("nan")], [10.0, 1.5]], "finite numbers"),
            ([[0.0, True], [10.0, 1.5]], "finite numbers"),
            ([[0.0], [10.0, 1.5]], "pair"),
        ]
        for bad, pattern in bad_tables:
            with self.subTest(bad=bad), self.assertRaisesRegex(rx.RawAccelExportError, pattern):
                rx.normalize_maccel(cfg(MODE="lookup", LUT_POINTS=bad))

        # validate_settings catches odd-length and non-monotonic Raw Accel LUT data
        for mutate, msg in (
            (lambda d: args_x(d).update(data=[0.0, 1.0, 10.0]), "at least 2 points"),
            (lambda d: args_x(d).update(data=[0.0, 1.0, 5.0, 1.2, 10.0]), "x, y pairs"),
            (lambda d: args_x(d).update(data=[5.0, 1.0, 5.0, 1.2]), "strictly increasing"),
        ):
            t = copy.deepcopy(s)
            mutate(t)
            with self.subTest(msg=msg):
                self.assertTrue(any(msg in e for e in rx.validate_settings(t)), rx.validate_settings(t))

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_maccel_eval_matches_python_and_rawaccel_on_lookup_curves(self):
        with open(os.path.join(REPO, "web", "accel_graph.js")) as f:
            src = f.read()
        js = re.search(r"^(?:export )?function maccelEval\(x, cfg\) \{.*?^\}$", src, re.S | re.M).group(0).removeprefix("export ")
        pts64 = [[round(i * 1.5, 3), round(0.5 + 1.8 * math.sin(i / 63.0 * math.pi), 4)] for i in range(64)]
        curves_list = [
            FIXTURES["lookup_ramp"],
            FIXTURES["lookup_scurve"],
            cfg(MODE="lookup", ACCEL=0.005, SENS_MULT=1.35, YX_RATIO=0.75, LUT_POINTS=pts64),
        ]
        cases = []
        for c in curves_list:
            speeds = sorted(set([0.001, 0.5, 1.0, 5.0, 12.5, 25.0, 40.0, 120.0] + rx.edge_speeds(c)[::4]))
            for x in speeds:
                cases.append((x, c))
        script = (js + "\nlet d='';process.stdin.on('data',c=>d+=c).on('end',()=>{"
                  "const out=JSON.parse(d).map(([x,c])=>maccelEval(x,c));process.stdout.write(JSON.stringify(out));});")
        res = subprocess.run(["node", "-e", script], input=json.dumps(cases), capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        js_vals = json.loads(res.stdout)
        worst = 0.0
        for (x, c), (jx, jy) in zip(cases, js_vals):
            px, py = rx.app_sens(x, c)
            rax, ray = rx.rawaccel_profile_sens(x, prof_of(rx.maccel_to_rawaccel(c)[0]))
            worst = max(worst, rx._rel(jx, px), rx._rel(jy, py), rx._rel(jx, rax), rx._rel(jy, ray))
        self.assertLessEqual(worst, 1e-12)

    def test_cli_preview_and_export_with_lookup_set(self):
        res = self.cli(["preview", "--set", "MODE=lookup", "LUT_POINTS=0,1;10,1.5;25,2.0", "ACCEL=0.005"])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Raw Accel lut (Gain off)", res.stdout)
        self.assertIn("EQUIVALENT:", res.stdout)
        self.assertIn("LUT_POINTS", res.stdout)


if __name__ == "__main__":
    unittest.main()
