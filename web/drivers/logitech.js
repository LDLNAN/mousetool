// Protocol 3: Logitech HID++ 2.0 driver
import { DevErr, hex, hex4 } from "./common.js";

const LOGI_VID = 0x046d;
const LOGI_DEVICES = {
  0xc088: { name: "Logitech G Pro Wireless", link: "USB cable" },
  0xc08b: { name: "Logitech G502 Hero", link: "USB cable" },
  0xc092: { name: "Logitech G Pro X Superlight", link: "USB cable" },
  0xc093: { name: "Logitech G502 Lightspeed", link: "USB cable" },
  0xc094: { name: "Logitech G Pro Wireless", link: "USB cable" },
  0xc095: { name: "Logitech G305", link: "USB" },
  0xc099: { name: "Logitech G Pro X Superlight 2", link: "USB cable" },
  0xc09d: { name: "Logitech G502 X Plus", link: "USB cable" },
  0xc09e: { name: "Logitech G502 X Lightspeed", link: "USB cable" },
  0xc52b: { name: "Logitech Unifying Receiver", link: "Unifying receiver" },
  0xc539: { name: "Logitech Lightspeed Receiver", link: "Lightspeed dongle" },
  0xc53f: { name: "Logitech Lightspeed Receiver", link: "Lightspeed dongle" },
  0xc547: { name: "Logitech Superlight Receiver", link: "Lightspeed dongle" },
  0xc548: { name: "Logitech Bolt Receiver", link: "Bolt receiver" },
  0xc54d: { name: "Logitech Superlight 2 Receiver", link: "Lightspeed 2 dongle" },
  0xb023: { name: "Logitech MX Master 3", link: "Bluetooth" },
  0xb034: { name: "Logitech MX Master 3S", link: "Bluetooth" },
};

function classifyLogitech(d) {
  const vid = d.vendorId, pid = d.productId;
  if (vid !== LOGI_VID) return null;
  const hasHidpp = d.collections?.some((c) => c.usagePage === 0xff00 || c.usagePage === 0xff43 ||
    (c.outputReports || []).some((r) => r.reportId === 0x10 || r.reportId === 0x11));
  if (!hasHidpp) return null;
  const known = LOGI_DEVICES[pid] ?? { name: d.productName || "Logitech HID++ Mouse", link: "HID++" };
  return {
    driver: "hidpp", protoLabel: "Logitech HID++ 2.0", protoTag: "HID++",
    info: { name: known.name, link: known.link, prio: 3, wireless: /dongle|receiver|bluetooth/i.test(known.link), hyper: false, dpiMax: 32000 },
    caps: {
      stages: false, dpi: false, maxStages: 1, dpiMin: 100, dpiMax: 32000, dpiStepLo: 50, dpiStepHi: 50,
      splitXY: false, pollRates: [], lodMode: "none", buttons: false, layers: false,
      wireless: /dongle|receiver|bluetooth/i.test(known.link), powerTune: false, batteryRead: false,
      hyper: false, dongle: false, factoryReset: false, genericDpi: false,
    },
  };
}

