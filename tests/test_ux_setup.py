#!/usr/bin/env python3
"""Unit tests for MT-017: /api/status setup diagnostics (Linux & Windows),
Guided Setup fallback derivation, light-mode tier badge WCAG AA contrast,
split X/Y fleet DPI formatting, and plain-English UI contracts.
"""
import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mousetool import desktop_pointer as desktop_mod
from mousetool import hid_discovery as hid_mod
from mousetool.accel import linux as linux_accel
from mousetool.accel import rawaccel_export as curves
from mousetool.accel.windows import (
    OFF,
    RAWACCEL_BINARIES,
    RAWACCEL_RELEASES_URL,
    WindowsAccel,
    WindowsRoutes,
    windows_setup_status,
)
from mousetool.server import Api

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"


def _srgb_channel(c8: int) -> float:
    c = c8 / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rel_luminance(rgb):
    r, g, b = rgb
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def _contrast_ratio(fg_rgb, bg_rgb) -> float:
    l1, l2 = _rel_luminance(fg_rgb), _rel_luminance(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _hex_to_rgb(hex_str: str):
    h = hex_str.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _blend_rgba_over(rgba, bg_rgb):
    r, g, b, a = rgba
    br, bg, bb = bg_rgb
    return (
        round(r * a + br * (1 - a)),
        round(g * a + bg * (1 - a)),
        round(b * a + bb * (1 - a)),
    )


def _run_node_esm(script: str) -> str:
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is required for JS module tests")
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"Node script failed ({proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout.strip()


class TestLinuxSetupDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.udev_dir = self.root / "udev"
        self.udev_dir.mkdir()
        self.unit_path = self.root / "mousetool.service"

    def _make_state(self, *, installed=False, loaded=False, writable=False, params_ok=True,
                    name="mouseaccel", candidate=None, also_loaded=None, in_group=False, group_exists=False):
        return {
            "installed": installed,
            "loaded": loaded,
            "parameters_available": loaded and params_ok,
            "writable": loaded and params_ok and writable,
            "mode": "linear" if (loaded and params_ok) else None,
            "params": {} if not (loaded and params_ok) else {"ACCEL": 0.0},
            "in_group": in_group,
            "group_exists": group_exists,
            "group": name if (loaded or candidate) else None,
            "backend": {
                "name": name if loaded else None,
                "label": ("maccel (legacy)" if name == "maccel" else name) if loaded else None,
                "legacy": name == "maccel",
                "loaded": loaded,
                "params_path": f"/sys/module/{name}/parameters" if loaded else None,
                "speed_device": f"/dev/{name}" if loaded else None,
                "also_loaded": list(also_loaded or []),
                "installed_backends": [candidate] if candidate else ([name] if installed else []),
                **({"candidate": candidate} if candidate else {}),
            },
        }

    def test_linux_missing_driver_and_rules(self):
        st = self._make_state(installed=False, loaded=False, writable=False)
        setup = linux_accel.linux_setup_status(
            state=st, udev_dirs=[str(self.udev_dir)], service_unit_path=str(self.unit_path), service_active=False
        )
        self.assertEqual(setup["platform"], "linux")
        self.assertFalse(setup["ready"])
        self.assertEqual(setup["driver"]["status"], "needs_setup")
        self.assertFalse(setup["udev"]["mousetool_rules"])
        self.assertFalse(setup["udev"]["accel_rules"])
        self.assertFalse(setup["service"]["unit_installed"])
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "needs_setup")
        self.assertIn("modprobe mouseaccel", steps["driver"]["command"])
        self.assertEqual(steps["permissions"]["status"], "needs_setup")
        self.assertIn("--mouseaccel-perms", steps["permissions"]["command"])
        self.assertEqual(steps["helper"]["status"], "needs_setup")
        self.assertIn("./install.sh --udev", steps["helper"]["command"])

    def test_linux_installed_but_not_loaded(self):
        st = self._make_state(installed=True, loaded=False, candidate="mouseaccel")
        setup = linux_accel.linux_setup_status(
            state=st, udev_dirs=[str(self.udev_dir)], service_unit_path=str(self.unit_path), service_active=True
        )
        self.assertFalse(setup["ready"])
        self.assertEqual(setup["summary"], "Driver not loaded")
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "needs_setup")
        self.assertEqual(steps["driver"]["command"], "sudo modprobe mouseaccel")

    def test_linux_loaded_readonly_recommends_group_or_perms(self):
        (self.udev_dir / "99-mouseaccel.rules").write_text("# rule\n", encoding="utf-8")
        (self.udev_dir / "60-mousetool.rules").write_text("# rule\n", encoding="utf-8")
        self.unit_path.write_text("[Unit]\n", encoding="utf-8")
        st = self._make_state(installed=True, loaded=True, writable=False, in_group=False, group_exists=True)
        setup = linux_accel.linux_setup_status(
            state=st, udev_dirs=[str(self.udev_dir)], service_unit_path=str(self.unit_path), service_active=True
        )
        self.assertFalse(setup["ready"])
        self.assertEqual(setup["summary"], "Needs save permission")
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "installed")
        self.assertEqual(steps["permissions"]["status"], "needs_setup")
        self.assertIn("usermod -aG mouseaccel", steps["permissions"]["command"])
        self.assertEqual(steps["helper"]["status"], "installed")

    def test_linux_loaded_writable_and_installed_is_ready(self):
        (self.udev_dir / "99-mouseaccel.rules").write_text("# rule\n", encoding="utf-8")
        (self.udev_dir / "60-mousetool.rules").write_text("# rule\n", encoding="utf-8")
        self.unit_path.write_text("[Unit]\n", encoding="utf-8")
        st = self._make_state(installed=True, loaded=True, writable=True, in_group=True, group_exists=True)
        setup = linux_accel.linux_setup_status(
            state=st, udev_dirs=[str(self.udev_dir)], service_unit_path=str(self.unit_path), service_active=True
        )
        self.assertTrue(setup["ready"])
        self.assertEqual(setup["driver"]["status"], "installed")
        self.assertTrue(all(s["status"] == "installed" for s in setup["steps"]))

    def test_linux_conflict_both_modules_loaded(self):
        st = self._make_state(installed=True, loaded=True, writable=True, also_loaded=["maccel"])
        setup = linux_accel.linux_setup_status(
            state=st, udev_dirs=[str(self.udev_dir)], service_unit_path=str(self.unit_path), service_active=True
        )
        self.assertFalse(setup["ready"])
        self.assertEqual(setup["summary"], "Driver conflict detected")
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "needs_setup")
        self.assertIn("rmmod maccel", steps["driver"]["command"])

    def test_linux_api_status_exposes_setup_without_running_privileged_commands(self):
        st = self._make_state(installed=False, loaded=False, writable=False)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(linux_accel, "maccel_state", return_value=st), \
                 mock.patch.object(linux_accel, "_check_systemd_service_active", return_value=False), \
                 mock.patch.object(linux_accel, "UDEV_RULES_DIRS", (str(self.udev_dir),)), \
                 mock.patch.object(linux_accel, "SYSTEMD_USER_UNIT", str(self.unit_path)), \
                 mock.patch.object(desktop_mod, "niri_state", return_value={}), \
                 mock.patch.object(desktop_mod, "desktop_state", return_value=None), \
                 mock.patch.object(hid_mod, "discover_system_mice", return_value=[]), \
                 mock.patch("subprocess.run") as m_run:
                conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
                conn.request("GET", "/api/status", headers={"Origin": "null"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read())
                conn.close()
                self.assertIn("setup", body)
                self.assertEqual(body["setup"]["platform"], "linux")
                self.assertFalse(body["setup"]["ready"])
                self.assertEqual(m_run.call_count, 0)
        finally:
            srv.shutdown()
            srv.server_close()


class TestWindowsSetupDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.ra_dir = self.root / "rawaccel"
        self.startup_lnk = self.root / "mousetool Raw Accel.lnk"
        self.active = curves.maccel_to_rawaccel(OFF)[0]

    def test_windows_missing_driver_writer_and_persistence(self):
        backend = WindowsAccel(
            self.state_dir,
            reader=lambda: (_ for _ in ()).throw(OSError("driver missing")),
            writer=lambda p: None,
            probe=lambda: "missing",
            rawaccel_dir=self.ra_dir,
            startup_shortcut=self.startup_lnk,
        )
        setup = backend.setup_status()
        self.assertEqual(setup["platform"], "windows")
        self.assertFalse(setup["ready"])
        self.assertEqual(setup["driver"]["status"], "needs_setup")
        self.assertEqual(setup["driver"]["driver_state"], "missing")
        self.assertFalse(setup["writer_present"])
        self.assertFalse(setup["persistence_installed"])
        self.assertEqual(setup["download_url"], RAWACCEL_RELEASES_URL)
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "needs_setup")
        self.assertEqual(steps["driver"]["url"], RAWACCEL_RELEASES_URL)
        self.assertEqual(steps["writer"]["status"], "needs_setup")
        self.assertIn("install.ps1", steps["writer"]["command"])
        self.assertEqual(steps["persistence"]["status"], "needs_setup")

    def test_windows_installed_and_ready_via_http_status(self):
        self.ra_dir.mkdir(parents=True)
        for name in RAWACCEL_BINARIES:
            (self.ra_dir / name).write_bytes(b"stub")
        self.startup_lnk.write_bytes(b"lnk")

        backend = WindowsAccel(
            self.state_dir,
            reader=lambda: self.active,
            writer=lambda p: None,
            probe=lambda: "running",
            rawaccel_dir=self.ra_dir,
            startup_shortcut=self.startup_lnk,
        )

        class WinHandler(WindowsRoutes, Api):
            pass

        with ThreadingHTTPServer(("127.0.0.1", 0), WinHandler) as server:
            server.acceleration = backend
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection(*server.server_address, timeout=3)
                conn.request("GET", "/api/status")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read())
                conn.close()
                setup = body["setup"]
                self.assertEqual(setup["platform"], "windows")
                self.assertTrue(setup["ready"])
                self.assertTrue(setup["writer_present"])
                self.assertTrue(setup["persistence_installed"])
                self.assertTrue(all(s["status"] == "installed" for s in setup["steps"]))
            finally:
                server.shutdown()
                thread.join()

    def test_windows_driver_running_but_writer_missing(self):
        st = {"driver": "running", "loaded": True, "writable": True}
        setup = windows_setup_status(st, rawaccel_dir=self.ra_dir, startup_shortcut=self.startup_lnk)
        self.assertTrue(setup["ready"])
        steps = {s["id"]: s for s in setup["steps"]}
        self.assertEqual(steps["driver"]["status"], "installed")
        self.assertEqual(steps["writer"]["status"], "needs_setup")
        self.assertEqual(steps["persistence"]["status"], "needs_setup")


