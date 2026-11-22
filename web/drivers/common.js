// Shared WebHID driver helpers and local state cache utilities

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const hex = (u8) => Array.from(u8, (b) => b.toString(16).padStart(2, "0")).join(" ");
export const hex4 = (n) => (n ?? 0).toString(16).padStart(4, "0");
export const vpKey = (vid, pid) => `${hex4(vid)}:${hex4(pid)}`;
export const fmtHz = (hz) => (hz >= 1000 ? hz / 1000 + "k" : String(hz));
export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

export class DevErr extends Error {
  constructor(msg, kind) {
    super(msg);
    this.kind = kind;
  }
}

export function calcReportBytes(rep) {
  let bits = 0;
  for (const it of rep?.items || []) bits += (it.reportSize || 0) * (it.reportCount || 0);
  return Math.ceil(bits / 8);
}

export function findCollection(d, { page = null, usage = null, featId = null, inId = null, outId = null } = {}) {
  for (const c of d.collections || []) {
    if (page !== null && c.usagePage !== page) continue;
    if (usage !== null && c.usage !== usage) continue;
    if (featId !== null && !(c.featureReports || []).some((r) => r.reportId === featId)) continue;
    if (inId !== null && !(c.inputReports || []).some((r) => r.reportId === inId)) continue;
    if (outId !== null && !(c.outputReports || []).some((r) => r.reportId === outId)) continue;
    return c;
  }
  return null;
}

export function unwrapReport(dv, rid, expectedLen) {
  const u = new Uint8Array(dv.buffer, dv.byteOffset, dv.byteLength);
  if (expectedLen && u.length === expectedLen + 1 && u[0] === rid) return u.subarray(1);
  return u;
}

export function cacheKeyFor(sess) {
  return `mousetool.devcache.${vpKey(sess.vid, sess.pid)}.${sess.dev.serialNumber || "default"}`;
}

export function loadLocalCache(sess) {
  try {
    return JSON.parse(localStorage.getItem(cacheKeyFor(sess)) || "{}") || {};
  } catch {
    return {};
  }
}

export function saveLocalCache(sess, patch) {
  try {
    const cur = loadLocalCache(sess);
    localStorage.setItem(cacheKeyFor(sess), JSON.stringify({ ...cur, ...patch }));
  } catch {}
}

export function describeHidCollections(d) {
  const ids = (rs) => (rs || []).map((r) => "0x" + r.reportId.toString(16).padStart(2, "0")).join(",") || "none";
  return (d.collections || []).map((c) =>
    `page 0x${hex4(c.usagePage)}/0x${c.usage.toString(16)} (in:${ids(c.inputReports)} out:${ids(c.outputReports)} feat:${ids(c.featureReports)})`
  ).join(" · ") || "no collections";
}