function makeHidppApi(sess, { isConnected, log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const SW_ID = 0x09;
  const waiters = [];
  sess._onHidppReport = (e) => {
    const rid = e.reportId;
    if (rid !== 0x10 && rid !== 0x11) return;
    const u = new Uint8Array(e.data.buffer, e.data.byteOffset, e.data.byteLength);
    for (let i = 0; i < waiters.length; i++) {
      const w = waiters[i];
      if (u[0] === w.devIdx && ((u[1] === 0xff && u[2] === w.feat) || (u[1] === w.feat && (u[2] & 0x0f) === SW_ID) || u[1] === 0x8f)) {
        waiters.splice(i, 1);
        clearTimeout(w.t);
        w.res({ rid, u });
        return;
      }
    }
  };
  d.addEventListener("inputreport", sess._onHidppReport);

  async function xferRaw(devIdx, featIdx, fnId, params = []) {
    const long = params.length > 3;
    const rid = long ? 0x11 : 0x10;
    const body = new Uint8Array(long ? 19 : 6);
    body[0] = devIdx; body[1] = featIdx; body[2] = ((fnId & 0x0f) << 4) | SW_ID;
    body.set(params.slice(0, long ? 16 : 3), 3);
    const replyP = new Promise((res, rej) => {
      const w = { devIdx, feat: featIdx, res, t: setTimeout(() => {
        const idx = waiters.indexOf(w); if (idx >= 0) waiters.splice(idx, 1);
        rej(new DevErr("HID++ timeout", "timeout"));
      }, 1100) };
      waiters.push(w);
    });
    await d.sendReport(rid, body);
    const { u } = await replyP;
    log(`→ hidpp(${devIdx.toString(16)}) f=${featIdx.toString(16)}:${fnId} ${hex(body.subarray(3, 8))} ← ${hex(u.subarray(0, 10))}`, tag);
    if (u[1] === 0xff || u[1] === 0x8f) throw new DevErr(`HID++ error 0x${(u[4] ?? u[3]).toString(16)}`, "failed");
    return u.slice(3);
  }
  function xfer(devIdx, featIdx, fnId, params = []) {
    const p = sess.queue.then(() => {
      if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
      return xferRaw(devIdx, featIdx, fnId, params);
    });
    sess.queue = p.catch(() => {});
    return p;
  }

  let probeP = null;
  async function ensureProbe() {
    if (sess.hidpp) return sess.hidpp;
    if (probeP) return probeP;
    probeP = (async () => {
      let devIdx = null;
      for (const cand of [0xff, 0x01, 0x02]) {
        try {
          await xfer(cand, 0x00, 0x01, [0x00, 0x00, 0xaa]);
          devIdx = cand;
          break;
        } catch {}
      }
      if (devIdx === null) { sess.hidpp = { devIdx: null, feats: {} }; return sess.hidpp; }
      const feats = {};
      for (const fid of [0x0003, 0x0005, 0x1000, 0x1004, 0x2201, 0x8060]) {
        try {
          const r = await xfer(devIdx, 0x00, 0x00, [fid >> 8, fid & 0xff]);
          if (r[0] > 0) feats[fid] = r[0];
        } catch {}
      }
      sess.hidpp = { devIdx, feats };
      log(`HID++ devIdx=0x${devIdx.toString(16)} features=${Object.keys(feats).map((k) => "0x" + (+k).toString(16)).join(",")}`, tag);
      return sess.hidpp;
    })();
    return probeP;
  }

  return {
    fastKeys: ["dpi"],
    slowKeys: ["poll", "battery", "charging"],
    init: async () => {
      const hp = await ensureProbe();
      sess.caps.dpi = hp.feats[0x2201] != null;
      sess.caps.poll = hp.feats[0x8060] != null;
      sess.caps.batteryRead = (hp.feats[0x1004] ?? hp.feats[0x1000]) != null;
      if (!sess.caps.dpi) { sess.caps.genericDpi = true; }
      if (hp.feats[0x8060] != null) {
        try {
          const sup = await xfer(hp.devIdx, hp.feats[0x8060], 0x00);
          const mask = (sup[0] << 8) | sup[1];
          const rates = [];
          for (let ms = 1; ms <= 8; ms++) {
            if (mask & (1 << (ms - 1))) rates.push(Math.round(1000 / ms));
          }
          if (rates.length) sess.caps.pollRates = rates.sort((a, b) => a - b);
        } catch {}
      }
      if (hp.feats[0x0005] != null) {
        try {
          const len = (await xfer(hp.devIdx, hp.feats[0x0005], 0x00))[0];
          let s = "";
          while (s.length < len) {
            const c = await xfer(hp.devIdx, hp.feats[0x0005], 0x01, [s.length]);
            s += new TextDecoder().decode(c).replace(/\0.*$/, "");
            if (!c.some(Boolean)) break;
          }
          if (s.trim()) { sess.name = s.trim(); sess.shortName = sess.name; }
        } catch {}
      }
    },
    firmware: async () => {
      const hp = await ensureProbe();
      if (hp.feats[0x0003] == null) return "–";
      try {
        const b = await xfer(hp.devIdx, hp.feats[0x0003], 0x01, [0x01]);
        const maj = (b[4] >> 4) * 10 + (b[4] & 0xf);
        const min = (b[5] >> 4) * 10 + (b[5] & 0xf);
        return `v${maj}.${String(min).padStart(2, "0")}`;
      } catch { return "–"; }
    },
    serial: async () => d.serialNumber || "",
    getDpi: async () => {
      const hp = await ensureProbe();
      if (hp.feats[0x2201] == null) {
        const ref = +(localStorage.getItem(`mousetool.genericDpi.${hex4(sess.vid)}:${hex4(sess.pid)}`) || 800);
        return [ref, ref];
      }
      const r = await xfer(hp.devIdx, hp.feats[0x2201], 0x02, [0x00]);
      const dpi = (r[1] << 8) | r[2];
      return dpi ? [dpi, dpi] : null;
    },
    setDpi: async (x) => {
      const hp = await ensureProbe();
      if (hp.feats[0x2201] == null) throw new DevErr("AdjustableDpi (0x2201) not exposed", "unsupported");
      return xfer(hp.devIdx, hp.feats[0x2201], 0x03, [0x00, (x >> 8) & 0xff, x & 0xff]);
    },
    getPoll: async () => {
      const hp = await ensureProbe();
      if (hp.feats[0x8060] == null) return undefined;
      const r = await xfer(hp.devIdx, hp.feats[0x8060], 0x01);
      return r[0] > 0 ? Math.round(1000 / r[0]) : undefined;
    },
    setPoll: async (hz) => {
      const hp = await ensureProbe();
      if (hp.feats[0x8060] == null) throw new DevErr("ReportRate (0x8060) not exposed", "unsupported");
      const ms = Math.max(1, Math.round(1000 / hz));
      return xfer(hp.devIdx, hp.feats[0x8060], 0x02, [ms]);
    },
    battery: async () => {
      const hp = await ensureProbe();
      if (hp.feats[0x1004] != null) {
        const r = await xfer(hp.devIdx, hp.feats[0x1004], 0x01);
        sess._hidppCharging = r[2] === 1 || r[2] === 2 || r[2] === 3;
        return r[0];
      }
      if (hp.feats[0x1000] != null) {
        const r = await xfer(hp.devIdx, hp.feats[0x1000], 0x00);
        sess._hidppCharging = r[2] === 1 || r[2] === 3;
        return r[0];
      }
      return undefined;
    },
    charging: async () => sess._hidppCharging ?? undefined,
  };
}

export {
  LOGI_VID,
  LOGI_DEVICES,
  classifyLogitech,
  makeHidppApi,
};
