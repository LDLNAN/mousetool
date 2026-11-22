// Protocol 1: Razer 90-byte feature report driver (registry-backed, strict Tier 0/1 dispatch guards)
import { DevErr, calcReportBytes, hex, sleep } from "./common.js";
import {
  assertWriteAllowed,
  getRazerDevices,
  isRazerCmdAllowed,
  parseHexInt,
  resolveMatch,
  tierMetaFromProfile,
} from "./registry.js";

const VID = 0x1532;

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

function hasRazerControlReport(d) {
  return (d.collections || []).some((c) =>
    (c.featureReports || []).some((r) => calcReportBytes(r) === 90)
  );
}

// Strict registry-only Razer classification: unknown Razer VIDs NEVER fall back into the Razer driver.
function classifyRazer(d) {
  const vid = d.vendorId, pid = d.productId;
  if (vid !== VID) return null;
  const resolved = resolveMatch(vid, pid, "usb");
  if (!resolved) return null;
  const { match, model, profile } = resolved;
  if (model.kind === "keyboard" || profile.driver !== "razer" || profile.tier < 1) return null;
  if (!hasRazerControlReport(d)) return null;

  const ops = profile.operations || {};
  const dpiOp = ops.dpi || {};
  const dpiMax = match.meta?.dpi_max ?? dpiOp.dpi_max ?? 30000;
  const dpiMin = dpiOp.dpi_min ?? 100;
  const dpiStepLo = dpiOp.dpi_step_lo ?? 50;
  const dpiStepHi = dpiOp.dpi_step_hi ?? 50;
  const isReadOnly = profile.tier < 2 || !profile.write_enabled;

  const info = {
    name: model.name,
    link: match.link,
    prio: match.prio,
    wireless: !!match.wireless,
    hyper: !!match.hyper,
    dpiMax,
  };

  const pollRates = ops.polling?.read
    ? (ops.polling.rates ? [...ops.polling.rates] : (info.hyper ? [125, 250, 500, 1000, 2000, 4000, 8000] : [125, 500, 1000]))
    : [];

  return {
    driver: "razer",
    protoLabel: profile.proto_label,
    protoTag: profile.proto_tag,
    ...tierMetaFromProfile(match.profile_id, profile),
    info,
    caps: {
      stages: !!(ops.stages?.read && ops.stages?.write && !isReadOnly),
      dpi: !!(dpiOp.write && !isReadOnly),
      dpiRead: !!dpiOp.read,
      dpiWrite: !!(dpiOp.write && !isReadOnly),
      maxStages: ops.stages?.max_stages ?? 5,
      dpiMin,
      dpiMax,
      dpiStepLo,
      dpiStepHi,
      splitXY: !!dpiOp.split_xy,
      pollRates,
      pollReadonly: !!(ops.polling?.read && (!ops.polling?.write || isReadOnly)),
      lodMode: ops.lod?.read && !isReadOnly ? "razer" : "none",
      lodAsync: !!(ops.lod?.async && !isReadOnly),
      buttons: !!(ops.buttons?.read && ops.buttons?.write && !isReadOnly),
      layers: !!(ops.layers?.read && ops.layers?.write && !isReadOnly),
      wireless: info.wireless,
      powerTune: !!(info.wireless && ops.power?.write && !isReadOnly),
      batteryRead: !!(info.wireless && ops.battery?.read),
      hyper: !!(info.hyper && !isReadOnly),
      dongle: !!(info.hyper && ops.dongle?.read && !isReadOnly),
      factoryReset: false,
      statusNote: profile.status_note || null,
    },
  };
}

