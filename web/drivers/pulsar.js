// Protocols 4-6: CompX Report 0x08 / Pulsar XS-1 / Pulsar Pro / Areson / ATK / Scyrox / Teevolution / VGN / Lamzu
import { DevErr, calcReportBytes, findCollection, hex, sleep, unwrapReport, vpKey } from "./common.js";

const PULSAR_VID = 0x3710;
const PULSAR_XS1_DEVICES = {
  0x3404: { name: "Pulsar X2A v3", link: "USB cable", wireless: false, maxPoll: 1000 },
  0x3409: { name: "Pulsar XS-1 Mouse", link: "USB cable", wireless: false, maxPoll: 1000 },
  0x3410: { name: "Pulsar XS-1 Mouse", link: "USB cable", wireless: false, maxPoll: 1000 },
  0x5402: { name: "Pulsar X3 M", link: "Wireless", wireless: true, maxPoll: 1000 },
  0x5403: { name: "Pulsar 8K Dongle", link: "8K dongle", wireless: true, maxPoll: 8000 },
};
const PULSAR_PRO_DEVICES = {
  0x5405: { name: "Pulsar Pro", link: "8K dongle", wireless: true, maxPoll: 8000 },
};

const ARESON_VID = 0x25a7;
const ARESON_DEVICES = {
  0xfa7b: { name: "Pulsar Areson (X2 v2 / X2H / Xlite v3)", link: "USB cable", wireless: false },
  0xfa7c: { name: "Pulsar Areson (X2 v2 / X2H / Xlite v3)", link: "Wireless dongle", wireless: true },
};

