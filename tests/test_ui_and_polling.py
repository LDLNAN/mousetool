#!/usr/bin/env python3
"""Unit and regression tests for MT-018, MT-019, MT-020, MT-022, MT-023, and MT-024."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from mousetool.drivers import egg as egg_mod
from mousetool.drivers import glorious as glorious_mod
from mousetool.drivers import pulsar as pulsar_mod
from mousetool.drivers import razer as razer_mod

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"


def _run_node_esm(script: str):
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


class TestMT019PollingMeterAndProtocols(unittest.TestCase):
    def test_polling_meter_steady_rates_1k_2k_4k_8k(self):
        script = """
        import { createPollingMeter } from "./web/polling_meter.js";
        const out = {};
        for (const hz of [1000, 2000, 4000, 8000]) {
          const m = createPollingMeter();
          const dt = 1000 / hz;
          let last = null;
          for (let i = 0; i <= hz * 0.5; i++) {
            last = m.pushTimestamp(i * dt);
          }
          out[hz] = last;
        }
        console.log(JSON.stringify(out));
        """
        data = json.loads(_run_node_esm(script))
        for hz in (1000, 2000, 4000, 8000):
            st = data[str(hz)]
            self.assertTrue(st["ready"])
            self.assertAlmostEqual(st["hz"], hz, delta=hz * 0.01)
            self.assertAlmostEqual(st["peakHz"], hz, delta=hz * 0.01)
            self.assertAlmostEqual(st["avgIntervalMs"], 1000.0 / hz, delta=0.01)

    def test_polling_meter_quantized_and_vblank_batched_2k_never_inflates(self):
        script = """
        import { createPollingMeter } from "./web/polling_meter.js";

        // Case A: 0.1 ms quantized timestamps with strong jitter (0.1ms and 0.9ms alternating, true avg = 0.50ms = 2000Hz)
        const mQuant = createPollingMeter();
        let t = 0;
        let stQuant = null;
        for (let i = 0; i < 1200; i++) {
          stQuant = mQuant.pushTimestamp(Math.round(t * 10) / 10);
          t += (i % 2 === 0) ? 0.1 : 0.9;
        }

        // Case B: 120 Hz Wayland/compositor vblank batching (8.333ms frames, 16-17 events packed in 0.1ms at frame delivery)
        const mBatch = createPollingMeter();
        let stBatch = null;
        let totalExpected = 0;
        let maxPeak = 0;
        for (let frame = 0; frame < 72; frame++) { // 600 ms
          const frameTs = frame * (1000 / 120);
          const nEvents = (frame % 3 === 0) ? 16 : 17; // avg 16.667 events / 8.333ms = 2000 Hz
          totalExpected += nEvents;
          const coalesced = [];
          for (let k = 0; k < nEvents; k++) {
            // Same 0.1 ms quantized cluster on arrival
            const ceTs = Math.round((frameTs + (k / nEvents) * 0.1) * 10) / 10;
            coalesced.push({ timeStamp: ceTs });
          }
          stBatch = mBatch.ingestPointerEvent({ timeStamp: frameTs, getCoalescedEvents: () => coalesced }, "raw");
          if (stBatch.peakHz > maxPeak) maxPeak = stBatch.peakHz;
        }

        // Case C: Double-listener interleaving (pointerrawupdate + pointermove firing for same motion at 2000 Hz)
        const mDual = createPollingMeter();
        let stDual = null;
        for (let frame = 0; frame < 60; frame++) {
          const base = frame * 10;
          const coalesced = [];
          for (let k = 0; k < 20; k++) {
            const ceTs = base + k * 0.5;
            coalesced.push({ timeStamp: ceTs });
            mDual.ingestPointerEvent({ timeStamp: ceTs }, "raw");
          }
          stDual = mDual.ingestPointerEvent({ timeStamp: base + 9.5, getCoalescedEvents: () => coalesced }, "move");
        }
        stDual = mDual.getState();

        // Case D: Out-of-order timestamp & pause gap (> 80 ms) handling
        const mGap = createPollingMeter();
        for (let i = 0; i < 400; i++) mGap.pushTimestamp(i * 0.5);
        const beforeGap = mGap.getState();
        // Out-of-order timestamp must be ignored
        mGap.pushTimestamp(10.0);
        const afterOop = mGap.getState();
        // 150 ms pause then resume at 2000 Hz
        for (let i = 0; i < 600; i++) mGap.pushTimestamp(350 + i * 0.5);
        const afterResume = mGap.getState();

        console.log(JSON.stringify({
          stQuant,
          stBatch,
          maxBatchPeak: maxPeak,
          totalExpected,
          stDual,
          beforeGap,
          afterOop,
          afterResume,
        }));
        """
        res = json.loads(_run_node_esm(script))

        # Case A: 0.1ms + 0.9ms quantized stream averages 0.50ms -> 2000 Hz (old 1/dt EMA gave ~5555 Hz!)
        self.assertGreaterEqual(res["stQuant"]["hz"], 1950)
        self.assertLessEqual(res["stQuant"]["hz"], 2050)
        self.assertGreaterEqual(res["stQuant"]["peakHz"], 1950)
        self.assertLessEqual(res["stQuant"]["peakHz"], 2050)
        self.assertAlmostEqual(res["stQuant"]["avgIntervalMs"], 0.50, delta=0.03)

        # Case B: 120 Hz batched 2000 Hz stream must stay ~2000 Hz (never 4446-6601 Hz)
        self.assertEqual(res["stBatch"]["totalEvents"], res["totalExpected"])
        self.assertGreaterEqual(res["stBatch"]["hz"], 1900)
        self.assertLessEqual(res["stBatch"]["hz"], 2120)
        self.assertGreaterEqual(res["maxBatchPeak"], 1900)
        self.assertLessEqual(res["maxBatchPeak"], 2120)

        # Case C: Dual pointerrawupdate + pointermove never double-counts
        self.assertEqual(res["stDual"]["totalEvents"], 1200)
        self.assertGreaterEqual(res["stDual"]["hz"], 1950)
        self.assertLessEqual(res["stDual"]["hz"], 2050)
        self.assertLessEqual(res["stDual"]["peakHz"], 2050)

        # Case D: Out-of-order ignored & gap reset works cleanly
        self.assertEqual(res["beforeGap"]["totalEvents"], res["afterOop"]["totalEvents"])
        self.assertGreaterEqual(res["afterResume"]["hz"], 1950)
        self.assertLessEqual(res["afterResume"]["hz"], 2050)

    def test_protocol_2000hz_encodings_match_python_and_js(self):
        # Python side verification
        self.assertEqual(razer_mod.POLL2_HZ[2000], 0x04)
        self.assertEqual(razer_mod.POLL2_CODES[0x04], 2000)
        self.assertEqual(8000 // 2000, 4)
        self.assertEqual(pulsar_mod.XS1_POLL_RATES[15], 2000)
        self.assertEqual(glorious_mod.CORE2_RATES[2000], 0x20)

        # JS driver verification + Razer HyperPolling 2000 Hz setPoll/getPoll packet trace
        script = """
        import { POLL2_CODES, POLL2_HZ, makeRazerApi } from "./web/drivers/razer.js";
        import { EGG_POLL_DIVS } from "./web/drivers/egg.js";

        const sent = [];
        let storedRateCode = POLL2_HZ[2000];
        const dev = {
          vendorId: 0x1532,
          productId: 0x00b3, // HyperPolling Wireless Dongle
          serialNumber: "TEST",
          opened: true,
          async sendFeatureReport(_rid, buf) {
            const u = new Uint8Array(buf.buffer || buf, buf.byteOffset || 0, buf.byteLength || buf.length);
            sent.push(Array.from(u));
            if (u[6] === 0x00 && u[7] === 0x40) {
              storedRateCode = u[9];
            }
          },
          async receiveFeatureReport(_rid) {
            const last = sent[sent.length - 1];
            const rep = new Uint8Array(90);
            rep[0] = 0x02; // status ok
            rep[1] = last[1];
            rep[5] = last[5];
            rep[6] = last[6];
            rep[7] = last[7];
            if (last[6] === 0x00 && last[7] === 0xc0) {
              rep[8] = 0x00;
              rep[9] = storedRateCode;
            }
            return new DataView(rep.buffer);
          },
        };
        const sess = {
          dev,
          shortName: "Viper V2 Pro",
          tier: 2,
          tierLabel: "Tier 2",
          writeEnabled: true,
          operations: { polling: { read: true, write: true, txn: "0x1f", rates: [125, 250, 500, 1000, 2000, 4000, 8000] } },
          caps: { hyper: true },
          queue: Promise.resolve(),
        };
        const api = makeRazerApi(sess, { isConnected: () => true, log: () => {} });
        await api.setPoll(2000);
        const readBack = await api.getPoll();
        console.log(JSON.stringify({
          poll2Code2k: POLL2_HZ[2000],
          poll2Decode04: POLL2_CODES[0x04],
          eggDiv2k: EGG_POLL_DIVS[2000],
          readBack,
          writeArgs: sent.filter((p) => p[6] === 0x00 && p[7] === 0x40).map((p) => p.slice(8, 10)),
        }));
        """
        res = json.loads(_run_node_esm(script))
        self.assertEqual(res["poll2Code2k"], 0x04)
        self.assertEqual(res["poll2Decode04"], 2000)
        self.assertEqual(res["eggDiv2k"], 4)
        self.assertEqual(res["readBack"], 2000)
        self.assertEqual(res["writeArgs"], [[0x00, 0x04], [0x01, 0x04]])


class TestMT018_020_022_023StaticContracts(unittest.TestCase):
    def test_css_and_html_contracts(self):
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        accel_js = (WEB_DIR / "accel_graph.js").read_text(encoding="utf-8")

        # MT-018: stable scrollbar gutter on root
        self.assertRegex(css, r"html\s*\{[^}]*scrollbar-gutter:\s*stable")

        # MT-020: uniform .ghead height
        self.assertRegex(css, r"\.ghead\s*\{[^}]*min-height:\s*44px")

        # MT-022: mouse wireframe Forward/Back buttons aligned along left flank without staggered X
        fwd = re.search(r'<rect[^>]*data-btn="5"[^>]*>', html)
        back = re.search(r'<rect[^>]*data-btn="4"[^>]*>', html)
        self.assertIsNotNone(fwd)
        self.assertIsNotNone(back)
        fwd_x = int(re.search(r'x="(\d+)"', fwd.group(0)).group(1))
        back_x = int(re.search(r'x="(\d+)"', back.group(0)).group(1))
        self.assertEqual(fwd_x, back_x)

        # MT-023: dashed Y sens swatch & 1x base legend match plotted elements
        self.assertIn('id="accelLegendY"', html)
        self.assertIn('id="accelLegendBase"', html)
        self.assertIn('class="sw-line-y"', html)
        self.assertIn('class="sw-base"', html)
        self.assertRegex(css, r"\.curvemeta\s+\.sw-line-y\s*\{[^}]*stroke:\s*var\(--muted\);[^}]*stroke-dasharray:\s*5 3")
        self.assertRegex(css, r"\.curvesvg\s+\.line-y\s*\{[^}]*stroke:\s*var\(--muted\);[^}]*stroke-dasharray:\s*5 3")
        self.assertIn('id="accelPeakDotY"', accel_js)
        self.assertIn('<polygon class="peak-dot-y"', accel_js)
        self.assertIn('accelLegendBase', accel_js)


class TestMT024UxPolishAndTierModal(unittest.TestCase):
    def test_quick_dpi_and_redundant_button_footer_removed_while_cli_driver_intact(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        app_js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
        razer_js = (WEB_DIR / "drivers" / "razer.js").read_text(encoding="utf-8")

        # Deliverable 1: Quick DPI UI row & dead handlers removed
        for removed in ("dpiNowRow", "dpiNowSet", "dpiNowY", "dpiNow", "Quick DPI", "One-off DPI"):
            self.assertNotIn(removed, html)
            self.assertNotIn(removed, app_js)
        # Underlying driver setDpi stays intact
        self.assertIn("setDpi", razer_js)

        # Deliverable 5: Redundant Buttons card footer removed
        self.assertNotIn("btnFootnote", html)
        self.assertNotIn("btnFootnote", app_js)
        self.assertNotIn("Primary click stays locked so you can't lock yourself out", html)
        self.assertNotIn("Primary click stays locked so you can't lock yourself out", app_js)

        # Deliverable 4: #track is accessible (not aria-hidden)
        track_tag = re.search(r'<div[^>]*id="track"[^>]*>', html)
        self.assertIsNotNone(track_tag)
        self.assertNotIn('aria-hidden="true"', track_tag.group(0))

        # Deliverable 6: Live polling rate meter locked to fixed width & height with tabular-nums
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="control hzcontrol"', html)
        self.assertRegex(css, r"\.hzcontrol\s*\{[^}]*width:\s*440px;[^}]*max-width:\s*100%")
        self.assertRegex(css, r"\.hzbox\s*\{[^}]*height:\s*58px")
        self.assertRegex(css, r"\.hzstats\s*\{[^}]*font-variant-numeric:\s*tabular-nums")

        # Deliverable 7: Nav tabs reserve bold width via data-label + ::after so switching never shifts tabs
        for tab_label in ("Sensitivity", "Buttons", "Power", "Dongle", "Device", "Acceleration"):
            self.assertIn(f'data-label="{tab_label}"', html)
        self.assertRegex(css, r"\.tab::after\s*\{[^}]*content:\s*attr\(data-label\);[^}]*font-weight:\s*600;[^}]*height:\s*0")

    def test_accel_y_swipe_trail_uses_phase_locked_clip_path(self):
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")
        accel_js = (WEB_DIR / "accel_graph.js").read_text(encoding="utf-8")

        # Both base Y line and swipe Y trail share identical 5 3 dasharray; trail is opaque & >= base width
        line_y_m = re.search(r"\.curvesvg\s+\.line-y\s*\{([^}]+)\}", css)
        trail_y_m = re.search(r"\.curvesvg\s+\.swipe-trail-y\s*\{([^}]+)\}", css)
        self.assertIsNotNone(line_y_m)
        self.assertIsNotNone(trail_y_m)
        self.assertIn("stroke-dasharray: 5 3", line_y_m.group(1))
        self.assertIn("stroke-dasharray: 5 3", trail_y_m.group(1))
        self.assertIn("opacity: 1", trail_y_m.group(1))
        base_w = float(re.search(r"stroke-width:\s*([0-9.]+)", line_y_m.group(1)).group(1))
        trail_w = float(re.search(r"stroke-width:\s*([0-9.]+)", trail_y_m.group(1)).group(1))
        self.assertGreaterEqual(trail_w, base_w)

        # Structural & z-order checks in accel_graph.js
        self.assertIn('clipPath id="accelSwipeClipY"', accel_js)
        self.assertIn('clip-path="url(#accelSwipeClipY)"', accel_js)
        line_y_pos = accel_js.index('id="accelLineY"')
        trail_y_pos = accel_js.index('id="accelSwipeTrailY"')
        line_x_pos = accel_js.index('id="accelLineX"')
        self.assertLess(line_y_pos, trail_y_pos, "swipe-trail-y must render above line-y")
        self.assertLess(trail_y_pos, line_x_pos, "line-x must stay above Y layers")

        # Exact same full-length pathY used on both accelLineY and accelSwipeTrailY
        self.assertIn('<path class="line-y" id="accelLineY" d="${pathY}"/>', accel_js)
        self.assertIn('<path class="swipe-trail-y" id="accelSwipeTrailY" d="${pathY}" clip-path="url(#accelSwipeClipY)"/>', accel_js)

    def test_tier_badge_button_and_modal_behavior_and_wcag_aa(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        css = (WEB_DIR / "styles.css").read_text(encoding="utf-8")

        # Deliverable 3: interactive button + dialog semantics + Device tab info trigger
        self.assertRegex(html, r'<button[^>]*id="tierBadge"[^>]*aria-haspopup="dialog"[^>]*aria-controls="tierModal"')
        self.assertRegex(html, r'<button[^>]*id="supportTierInfo"[^>]*aria-haspopup="dialog"[^>]*aria-controls="tierModal"')
        self.assertRegex(html, r'<div[^>]*id="tierModal"[^>]*role="dialog"[^>]*aria-modal="true"')
        for tier in ("2", "1", "0"):
            self.assertIn(f'data-tier="{tier}"', html)

        # Behavioral test of createTierModalController across Tier 2, Tier 1, Tier 0 & disconnected
        script = """
        import { createTierModalController } from "./web/tier_modal.js";

        function makeEl(id, extra = {}) {
          const listeners = {};
          const attrs = {};
          const classes = new Set();
          return {
            id,
            hidden: false,
            textContent: "",
            className: "",
            dataset: {},
            focused: false,
            isConnected: true,
            classList: {
              toggle(c, on) { if (on) classes.add(c); else classes.delete(c); },
              contains(c) { return classes.has(c); },
            },
            setAttribute(k, v) { attrs[k] = String(v); },
            getAttribute(k) { return attrs[k] ?? null; },
            removeAttribute(k) { delete attrs[k]; },
            addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
            dispatch(type, ev = {}) {
              for (const fn of listeners[type] || []) fn({ target: this, preventDefault() {}, stopPropagation() {}, ...ev });
            },
            focus() { this.focused = true; doc.activeElement = this; },
            ...extra,
          };
        }

        const cards = [2, 1, 0].map((t) => {
          const tag = makeEl(`tag-${t}`);
          tag.hidden = true;
          const card = makeEl(`card-${t}`, {
            dataset: { tier: String(t) },
            querySelector(sel) { return sel === ".tier-current-tag" ? tag : null; },
            _tag: tag,
          });
          return card;
        });

        const els = {
          tierBadge: makeEl("tierBadge"),
          supportTierInfo: makeEl("supportTierInfo"),
          tierModalClose: makeEl("tierModalClose"),
          tierModalDone: makeEl("tierModalDone"),
          tierModalActive: makeEl("tierModalActive"),
          tierModalActiveName: makeEl("tierModalActiveName"),
          tierModalActiveTier: makeEl("tierModalActiveTier"),
          tierModalActiveTierText: makeEl("tierModalActiveTierText"),
          tierModalActiveNote: makeEl("tierModalActiveNote"),
        };
        const modal = makeEl("tierModal", {
          hidden: true,
          querySelectorAll(sel) {
            if (sel === ".tier-card[data-tier]") return cards;
            return [els.tierModalClose, els.tierModalDone];
          },
        });
        els.tierModal = modal;

        const docListeners = {};
        const doc = {
          activeElement: null,
          addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); },
          dispatch(type, ev = {}) {
            for (const fn of docListeners[type] || []) fn({ preventDefault() {}, stopPropagation() {}, ...ev });
          },
        };

        let session = { name: "Razer Viper V2 Pro", tier: 2, tierLabel: "Tier 2 · Verified" };
        const ctrl = createTierModalController({ $: (id) => els[id] ?? null, getSession: () => session, doc });

        const snap = () => ({
          open: ctrl.isOpen(),
          expanded: els.tierBadge.getAttribute("aria-expanded"),
          activeHidden: els.tierModalActive.hidden,
          activeName: els.tierModalActiveName.textContent,
          activeTierText: els.tierModalActiveTierText.textContent,
          currentTiers: cards.filter((c) => c.classList.contains("is-current")).map((c) => Number(c.dataset.tier)),
          visibleTags: cards.filter((c) => !c._tag.hidden).map((c) => Number(c.dataset.tier)),
          ariaCurrents: cards.filter((c) => c.getAttribute("aria-current") === "true").map((c) => Number(c.dataset.tier)),
          focusedId: doc.activeElement?.id ?? null,
        });

        // Open via badge click (Tier 2)
        els.tierBadge.dispatch("click");
        const onTier2 = snap();

        // Close via Escape -> restores focus to #tierBadge
        doc.dispatch("keydown", { key: "Escape" });
        const afterEsc = snap();

        // Switch to Tier 1 and open via Device tab info button
        session = { name: "Razer Viper Mini", tier: 1, tierLabel: "Tier 1 · Read-only" };
        els.supportTierInfo.dispatch("click");
        const onTier1 = snap();

        // Close via backdrop click -> restores focus to #supportTierInfo
        modal.dispatch("click", { target: modal });
        const afterBackdrop = snap();

        // Switch to Tier 0 and open, then live-sync disconnect
        session = { name: "Standard USB Mouse", tier: 0, tierLabel: "Tier 0 · Pointer only" };
        els.tierBadge.dispatch("click");
        const onTier0 = snap();

        session = null;
        ctrl.sync();
        const onDisconnected = snap();

        // Close via Done button
        els.tierModalDone.dispatch("click");
        const afterDone = snap();

        console.log(JSON.stringify({ onTier2, afterEsc, onTier1, afterBackdrop, onTier0, onDisconnected, afterDone }));
        """
        out = json.loads(_run_node_esm(script))
        self.assertTrue(out["onTier2"]["open"])
        self.assertEqual(out["onTier2"]["expanded"], "true")
        self.assertFalse(out["onTier2"]["activeHidden"])
        self.assertEqual(out["onTier2"]["activeName"], "Razer Viper V2 Pro")
        self.assertEqual(out["onTier2"]["currentTiers"], [2])
        self.assertEqual(out["onTier2"]["visibleTags"], [2])
        self.assertEqual(out["onTier2"]["ariaCurrents"], [2])
        self.assertEqual(out["onTier2"]["focusedId"], "tierModalClose")

        self.assertFalse(out["afterEsc"]["open"])
        self.assertEqual(out["afterEsc"]["expanded"], "false")
        self.assertEqual(out["afterEsc"]["focusedId"], "tierBadge")

        self.assertTrue(out["onTier1"]["open"])
        self.assertEqual(out["onTier1"]["currentTiers"], [1])
        self.assertEqual(out["onTier1"]["visibleTags"], [1])
        self.assertEqual(out["onTier1"]["activeTierText"], "Tier 1 · Read-only")

        self.assertFalse(out["afterBackdrop"]["open"])
        self.assertEqual(out["afterBackdrop"]["focusedId"], "supportTierInfo")

        self.assertEqual(out["onTier0"]["currentTiers"], [0])
        self.assertEqual(out["onTier0"]["visibleTags"], [0])

        self.assertTrue(out["onDisconnected"]["activeHidden"])
        self.assertEqual(out["onDisconnected"]["currentTiers"], [])
        self.assertEqual(out["onDisconnected"]["visibleTags"], [])

        self.assertFalse(out["afterDone"]["open"])

        # WCAG AA (>= 4.5:1) contrast verification for light & dark themes on modal surfaces & tier badges
        def srgb(c8):
            c = c8 / 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        def lum(rgb):
            return 0.2126 * srgb(rgb[0]) + 0.7152 * srgb(rgb[1]) + 0.0722 * srgb(rgb[2])

        def ratio(fg, bg):
            l1, l2 = lum(fg), lum(bg)
            return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)

        def parse_hex(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

        def blend(rgba, bg):
            r, g, b, a = rgba
            return (round(r * a + bg[0] * (1 - a)), round(g * a + bg[1] * (1 - a)), round(b * a + bg[2] * (1 - a)))

        def extract_vars(block):
            vals = {}
            for m in re.finditer(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block):
                vals[m.group(1)] = parse_hex(m.group(2))
            for m in re.finditer(r"--([a-z0-9-]+):\s*rgba\(\s*(\d+),\s*(\d+),\s*(\d+),\s*([0-9.]+)\)", block):
                vals[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)), float(m.group(5)))
            return vals

        light_vars = extract_vars(re.search(r":root\s*\{([^}]+)\}", css).group(1))
        dark_vars = extract_vars(re.search(r':root\[data-theme="dark"\]\s*\{([^}]+)\}', css).group(1))

        for theme_name, v in (("light", light_vars), ("dark", dark_vars)):
            surf = v["surface"]
            surf2 = v["surface-2"]
            inset = v["inset"]
            ok_bg = blend(v["ok-soft"], surf)
            warn_bg = blend(v["warn-soft"], surf)
            checks = [
                ("tier-0 badge ink-2 on surface-2", v["ink-2"], surf2),
                ("tier-1 badge warn-ink on warn-soft", v["warn-ink"], warn_bg),
                ("tier-2 badge ok-ink on ok-soft", v["ok-ink"], ok_bg),
                ("modal desc ink-2 on inset", v["ink-2"], inset),
                ("modal desc ink-2 on active tier-2", v["ink-2"], ok_bg),
                ("modal desc ink-2 on active tier-1", v["ink-2"], warn_bg),
                ("modal desc ink-2 on active tier-0", v["ink-2"], surf2),
                ("tier-2 current tag ok-ink on surface", v["ok-ink"], surf),
                ("tier-1 current tag warn-ink on surface", v["warn-ink"], surf),
                ("tier-0 current tag ink on surface", v["ink"], surf),
            ]
            for label, fg, bg in checks:
                cr = ratio(fg, bg)
                self.assertGreaterEqual(cr, 4.5, f"{theme_name} {label} contrast {cr:.2f} < 4.5")

    def test_dpi_track_drag_snap_clamp_commit_once_and_keyboard(self):
        script = """
        import { dpiToLogPct, logPctToDpi, snapDpiValue, createDpiTrackController } from "./web/dpi_track.js";

        // Math round-trip & snapping checks
        const cfg = { min: 100, max: 30000, step: 50, stepHi: 50, canWrite: true };
        const rt800 = logPctToDpi(dpiToLogPct(800, 100, 30000), cfg);
        const rt1600 = logPctToDpi(dpiToLogPct(1600, 100, 30000), cfg);
        const clampLo = logPctToDpi(-25, cfg);
        const clampHi = logPctToDpi(140, cfg);
        const snap100 = snapDpiValue(1240, { min: 100, max: 16000, step: 100 });

        // Interactive pointer-capture drag + click + cancel + keyboard test
        const listeners = {};
        const trackClasses = new Set();
        const markClasses = new Set();
        const markAttrs = {};
        let capturedId = null;
        let releasedId = null;
        const mark = {
          dataset: { stageIdx: "1", dpi: "800" },
          style: { left: "36%" },
          title: "",
          classList: {
            add(c) { markClasses.add(c); },
            remove(c) { markClasses.delete(c); },
            contains(c) { return markClasses.has(c); },
          },
          setAttribute(k, v) { markAttrs[k] = String(v); },
          closest(sel) { return sel === ".mark[data-stage-idx]" ? mark : null; },
          setPointerCapture(id) { capturedId = id; },
          releasePointerCapture(id) { releasedId = id; },
          focus() {},
        };
        const trackEl = {
          innerHTML: "",
          classList: {
            add(c) { trackClasses.add(c); },
            remove(c) { trackClasses.delete(c); },
            contains(c) { return trackClasses.has(c); },
          },
          getBoundingClientRect() { return { left: 100, width: 500 }; },
          addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
          dispatch(type, ev) {
            for (const fn of listeners[type] || []) fn({ target: mark, preventDefault() {}, ...ev });
          },
        };

        const stages = { active: 1, list: [{ x: 400, y: 400 }, { x: 800, y: 800 }, { x: 1600, y: 1600 }] };
        const previews = [];
        const selects = [];
        const reverts = [];
        let commits = 0;

        const ctrl = createDpiTrackController({
          trackEl,
          getConfig: () => cfg,
          getStages: () => stages,
          getLiveDpi: () => [800, 800],
          onPreviewDpi: (idx, dpi) => {
            stages.list[idx].x = dpi;
            stages.list[idx].y = dpi;
            previews.push([idx, dpi]);
          },
          onSelectStage: (idx) => {
            stages.active = idx + 1;
            selects.push(idx);
          },
          onCommitStages: () => { commits++; },
          onRevertDrag: (idx, sx, sy) => {
            stages.list[idx].x = sx;
            stages.list[idx].y = sy;
            reverts.push([idx, sx, sy]);
          },
        });

        // 1. Plain click (<4px) selects stage 2 without dragging or committing
        const startX = 100 + (dpiToLogPct(800, 100, 30000) / 100) * 500;
        trackEl.dispatch("pointerdown", { pointerId: 1, button: 0, clientX: startX });
        trackEl.dispatch("pointermove", { pointerId: 1, clientX: startX + 2 });
        trackEl.dispatch("pointerup", { pointerId: 1, clientX: startX + 2 });
        const afterClick = {
          active: stages.active,
          selects: [...selects],
          commits,
          previewsLen: previews.length,
        };

        // 2. Drag stage 2 to 3200 DPI across multiple moves -> 0 commits mid-drag, 1 commit on release
        const target3200X = 100 + (dpiToLogPct(3200, 100, 30000) / 100) * 500;
        trackEl.dispatch("pointerdown", { pointerId: 2, button: 0, clientX: startX });
        trackEl.dispatch("pointermove", { pointerId: 2, clientX: (startX + target3200X) / 2 });
        const midDrag = {
          dragging: ctrl.isDragging(),
          commitsDuringDrag: commits,
          markHasDragging: markClasses.has("dragging"),
          trackHasDragging: trackClasses.has("is-dragging"),
          capturedId,
        };
        trackEl.dispatch("pointermove", { pointerId: 2, clientX: target3200X });
        trackEl.dispatch("pointerup", { pointerId: 2, clientX: target3200X });
        const afterDrag = {
          dragging: ctrl.isDragging(),
          stage2Dpi: stages.list[1].x,
          stage2Y: stages.list[1].y,
          commits,
          releasedId,
          ariaNow: markAttrs["aria-valuenow"],
          previewsCount: previews.length,
        };

        // 3. Drag past right bound clamps to 30000, then cancel reverts cleanly without committing
        const commitsBeforeCancel = commits;
        trackEl.dispatch("pointerdown", { pointerId: 3, button: 0, clientX: target3200X });
        trackEl.dispatch("pointermove", { pointerId: 3, clientX: 9999 });
        const clampedMax = stages.list[1].x;
        trackEl.dispatch("pointercancel", { pointerId: 3, clientX: 9999 });
        const afterCancel = {
          stage2Dpi: stages.list[1].x,
          commitsDelta: commits - commitsBeforeCancel,
          revertsCount: reverts.length,
        };

        // 4. Keyboard slider nudging (+50, Shift +250, Home -> 100) commits once on keyup
        const commitsBeforeKbd = commits;
        trackEl.dispatch("keydown", { key: "ArrowRight", shiftKey: false });
        const kbd1 = stages.list[1].x;
        trackEl.dispatch("keydown", { key: "ArrowRight", shiftKey: true });
        const kbd2 = stages.list[1].x;
        trackEl.dispatch("keyup", { key: "ArrowRight" });
        const kbdCommitsAfterUp = commits - commitsBeforeKbd;
        trackEl.dispatch("keydown", { key: "Home", shiftKey: false });
        const kbdHome = stages.list[1].x;
        trackEl.dispatch("keyup", { key: "Home" });

        console.log(JSON.stringify({
          rt800, rt1600, clampLo, clampHi, snap100,
          afterClick, midDrag, afterDrag, clampedMax, afterCancel,
          kbd1, kbd2, kbdCommitsAfterUp, kbdHome,
        }));
        """
        out = json.loads(_run_node_esm(script))
        self.assertEqual(out["rt800"], 800)
        self.assertEqual(out["rt1600"], 1600)
        self.assertEqual(out["clampLo"], 100)
        self.assertEqual(out["clampHi"], 30000)
        self.assertEqual(out["snap100"], 1200)

        self.assertEqual(out["afterClick"]["active"], 2)
        self.assertEqual(out["afterClick"]["selects"], [1])
        self.assertEqual(out["afterClick"]["commits"], 0)
        self.assertEqual(out["afterClick"]["previewsLen"], 0)

        self.assertTrue(out["midDrag"]["dragging"])
        self.assertEqual(out["midDrag"]["commitsDuringDrag"], 0)
        self.assertTrue(out["midDrag"]["markHasDragging"])
        self.assertTrue(out["midDrag"]["trackHasDragging"])
        self.assertEqual(out["midDrag"]["capturedId"], 2)

        self.assertFalse(out["afterDrag"]["dragging"])
        self.assertEqual(out["afterDrag"]["stage2Dpi"], 3200)
        self.assertEqual(out["afterDrag"]["stage2Y"], 3200)
        self.assertEqual(out["afterDrag"]["commits"], 1)
        self.assertEqual(out["afterDrag"]["releasedId"], 2)
        self.assertEqual(out["afterDrag"]["ariaNow"], "3200")
        self.assertGreaterEqual(out["afterDrag"]["previewsCount"], 2)

        self.assertEqual(out["clampedMax"], 30000)
        self.assertEqual(out["afterCancel"]["stage2Dpi"], 3200)
        self.assertEqual(out["afterCancel"]["commitsDelta"], 0)
        self.assertEqual(out["afterCancel"]["revertsCount"], 1)

        self.assertEqual(out["kbd1"], 3250)
        self.assertEqual(out["kbd2"], 3500)
        self.assertEqual(out["kbdCommitsAfterUp"], 1)
        self.assertEqual(out["kbdHome"], 100)


if __name__ == "__main__":
    unittest.main()
