// Shared devices/devices.json registry loader & dispatch guards for WebHID
import { DevErr, hex4 } from "./common.js";

export const TIER_LABELS = {
  0: "Tier 0 · Pointer only",
  1: "Tier 1 · Read-only",
  2: "Tier 2 · Verified",
};

let _doc = null;
let _modelsByKey = new Map();
let _profilesById = new Map();
let _usbMatches = new Map();      // "vid:pid" -> match
let _btMatches = new Map();       // "vid:pid" -> match
let _wpidMatches = new Map();     // "vid:wpid" -> match
let _loadPromise = null;

export function tierLabel(tier) {
  return TIER_LABELS[tier] ?? TIER_LABELS[0];
}

export function parseHexInt(val, fallback = 0) {
  if (val == null) return fallback;
  if (typeof val === "number") return val;
  const parsed = parseInt(String(val), 16);
  return Number.isNaN(parsed) ? fallback : parsed;
}

export function setRegistry(doc) {
  if (!doc || typeof doc !== "object" || doc.schema_version !== 1) {
    throw new Error("Invalid device registry payload");
  }
  _doc = doc;
  _modelsByKey = new Map((doc.models || []).map((m) => [m.key, m]));
  _profilesById = new Map(Object.entries(doc.profiles || {}));
  _usbMatches = new Map();
  _btMatches = new Map();
  _wpidMatches = new Map();
  for (const m of doc.matches || []) {
    if (m.id_namespace === "usb_pid" && m.pid) {
      _usbMatches.set(`${m.vid}:${m.pid}`, m);
    } else if (m.id_namespace === "bluetooth_pid" && m.pid) {
      _btMatches.set(`${m.vid}:${m.pid}`, m);
    } else if (m.id_namespace === "logitech_wpid" && m.wpid) {
      _wpidMatches.set(`${m.vid}:${m.wpid}`, m);
    }
  }
  return _doc;
}

export function getRegistry() {
  if (!_doc) {
    throw new Error("Device registry not loaded yet; await loadRegistry() or call setRegistry() first");
  }
  return _doc;
}

export function isRegistryLoaded() {
  return _doc !== null;
}

export function loadRegistry(url = null) {
  if (_doc) return Promise.resolve(_doc);
  if (_loadPromise) return _loadPromise;
  const target = url || (typeof window !== "undefined" ? "/devices/devices.json" : new URL("../../devices/devices.json", import.meta.url).href);
  _loadPromise = fetch(target, { cache: "no-store" })
    .then((res) => {
      if (!res.ok) throw new Error(`Failed to load device registry (${res.status})`);
      return res.json();
    })
    .then((doc) => setRegistry(doc))
    .catch((err) => {
      _loadPromise = null;
      throw err;
    });
  return _loadPromise;
}

function _packMatch(match) {
  if (!match) return null;
  const model = _modelsByKey.get(match.model_key);
  const profile = _profilesById.get(match.profile_id);
  if (!model || !profile) return null;
  return { match, model, profile };
}

export function resolveMatch(vid, pid, bus = "usb") {
  if (!_doc) return null;
  const key = `${hex4(vid)}:${hex4(pid)}`;
  const m = bus === "bluetooth" ? _btMatches.get(key) : _usbMatches.get(key);
  return _packMatch(m);
}

export function resolveMatchAnyBus(vid, pid) {
  if (!_doc) return null;
  const key = `${hex4(vid)}:${hex4(pid)}`;
  // Never cross-lookup logitech_wpid against USB/Bluetooth VID:PID
  return _packMatch(_usbMatches.get(key) || _btMatches.get(key));
}

export function getProfile(profileId) {
  return _profilesById.get(profileId) || null;
}

export function isKeyboardModel(vid, pid) {
  const res = resolveMatchAnyBus(vid, pid);
  return !!(res && res.model.kind === "keyboard");
}

