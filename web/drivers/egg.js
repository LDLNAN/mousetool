// Protocol 2: Endgame Gear 8k (OP1 8k / XM2 8k / Purple Frost / v2) & -we device tables (registry-backed)
import { DevErr, findCollection, sleep, vpKey } from "./common.js";
import { getEggDevices, getEggWeDevices, tierMetaFromProfile } from "./registry.js";

const EGG_VID = 0x3367;
const EGG_GLASS_LOD = ["1.0 mm", "2.0 mm"];

const EGG_POLL_RATES = [1000, 2000, 4000, 8000];
const EGG_POLL_DIVS = { 8000: 1, 4000: 2, 2000: 4, 1000: 8 };

const EGG_REPORT = { CFG: 0xa0, CMD: 0xa1, EVT: 0x03 };
const EGG_CFG_LEN = 1041;
const EGG_CMD_LEN = 64;
const EGG_OP = { STORE: 0x11, LOAD: 0x12, RESET: 0x13, VERSION: 0x02 };
const EGG_OFF = {
  POLLING: 21, FILTERS: 22, LED_LIFTOFF: 24, LOD: 25, ANGLE_SNAP: 26, RIPPLE: 27, MOTION_SYNC: 28,
  ACTIVE_STAGE: 29, STAGE_COUNT: 30, STAGES: 51, HANDED: 71, BUTTONS: 77,
  GLASS: 127, ANGLE_TUNE: 128, MAX_FPS: 129,
};

function eggClampCpi(profile, v) {
  let n = Math.round(+v || profile.cpiMin);
  n = Math.max(profile.cpiMin, Math.min(profile.cpiMax, n));
  const step = n <= 10000 ? profile.stepLo : profile.stepHi;
  n = Math.round(n / step) * step;
  return Math.max(profile.cpiMin, Math.min(profile.cpiMax, n));
}

function eggFeatureLen(dev, reportId, fallback) {
  for (const c of dev.collections || []) {
    for (const r of c.featureReports || []) {
      if (r.reportId !== reportId) continue;
      let bits = 0;
      for (const it of r.items || []) bits += (it.reportSize || 0) * (it.reportCount || 0);
      if (bits > 0) return Math.ceil(bits / 8);
    }
  }
  return fallback;
}

function eggNormalize(dv, reportId, expectTotal, payloadLen) {
  let u = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
  if (expectTotal === EGG_CFG_LEN && payloadLen === EGG_CFG_LEN - 1 && u.length === payloadLen &&
      (u[0] === 0x00 || u[0] === 0xa0 || u[0] === 0xa1) && (u[1] === 0x01 || u[1] === 0x03)) {
    const t = new Uint8Array(expectTotal);
    t.set(u); t[0] = reportId;
    return t;
  }
  const hasId = (payloadLen && u.length === payloadLen) ? false
    : (payloadLen && u.length === payloadLen + 1 && u[0] === reportId) ? true
    : !!(u.length && u[0] === reportId);
  if (!hasId) {
    const t = new Uint8Array(u.length + 1);
    t[0] = reportId; t.set(u, 1); u = t;
  }
  if (u.length < expectTotal) { const t = new Uint8Array(expectTotal); t.set(u); u = t; }
  return u;
}

function eggFormatVer(buf) {
  for (const off of [17, 16, 18, 2, 4, 6]) {
    const patch = buf[off], major = buf[off + 1];
    if (major >= 1 && major <= 9 && (patch >> 4) <= 9 && (patch & 0x0f) <= 9) {
      return `v${major}.${patch.toString(16).padStart(2, "0")}`;
    }
  }
  for (const off of [17, 16, 18, 2, 4, 6]) {
    const patch = buf[off], major = buf[off + 1];
    if (major >= 1 && major <= 9 && patch <= 99) return `v${major}.${String(patch).padStart(2, "0")}`;
  }
  for (const off of [17, 16, 18, 2, 4, 6]) {
    const v = buf[off] | (buf[off + 1] << 8);
    if (v >= 50 && v <= 5000) return `v${(v / 100).toFixed(2)}`;
  }
  return null;
}