function makeRazerApi(sess, { isConnected, log }) {
  const d = sess.dev;
  const ops = sess.operations || {};
  const hyper = !!sess.caps.hyper;
  const tag = sess.shortName;

  const opTxn = (opName, fallback = 0x1f) => parseHexInt(ops[opName]?.txn, fallback);
  const opStorage = (opName, fallback = 0x00) => parseHexInt(ops[opName]?.storage, fallback);
  const canRead = (opName) => ops[opName]?.read !== false && (sess.tier >= 2 || !!ops[opName]?.read);

  async function readReply() {
    const dv = await d.receiveFeatureReport(0);
    let b = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
    if (b.length === 91) b = b.subarray(1);
    return b;
  }

  async function xferRaw(cls, cmd, size, args, txn) {
    if (!isRazerCmdAllowed(sess, cls, cmd, txn)) {
      throw new DevErr(
        `Blocked at dispatch: ${sess.tierLabel || "Tier 1"} does not allow Razer cmd 0x${cls.toString(16)}:0x${cmd.toString(16)} (txn 0x${txn.toString(16)})`,
        "readonly"
      );
    }
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
    if (!isRazerCmdAllowed(sess, cls, cmd, txn)) {
      return Promise.reject(
        new DevErr(
          `Blocked at dispatch: ${sess.tierLabel || "Tier 1"} does not allow Razer cmd 0x${cls.toString(16)}:0x${cmd.toString(16)} (txn 0x${txn.toString(16)})`,
          "readonly"
        )
      );
    }
    const p = sess.queue.then(() => xferRaw(cls, cmd, size, args, txn));
    sess.queue = p.catch(() => {});
    return p;
  }

  const fastKeys = [
    ...(canRead("dpi") ? ["dpi"] : []),
    ...(canRead("stages") ? ["stages"] : []),
  ];
  const slowKeys = [
    ...(canRead("polling") ? ["poll"] : []),
    ...(canRead("lod") ? ["lod"] : []),
    ...(sess.caps.wireless && canRead("power") ? ["idle", "low"] : []),
    ...(sess.caps.wireless && canRead("battery") ? ["battery", "charging"] : []),
    ...(hyper && canRead("dongle") ? ["led"] : []),
  ];

  return {
    fastKeys,
    slowKeys,
    firmware: async () => {
      if (!canRead("firmware")) return "–";
      const a = await xfer(0x00, 0x81, 0x02, [], opTxn("firmware", 0x1f));
      return `v${a[0]}.${a[1]}`;
    },
    serial: async () => {
      if (!canRead("serial")) return d.serialNumber || "";
      const a = await xfer(0x00, 0x82, 0x16, [], opTxn("serial", 0x1f));
      return new TextDecoder().decode(a.subarray(0, 22)).replace(/\0.*$/, "").trim();
    },
    battery: async () => {
      if (!sess.caps.wireless || !canRead("battery")) return undefined;
      return (await xfer(0x07, 0x80, 0x02, [], opTxn("battery", 0x1f)))[1] / 255 * 100;
    },
    charging: async () => {
      if (!sess.caps.wireless || !canRead("battery")) return undefined;
      return !!(await xfer(0x07, 0x84, 0x02, [], opTxn("battery", 0x1f)))[1];
    },
    getPoll: async () => {
      if (!canRead("polling")) return undefined;
      const t = opTxn("polling", 0x1f);
      return hyper
        ? POLL2_CODES[(await xfer(0x00, 0xc0, 0x01, [], t))[1]] ?? null
        : POLL_CODES[(await xfer(0x00, 0x85, 0x01, [], t))[0]] ?? null;
    },
    setPoll: async (hz) => {
      assertWriteAllowed(sess, "polling");
      const t = opTxn("polling", 0x1f);
      if (!hyper) return xfer(0x00, 0x05, 0x01, [POLL_HZ[hz]], t);
      await xfer(0x00, 0x40, 0x02, [0x00, POLL2_HZ[hz]], t);
      try { await xfer(0x00, 0x40, 0x02, [0x01, POLL2_HZ[hz]], t); }
      catch (e) { log(`! polling: mouse half didn't confirm (${e.message})`, tag); }
    },
    pair: async (pid) => {
      assertWriteAllowed(sess, "dongle");
      await xfer(0x00, 0x46, 0x01, [0x01]);
      return xfer(0x00, 0x41, 0x03, [0x01, pid >> 8, pid & 255]);
    },
    unpair: async (pid) => {
      assertWriteAllowed(sess, "dongle");
      return xfer(0x00, 0x42, 0x02, [pid >> 8, pid & 255], 0xff);
    },
    getLod: async () => {
      if (!canRead("lod")) return undefined;
      const t = opTxn("lod", 0x1f);
      for (let i = 0; i < 3; i++) {
        const a = await xfer(0x0b, 0x85, 0x01, [], t);
        if (a[2] === 0x01 && a[3] <= 2) return { mode: "preset", level: a[3] };
        if (a[2] === 0x04) return { mode: "async", lift: a[4] + 1, land: a[5] + 1 };
        await sleep(60);
      }
      return null;
    },
    setLodPreset: async (level) => {
      assertWriteAllowed(sess, "lod");
      const t = opTxn("lod", 0x1f);
      await xfer(0x0b, 0x03, 0x03, [0x00, 0x04, 0x28], t);
      return xfer(0x0b, 0x0b, 0x04, [0x00, 0x04, 0x01, level], t);
    },
    setLodAsync: async (lift, land) => {
      assertWriteAllowed(sess, "lod");
      const t = opTxn("lod", 0x1f);
      await xfer(0x0b, 0x03, 0x03, [0x00, 0x04, 0x01], t);
      await xfer(0x0b, 0x0b, 0x04, [0x00, 0x04, 0x04, 0x00], t);
      return xfer(0x0b, 0x05, 0x04, [0x00, 0x04, lift - 1, land - 1], t);
    },
    buttonIds: async () => {
      if (!canRead("buttons")) return [];
      const a = await xfer(0x02, 0x84, 0x50, [], opTxn("buttons", 0x1f));
      return Array.from(a.subarray(1, 1 + Math.min(a[0], 20)));
    },
    getButton: async (b, layer = sess.btnLayer) => {
      if (!canRead("buttons")) return null;
      const a = await xfer(0x02, 0x8c, 0x0a, [0x01, b, layer], opTxn("buttons", 0x1f));
      return { fn: a[3], size: a[4], data: Array.from(a.subarray(5, 10)) };
    },
    setButton: async (b, act, layer = sess.btnLayer) => {
      assertWriteAllowed(sess, "buttons");
      const data = [...act.data, 0, 0, 0, 0, 0].slice(0, 5);
      return xfer(0x02, 0x0c, 0x50, [0x01, b, layer, act.fn, act.size, ...data], opTxn("buttons", 0x1f));
    },
    getDongleLed: async () => {
      if (!canRead("dongle")) return undefined;
      return (await xfer(0x07, 0x90, 0x01, [], 0xff))[0];
    },
    setDongleLed: async (m) => {
      assertWriteAllowed(sess, "dongle");
      return xfer(0x07, 0x10, 0x01, [m], 0xff);
    },
    getDpi: async () => {
      if (!canRead("dpi")) return undefined;
      const a = await xfer(0x04, 0x85, 0x07, [opStorage("dpi", 0x00)], opTxn("dpi", 0x1f));
      const x = (a[1] << 8) | a[2];
      const y = (a[3] << 8) | a[4];
      const mn = sess.caps.dpiMin ?? 100;
      const mx = sess.caps.dpiMax ?? 35000;
      if (x < mn || x > mx || y < mn || y > mx) return null;
      return [x, y];
    },
    setDpi: async (x, y) => {
      assertWriteAllowed(sess, "dpi");
      return xfer(0x04, 0x05, 0x07, [0x01, x >> 8, x & 255, y >> 8, y & 255, 0, 0], opTxn("dpi", 0x1f));
    },
    getStages: async () => {
      if (!canRead("stages")) return undefined;
      const a = await xfer(0x04, 0x86, 0x26, [0x01], opTxn("stages", 0x1f));
      const active = a[1], n = Math.min(a[2], 5), list = [];
      for (let i = 0; i < n; i++) {
        const o = 3 + i * 7;
        list.push({ x: a[o + 1] << 8 | a[o + 2], y: a[o + 3] << 8 | a[o + 4] });
      }
      return { active, list };
    },
    setStages: async (active, list) => {
      assertWriteAllowed(sess, "stages");
      const args = [0x01, active, list.length];
      list.forEach((s, i) => args.push(i + 1, s.x >> 8, s.x & 255, s.y >> 8, s.y & 255, 0, 0));
      return xfer(0x04, 0x06, 0x26, args, opTxn("stages", 0x1f));
    },
    getIdle: async () => {
      if (!sess.caps.wireless || !canRead("power")) return undefined;
      const a = await xfer(0x07, 0x83, 0x02, [], opTxn("power", 0x1f));
      return a[0] << 8 | a[1];
    },
    setIdle: async (sec) => {
      assertWriteAllowed(sess, "power");
      return xfer(0x07, 0x03, 0x02, [sec >> 8, sec & 255], opTxn("power", 0x1f));
    },
    getLow: async () => {
      if (!sess.caps.wireless || !canRead("power")) return undefined;
      return (await xfer(0x07, 0x81, 0x01, [], opTxn("power", 0x1f)))[0];
    },
    setLow: async (raw) => {
      assertWriteAllowed(sess, "power");
      return xfer(0x07, 0x01, 0x01, [raw], opTxn("power", 0x1f));
    },
  };
}

export {
  VID,
  getRazerDevices,
  POLL_CODES,
  POLL_HZ,
  POLL2_CODES,
  POLL2_HZ,
  STATUS,
  razerBuild,
  hasRazerControlReport,
  classifyRazer,
  makeRazerApi,
};