const COMPX_VID = 0x3554;
const ATK_VID = 0x373b;
const COMPX_DEVICES = {
  "3554:f507": { brand: "pulsar_4k", name: "Pulsar 4K Receiver", link: "4K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000] },
  "3554:f508": { brand: "pulsar_4k", name: "Pulsar 4K Receiver", link: "4K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000] },
  "3554:f509": { brand: "pulsar_4k", name: "Pulsar 4K Receiver", link: "4K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000] },
  "3554:f515": { brand: "scyrox", model: "v8s", name: "Scyrox V8S", link: "USB cable", wireless: false, dpiMax: 50000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f517": { brand: "scyrox", model: "v8s", name: "Scyrox V8S Turbo", link: "8K dongle", wireless: true, dpiMax: 50000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f5f7": { brand: "scyrox", model: "v8", name: "Scyrox V8", link: "8K dongle", wireless: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f520": { brand: "teevolution", name: "Teevolution Terra Pro", link: "Wireless", wireless: true, dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f522": { brand: "teevolution", name: "Teevolution Terra Pro", link: "USB cable", wireless: false, dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f523": { brand: "teevolution", name: "Teevolution Terra Pro", link: "8K dongle", wireless: true, dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f5bb": { brand: "teevolution", name: "Teevolution Terra Pro", link: "Wireless dongle", wireless: true, dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:fb56": { brand: "vgn", name: "VGN F2 Master+", link: "8K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:fb57": { brand: "vgn", name: "VGN F2 Master+", link: "USB cable", wireless: false, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f50d": { brand: "lamzu", name: "Lamzu Atlantis", link: "1K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000] },
  "3554:f50f": { brand: "lamzu", name: "Lamzu Atlantis", link: "USB cable", wireless: false, dpiMax: 26000, rates: [125, 250, 500, 1000] },
  "3554:f510": { brand: "lamzu", name: "Lamzu Atlantis 4K", link: "4K dongle", wireless: true, dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000] },
  "3554:f58a": { brand: "atk", name: "ATK / VXE Mouse", link: "Wireless dongle", wireless: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f58c": { brand: "atk", name: "ATK / VXE Mouse", link: "USB cable", wireless: false, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f58e": { brand: "atk", name: "ATK / VXE Mouse", link: "Wireless dongle", wireless: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "3554:f58f": { brand: "atk", name: "ATK / VXE Mouse", link: "8K dongle", wireless: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "373b:1085": { brand: "atk", name: "VXE R1 SE+ (Beken)", link: "1K dongle", wireless: true, dpiMax: 18000, rates: [250, 500, 1000] },
  "373b:10c9": { brand: "atk", name: "ATK NearLink Mouse", link: "NearLink dongle", wireless: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
  "373b:11d9": { brand: "atk", name: "ATK F1 Ultimate 2.0", link: "8K dongle", wireless: true, dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] },
};
const ATK_PRODUCTS = {
  "1:8":  { name: "ATK Blazing Sky F1 Ultimate 2.0", sensor: "PAW3950Ultra", dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000], maxStages: 6, isR1: false, antiMistouch: true },
  "1:31": { name: "ATK A9 Mini+", sensor: "PAW3955Master", dpiMax: 50000, rates: [125, 250, 500, 1000, 2000, 4000, 8000], maxStages: 6, isR1: false, dpi6: true },
  "1:52": { name: "ATK A9 Mini+", sensor: "PAW3955Master", dpiMax: 50000, rates: [125, 250, 500, 1000, 2000, 4000, 8000], maxStages: 6, isR1: false, dpi6: true },
  "2:11": { name: "VXE R1", sensor: "PAW3395", dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000], maxStages: 4, isR1: true },
  "2:12": { name: "VXE R1", sensor: "PAW3395", dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000], maxStages: 4, isR1: true },
  "2:27": { name: "VXE R1 Pro Max", sensor: "PAW3395", dpiMax: 26000, rates: [125, 250, 500, 1000, 2000, 4000], maxStages: 4, isR1: true },
  "2:32": { name: "VXE R1 SE+", sensor: "PAW3395SE", dpiMax: 18000, rates: [250, 500, 1000], maxStages: 4, isR1: true },
  "2:39": { name: "ATK Blazing Sky X1 Pro Max", sensor: "PAW3950", dpiMax: 42000, rates: [125, 250, 500, 1000, 2000, 4000, 8000], maxStages: 6, isR1: false },
  "2:83": { name: "ATK A9 Plus NearLink", sensor: "PAW3395", dpiMax: 26000, rates: [125, 250, 500, 1000], maxStages: 6, isR1: false },
};

function classifyPulsar(d) {
  const vid = d.vendorId, pid = d.productId;
  const vp = vpKey(vid, pid);

  if (vid === PULSAR_VID && PULSAR_PRO_DEVICES[pid]) {
    if (!findCollection(d, { page: 0x0001, inId: 0, outId: 0 })) return null;
    const prof = PULSAR_PRO_DEVICES[pid];
    return {
      driver: "pulsar_pro", protoLabel: "Pulsar Pro HID", protoTag: "Pulsar",
      info: { ...prof, prio: 1, hyper: true, dpiMax: 32000 },
      caps: {
        stages: true, dpi: true, maxStages: 6, dpiMin: 50, dpiMax: 32000, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: [125, 250, 500, 1000, 2000, 4000, 8000], lodMode: "preset", lodAsync: false, buttons: false, layers: false,
        wireless: true, powerTune: true, lowBatt: true, lowMin: 5, lowMax: 30, lowStep: 5, idleMaxMin: 30, batteryRead: true,
        hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 0, debMax: 20, debStep: 1, motionSync: true, angleSnapping: true, rippleControl: true, performance: true, angleTuning: true, angleMin: -30, angleMax: 30 },
      },
    };
  }

  if (vid === PULSAR_VID) {
    const xs1Col = findCollection(d, { page: 0xffff, usage: 0x0001, featId: 0 });
    const xs1Rep = xs1Col && (xs1Col.featureReports || []).find((r) => r.reportId === 0);
    if (xs1Col && calcReportBytes(xs1Rep) >= 64 && !findCollection(d, { inId: 0x08 })) {
      const prof = PULSAR_XS1_DEVICES[pid] ?? { name: d.productName || "Pulsar XS-1 Mouse", link: "USB / Dongle", wireless: false, maxPoll: 8000 };
      return {
        driver: "pulsar_xs1", protoLabel: "Pulsar XS-1 HID", protoTag: "Pulsar",
        info: { ...prof, prio: prof.wireless ? 1 : 0, hyper: (prof.maxPoll || 1000) > 1000, dpiMax: 26000 },
        caps: {
          stages: false, dpi: true, maxStages: 1, dpiMin: 50, dpiMax: 26000, dpiStepLo: 50, dpiStepHi: 50,
          splitXY: true, pollRates: [125, 250, 500, 1000, 2000, 4000, 8000], pollReadonly: true, lodMode: "preset", lodAsync: false, buttons: false, layers: false,
          wireless: prof.wireless, powerTune: false, batteryRead: prof.wireless, hyper: false, dongle: false, factoryReset: false,
          tune: { debounce: true, debMin: 0, debMax: 15, debStep: 1, motionSync: true, angleSnapping: true, rippleControl: true },
        },
      };
    }
    if (findCollection(d, { inId: 0x08, outId: 0x08 })) {
      const rates = [125, 250, 500, 1000, 2000, 4000, 8000];
      return {
        driver: "compx", protoLabel: "Pulsar CompX 0x08", protoTag: "Pulsar",
        info: { brand: "pulsar_compx", name: d.productName || "Pulsar Wireless Mouse", link: "USB / Dongle", wireless: true, prio: 1, hyper: true, dpiMax: 32000, rates },
        caps: {
          stages: true, dpi: true, maxStages: 6, dpiMin: 50, dpiMax: 32000, dpiStepLo: 10, dpiStepHi: 50,
          splitXY: true, pollRates: rates, lodMode: "preset", lodAsync: false, buttons: false, layers: false,
          wireless: true, powerTune: true, lowBatt: false, idleMaxMin: 30, batteryRead: true, hyper: false, dongle: false, factoryReset: false,
          tune: { debounce: true, debMin: 0, debMax: 30, debStep: 1, motionSync: true, angleSnapping: true, rippleControl: true },
        },
      };
    }
  }

  if (vid === ARESON_VID && ARESON_DEVICES[pid]) {
    if (!findCollection(d, { featId: 0x08 }) || findCollection(d, { page: 0xffff, usage: 0x0002 })) return null;
    const prof = ARESON_DEVICES[pid];
    return {
      driver: "compx", protoLabel: "Pulsar Areson (CompX 0x08)", protoTag: "Pulsar",
      info: { ...prof, brand: "areson", prio: prof.wireless ? 2 : 0, hyper: false, dpiMax: 26000, rates: [125, 250, 500, 1000] },
      caps: {
        stages: true, dpi: true, maxStages: 4, dpiMin: 50, dpiMax: 26000, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: [125, 250, 500, 1000], lodMode: "preset", lodAsync: false, buttons: false, layers: false,
        wireless: prof.wireless, powerTune: prof.wireless, lowBatt: false, idleMaxMin: 30, batteryRead: prof.wireless,
        hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 0, debMax: 30, debStep: 1, motionSync: true, angleSnapping: true, rippleControl: true },
      },
    };
  }

  const cxKnown = COMPX_DEVICES[vp];
  const cxVendor = (vid === COMPX_VID || vid === ATK_VID) && findCollection(d, { page: 0xff02, usage: 0x02 });
  if ((cxKnown || cxVendor) && findCollection(d, { outId: 0x08, inId: 0x08 })) {
    const info = cxKnown
      ? { ...cxKnown, prio: cxKnown.wireless ? 2 : 0, hyper: (cxKnown.rates || []).some((r) => r > 1000) }
      : { brand: vid === ATK_VID ? "atk" : "compx", name: d.productName || `CompX Mouse (${vp})`, link: "USB / Dongle", wireless: true, prio: 2, hyper: true, dpiMax: 30000, rates: [125, 250, 500, 1000, 2000, 4000, 8000] };
    const isV8s = info.brand === "scyrox" && info.model === "v8s";
    if (isV8s) info.lodOptions = ["0.7 mm", "0.9 mm", "1.2 mm", "1.4 mm", "1.6 mm"];
    return {
      driver: "compx", protoLabel: "CompX Report 0x08", protoTag: info.brand.toUpperCase(), info,
      caps: {
        stages: true, dpi: true, maxStages: 6, dpiMin: 50, dpiMax: info.dpiMax, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: info.rates, lodMode: isV8s ? "egg" : "preset", lodAsync: false, buttons: false, layers: false,
        wireless: info.wireless, powerTune: info.wireless, lowBatt: false, idleMaxMin: 30, batteryRead: info.wireless,
        hyper: false, dongle: false, factoryReset: false,
        tune: {
          debounce: true, debMin: 0, debMax: 30, debStep: 1,
          motionSync: true, angleSnapping: true, rippleControl: true,
          performance: ["teevolution", "lamzu", "scyrox"].includes(info.brand),
          scan20k: isV8s,
          antiMistouch: false,
        },
      },
    };
  }

  return null;
}

function makeCompxApi(sess, { isConnected, log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const brand = sess.info.brand || "";
  const featMode = sess.driver === "egg_we" || brand === "areson";
  const isV8s = brand === "scyrox" && sess.info.model === "v8s";
  const waiters = [];
  const COMPX_RATES_ALL = { 0x08: 125, 0x04: 250, 0x02: 500, 0x01: 1000, 0x10: 2000, 0x20: 4000, 0x40: 8000 };
  const COMPX_RATES_INV = Object.fromEntries(Object.entries(COMPX_RATES_ALL).map(([c, hz]) => [hz, +c]));
  const V8S_LOD = ["0.7 mm", "0.9 mm", "1.2 mm", "1.4 mm", "1.6 mm"];

  sess._onCompxReport = (e) => {
    const rid = e.reportId;
    if (rid !== 0x08 && rid !== 0x09) return;
    const u = new Uint8Array(e.data.buffer, e.data.byteOffset, e.data.byteLength);
    if (u.length < 16) return;
    for (let i = 0; i < waiters.length; i++) {
      const w = waiters[i];
      if (u[0] === w.cmd) {
        waiters.splice(i, 1);
        clearTimeout(w.t);
        w.res(u.slice(0, 16));
        return;
      }
    }
  };
  d.addEventListener("inputreport", sess._onCompxReport);
  for (const s of sess.siblings) s.addEventListener("inputreport", sess._onCompxReport);

  function buildPkt(cmd, data = []) {
    const pkt = new Uint8Array(16);
    pkt[0] = cmd & 0xff;
    for (let i = 0; i < Math.min(14, data.length); i++) pkt[1 + i] = data[i] & 0xff;
    let sum = 0x08;
    for (let i = 0; i < 15; i++) sum += pkt[i];
    pkt[15] = (0x55 - sum) & 0xff;
    return pkt;
  }

  async function xferRaw(cmd, data = [], timeoutMs = 650) {
    if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
    const pkt = buildPkt(cmd, data);
    const replyP = new Promise((res, rej) => {
      const w = {
        cmd: cmd & 0xff, res,
        t: setTimeout(() => {
          const idx = waiters.indexOf(w);
          if (idx >= 0) waiters.splice(idx, 1);
          rej(new DevErr("Mouse timed out — move it to wake it up", "timeout"));
        }, timeoutMs),
      };
      waiters.push(w);
    });
    if (featMode) await d.sendFeatureReport(0x08, pkt);
    else await d.sendReport(0x08, pkt);
    const rep = await replyP;
    log(`→ compx(0x${cmd.toString(16)}) ${hex(pkt.subarray(0, 8))} ← ${hex(rep.subarray(0, 12))}`, tag);
    return rep;
  }

  function xfer(cmd, data = [], timeoutMs = 650) {
    const p = sess.queue.then(() => xferRaw(cmd, data, timeoutMs));
    sess.queue = p.catch(() => {});
    return p;
  }

  async function readFlash(addr, length) {
    const out = new Uint8Array(length);
    let off = 0;
    while (off < length) {
      const chunk = Math.min(8, length - off);
      const a = addr + off;
      const rep = await xfer(0x08, [0x00, (a >> 8) & 0xff, a & 0xff, chunk]);
      out.set(rep.subarray(5, 5 + chunk), off);
      off += chunk;
      if (featMode || off < length) await sleep(10);
    }
    return out;
  }

  async function writeFlash(addr, bytes) {
    let off = 0;
    while (off < bytes.length) {
      const chunk = bytes.slice(off, off + 8);
      const a = addr + off;
      await xfer(0x07, [0x00, (a >> 8) & 0xff, a & 0xff, chunk.length, ...chunk]);
      off += chunk.length;
      await sleep(10);
    }
  }

  async function writePair(addr, val) {
    const v = val & 0xff;
    await writeFlash(addr, [v, (0x55 - v) & 0xff]);
  }

  function decodeDpi4(b, enc) {
    const lx = b[0], ly = b[1], fl = b[2];
    if (enc === "byte50") return [(lx + 1) * 50, (ly + 1) * 50];
    const rx = (((fl >> 2) & 0x03) << 8 | lx) + 1;
    const ry = (((fl >> 6) & 0x03) << 8 | ly) + 1;
    if (enc === "pulsar") {
      const mx = fl & 0x03, my = (fl >> 4) & 0x03;
      return [rx * (mx === 3 ? 100 : mx === 2 ? 50 : 10), ry * (my === 3 ? 100 : my === 2 ? 50 : 10)];
    }
    if (enc === "paw3950") {
      const mx = fl & 0x03, my = (fl >> 4) & 0x03;
      return [rx * (mx === 1 ? 100 : 50), ry * (my === 1 ? 100 : 50)];
    }
    return [rx * 50, ry * 50];
  }

  function encodeDpi4(x, y, enc) {
    const one = (v) => {
      v = Math.max(10, Math.round(v));
      if (enc === "byte50") return [Math.max(50, Math.min(12800, Math.round(v / 50) * 50)) / 50 - 1, 0, 0];
      if (enc === "pulsar") {
        if (v <= 10000) { const q = Math.max(10, Math.round(v / 10) * 10), r = q / 10 - 1; return [r & 0xff, (r >> 8) & 3, 0]; }
        if (v <= 30000) { const q = Math.round(v / 50) * 50, r = q / 50 - 1; return [r & 0xff, (r >> 8) & 3, 2]; }
        const q = Math.min(32000, Math.round(v / 100) * 100), r = q / 100 - 1; return [r & 0xff, (r >> 8) & 3, 3];
      }
      if (enc === "paw3950") {
        if (v <= 30000) { const q = Math.max(50, Math.round(v / 50) * 50), r = q / 50 - 1; return [r & 0xff, (r >> 8) & 3, 0]; }
        const q = Math.min(42000, Math.round(v / 100) * 100), r = q / 100 - 1; return [r & 0xff, (r >> 8) & 3, 1];
      }
      const q = Math.max(50, Math.min(30000, Math.round(v / 50) * 50)), r = q / 50 - 1;
      return [r & 0xff, (r >> 8) & 3, 0];
    };
    const [lx, hx, mx] = one(x), [ly, hy, my] = one(y);
    const fl = ((hx << 2) | mx) | (((hy << 2) | my) << 4);
    return [lx, ly, fl, (0x55 - (lx + ly + fl)) & 0xff];
  }

  function decodeDpi6(b) {
    const rx = b[0] | (b[1] << 8), ry = b[2] | (b[3] << 8);
    return [(rx + 1) * 50, (ry + 1) * 50];
  }
  function encodeDpi6(x, y) {
    const rx = Math.max(0, Math.round(x / 50) - 1), ry = Math.max(0, Math.round(y / 50) - 1);
    const body = [rx & 0xff, (rx >> 8) & 0xff, ry & 0xff, (ry >> 8) & 0xff, 0];
    const crc = (0x55 - body.reduce((a, c) => a + c, 0)) & 0xff;
    return [...body, crc];
  }

  const state = {
    dpiEnc: sess.driver === "egg_we" ? "byte50"
      : brand === "pulsar_compx" ? "pulsar"
      : brand === "teevolution" ? "paw3950" : "flat50",
    isR1: false, dpi6: false, maxStages: 8, antiMistouch: false,
    hasPerf: ["teevolution", "lamzu", "scyrox"].includes(brand),
  };

  async function readAllCompx() {
    if (!featMode && brand !== "vgn" && sess.caps.wireless) {
      for (let i = 0; i < 6; i++) {
        try { const r = await xfer(0x03, [], 250); if (r[1] === 0x01) break; } catch {}
        await sleep(10);
      }
    }
    const cfg = await readFlash(0x00, 16);
    const hz = COMPX_RATES_ALL[cfg[0]] ?? sess.S.poll;
    const cnt = Math.max(1, Math.min(state.maxStages, cfg[2] || 1));
    const act = Math.max(1, Math.min(cnt, cfg[4] + 1));
    const list = [];
    if (isV8s || state.dpi6) {
      const base = isV8s ? 6912 : 0x1b00;
      const raw = await readFlash(base, cnt * 6);
      for (let i = 0; i < cnt; i++) {
        const [x, y] = decodeDpi6(raw.subarray(i * 6, i * 6 + 6));
        list.push({ x, y });
      }
    } else {
      const raw = await readFlash(0x0c, cnt * 4);
      for (let i = 0; i < cnt; i++) {
        const [x, y] = decodeDpi4(raw.subarray(i * 4, i * 4 + 4), state.dpiEnc);
        list.push({ x, y });
      }
    }
    sess.S.stages = { active: act, list };
    const curSt = list[act - 1] ?? list[0];
    if (curSt) sess.S.dpi = [curSt.x, curSt.y];
    if (hz) sess.S.poll = hz;

    const lodRaw = cfg[10];
    if (isV8s) {
      sess.S.lod = { mode: "egg", index: Math.max(0, Math.min(4, lodRaw >= 1 ? lodRaw - 1 : 0)) };
    } else if (["pulsar_compx", "pulsar_4k", "vgn", "teevolution"].includes(brand)) {
      sess.S.lod = { mode: "preset", level: { 3: 0, 1: 1, 2: 2 }[lodRaw] ?? 1 };
    } else {
      sess.S.lod = { mode: "preset", level: lodRaw === 2 ? (brand === "lamzu" ? 1 : 2) : 0 };
    }

    const sysBlk = await readFlash(0xa8, 32);
    const tune = {
      debounceMs: sysBlk[1],
      motionSync: !!sysBlk[7],
      angleSnapping: !!sysBlk[9],
      rippleControl: !!sysBlk[11],
    };
    if (state.antiMistouch) tune.antiMistouch = !!sysBlk[171 - 0xa8];
    if (state.hasPerf) tune.performance = sysBlk[185 - 0xa8] === 1;
    if (isV8s) {
      const s20 = await readFlash(0x08, 2);
      tune.scan20k = s20[0] === 1;
    }
    sess.S.tune = tune;
    if (sysBlk[5]) sess.S.idle = sysBlk[5] * 10;
    return sess.S;
  }

  return {
    fastKeys: ["compxAll"],
    slowKeys: sess.caps.wireless ? ["battery", "charging"] : [],
    readKey: readAllCompx,
    init: async () => {
      if (sess.driver === "compx" && brand === "atk") {
        try {
          const id = await xfer(0x10, [], 300);
          const prod = ATK_PRODUCTS[`${id[5]}:${id[6]}`];
          if (prod) {
            sess.name = prod.name; sess.shortName = prod.name;
            sess.caps.dpiMax = prod.dpiMax;
            sess.caps.pollRates = prod.rates;
            sess.caps.maxStages = prod.maxStages;
            state.maxStages = prod.maxStages;
            state.isR1 = !!prod.isR1;
            state.dpi6 = !!prod.dpi6;
            state.antiMistouch = !!prod.antiMistouch;
            if (prod.antiMistouch) sess.caps.tune.antiMistouch = true;
            if (prod.sensor === "PAW3950" || prod.sensor === "PAW3950Ultra") state.dpiEnc = "paw3950";
          }
        } catch {}
      }
      if (isV8s) {
        sess.caps.lodMode = "egg";
        sess.info.lodOptions = V8S_LOD;
      }
    },
    firmware: async () => {
      if (featMode) return "–";
      try { const v = await xfer(0x02, [], 300); return `v${v[5]}.${String(v[6]).padStart(2, "0")}`; } catch { return "–"; }
    },
    serial: async () => d.serialNumber || "",
    battery: async () => {
      if (!sess.caps.wireless) return undefined;
      const b = await xfer(0x04, [], 450);
      sess._compxChg = b[6] !== 0;
      return b[5];
    },
    charging: async () => sess._compxChg,
    getDpi: async () => { await readAllCompx(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      const act = sess.S.stages?.active ?? 1;
      if (isV8s || state.dpi6) {
        await writeFlash((isV8s ? 6912 : 0x1b00) + (act - 1) * 6, encodeDpi6(x, y));
      } else {
        await writeFlash(0x0c + (act - 1) * 4, encodeDpi4(x, y, state.dpiEnc));
      }
    },
    getStages: async () => { await readAllCompx(); return sess.S.stages; },
    setStages: async (active, list) => {
      await writePair(0x02, list.length);
      await writePair(0x04, active - 1);
      for (let i = 0; i < list.length; i++) {
        if (isV8s || state.dpi6) await writeFlash((isV8s ? 6912 : 0x1b00) + i * 6, encodeDpi6(list[i].x, list[i].y));
        else await writeFlash(0x0c + i * 4, encodeDpi4(list[i].x, list[i].y, state.dpiEnc));
      }
    },
    getPoll: async () => { await readAllCompx(); return sess.S.poll; },
    setPoll: async (hz) => {
      const code = COMPX_RATES_INV[hz];
      if (!code) throw new DevErr(`Unsupported polling rate ${hz}`, "unsupported");
      await writePair(0x00, code);
    },
    getLod: async () => { await readAllCompx(); return sess.S.lod; },
    setLodEgg: async (idx) => { await writePair(0x0a, idx + 1); },
    setLodPreset: async (lvl) => {
      const code = ["pulsar_compx", "pulsar_4k", "vgn", "teevolution"].includes(brand)
        ? { 0: 3, 1: 1, 2: 2 }[lvl]
        : { 0: 1, 1: featMode ? 1 : 2, 2: 2 }[lvl];
      await writePair(0x0a, code);
    },
    getIdle: async () => { await readAllCompx(); return sess.S.idle; },
    setIdle: async (sec) => {
      const val = Math.max(1, Math.min(180, Math.round(sec / 10)));
      await writePair(0xad, val);
      if (["pulsar_compx", "pulsar_4k", "scyrox"].includes(brand)) await writePair(0xb7, val);
    },
    getTune: async () => { await readAllCompx(); return sess.S.tune; },
    setTune: async (patch) => {
      if (patch.debounceMs != null) await writePair(0xa9, Math.max(0, Math.min(30, patch.debounceMs)));
      if (patch.motionSync != null) await writePair(0xaf, patch.motionSync ? 1 : 0);
      if (patch.angleSnapping != null) await writePair(0xb1, patch.angleSnapping ? 1 : 0);
      if (patch.rippleControl != null) await writePair(0xb3, patch.rippleControl ? 1 : 0);
      if (patch.antiMistouch != null) await writePair(171, patch.antiMistouch ? 1 : 0);
      if (patch.performance != null) await writePair(185, patch.performance ? 1 : 0);
      if (patch.scan20k != null) await writePair(0x08, patch.scan20k ? 1 : 0);
    },
  };
}

function makePulsarXs1Api(sess, { isConnected, log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const XS1_RATES = { 240: 125, 120: 250, 60: 500, 30: 1000, 15: 2000, 8: 4000, 4: 8000 };
  const XS1_LOD_READ = { 0x07: 0, 0x0a: 1, 0x14: 2 };
  const XS1_LOD_WRITE = { 0: 0x07, 1: 0x0a, 2: 0x14 };

  async function xferRaw(payload) {
    if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
    const pkt = new Uint8Array(64);
    pkt.set(payload, 1);
    let chk = 0;
    for (let i = 0; i < 62; i++) chk = (chk + pkt[i]) & 0xffff;
    pkt[62] = chk & 0xff; pkt[63] = (chk >> 8) & 0xff;
    await d.sendFeatureReport(0, pkt);
    await sleep(25);
    const dv = await d.receiveFeatureReport(0);
    const b = unwrapReport(dv, 0, 64);
    log(`→ xs1(${hex(payload.slice(0, 6))}) ← ${hex(b.subarray(0, 12))}`, tag);
    return b;
  }
  function xfer(payload) {
    const p = sess.queue.then(() => xferRaw(payload));
    sess.queue = p.catch(() => {});
    return p;
  }
  async function readAllXs1() {
    const poll = await xfer([0x08, 0x85, 0x03]);
    const dpi = await xfer([0x05, 0x82, 0x05]);
    const lod = await xfer([0x07, 0x82, 0x03]);
    const deb = await xfer([0x04, 0x83, 0x03]);
    const ms = await xfer([0x07, 0x85, 0x02]);
    const as = await xfer([0x07, 0x84, 0x02]);
    const rc = await xfer([0x07, 0x83, 0x02]);
    const dx = dpi[7] | (dpi[8] << 8), dy = (dpi[9] | (dpi[10] << 8)) || dx;
    sess.S.dpi = [dx, dy];
    sess.S.poll = XS1_RATES[poll[7]] ?? sess.S.poll;
    sess.S.lod = { mode: "preset", level: XS1_LOD_READ[lod[8]] ?? 1 };
    sess.S.tune = {
      debounceMs: deb[7],
      motionSync: !!ms[7],
      angleSnapping: !!as[7],
      rippleControl: !!rc[7],
    };
    return sess.S;
  }
  return {
    fastKeys: ["dpi", "tune"],
    slowKeys: ["poll", "lod", ...(sess.caps.wireless ? ["battery", "charging"] : [])],
    firmware: async () => { const a = await xfer([0x01, 0x87, 0x04]); return `v${a[7].toString(16).padStart(2, "0")}.${a[6].toString(16).padStart(2, "0")}`; },
    serial: async () => d.serialNumber || "",
    getDpi: async () => { await readAllXs1(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      const qx = Math.max(50, Math.min(26000, Math.round(x / 50) * 50));
      const qy = Math.max(50, Math.min(26000, Math.round(y / 50) * 50));
      return xfer([0x05, 0x02, 0x05, 0, 0, 1, qx & 0xff, qx >> 8, qy & 0xff, qy >> 8]);
    },
    getPoll: async () => { const a = await xfer([0x08, 0x85, 0x03]); return XS1_RATES[a[7]]; },
    setPoll: async () => { throw new DevErr("Polling rate on Pulsar XS-1 is read-only over USB HID", "unsupported"); },
    getLod: async () => { const a = await xfer([0x07, 0x82, 0x03]); return { mode: "preset", level: XS1_LOD_READ[a[8]] ?? 1 }; },
    setLodPreset: async (lvl) => xfer([0x07, 0x02, 0x03, 0, 0, 1, 2, XS1_LOD_WRITE[lvl] ?? 0x0a]),
    getTune: async () => { await readAllXs1(); return sess.S.tune; },
    setTune: async (patch) => {
      if (patch.debounceMs != null) await xfer([0x04, 0x03, 0x03, 0, 0, 1, Math.max(0, Math.min(15, patch.debounceMs))]);
      if (patch.motionSync != null) await xfer([0x07, 0x05, 0x02, 0, 0, 1, patch.motionSync ? 1 : 0]);
      if (patch.angleSnapping != null) await xfer([0x07, 0x04, 0x02, 0, 0, 1, patch.angleSnapping ? 1 : 0]);
      if (patch.rippleControl != null) await xfer([0x07, 0x03, 0x02, 0, 0, 1, patch.rippleControl ? 1 : 0]);
    },
    battery: async () => { const b = await xfer([0x08, 0x81, 0x01]); return b[6]; },
    charging: async () => false,
  };
}

function makePulsarProApi(sess, { isConnected, log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const waiters = [];
  sess._onPulsarProReport = (e) => {
    if (e.reportId !== 0) return;
    const u = new Uint8Array(e.data.buffer, e.data.byteOffset, e.data.byteLength);
    if (!u.length) return;
    for (let i = 0; i < waiters.length; i++) {
      const w = waiters[i];
      if (u[0] === w.cmd || u[0] === 0xa6 || u[0] === 0xa7) {
        waiters.splice(i, 1);
        clearTimeout(w.t);
        w.res(u.slice());
        return;
      }
    }
  };
  d.addEventListener("inputreport", sess._onPulsarProReport);

  async function xferRaw(cmd, params = []) {
    const pkt = new Uint8Array(63);
    pkt[0] = cmd & 0xff;
    pkt.set(params.slice(0, 62), 1);
    for (let attempt = 0; attempt < 8; attempt++) {
      if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
      const replyP = new Promise((res, rej) => {
        const w = { cmd: cmd & 0xff, res, t: setTimeout(() => {
          const i = waiters.indexOf(w); if (i >= 0) waiters.splice(i, 1);
          rej(new DevErr("Pulsar Pro timed out", "timeout"));
        }, 650) };
        waiters.push(w);
      });
      await d.sendReport(0, pkt);
      const u = await replyP;
      if (u[0] === 0xa6) throw new DevErr(`Pulsar Pro rejected 0x${cmd.toString(16)}`, "failed");
      if (u[0] === 0xa7) { await sleep(50); continue; }
      log(`→ ppro(0x${cmd.toString(16)}) ← ${hex(u.subarray(0, 12))}`, tag);
      return u;
    }
    throw new DevErr("Pulsar Pro busy", "timeout");
  }
  function xfer(cmd, params = []) {
    const p = sess.queue.then(() => xferRaw(cmd, params));
    sess.queue = p.catch(() => {});
    return p;
  }
  const commitFlash = () => xfer(0xc3, [0x01]);

  async function readAllPro() {
    const dpi = await xfer(0xb1);
    const poll = await xfer(0xb2);
    const lod = await xfer(0xb3);
    const ms = await xfer(0xb5);
    const rc = await xfer(0xb6);
    const as = await xfer(0xb7);
    const at = await xfer(0xb8);
    const tur = await xfer(0xc4);
    const deb = await xfer(0xc5);
    const pwr = await xfer(0xc2);
    const low = await xfer(0xbe);
    const cnt = Math.max(1, Math.min(6, dpi[2] || 1));
    const active = Math.max(1, Math.min(cnt, dpi[3] || 1));
    const list = [];
    for (let i = 0; i < cnt; i++) {
      const o = 4 + i * 4;
      const x = dpi[o] | (dpi[o + 1] << 8), y = (dpi[o + 2] | (dpi[o + 3] << 8)) || x;
      list.push({ x, y });
    }
    sess.S.stages = { active, list };
    sess.S.dpi = [list[active - 1].x, list[active - 1].y];
    sess.S.poll = poll[2] | (poll[3] << 8);
    sess.S.lod = { mode: "preset", level: ({ 1: 0, 2: 1, 3: 2 }[lod[2]]) ?? 1 };
    sess.S.tune = {
      motionSync: ms[2] === 1,
      rippleControl: rc[2] === 1,
      angleSnapping: as[2] === 1,
      angleTuning: at[2] > 127 ? at[2] - 256 : at[2],
      performance: tur[2] === 1,
      debounceMs: deb[2],
    };
    const sleepMs = pwr[2] | (pwr[3] << 8) | (pwr[4] << 16) | (pwr[5] << 24);
    if (sleepMs) sess.S.idle = Math.round(sleepMs / 1000);
    sess.S.low = Math.round((low[2] / 100) * 255);
    return sess.S;
  }
  return {
    fastKeys: ["pproAll"],
    slowKeys: ["battery", "charging"],
    readKey: readAllPro,
    firmware: async () => { const a = await xfer(0xbf); return `v${a[1]}.${a[2]}.${a[3]}`; },
    serial: async () => d.serialNumber || "",
    battery: async () => { const b = await xfer(0xb4); sess._pproChg = b[6] === 1; return b[1]; },
    charging: async () => sess._pproChg,
    getDpi: async () => { await readAllPro(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      const st = sess.S.stages ?? (await readAllPro()).stages;
      const list = st.list.map((s) => ({ ...s }));
      list[st.active - 1] = { x: Math.max(50, Math.min(32000, x)), y: Math.max(50, Math.min(32000, y)) };
      const flat = [];
      for (const s of list) flat.push(s.x & 0xff, s.x >> 8, s.y & 0xff, s.y >> 8);
      await xfer(0xb1, [1, list.length, st.active, ...flat]);
      await commitFlash();
    },
    getStages: async () => { await readAllPro(); return sess.S.stages; },
    setStages: async (active, list) => {
      const flat = [];
      const clipped = list.slice(0, 6);
      for (const s of clipped) flat.push(s.x & 0xff, s.x >> 8, s.y & 0xff, s.y >> 8);
      await xfer(0xb1, [1, clipped.length, active, ...flat]);
      await commitFlash();
    },
    getPoll: async () => { await readAllPro(); return sess.S.poll; },
    setPoll: async (hz) => { await xfer(0xb2, [1, hz & 0xff, hz >> 8]); await commitFlash(); },
    getLod: async () => { await readAllPro(); return sess.S.lod; },
    setLodPreset: async (lvl) => { await xfer(0xb3, [1, ({ 0: 1, 1: 2, 2: 3 }[lvl]) ?? 2]); await commitFlash(); },
    getIdle: async () => { await readAllPro(); return sess.S.idle; },
    setIdle: async (sec) => {
      const ms = Math.max(10000, Math.min(1800000, sec * 1000));
      await xfer(0xc2, [1, ms & 0xff, (ms >> 8) & 0xff, (ms >> 16) & 0xff, (ms >> 24) & 0xff]);
      await commitFlash();
    },
    getLow: async () => { await readAllPro(); return sess.S.low; },
    setLow: async (raw) => {
      const pct = Math.max(0, Math.min(30, Math.round((raw / 255) * 100)));
      await xfer(0xbe, [1, pct]);
      await commitFlash();
    },
    getTune: async () => { await readAllPro(); return sess.S.tune; },
    setTune: async (patch) => {
      let chg = false;
      if (patch.debounceMs != null) { await xfer(0xc5, [1, Math.max(0, Math.min(20, patch.debounceMs))]); chg = true; }
      if (patch.motionSync != null) { await xfer(0xb5, [1, patch.motionSync ? 1 : 0]); chg = true; }
      if (patch.rippleControl != null) { await xfer(0xb6, [1, patch.rippleControl ? 1 : 0]); chg = true; }
      if (patch.angleSnapping != null) { await xfer(0xb7, [1, patch.angleSnapping ? 1 : 0]); chg = true; }
      if (patch.angleTuning != null) { await xfer(0xb8, [1, Math.max(-30, Math.min(30, patch.angleTuning)) & 0xff]); chg = true; }
      if (patch.performance != null) { await xfer(0xc4, [1, patch.performance ? 1 : 0]); chg = true; }
      if (chg) await commitFlash();
    },
  };
}

export {
  PULSAR_VID,
  PULSAR_XS1_DEVICES,
  PULSAR_PRO_DEVICES,
  ARESON_VID,
  ARESON_DEVICES,
  COMPX_VID,
  ATK_VID,
  COMPX_DEVICES,
  ATK_PRODUCTS,
  classifyPulsar,
  makeCompxApi,
  makePulsarXs1Api,
  makePulsarProApi,
};