function eggParseConfig(b) {
  const ho = EGG_OFF.HANDED;
  const handedSlot = Array.from(b.subarray(ho, ho + 6));
  const plainLeft = handedSlot[0] === 0x00 && handedSlot[1] === 0x01 && !handedSlot.slice(2).some(Boolean);
  const leftHanded = !plainLeft && handedSlot.some(Boolean);
  const stages = [];
  for (let i = 0; i < 4; i++) {
    const p = EGG_OFF.STAGES + i * 5;
    stages.push({ split: !!b[p], x: b[p + 1] | (b[p + 2] << 8), y: b[p + 3] | (b[p + 4] << 8) });
  }
  const buttons = [];
  for (let i = 0; i < 7; i++) {
    const o = EGG_OFF.BUTTONS + i * 7;
    buttons.push({ ctl: b[o], type: b[o + 1], params: Array.from(b.subarray(o + 2, o + 7)) });
  }
  return {
    pollingDivider: b[EGG_OFF.POLLING],
    slamclick: !!(b[EGG_OFF.FILTERS] & 0x01),
    motionJitter: !!(b[EGG_OFF.FILTERS] & 0x10),
    ledLiftoffDisabled: b[EGG_OFF.LED_LIFTOFF] === 0,
    lodIndex: b[EGG_OFF.LOD],
    angleSnapping: !!b[EGG_OFF.ANGLE_SNAP],
    rippleControl: !!b[EGG_OFF.RIPPLE],
    motionSync: !!b[EGG_OFF.MOTION_SYNC],
    activeStage: Math.min(b[EGG_OFF.ACTIVE_STAGE], 3),
    stageCount: Math.min(Math.max(b[EGG_OFF.STAGE_COUNT], 1), 4),
    stages,
    handedSlot,
    leftHanded,
    buttons,
    glassMode: !!b[EGG_OFF.GLASS],
    angleTuning: b[EGG_OFF.ANGLE_TUNE] > 127 ? b[EGG_OFF.ANGLE_TUNE] - 256 : b[EGG_OFF.ANGLE_TUNE],
    forceMaxFps: !!b[EGG_OFF.MAX_FPS],
  };
}

function eggSlotToAction(slot) {
  const { type, params } = slot;
  if (type === 0xff) return { fn: 0, size: 0, data: [] };
  if (type === 0x00) {
    const m = { 1: 1, 2: 2, 4: 3, 8: 4, 16: 5 }[params[0]];
    if (m) return { fn: 1, size: 1, data: [m] };
  }
  if (type === 0x01) {
    if (params[0] === 0x01) return { fn: 1, size: 1, data: [9] };
    if (params[0] === 0xff) return { fn: 1, size: 1, data: [10] };
  }
  if (type === 0x02) return { fn: 2, size: 2, data: [params[0], params[1]] };
  if (type === 0x09) return { fn: 6, size: 1, data: [6] };
  if (type === 0x20) return { fn: 10, size: 2, data: [0, params[0]] };
  if (type === 0x0c) {
    const x = params[1] | (params[2] << 8), y = params[3] | (params[4] << 8);
    return { fn: 99, size: 4, data: [x, y], label: x === y ? `Fixed ${x} DPI` : `Fixed ${x}×${y} DPI`, raw: slot };
  }
  if (type === 0x18) return { fn: 99, size: 1, data: [params[0]], label: params[0] === 0x96 ? "Browser" : "Explorer", raw: slot };
  return { fn: 99, size: 5, data: params, label: `EGG type 0x${type.toString(16)}`, raw: slot };
}

function eggActionToSlot(act) {
  if (act.fn === 99 && act.raw) return { type: act.raw.type, params: [...act.raw.params] };
  if (act.fn === 0) return { type: 0xff, params: [0, 0, 0, 0, 0] };
  if (act.fn === 1) {
    const mask = { 1: 1, 2: 2, 3: 4, 4: 8, 5: 16 }[act.data[0]];
    if (mask) return { type: 0x00, params: [mask, 0, 0, 0, 0] };
    if (act.data[0] === 9) return { type: 0x01, params: [0x01, 0, 0, 0, 0] };
    if (act.data[0] === 10) return { type: 0x01, params: [0xff, 0, 0, 0, 0] };
  }
  if (act.fn === 2) return { type: 0x02, params: [act.data[0] || 0, act.data[1] || 0, 0, 0, 0] };
  if (act.fn === 6) return { type: 0x09, params: [0xf1, 0, 0, 0, 0] };
  if (act.fn === 10) return { type: 0x20, params: [act.data[1] || 0, 0, 0, 0, 0] };
  return null;
}