class TestMT017UiPolishAndContracts(unittest.TestCase):
    def test_light_mode_tier_badges_meet_wcag_aa_contrast(self):
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        root_block = re.search(r":root\s*\{([^}]+)\}", css)
        self.assertIsNotNone(root_block)
        block = root_block.group(1)

        bg = _hex_to_rgb(re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", block).group(1))
        surface = _hex_to_rgb(re.search(r"--surface:\s*(#[0-9a-fA-F]{6})", block).group(1))
        ok_ink = _hex_to_rgb(re.search(r"--ok-ink:\s*(#[0-9a-fA-F]{6})", block).group(1))
        warn_ink = _hex_to_rgb(re.search(r"--warn-ink:\s*(#[0-9a-fA-F]{6})", block).group(1))

        ok_soft_m = re.search(r"--ok-soft:\s*rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\)", block)
        warn_soft_m = re.search(r"--warn-soft:\s*rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\)", block)
        self.assertIsNotNone(ok_soft_m)
        self.assertIsNotNone(warn_soft_m)
        ok_rgba = (int(ok_soft_m[1]), int(ok_soft_m[2]), int(ok_soft_m[3]), float(ok_soft_m[4]))
        warn_rgba = (int(warn_soft_m[1]), int(warn_soft_m[2]), int(warn_soft_m[3]), float(warn_soft_m[4]))

        for base_name, base_rgb in (("page-bg", bg), ("card-surface", surface)):
            t1_bg = _blend_rgba_over(warn_rgba, base_rgb)
            t2_bg = _blend_rgba_over(ok_rgba, base_rgb)
            t1_ratio = _contrast_ratio(warn_ink, t1_bg)
            t2_ratio = _contrast_ratio(ok_ink, t2_bg)
            self.assertGreaterEqual(t1_ratio, 4.5, f"tier-1 contrast on {base_name} was {t1_ratio:.2f}")
            self.assertGreaterEqual(t2_ratio, 4.5, f"tier-2 contrast on {base_name} was {t2_ratio:.2f}")

        self.assertRegex(css, r"\.tierbadge\.tier-1\s*\{[^}]*color:\s*var\(--warn-ink\)")
        self.assertRegex(css, r"\.tierbadge\.tier-2\s*\{[^}]*color:\s*var\(--ok-ink\)")

    def test_plain_english_and_collapsible_details_contracts(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        accel_js = (WEB_DIR / "accel_graph.js").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")

        # Guided setup container + collapsible disclosures present
        self.assertIn('id="accelSetup"', html)
        self.assertIn('id="deviceTechDetails"', html)
        self.assertIn('id="protocolLogDetails"', html)
        self.assertIn('id="accelExportDetails"', html)

        # Jargon removed from primary everyday copy
        for jargon in ("asymptote multiplier", "0xff getter", "blocked at dispatch", "Applies before libinput"):
            self.assertNotIn(jargon, html)
            self.assertNotIn(jargon, accel_js)

        # Split X/Y DPI formatting on fleet cards
        self.assertIn("`${s.S.dpi[0]}×${s.S.dpi[1]} DPI`", app_js)

    def test_derive_setup_status_fallback_in_js(self):
        script = """
        import { deriveSetupStatus } from "./web/accel_graph.js";

        const linuxMissing = deriveSetupStatus({
          accel: { installed: false, loaded: false, writable: false, backend: { name: null, loaded: false, also_loaded: [] } }
        });
        const linuxRo = deriveSetupStatus({
          accel: { installed: true, loaded: true, writable: false, group: "mouseaccel",
                   backend: { name: "mouseaccel", label: "mouseaccel", loaded: true, also_loaded: [] } }
        });
        const linuxReady = deriveSetupStatus({
          accel: { installed: true, loaded: true, writable: true,
                   backend: { name: "mouseaccel", label: "mouseaccel", loaded: true, also_loaded: [] } }
        });
        const winMissing = deriveSetupStatus({
          accel: { driver: "missing", loaded: false, writable: false, backend: { name: "rawaccel", label: "Raw Accel (Windows)" } }
        });
        console.log(JSON.stringify({ linuxMissing, linuxRo, linuxReady, winMissing }));
        """
        out = json.loads(_run_node_esm(script))
        self.assertFalse(out["linuxMissing"]["ready"])
        self.assertEqual(out["linuxMissing"]["platform"], "linux")
        self.assertFalse(out["linuxRo"]["ready"])
        self.assertEqual(out["linuxRo"]["steps"][0]["status"], "installed")
        self.assertEqual(out["linuxRo"]["steps"][1]["status"], "needs_setup")
        self.assertTrue(out["linuxReady"]["ready"])
        self.assertFalse(out["winMissing"]["ready"])
        self.assertEqual(out["winMissing"]["platform"], "windows")
        self.assertEqual(out["winMissing"]["steps"][0]["url"], RAWACCEL_RELEASES_URL)


if __name__ == "__main__":
    unittest.main()