export function getSupportedFilters() {
  if (!_doc) return [];
  const filters = [];
  const seen = new Set();
  for (const m of _doc.matches || []) {
    if (!m.udev_eligible || m.bus !== "usb" || m.id_namespace !== "usb_pid" || !m.pid) continue;
    const model = _modelsByKey.get(m.model_key);
    const prof = _profilesById.get(m.profile_id);
    if (!model || !prof || model.kind === "keyboard" || prof.tier < 1) continue;
    const key = `${m.vid}:${m.pid}`;
    if (seen.has(key)) continue;
    seen.add(key);
    filters.push({ vendorId: parseInt(m.vid, 16), productId: parseInt(m.pid, 16) });
  }
  return filters;
}

export function getRazerDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "1532" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "razer" || prof.tier < 1) continue;
    const pid = parseInt(m.pid, 16);
    out[pid] = {
      name: model.name,
      link: m.link,
      prio: m.prio,
      wireless: !!m.wireless,
      hyper: !!m.hyper,
      dpiMax: m.meta?.dpi_max ?? prof.operations?.dpi?.dpi_max ?? 30000,
      tier: prof.tier,
      profileId: m.profile_id,
    };
  }
  return out;
}

export function getEggDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "3367" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "egg8k") continue;
    const meta = m.meta || {};
    out[parseInt(m.pid, 16)] = {
      name: model.name,
      family: meta.family || "v1",
      cpiMin: meta.cpi_min ?? 50,
      cpiMax: meta.cpi_max ?? 26000,
      stepLo: meta.cpi_step_lo ?? 50,
      stepHi: meta.cpi_step_hi ?? 50,
      lodOptions: [...(meta.lod_options || ["0.7 mm", "1 mm", "2 mm"])],
      lodGlass: !!meta.lod_glass,
      angleTuning: !!meta.angle_tuning,
      forceMaxFps: !!meta.force_max_fps,
      ledLiftoff: !!meta.led_liftoff,
      motionJitter: meta.motion_jitter !== false,
      profileId: m.profile_id,
    };
  }
  return out;
}

export function getEggWeDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "3367" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "egg_we") continue;
    out[parseInt(m.pid, 16)] = {
      name: model.name,
      link: m.link,
      wireless: !!m.wireless,
      prio: m.prio,
      profileId: m.profile_id,
    };
  }
  return out;
}

export function getLogiDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (!["usb_pid", "bluetooth_pid"].includes(m.id_namespace) || m.vid !== "046d" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "hidpp") continue;
    out[parseInt(m.pid, 16)] = { name: model.name, link: m.link, bus: m.bus, profileId: m.profile_id };
  }
  return out;
}

export function getPulsarXs1Devices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "3710" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "pulsar_xs1") continue;
    out[parseInt(m.pid, 16)] = {
      name: model.name, link: m.link, wireless: !!m.wireless, maxPoll: m.meta?.max_poll ?? 1000, profileId: m.profile_id,
    };
  }
  return out;
}

export function getPulsarProDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "3710" || !m.pid) continue;
    const prof = _profilesById.get(m.profile_id);
    const model = _modelsByKey.get(m.model_key);
    if (!prof || !model || prof.driver !== "pulsar_pro") continue;
    out[parseInt(m.pid, 16)] = {
      name: model.name, link: m.link, wireless: !!m.wireless, maxPoll: m.meta?.max_poll ?? 8000, profileId: m.profile_id,
    };
  }
  return out;
}

export function getAresonDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || m.vid !== "25a7" || !m.pid || m.profile_id !== "pulsar_areson_compx") continue;
    const model = _modelsByKey.get(m.model_key);
    if (!model) continue;
    out[parseInt(m.pid, 16)] = { name: model.name, link: m.link, wireless: !!m.wireless, profileId: m.profile_id };
  }
  return out;
}