// UI button ID -> physical mapping slot in EGG blob
function eggSlotLoc(b, leftHanded) {
  if (b === 1) return leftHanded ? { kind: "handed" } : null;
  if (b === 2) return leftHanded ? null : { kind: "rec", idx: 0 };
  const idx = { 3: 1, 5: 2, 4: 3, 9: 5, 10: 6 }[b];
  return idx !== undefined ? { kind: "rec", idx } : null;
}

function classifyEgg(d, ctx = {}) {
  const vid = d.vendorId, pid = d.productId;
  const vp = vpKey(vid, pid);
  const eggDevices = getEggDevices();
  const eggWeDevices = getEggWeDevices();

  if (vid === EGG_VID && eggDevices[pid]) {
    const prof = eggDevices[pid];
    const hasVendor = d.collections?.some((c) => c.usagePage === 0xff01 && c.usage === 0x02);
    if (!hasVendor) return null;
    return {
      driver: "egg", protoLabel: `Endgame Gear 8k (${prof.family.toUpperCase()})`, protoTag: "EGG 8k",
      ...tierMetaFromProfile(prof.profileId || (prof.family === "v2" ? "egg8k_v2" : "egg8k_v1")),
      info: { ...prof, link: "USB 8k cable", prio: 0, wireless: false, hyper: true, dpiMax: prof.cpiMax },
      caps: {
        stages: true, dpi: true, dpiRead: true, dpiWrite: true, maxStages: 4, dpiMin: prof.cpiMin, dpiMax: prof.cpiMax, dpiStepLo: prof.stepLo, dpiStepHi: prof.stepHi,
        splitXY: true, pollRates: EGG_POLL_RATES, lodMode: "egg", buttons: true, layers: false, eggHanded: true,
        wireless: false, powerTune: false, batteryRead: false, hyper: false, dongle: false, factoryReset: true,
      },
    };
  }

  if (vid === EGG_VID && eggWeDevices[pid] && !ctx.eggA1Vps?.has(vp)) {
    if (!findCollection(d, { featId: 0x08 })) return null;
    const prof = eggWeDevices[pid];
    return {
      driver: "egg_we", protoLabel: "Endgame Gear -we (CompX 0x08)", protoTag: "EGG -we",
      ...tierMetaFromProfile(prof.profileId || "egg_we_compx"),
      info: { ...prof, brand: "egg_we", dpiMax: 12800, rates: [125, 250, 500, 1000] },
      caps: {
        stages: true, dpi: true, dpiRead: true, dpiWrite: true, maxStages: 4, dpiMin: 50, dpiMax: 12800, dpiStepLo: 50, dpiStepHi: 50,
        splitXY: true, pollRates: [125, 250, 500, 1000], lodMode: "preset", lodAsync: false, buttons: false, layers: false,
        wireless: prof.wireless, powerTune: prof.wireless, lowBatt: false, idleMaxMin: 30, batteryRead: prof.wireless,
        hyper: false, dongle: false, factoryReset: false,
        tune: { debounce: true, debMin: 0, debMax: 30, debStep: 1, motionSync: true, angleSnapping: true, rippleControl: true },
      },
    };
  }

  return null;
}

