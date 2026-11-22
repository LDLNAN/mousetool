// Protocol 1: Razer 90-byte feature report driver
import { DevErr, hex, sleep } from "./common.js";

const VID = 0x1532;
const DEVICES = {
  0x00a5: { name: "Razer Viper V2 Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 30000 },
  0x00a6: { name: "Razer Viper V2 Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 30000 },
  0x00b3: { name: "Razer HyperPolling Dongle", link: "HyperPolling dongle", prio: 1, wireless: true, hyper: true, dpiMax: 35000 },
  0x00b2: { name: "Razer DeathAdder V3", link: "USB cable", prio: 0, wireless: false, hyper: false, dpiMax: 30000 },
  0x00b6: { name: "Razer DeathAdder V3 Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 35000 },
  0x00b7: { name: "Razer DeathAdder V3 Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 35000 },
  0x00c2: { name: "Razer DeathAdder V3 Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 35000 },
  0x00c3: { name: "Razer DeathAdder V3 Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 35000 },
  0x00c4: { name: "Razer DeathAdder V3 HyperSpeed", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 26000 },
  0x00c5: { name: "Razer DeathAdder V3 HyperSpeed", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 26000 },
  0x00b8: { name: "Razer Viper V3 HyperSpeed", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 30000 },
  0x00c0: { name: "Razer Viper V3 Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 35000 },
  0x00c1: { name: "Razer Viper V3 Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 35000 },
  0x00af: { name: "Razer Cobra Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 30000 },
  0x00b0: { name: "Razer Cobra Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 30000 },
  0x00aa: { name: "Razer Basilisk V3 Pro", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 30000 },
  0x00ab: { name: "Razer Basilisk V3 Pro", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 30000 },
  0x00cc: { name: "Razer Basilisk V3 Pro 35K", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 35000 },
  0x00cd: { name: "Razer Basilisk V3 Pro 35K", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 35000 },
  0x00d6: { name: "Razer Basilisk V3 Pro 35K", link: "USB cable", prio: 0, wireless: true, hyper: false, dpiMax: 35000 },
  0x00d7: { name: "Razer Basilisk V3 Pro 35K", link: "HyperSpeed dongle", prio: 2, wireless: true, hyper: false, dpiMax: 35000 },
};

const POLL_CODES = { 1: 1000, 2: 500, 4: 250, 8: 125 };
const POLL_HZ = { 1000: 1, 500: 2, 250: 4, 125: 8 };
const POLL2_CODES = { 0x01: 8000, 0x02: 4000, 0x04: 2000, 0x08: 1000, 0x10: 500, 0x20: 250, 0x40: 125 };
const POLL2_HZ = Object.fromEntries(Object.entries(POLL2_CODES).map(([c, hz]) => [hz, +c]));
const STATUS = { 0: "new", 1: "busy", 2: "ok", 3: "failed", 4: "timeout", 5: "unsupported" };

function razerBuild(cls, cmd, size, args = [], txn = 0x1f) {
  const r = new Uint8Array(90);
  r[1] = txn; r[5] = size; r[6] = cls; r[7] = cmd;
  r.set(args, 8);
  let crc = 0;
  for (let i = 2; i < 88; i++) crc ^= r[i];
  r[88] = crc;
  return r;
}

function classifyRazer(d, allowGeneric = false) {
  const vid = d.vendorId, pid = d.productId;
  if (vid === VID && (DEVICES[pid] || allowGeneric || d.collections?.some((c) => (c.featureReports || []).length))) {
    const hasFeat = d.collections?.some((c) => (c.featureReports || []).length);
    if (!hasFeat) return null;
    const info = DEVICES[pid] ?? { name: d.productName || "Razer Mouse", link: "USB / Dongle", prio: 5, wireless: true, hyper: false, dpiMax: 30000 };
    return {
      driver: "razer", protoLabel: "Razer 90-byte HID", protoTag: "Razer", info,
      caps: {
        stages: true, dpi: true, maxStages: 5, dpiMin: 100, dpiMax: info.dpiMax, dpiStepLo: 50, dpiStepHi: 50, splitXY: true,
        pollRates: info.hyper ? [125, 250, 500, 1000, 2000, 4000, 8000] : [125, 500, 1000],
        lodMode: "razer", lodAsync: true, buttons: true, layers: true, wireless: info.wireless, powerTune: info.wireless, batteryRead: info.wireless,
        hyper: info.hyper, dongle: info.hyper, factoryReset: false,
      },
    };
  }
  return null;
}

function makeRazerApi(sess, { isConnected, log }) {
  const d = sess.dev;
  const hyper = !!sess.caps.hyper;
  const tag = sess.shortName;
  async function readReply() {
    const dv = await d.receiveFeatureReport(0);
    let b = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
    if (b.length === 91) b = b.subarray(1);
    return b;
  }
  async function xferRaw(cls, cmd, size, args, txn) {
    const req = razerBuild(cls, cmd, size, args, txn);
    let last = null;
    for (let attempt = 0; attempt < 5; attempt++) {
      if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
      await d.sendFeatureReport(0, req);
      const t0 = performance.now();
      let wait = 10;
      for (;;) {
        await sleep(wait); wait = 10;
        last = await readReply();
        const ours = last[6] === cls && last[7] === cmd;
        const elapsed = performance.now() - t0;
        if (ours && last[0] >= 2) break;
        if (elapsed > 450) break;
      }
      const ours = last[6] === cls && last[7] === cmd;
      log(`→ ${hex(req.subarray(0, 8 + Math.max(size, 2)))}   ← ${ours ? STATUS[last[0]] ?? last[0] : "no reply"}` +
          (ours && last[0] === 2 ? "  " + hex(last.subarray(8, 8 + Math.min(Math.max(last[5], 2), 16))) : ""), tag);
      if (!ours) { await sleep(30); continue; }
      if (last[0] === 2) return last.slice(8, 88);
      if (last[0] === 5) throw new DevErr("Not supported by this firmware", "unsupported");
      if (last[0] === 3) throw new DevErr("The mouse rejected the command", "failed");
      await sleep(attempt === 0 ? 80 : 160 * attempt);
    }
    if (last && last[0] === 4) throw new DevErr("Mouse didn't answer — move it to wake it up", "timeout");
    throw new DevErr("No reply from the mouse/dongle", "noreply");
  }
  function xfer(cls, cmd, size, args = [], txn = 0x1f) {
    const p = sess.queue.then(() => xferRaw(cls, cmd, size, args, txn));
    sess.queue = p.catch(() => {});
    return p;
  }
  return {
    fastKeys: ["dpi", "stages"],
    slowKeys: ["poll", "lod", ...(sess.caps.wireless ? ["idle", "low", "battery", "charging"] : []), ...(hyper ? ["led"] : [])],
    firmware: async () => { const a = await xfer(0x00, 0x81, 0x02); return `v${a[0]}.${a[1]}`; },
    serial: async () => {
      const a = await xfer(0x00, 0x82, 0x16);
      return new TextDecoder().decode(a.subarray(0, 22)).replace(/\0.*$/, "").trim();
    },
    battery: async () => (await xfer(0x07, 0x80, 0x02))[1] / 255 * 100,
    charging: async () => !!(await xfer(0x07, 0x84, 0x02))[1],
    getPoll: async () => hyper
      ? POLL2_CODES[(await xfer(0x00, 0xc0, 0x01))[1]] ?? null
      : POLL_CODES[(await xfer(0x00, 0x85, 0x01))[0]] ?? null,
    setPoll: async (hz) => {
      if (!hyper) return xfer(0x00, 0x05, 0x01, [POLL_HZ[hz]]);
      await xfer(0x00, 0x40, 0x02, [0x00, POLL2_HZ[hz]]);
      try { await xfer(0x00, 0x40, 0x02, [0x01, POLL2_HZ[hz]]); }
      catch (e) { log(`! polling: mouse half didn't confirm (${e.message})`, tag); }
    },
    pair: async (pid) => {
      await xfer(0x00, 0x46, 0x01, [0x01]);
      return xfer(0x00, 0x41, 0x03, [0x01, pid >> 8, pid & 255]);
    },
    unpair: (pid) => xfer(0x00, 0x42, 0x02, [pid >> 8, pid & 255], 0xff),
    getLod: async () => {
      for (let i = 0; i < 3; i++) {
        const a = await xfer(0x0b, 0x85, 0x01);
        if (a[2] === 0x01 && a[3] <= 2) return { mode: "preset", level: a[3] };
        if (a[2] === 0x04) return { mode: "async", lift: a[4] + 1, land: a[5] + 1 };
        await sleep(60);
      }
      return null;
    },
    setLodPreset: async (level) => {
      await xfer(0x0b, 0x03, 0x03, [0x00, 0x04, 0x28]);
      return xfer(0x0b, 0x0b, 0x04, [0x00, 0x04, 0x01, level]);
    },
    setLodAsync: async (lift, land) => {
      await xfer(0x0b, 0x03, 0x03, [0x00, 0x04, 0x01]);
      await xfer(0x0b, 0x0b, 0x04, [0x00, 0x04, 0x04, 0x00]);
      return xfer(0x0b, 0x05, 0x04, [0x00, 0x04, lift - 1, land - 1]);
    },
    buttonIds: async () => { const a = await xfer(0x02, 0x84, 0x50); return Array.from(a.subarray(1, 1 + Math.min(a[0], 20))); },
    getButton: async (b, layer = sess.btnLayer) => {
      const a = await xfer(0x02, 0x8c, 0x0a, [0x01, b, layer]);
      return { fn: a[3], size: a[4], data: Array.from(a.subarray(5, 10)) };
    },
    setButton: (b, act, layer = sess.btnLayer) => {
      const data = [...act.data, 0, 0, 0, 0, 0].slice(0, 5);
      return xfer(0x02, 0x0c, 0x50, [0x01, b, layer, act.fn, act.size, ...data]);
    },
    getDongleLed: async () => (await xfer(0x07, 0x90, 0x01, [], 0xff))[0],
    setDongleLed: (m) => xfer(0x07, 0x10, 0x01, [m], 0xff),
    getDpi: async () => { const a = await xfer(0x04, 0x85, 0x07, [0x00]); return [a[1] << 8 | a[2], a[3] << 8 | a[4]]; },
    setDpi: (x, y) => xfer(0x04, 0x05, 0x07, [0x01, x >> 8, x & 255, y >> 8, y & 255, 0, 0]),
    getStages: async () => {
      const a = await xfer(0x04, 0x86, 0x26, [0x01]);
      const active = a[1], n = Math.min(a[2], 5), list = [];
      for (let i = 0; i < n; i++) {
        const o = 3 + i * 7;
        list.push({ x: a[o + 1] << 8 | a[o + 2], y: a[o + 3] << 8 | a[o + 4] });
      }
      return { active, list };
    },
    setStages: (active, list) => {
      const args = [0x01, active, list.length];
      list.forEach((s, i) => args.push(i + 1, s.x >> 8, s.x & 255, s.y >> 8, s.y & 255, 0, 0));
      return xfer(0x04, 0x06, 0x26, args);
    },
    getIdle: async () => { const a = await xfer(0x07, 0x83, 0x02); return a[0] << 8 | a[1]; },
    setIdle: (sec) => xfer(0x07, 0x03, 0x02, [sec >> 8, sec & 255]),
    getLow: async () => (await xfer(0x07, 0x81, 0x01))[0],
    setLow: (raw) => xfer(0x07, 0x01, 0x01, [raw]),
  };
}

export {
  VID,
  DEVICES,
  POLL_CODES,
  POLL_HZ,
  POLL2_CODES,
  POLL2_HZ,
  STATUS,
  razerBuild,
  classifyRazer,
  makeRazerApi,
};