export function getCompxDevices() {
  const out = {};
  if (!_doc) return out;
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || !m.pid || m.profile_id !== "compx_odm") continue;
    const model = _modelsByKey.get(m.model_key);
    if (!model) continue;
    const meta = m.meta || {};
    const rec = {
      brand: meta.brand,
      name: model.name,
      link: m.link,
      wireless: !!m.wireless,
      dpiMax: meta.dpi_max ?? 30000,
      rates: [...(meta.rates || [125, 250, 500, 1000])],
      profileId: m.profile_id,
    };
    if (meta.model) rec.model = meta.model;
    out[`${m.vid}:${m.pid}`] = rec;
  }
  return out;
}

export function getAtkProducts() {
  const out = {};
  if (!_doc) return out;
  for (const item of _doc.atk_products || []) {
    const rec = {
      name: item.name,
      sensor: item.sensor,
      dpiMax: item.dpi_max,
      rates: [...item.rates],
      maxStages: item.max_stages,
      isR1: !!item.is_r1,
    };
    if (item.dpi6) rec.dpi6 = true;
    if (item.anti_mistouch) rec.antiMistouch = true;
    out[`${item.cid}:${item.mid}`] = rec;
  }
  return out;
}

export function getGloriousTables() {
  const sinowealth = {};
  const core2 = {};
  const classic = {};
  const pixart = {};
  if (!_doc) return { sinowealth, core2, classic, pixart };
  for (const m of _doc.matches || []) {
    if (m.id_namespace !== "usb_pid" || !m.pid) continue;
    const model = _modelsByKey.get(m.model_key);
    if (!model) continue;
    const vp = `${m.vid}:${m.pid}`;
    const meta = m.meta || {};
    if (m.profile_id === "glorious_sinowealth") {
      sinowealth[vp] = { name: model.name, link: m.link };
    } else if (m.profile_id === "glorious_core2") {
      core2[vp] = { name: model.name, link: m.link, wireless: !!m.wireless, maxPoll: meta.max_poll ?? 4000 };
    } else if (m.profile_id === "glorious_classic") {
      classic[vp] = { name: model.name, link: m.link, wireless: !!m.wireless, gen: meta.gen || "legacy" };
    } else if (m.profile_id === "glorious_pixart") {
      pixart[vp] = { name: model.name, link: m.link, wireless: !!m.wireless };
    }
  }
  return { sinowealth, core2, classic, pixart };
}

export function tierMetaFromProfile(profileId, profile) {
  const p = profile || _profilesById.get(profileId);
  const tier = p?.tier ?? 0;
  return {
    tier,
    tierLabel: tierLabel(tier),
    profileId: profileId || "generic_tier0",
    writeEnabled: !!(p?.write_enabled && tier >= 2),
    evidenceStatus: p?.evidence_status ?? "none",
    operations: p?.operations ? structuredClone(p.operations) : {},
  };
}

export function assertWriteAllowed(sess, opName) {
  const tier = sess?.tier ?? 0;
  if (tier === 0) {
    throw new DevErr(`Blocked: ${sess?.name || "device"} is ${tierLabel(0)} (zero vendor commands)`, "readonly");
  }
  if (tier < 2 || !sess?.writeEnabled) {
    throw new DevErr(`Blocked at dispatch: ${sess?.name || "device"} is ${tierLabel(tier)} (cannot write ${opName})`, "readonly");
  }
  const op = sess?.operations?.[opName];
  if (op && !op.write) {
    throw new DevErr(`Blocked at dispatch: ${opName} write is not enabled on this profile`, "readonly");
  }
}

export function isRazerCmdAllowed(sess, cls, cmd, txn) {
  const tier = sess?.tier ?? 0;
  if (tier === 0) return false;
  if (tier >= 2 && sess?.writeEnabled) return true;
  const ops = sess?.operations || {};
  for (const op of Object.values(ops)) {
    if (!op?.read || op?.write) continue;
    if (!op.cmd_class || !op.cmd_id) continue;
    const opCls = parseHexInt(op.cmd_class, -1);
    const opCmd = parseHexInt(op.cmd_id, -1);
    const opTxn = parseHexInt(op.txn, 0x1f);
    if (cls === opCls && cmd === opCmd && txn === opTxn) return true;
  }
  return false;
}