function makeEggApi(sess, { isConnected, isCurrent, log, render, renderFleet }) {
  const d = sess.dev;
  const profile = getEggDevices()[sess.pid] || sess.info;
  const cfgLen = eggFeatureLen(d, EGG_REPORT.CFG, EGG_CFG_LEN - 1);
  const cmdLen = eggFeatureLen(d, EGG_REPORT.CMD, EGG_CMD_LEN - 1);
  const tag = sess.shortName;
  sess.eggBlob = null;

  async function sendCmd(op) {
    const buf = new Uint8Array(cmdLen);
    buf[0] = op;
    await d.sendFeatureReport(EGG_REPORT.CMD, buf);
  }
  async function recvFeat(reportId, expectTotal, payloadLen) {
    const p = d.receiveFeatureReport(reportId);
    const dv = await Promise.race([p, sleep(3000).then(() => { throw new DevErr("Mouse timed out", "timeout"); })]);
    return { buf: eggNormalize(dv, reportId, expectTotal, payloadLen), rawLen: dv.byteLength };
  }
  function blobOk(buf, rawLen) {
    if (rawLen < 131) return false;
    const stages = buf[EGG_OFF.STAGE_COUNT];
    const cpi1 = buf[EGG_OFF.STAGES + 1] | (buf[EGG_OFF.STAGES + 2] << 8);
    return buf[1] === 0x01 && stages >= 1 && stages <= 4 && buf[EGG_OFF.ACTIVE_STAGE] <= 3 &&
      buf[EGG_OFF.POLLING] >= 1 && buf[EGG_OFF.POLLING] <= 64 && cpi1 >= 10 && cpi1 <= 30000;
  }
  async function pollAck(what, polls = 8) {
    let delay = 60;
    for (let i = 0; i < polls; i++) {
      const { buf } = await recvFeat(EGG_REPORT.CMD, EGG_CMD_LEN, cmdLen);
      if (buf[1] === 0x01) return buf;
      await sleep(delay); delay = Math.min(delay * 2, 400);
    }
    throw new DevErr(`${what} not acknowledged`, "timeout");
  }
  async function readRaw() {
    for (let round = 0; round < 3; round++) {
      await sendCmd(EGG_OP.LOAD);
      await sleep(80);
      let delay = 60;
      for (let poll = 0; poll < 6; poll++) {
        for (const rid of [EGG_REPORT.CMD, EGG_REPORT.CFG]) {
          const { buf, rawLen } = await recvFeat(rid, EGG_CFG_LEN, cfgLen);
          if (blobOk(buf, rawLen)) {
            buf[0] = EGG_REPORT.CFG;
            sess.eggBlob = buf.slice();
            log(`← blob ok via 0x${rid.toString(16)} (poll=${buf[EGG_OFF.POLLING]} st=${buf[EGG_OFF.ACTIVE_STAGE] + 1}/${buf[EGG_OFF.STAGE_COUNT]})`, tag);
            return buf.slice();
          }
        }
        await sleep(delay); delay = Math.min(delay * 2, 400);
      }
    }
    throw new DevErr("Failed to read configuration blob", "noreply");
  }
  async function writeRaw(blob) {
    const payload = new Uint8Array(cfgLen);
    payload.set(blob.subarray(1, 1 + Math.min(blob.length - 1, cfgLen)));
    payload[0] = EGG_OP.STORE;
    await d.sendFeatureReport(EGG_REPORT.CFG, payload);
    await sleep(300);
    await pollAck("Write config");
    log(`→ store 1041B blob ok`, tag);
    sess.eggBlob = blob.slice();
  }
  function xrun(fn) {
    const p = sess.queue.then(() => {
      if (!isConnected(sess)) throw new DevErr("Not connected", "offline");
      return fn();
    });
    sess.queue = p.catch(() => {});
    return p;
  }
  function applyParsedToSession(cfg) {
    sess.S.egg = cfg;
    const hz = cfg.pollingDivider ? Math.round(8000 / cfg.pollingDivider) : null;
    if (hz) sess.S.poll = hz;
    sess.S.lod = { mode: "egg", index: cfg.lodIndex, glass: cfg.glassMode };
    const stList = cfg.stages.slice(0, cfg.stageCount).map((s) => ({ x: s.x, y: s.y }));
    const active = cfg.activeStage + 1;
    sess.S.stages = { active, list: stList };
    const curSt = stList[active - 1] ?? stList[0];
    if (curSt) sess.S.dpi = [curSt.x, curSt.y];
    const btns = {};
    for (const b of [1, 2, 3, 5, 4, 9, 10]) {
      const loc = eggSlotLoc(b, cfg.leftHanded);
      if (!loc) btns[b] = { fn: 1, size: 1, data: [1] };
      else if (loc.kind === "handed") btns[b] = eggSlotToAction({ type: cfg.handedSlot[0], params: cfg.handedSlot.slice(1) });
      else btns[b] = eggSlotToAction(cfg.buttons[loc.idx]);
    }
    sess.S.buttons = btns;
    return cfg;
  }
  async function syncBlob() {
    const raw = await readRaw();
    return applyParsedToSession(eggParseConfig(raw));
  }
  async function mutate(mutator) {
    return xrun(async () => {
      const raw = await readRaw();
      const out = raw.slice();
      out[0] = EGG_REPORT.CFG; out[1] = EGG_OP.STORE;
      const cfg = eggParseConfig(raw);
      mutator(out, cfg);
      await writeRaw(out);
      const fresh = await readRaw();
      return applyParsedToSession(eggParseConfig(fresh));
    });
  }

  // Real-time 0x03 event listener on primary & siblings (DPI button / polling changes)
  sess._onEggReport = (e) => {
    if (e.reportId !== EGG_REPORT.EVT || e.data.byteLength < 2) return;
    const t = e.data.getUint8(0), v = e.data.getUint8(1);
    if (t === 0x02 && sess.S.stages) {
      const st = (v & 3) + 1;
      if (sess.S.egg) sess.S.egg.activeStage = v & 3;
      sess.S.stages = { ...sess.S.stages, active: st };
      const curSt = sess.S.stages.list[st - 1];
      if (curSt) sess.S.dpi = [curSt.x, curSt.y];
      log(`⚡ hardware event: active stage ${st}`, tag);
      if (isCurrent(sess)) { render("stages"); render("dpi"); render("tune"); }
      renderFleet();
    } else if (t === 0x06 && v >= 1) {
      const hz = Math.round(8000 / v);
      if (sess.S.egg) sess.S.egg.pollingDivider = v;
      sess.S.poll = hz;
      log(`⚡ hardware event: polling ${hz} Hz`, tag);
      if (isCurrent(sess)) render("poll");
      renderFleet();
    }
  };
  d.addEventListener("inputreport", sess._onEggReport);
  for (const s of sess.siblings) s.addEventListener("inputreport", sess._onEggReport);

  return {
    fastKeys: ["eggAll"],
    slowKeys: [],
    readKey: async () => xrun(() => syncBlob()),
    firmware: () => xrun(async () => {
      await sendCmd(EGG_OP.VERSION);
      await sleep(50);
      try {
        const st = await pollAck("Version", 4);
        return eggFormatVer(st) ?? "–";
      } catch {
        const { buf } = await recvFeat(EGG_REPORT.CMD, EGG_CMD_LEN, cmdLen);
        return eggFormatVer(buf) ?? "–";
      }
    }),
    serial: async () => d.serialNumber || "",
    getDpi: async () => sess.S.dpi ?? (await xrun(() => syncBlob()), sess.S.dpi),
    setDpi: async (x, y) => {
      await mutate((out, cfg) => {
        const idx = cfg.activeStage;
        const p = EGG_OFF.STAGES + idx * 5;
        const cx = eggClampCpi(profile, x), cy = eggClampCpi(profile, y);
        out[p] = cx !== cy ? 1 : 0;
        out[p + 1] = cx & 0xff; out[p + 2] = (cx >> 8) & 0xff;
        out[p + 3] = cy & 0xff; out[p + 4] = (cy >> 8) & 0xff;
      });
      return sess.S.dpi;
    },
    getStages: async () => sess.S.stages ?? (await xrun(() => syncBlob()), sess.S.stages),
    setStages: async (active, list) => {
      await mutate((out) => {
        const n = Math.min(4, Math.max(1, list.length));
        out[EGG_OFF.STAGE_COUNT] = n;
        out[EGG_OFF.ACTIVE_STAGE] = Math.min(n - 1, Math.max(0, active - 1));
        for (let i = 0; i < Math.min(4, list.length); i++) {
          const p = EGG_OFF.STAGES + i * 5;
          const cx = eggClampCpi(profile, list[i].x), cy = eggClampCpi(profile, list[i].y);
          out[p] = cx !== cy ? 1 : 0;
          out[p + 1] = cx & 0xff; out[p + 2] = (cx >> 8) & 0xff;
          out[p + 3] = cy & 0xff; out[p + 4] = (cy >> 8) & 0xff;
        }
      });
    },
    getPoll: async () => sess.S.poll,
    setPoll: async (hz) => {
      const div = EGG_POLL_DIVS[hz];
      if (!div) throw new DevErr(`Unsupported polling rate ${hz}`, "failed");
      await mutate((out) => { out[EGG_OFF.POLLING] = div; });
    },
    getLod: async () => sess.S.lod,
    setLodEgg: async (idx) => { await mutate((out) => { out[EGG_OFF.LOD] = idx & 0xff; }); },
    updateTune: async (fn) => { await mutate(fn); },
    setHanded: async (left) => {
      await mutate((out, cfg) => {
        if (left === cfg.leftHanded) return;
        const ho = EGG_OFF.HANDED, r0 = EGG_OFF.BUTTONS;
        if (left) {
          // Move Right's mapping (rec 0) into HANDED (71..76), or fallback Right Click; put plain Left into rec 0
          const prevRight = out.subarray(r0 + 1, r0 + 7);
          const valid = prevRight.some(Boolean) ? Array.from(prevRight) : [0x00, 0x02, 0, 0, 0, 0];
          for (let k = 0; k < 6; k++) out[ho + k] = valid[k];
          out[r0 + 1] = 0x00; out[r0 + 2] = 0x01;
          for (let k = 3; k <= 6; k++) out[r0 + k] = 0x00;
        } else {
          // Move Left's custom action (71..76) back to rec 0, put plain LEFT CLICK in 71..76
          const prevLeft = out.subarray(ho, ho + 6);
          const isPlain = prevLeft[0] === 0 && prevLeft[1] === 1;
          if (!isPlain && prevLeft.some(Boolean)) {
            for (let k = 0; k < 6; k++) out[r0 + 1 + k] = prevLeft[k];
          } else {
            out[r0 + 1] = 0x00; out[r0 + 2] = 0x02;
            for (let k = 3; k <= 6; k++) out[r0 + k] = 0x00;
          }
          out[ho] = 0x00; out[ho + 1] = 0x01;
          for (let k = 2; k < 6; k++) out[ho + k] = 0x00;
        }
      });
    },
    buttonIds: async () => [1, 2, 3, 5, 4, 9, 10],
    getButton: async (b) => sess.S.buttons?.[b] ?? null,
    setButton: async (b, act) => {
      const slot = eggActionToSlot(act);
      if (!slot) throw new DevErr("Unsupported button action on Endgame Gear", "unsupported");
      await mutate((out, cfg) => {
        const loc = eggSlotLoc(b, cfg.leftHanded);
        if (!loc) return;
        if (loc.kind === "handed") {
          const ho = EGG_OFF.HANDED;
          out[ho] = slot.type & 0xff;
          for (let k = 0; k < 5; k++) out[ho + 1 + k] = (slot.params[k] || 0) & 0xff;
        } else {
          const o = EGG_OFF.BUTTONS + loc.idx * 7;
          out[o + 1] = slot.type & 0xff;
          for (let k = 0; k < 5; k++) out[o + 2 + k] = (slot.params[k] || 0) & 0xff;
        }
      });
    },
    factoryReset: () => xrun(async () => {
      await sendCmd(EGG_OP.RESET);
      await sleep(1100);
      await pollAck("Factory reset");
      return syncBlob();
    }),
  };
}

export {
  EGG_VID,
  getEggDevices,
  EGG_GLASS_LOD,
  getEggWeDevices,
  EGG_POLL_RATES,
  EGG_POLL_DIVS,
  EGG_REPORT,
  EGG_CFG_LEN,
  EGG_CMD_LEN,
  EGG_OP,
  EGG_OFF,
  eggClampCpi,
  eggFeatureLen,
  eggNormalize,
  eggFormatVer,
  eggParseConfig,
  eggSlotToAction,
  eggActionToSlot,
  eggSlotLoc,
  classifyEgg,
  makeEggApi,
};
