// Pure DOM-free WebHID device classification & target grouping (shared by app.js and Node tests)
import { findCollection, vpKey } from "./common.js";
import { EGG_REPORT, EGG_VID, classifyEgg } from "./egg.js";
import { classifyGlorious } from "./glorious.js";
import { classifyLogitech } from "./logitech.js";
import { classifyPulsar } from "./pulsar.js";
import { classifyRazer } from "./razer.js";
import {
  getProfile,
  isKeyboardModel,
  resolveMatchAnyBus,
  tierMetaFromProfile,
} from "./registry.js";

function isKeyboardOnlyCollections(d) {
  const cols = d.collections || [];
  if (!cols.length) return false;
  const hasKbd = cols.some((c) => c.usagePage === 0x01 && c.usage === 0x06);
  const hasMouse = cols.some((c) => c.usagePage === 0x01 && c.usage === 0x02);
  const hasVendor = cols.some((c) => c.usagePage >= 0xff00);
  return hasKbd && !hasMouse && !hasVendor;
}

export function classifyDevice(d, allowGeneric = false, ctx = {}) {
  if (isKeyboardModel(d.vendorId, d.productId) || isKeyboardOnlyCollections(d)) {
    return null;
  }

  const spec =
    classifyRazer(d) ||
    classifyEgg(d, ctx) ||
    classifyLogitech(d) ||
    classifyPulsar(d) ||
    classifyGlorious(d);
  if (spec) return spec;

  if (allowGeneric) {
    const vp = vpKey(d.vendorId, d.productId);
    const catalog = resolveMatchAnyBus(d.vendorId, d.productId);
    const t0 = getProfile("generic_tier0");
    const displayName = catalog?.model?.name || d.productName || `HID Device (${vp})`;
    const linkName = catalog?.match?.link || "Generic HID";
    return {
      driver: "generic",
      protoLabel: t0?.proto_label || "Generic USB / HID Mouse",
      protoTag: t0?.proto_tag || "HID",
      ...tierMetaFromProfile("generic_tier0", t0),
      info: { name: displayName, link: linkName, prio: 9, wireless: false, hyper: false, dpiMax: 50000 },
      caps: {
        stages: false,
        dpi: false,
        dpiRead: false,
        dpiWrite: false,
        maxStages: 1,
        dpiMin: 50,
        dpiMax: 50000,
        dpiStepLo: 50,
        dpiStepHi: 50,
        splitXY: false,
        pollRates: [],
        lodMode: "none",
        buttons: false,
        layers: false,
        wireless: false,
        powerTune: false,
        batteryRead: false,
        hyper: false,
        dongle: false,
        factoryReset: false,
        genericDpi: true,
        statusNote: `Tier 0 · Pointer only (${vp}): no reviewed protocol profile matches this device. Zero vendor HID commands are sent. Use local DPI reference, live polling meter, input tester, or host Acceleration & desktop settings.`,
      },
    };
  }
  return null;
}

export function groupHidTargets(devices, allowGeneric = false, existingClaimed = new Set(), existingSessions = []) {
  const claimed = new Set(existingClaimed);
  const eggA1Vps = new Set();
  for (const d of devices) {
    if (d.vendorId === EGG_VID && findCollection(d, { featId: 0xa1 })) eggA1Vps.add(vpKey(d.vendorId, d.productId));
  }
  const claimNotify = (d, pred) => {
    const cands = devices.filter(
      (cand) => cand !== d && !claimed.has(cand) && cand.vendorId === d.vendorId && cand.productId === d.productId && pred(cand)
    );
    if (!cands.length) return [];
    const exact = d.serialNumber ? cands.find((c) => c.serialNumber === d.serialNumber) : null;
    const match = exact || cands[0];
    claimed.add(match);
    return [match];
  };

  const targets = [];
  for (const d of devices) {
    if (claimed.has(d)) continue;
    const spec = classifyDevice(d, false, { eggA1Vps });
    if (!spec) continue;
    let siblings = [];
    if (spec.driver === "egg") {
      siblings = claimNotify(d, (cand) => cand.collections?.some((c) => (c.inputReports || []).some((r) => r.reportId === EGG_REPORT.EVT)));
    } else if (spec.driver === "egg_we" || (spec.driver === "compx" && spec.info.brand === "areson")) {
      siblings = claimNotify(d, (cand) => cand.collections?.some((c) => (c.inputReports || []).some((r) => r.reportId === 0x09)));
    }
    claimed.add(d);
    targets.push({ dev: d, siblings, spec });
  }
  if (allowGeneric) {
    for (const d of devices) {
      if (claimed.has(d)) continue;
      // Skip secondary non-control interfaces of an already-claimed mouse with same VID:PID and serial
      const alreadyOwned = [
        ...existingSessions,
        ...targets.map((t) => ({ vid: t.dev.vendorId, pid: t.dev.productId, serial: t.dev.serialNumber })),
      ].some(
        (x) => x.vid === d.vendorId && x.pid === d.productId && (!d.serialNumber || !x.serial || x.serial === d.serialNumber)
      );
      if (alreadyOwned) continue;
      const spec = classifyDevice(d, true, { eggA1Vps });
      if (spec) {
        claimed.add(d);
        targets.push({ dev: d, siblings: [], spec });
      }
    }
  }
  targets.sort((a, b) => a.spec.info.prio - b.spec.info.prio);
  return targets;
}
