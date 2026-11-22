#!/usr/bin/env python3
"""Unit and regression tests for MT-018, MT-019, MT-020, MT-022, and MT-023."""
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


if __name__ == "__main__":
    unittest.main()
