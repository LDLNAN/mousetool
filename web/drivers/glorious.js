// Protocol 7: Glorious Sinowealth / CORE 2 / Classic / Pixart drivers (registry-backed)
import {
  DevErr,
  calcReportBytes,
  findCollection,
  hex,
  loadLocalCache,
  saveLocalCache,
  sleep,
  unwrapReport,
  vpKey,
} from "./common.js";
import { getGloriousTables, tierMetaFromProfile } from "./registry.js";

function classifyGlorious(d) {
  const vp = vpKey(d.vendorId, d.productId);
  const { sinowealth, core2, classic, pixart } = getGloriousTables();

  if (sinowealth[vp]) {
    if (!findCollection(d, { page: 0xff00, featId: 0x05 }) || !(findCollection(d, { featId: 0x06 }) || findCollection(d, { featId: 0x04 }))) return null;
    const prof = sinowealth[vp];
    return {
      driver: "glorious_sinowealth", protoLabel: "Glorious Sinowealth HID", protoTag: "Glorious",
      ...tierMetaFromProfile("glorious_sinowealth"),
      info: { ...prof, prio: 0, wireless: false, hyper: false, dpiMax: 16000 },
      caps: {
        stages: true, dpi: true, dpiRead: true, dpiWrite: true, maxStages: 6, dpiMin: 100, dpiMax: 16000, dpiStepLo: 100, dpiStepHi: 100,
        splitXY: true, pollRates: [125, 250, 500, 1000], lodMode: "none", buttons: false, layers: false,
        wireless: false, powerTune: false, batteryRead: false, hyper: false, dongle: false, factoryReset: false, tune: {},
      },
    };
  }

  if (core2[vp]) {
    const col = findCollection(d, { page: 0xffff, featId: 0 });
    const rep = col && (col.featureReports || []).find((r) => r.reportId === 0);
    if (!col || calcReportBytes(rep) < 64) return null;
    const prof = core2[vp];
    const rates = prof.maxPoll >= 8000 ? [125, 250, 500, 1000, 2000, 4000, 8000] : [125, 250, 500, 1000, 2000, 4000];
    return {
      driver: "glorious_core2", protoLabel: "Glorious CORE 2 HID", protoTag: "Glorious",
      ...tierMetaFromProfile("glorious_core2"),
      info: { ...prof, prio: prof.wireless ? 2 : 0, hyper: true, dpiMax: 26000 },
      caps: {
        stages: true, dpi: true, dpiRead: false, dpiWrite: true, maxStages: 6, dpiMin: 100, dpiMax: 26000, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: rates, lodMode: "none", buttons: false, layers: false,
        wireless: prof.wireless, powerTune: false, batteryRead: prof.wireless, hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 4, debMax: 16, debStep: 2, motionSync: true },
      },
    };
  }

  if (classic[vp]) {
    const hasFeat = (d.collections || []).some((c) => [0xff01, 0xff00, 0xffff].includes(c.usagePage) && (c.featureReports || []).length);
    if (!hasFeat) return null;
    const prof = classic[vp];
    return {
      driver: "glorious_classic", protoLabel: "Glorious Classic HID", protoTag: "Glorious",
      ...tierMetaFromProfile("glorious_classic"),
      info: { ...prof, prio: prof.wireless ? 2 : 0, hyper: false, dpiMax: 19000 },
      caps: {
        stages: true, dpi: true, dpiRead: false, dpiWrite: true, maxStages: 4, dpiMin: 100, dpiMax: 19000, dpiStepLo: 100, dpiStepHi: 100,
        splitXY: true, pollRates: [125, 250, 500, 1000], lodMode: "preset", lodAsync: false, buttons: false, layers: false,
        wireless: prof.wireless, powerTune: false, batteryRead: prof.wireless, hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 0, debMax: 32, debStep: 1 },
      },
    };
  }

  if (pixart[vp]) {
    if (!findCollection(d, { page: 0xff00, featId: 0x03 })) return null;
    const prof = pixart[vp];
    return {
      driver: "glorious_pixart", protoLabel: "Glorious Pixart HID", protoTag: "Glorious",
      ...tierMetaFromProfile("glorious_pixart"),
      info: { ...prof, prio: prof.wireless ? 2 : 0, hyper: false, dpiMax: 26000 },
      caps: {
        stages: true, dpi: true, dpiRead: false, dpiWrite: true, maxStages: 6, dpiMin: 100, dpiMax: 26000, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: [125, 250, 500, 1000], lodMode: "preset", lodAsync: false, buttons: false, layers: false,
        wireless: false, powerTune: false, batteryRead: false, hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 0, debMax: 16, debStep: 1 },
      },
    };
  }

  return null;
}

function makeGloriousSinowealthApi(sess, { log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const col = findCollection(d, { page: 0xff00, featId: 0x05 });
  const cmdRep = (col?.featureReports || []).find((r) => r.reportId === 0x05);
  const cfgRep = (col?.featureReports || []).find((r) => r.reportId === 0x06) || (col?.featureReports || []).find((r) => r.reportId === 0x04);
  const cmdLen = calcReportBytes(cmdRep) || 6;
  const cfgRid = cfgRep?.reportId ?? 0x04;
  const cfgLen = calcReportBytes(cfgRep) || 123;
  const SINO_SENSORS = { 0x06: ["PMW3360", 12000, true], 0x08: ["PMW3212", 7200, false], 0x0e: ["PMW3327", 10200, true], 0x0f: ["PMW3389", 16000, false] };
  const SINO_RATES = { 1: 125, 2: 250, 3: 500, 4: 1000 };
  const SINO_RATES_INV = { 125: 1, 250: 2, 500: 3, 1000: 4 };
  let lastBlock = null, offset1 = false;

  async function readBlock() {
    const cmd = new Uint8Array(cmdLen);
    cmd[0] = 0x11;
    await d.sendFeatureReport(0x05, cmd);
    await sleep(60);
    const dv = await d.receiveFeatureReport(cfgRid);
    const b = unwrapReport(dv, cfgRid, cfgLen);
    if (b.length < 123) throw new DevErr(`Sinowealth block too short (${b.length}B < 123B)`, "unsupported");
    lastBlock = b.slice();
    const s = SINO_SENSORS[b[9]];
    if (!s) {
      sess.caps.dpi = false; sess.caps.stages = false; sess.caps.pollRates = [];
      sess.caps.statusNote = `Unknown Sinowealth sensor 0x${b[9].toString(16)} — read-only safety mode.`;
    } else {
      offset1 = s[2];
      sess.caps.dpiMax = s[1];
    }
    const xy = !!(b[0x0a] & 0x80);
    const hz = SINO_RATES[b[0x0a] & 0x0f];
    const active = Math.max(1, (b[0x0b] >> 4) & 0x0f);
    const disabled = b[0x0c];
    const list = [];
    for (let i = 0; i < 6; i++) {
      if (disabled & (1 << i)) continue;
      const vx = (b[0x0d + (xy ? i * 2 : i)] + (offset1 ? 1 : 0)) * 100;
      const vy = xy ? (b[0x0d + i * 2 + 1] + (offset1 ? 1 : 0)) * 100 : vx;
      list.push({ x: vx, y: vy });
    }
    if (!list.length) list.push({ x: 800, y: 800 });
    const act = Math.min(active, list.length);
    sess.S.stages = { active: act, list };
    sess.S.dpi = [list[act - 1].x, list[act - 1].y];
    if (hz) sess.S.poll = hz;
    log(`← sino sensor=${s ? s[0] : "0x" + b[9].toString(16)} poll=${hz} active=${act}/${list.length}`, tag);
    return sess.S;
  }
  async function writeBlock(mutator) {
    if (!lastBlock || lastBlock.length < 123 || lastBlock.length > 200 || !SINO_SENSORS[lastBlock[9]]) {
      throw new DevErr("Sinowealth write blocked by sensor/size safety gate", "unsupported");
    }
    const out = lastBlock.slice();
    mutator(out);
    await d.sendFeatureReport(cfgRid, out);
  }
  return {
    fastKeys: ["sinoAll"],
    slowKeys: [],
    readKey: readBlock,
    firmware: async () => "–",
    serial: async () => d.serialNumber || "",
    getDpi: async () => { await readBlock(); return sess.S.dpi; },
    setDpi: async (x, y) => writeBlock((out) => {
      const act = Math.max(1, (out[0x0b] >> 4) & 0x0f) - 1;
      const xy = !!(out[0x0a] & 0x80);
      const qx = Math.max(0, Math.round(x / 100) - (offset1 ? 1 : 0));
      const qy = Math.max(0, Math.round(y / 100) - (offset1 ? 1 : 0));
      if (xy) { out[0x0d + act * 2] = qx & 0xff; out[0x0d + act * 2 + 1] = qy & 0xff; }
      else out[0x0d + act] = qx & 0xff;
    }),
    getStages: async () => { await readBlock(); return sess.S.stages; },
    setStages: async (active, list) => writeBlock((out) => {
      const split = list.some((s) => s.x !== s.y);
      out[0x0a] = (out[0x0a] & 0x0f) | (split ? 0x80 : 0);
      out[0x0b] = ((active & 0x0f) << 4) | (list.length & 0x0f);
      let mask = 0;
      for (let i = 0; i < 6; i++) {
        if (i >= list.length) { mask |= (1 << i); continue; }
        const qx = Math.max(0, Math.round(list[i].x / 100) - (offset1 ? 1 : 0));
        const qy = Math.max(0, Math.round(list[i].y / 100) - (offset1 ? 1 : 0));
        if (split) { out[0x0d + i * 2] = qx & 0xff; out[0x0d + i * 2 + 1] = qy & 0xff; }
        else out[0x0d + i] = qx & 0xff;
      }
      out[0x0c] = mask;
    }),
    getPoll: async () => { await readBlock(); return sess.S.poll; },
    setPoll: async (hz) => writeBlock((out) => {
      const c = SINO_RATES_INV[hz];
      if (!c) throw new DevErr("Unsupported polling rate", "unsupported");
      out[0x0a] = (out[0x0a] & 0xf0) | c;
    }),
  };
}

function makeGloriousCore2Api(sess, { isConnected, log }) {
  const d = sess.dev;
  const tag = sess.shortName;
  const target = sess.caps.wireless ? 0x00 : 0x02;
  const RATES = { 125: 0x08, 250: 0x04, 500: 0x02, 1000: 0x01, 2000: 0x20, 4000: 0x40, 8000: 0x80 };
  const defaults = { dpi: [800, 800], stages: { active: 2, list: [{ x: 400, y: 400 }, { x: 800, y: 800 }, { x: 1600, y: 1600 }, { x: 3200, y: 3200 }] }, poll: 1000, tune: { debounceMs: 4, motionSync: false } };
  const loadCached = () => ({ ...defaults, ...loadLocalCache(sess) });

  async function xferRaw(t, bank, reg, data = [], readReply = false) {
    if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
    const pkt = new Uint8Array(64);
    pkt[2] = t & 0xff; pkt[3] = data.length & 0xff; pkt[4] = bank & 0xff; pkt[5] = reg & 0xff;
    pkt.set(data, 6);
    await d.sendFeatureReport(0x00, pkt);
    if (!readReply) return null;
    await sleep(30);
    const dv = await d.receiveFeatureReport(0x00);
    const b = unwrapReport(dv, 0, 64);
    log(`→ core2(${t}:${bank}:${reg}) ← ${hex(b.subarray(0, 12))}`, tag);
    return b;
  }
  function xfer(t, bank, reg, data = [], readReply = false) {
    const p = sess.queue.then(() => xferRaw(t, bank, reg, data, readReply));
    sess.queue = p.catch(() => {});
    return p;
  }
  function hydrate() {
    const c = loadCached();
    sess.S.stages = c.stages;
    sess.S.dpi = c.dpi;
    sess.S.poll = c.poll;
    sess.S.tune = c.tune;
  }
  hydrate();
  return {
    fastKeys: [],
    slowKeys: sess.caps.wireless ? ["battery", "charging"] : [],
    firmware: async () => {
      try {
        const r = await xfer(target, 0x00, 0x81, [], true);
        if (r && (r[6] || r[7] || r[8] || r[9])) return `v${r[6]}.${r[7]}.${r[8]}.${r[9]}`;
      } catch {}
      return "–";
    },
    serial: async () => d.serialNumber || "",
    battery: async () => {
      if (!sess.caps.wireless) return undefined;
      const b = await xfer(0x00, 0x00, 0x83, [], true);
      if (b && b[0] === 0xa1) { sess._c2Chg = b[6] === 1; return b[7]; }
      return undefined;
    },
    charging: async () => sess._c2Chg,
    getDpi: async () => { hydrate(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      const st = loadCached().stages;
      const list = st.list.map((s) => ({ ...s }));
      list[st.active - 1] = { x, y };
      const flat = [0x01];
      for (const s of list) flat.push(s.x >> 8, s.x & 0xff, s.y >> 8, s.y & 0xff);
      await xfer(target, 0x01, 0x01, flat);
      saveLocalCache(sess, { dpi: [x, y], stages: { active: st.active, list } });
      hydrate();
    },
    getStages: async () => { hydrate(); return sess.S.stages; },
    setStages: async (active, list) => {
      const clipped = list.slice(0, 6);
      const flat = [0x01];
      for (const s of clipped) flat.push(s.x >> 8, s.x & 0xff, s.y >> 8, s.y & 0xff);
      await xfer(target, 0x01, 0x01, flat);
      const curSt = clipped[active - 1] ?? clipped[0];
      saveLocalCache(sess, { stages: { active, list: clipped }, dpi: [curSt.x, curSt.y] });
      hydrate();
    },
    getPoll: async () => { hydrate(); return sess.S.poll; },
    setPoll: async (hz) => {
      const wcode = RATES[hz]; if (!wcode) throw new DevErr("Unsupported polling rate", "unsupported");
      const wlcode = RATES[Math.min(hz, 4000)];
      await xfer(target, 0x01, 0x0a, [0x01, wcode, wlcode]);
      saveLocalCache(sess, { poll: hz });
      hydrate();
    },
    getTune: async () => { hydrate(); return sess.S.tune; },
    setTune: async (patch) => {
      const tune = { ...loadCached().tune };
      if (patch.debounceMs != null) {
        const dms = Math.max(4, Math.min(16, Math.round(patch.debounceMs / 2) * 2));
        await xfer(target, 0x00, 0x08, [0x01, dms, 0, 0, 0, 0, 0]);
        tune.debounceMs = dms;
      }
      if (patch.motionSync != null) {
        await xfer(target, 0x01, 0x09, [0x01, patch.motionSync ? 1 : 0]);
        tune.motionSync = !!patch.motionSync;
      }
      saveLocalCache(sess, { tune });
      hydrate();
    },
  };
}

function makeGloriousClassicApi(sess) {
  const d = sess.dev;
  const spec = sess.info;
  let rid = 0, flen = 0;
  for (const c of d.collections || []) {
    if ([0xff01, 0xff00, 0xffff].includes(c.usagePage) && (c.featureReports || []).length) {
      rid = c.featureReports[0].reportId;
      flen = calcReportBytes(c.featureReports[0]);
      break;
    }
  }
  const writable = flen === 64;
  const isCore2Gen = spec.gen === "core2";
  const defaults = { dpi: [800, 800], stages: { active: 2, list: [{ x: 400, y: 400 }, { x: 800, y: 800 }, { x: 1600, y: 1600 }, { x: 3200, y: 3200 }] }, poll: 1000, lod: { mode: "preset", level: 1 }, tune: { debounceMs: 10 } };
  const loadCached = () => ({ ...defaults, ...loadLocalCache(sess) });
  function hydrate() {
    const c = loadCached();
    sess.S.stages = c.stages; sess.S.dpi = c.dpi; sess.S.poll = c.poll; sess.S.lod = c.lod; sess.S.tune = c.tune;
  }
  hydrate();
  if (!writable) {
    sess.caps.dpi = false; sess.caps.stages = false; sess.caps.pollRates = []; sess.caps.lodMode = "none"; sess.caps.tune = {};
    sess.caps.statusNote = `Safety gate active: HID feature descriptor length is ${flen}B (expected 64B). Read-only mode.`;
  } else if (isCore2Gen) {
    sess.caps.dpi = false; sess.caps.stages = false; sess.caps.pollRates = []; sess.caps.lodMode = "none";
    sess.caps.tune = { debounce: true, debMin: 0, debMax: 32, debStep: 1 };
    sess.caps.statusNote = "CORE 2-generation Classic PID: restricted safely to battery read + click debounce.";
  }

  return {
    fastKeys: [],
    slowKeys: writable && sess.caps.wireless ? ["battery", "charging"] : [],
    firmware: async () => "–",
    serial: async () => d.serialNumber || "",
    battery: async () => {
      if (!writable || !sess.caps.wireless) return undefined;
      const pkt = new Uint8Array(64);
      pkt[2] = 2; pkt[3] = 2; pkt[5] = 0x83;
      await d.sendFeatureReport(rid, pkt);
      await sleep(30);
      const dv = await d.receiveFeatureReport(rid);
      const bfr = unwrapReport(dv, rid, 64);
      if (bfr[0] === 0xa1 && bfr[5] === 0x83) { sess._clChg = bfr[6] === 1; return bfr[7]; }
      return undefined;
    },
    charging: async () => sess._clChg,
    getDpi: async () => { hydrate(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      if (!writable || isCore2Gen) throw new DevErr("Blocked by Classic safety gate", "unsupported");
      const st = loadCached().stages;
      const list = st.list.map((s) => ({ ...s }));
      list[st.active - 1] = { x, y };
      const pkt = new Uint8Array(64);
      pkt[2] = 0x02; pkt[3] = 0x10; pkt[4] = 0x02; pkt[5] = 0x01; pkt[6] = 0x01;
      for (let i = 0; i < Math.min(4, list.length); i++) {
        const o = 8 + i * 4;
        pkt[o] = list[i].x >> 8; pkt[o + 1] = list[i].x & 0xff; pkt[o + 2] = list[i].y >> 8; pkt[o + 3] = list[i].y & 0xff;
      }
      await d.sendFeatureReport(rid, pkt);
      saveLocalCache(sess, { dpi: [x, y], stages: { active: st.active, list } });
      hydrate();
    },
    getStages: async () => { hydrate(); return sess.S.stages; },
    setStages: async (active, list) => {
      if (!writable || isCore2Gen) throw new DevErr("Blocked by Classic safety gate", "unsupported");
      const clipped = list.slice(0, 4);
      const pkt = new Uint8Array(64);
      pkt[2] = 0x02; pkt[3] = 0x10; pkt[4] = 0x02; pkt[5] = 0x01; pkt[6] = 0x01;
      for (let i = 0; i < clipped.length; i++) {
        const o = 8 + i * 4;
        pkt[o] = clipped[i].x >> 8; pkt[o + 1] = clipped[i].x & 0xff; pkt[o + 2] = clipped[i].y >> 8; pkt[o + 3] = clipped[i].y & 0xff;
      }
      await d.sendFeatureReport(rid, pkt);
      const curSt = clipped[active - 1] ?? clipped[0];
      saveLocalCache(sess, { stages: { active, list: clipped }, dpi: [curSt.x, curSt.y] });
      hydrate();
    },
    getPoll: async () => { hydrate(); return sess.S.poll; },
    setPoll: async (hz) => {
      if (!writable || isCore2Gen) throw new DevErr("Blocked by Classic safety gate", "unsupported");
      const code = { 125: 0x04, 250: 0x03, 500: 0x02, 1000: 0x01 }[hz] ?? 0x01;
      const pkt = new Uint8Array(64);
      pkt[2] = 0x02; pkt[3] = 0x02; pkt[4] = 0x02; pkt[5] = 0x06; pkt[6] = 0x01; pkt[7] = code;
      await d.sendFeatureReport(rid, pkt);
      saveLocalCache(sess, { poll: hz });
      hydrate();
    },
    getLod: async () => { hydrate(); return sess.S.lod; },
    setLodPreset: async (lvl) => {
      if (!writable || isCore2Gen) throw new DevErr("Blocked by Classic safety gate", "unsupported");
      const pkt = new Uint8Array(64);
      pkt[2] = 0x02; pkt[3] = 0x02; pkt[4] = 0x02; pkt[5] = 0x07; pkt[6] = 0x01; pkt[7] = lvl + 1;
      await d.sendFeatureReport(rid, pkt);
      saveLocalCache(sess, { lod: { mode: "preset", level: lvl } });
      hydrate();
    },
    getTune: async () => { hydrate(); return sess.S.tune; },
    setTune: async (patch) => {
      if (!writable) throw new DevErr("Blocked by Classic safety gate", "unsupported");
      if (patch.debounceMs != null) {
        const dms = Math.max(0, Math.min(32, patch.debounceMs));
        const pkt = new Uint8Array(64);
        pkt[2] = 0x02; pkt[3] = 0x03; pkt[4] = 0x02; pkt[5] = 0x08; pkt[6] = 0x01; pkt[7] = dms;
        await d.sendFeatureReport(rid, pkt);
        saveLocalCache(sess, { tune: { ...loadCached().tune, debounceMs: dms } });
        hydrate();
      }
    },
  };
}

function makeGloriousPixartApi(sess) {
  const d = sess.dev;
  const col = findCollection(d, { page: 0xff00, featId: 0x03 });
  const rep = (col?.featureReports || []).find((r) => r.reportId === 0x03);
  const bodyLen = calcReportBytes(rep) || 63;
  const defaults = { dpi: [800, 800], stages: { active: 2, list: [{ x: 400, y: 400 }, { x: 800, y: 800 }, { x: 1600, y: 1600 }, { x: 3200, y: 3200 }] }, poll: 1000, lod: { mode: "preset", level: 1 }, tune: { debounceMs: 4 } };
  const loadCached = () => ({ ...defaults, ...loadLocalCache(sess) });
  function hydrate() {
    const c = loadCached();
    sess.S.stages = c.stages; sess.S.dpi = c.dpi; sess.S.poll = c.poll; sess.S.lod = c.lod; sess.S.tune = c.tune;
  }
  hydrate();

  async function writeConfig(patch) {
    const c = { ...loadCached(), ...patch };
    const st = c.stages;
    const pollCode = { 125: 2, 250: 3, 500: 4, 1000: 1 }[c.poll] ?? 1;
    const lodCode = c.lod?.level === 2 ? 2 : 1;
    const deb = Math.max(0, Math.min(16, c.tune?.debounceMs ?? 4));
    for (let seq = 0; seq < 4; seq++) {
      const body = new Uint8Array(bodyLen); // 63B payload (+ Report ID 0x03 = 64B total)
      body[0] = 0x04; body[1] = 0xfb; body[2] = seq; body[3] = 0x01;
      if (seq === 0) {
        body[4] = pollCode; body[5] = st.list.length; body[6] = st.active - 1; body[7] = lodCode; body[8] = deb;
        for (let i = 0; i < Math.min(6, st.list.length); i++) {
          const vx = Math.max(2, Math.min(520, Math.round(st.list[i].x / 50)));
          const vy = Math.max(2, Math.min(520, Math.round(st.list[i].y / 50)));
          const o = 9 + i * 4;
          body[o] = vx & 0xff; body[o + 1] = vx >> 8; body[o + 2] = vy & 0xff; body[o + 3] = vy >> 8;
        }
      }
      await d.sendFeatureReport(0x03, body);
      await sleep(15);
    }
    saveLocalCache(sess, c);
    hydrate();
  }
  return {
    fastKeys: [],
    slowKeys: [],
    firmware: async () => "–",
    serial: async () => d.serialNumber || "",
    getDpi: async () => { hydrate(); return sess.S.dpi; },
    setDpi: async (x, y) => {
      const st = loadCached().stages;
      const list = st.list.map((s) => ({ ...s }));
      list[st.active - 1] = { x, y };
      await writeConfig({ dpi: [x, y], stages: { active: st.active, list } });
    },
    getStages: async () => { hydrate(); return sess.S.stages; },
    setStages: async (active, list) => {
      const clipped = list.slice(0, 6);
      const curSt = clipped[active - 1] ?? clipped[0];
      await writeConfig({ stages: { active, list: clipped }, dpi: [curSt.x, curSt.y] });
    },
    getPoll: async () => { hydrate(); return sess.S.poll; },
    setPoll: async (hz) => writeConfig({ poll: hz }),
    getLod: async () => { hydrate(); return sess.S.lod; },
    setLodPreset: async (lvl) => writeConfig({ lod: { mode: "preset", level: lvl } }),
    getTune: async () => { hydrate(); return sess.S.tune; },
    setTune: async (patch) => writeConfig({ tune: { ...loadCached().tune, ...patch } }),
  };
}

export {
  getGloriousTables,
  classifyGlorious,
  makeGloriousSinowealthApi,
  makeGloriousCore2Api,
  makeGloriousClassicApi,
  makeGloriousPixartApi,
};
