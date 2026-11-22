// Mouse Tool web UI entry point (native ES modules, zero build step)
import { createAccelController } from "./accel_graph.js";
import { groupHidTargets } from "./drivers/classifier.js";
import {
  describeHidCollections,
  fmtHz,
  hex,
  hex4,
  same,
  sleep,
  vpKey,
} from "./drivers/common.js";
import {
  EGG_GLASS_LOD,
  EGG_OFF,
  eggClampCpi,
  makeEggApi,
} from "./drivers/egg.js";
import {
  makeGloriousClassicApi,
  makeGloriousCore2Api,
  makeGloriousPixartApi,
  makeGloriousSinowealthApi,
} from "./drivers/glorious.js";
import { makeHidppApi } from "./drivers/logitech.js";
import {
  makeCompxApi,
  makePulsarProApi,
  makePulsarXs1Api,
} from "./drivers/pulsar.js";
import { makeRazerApi } from "./drivers/razer.js";
import {
  getSupportedFilters,
  loadRegistry,
  tierLabel,
} from "./drivers/registry.js";
import {
  fetchHelperBattery,
  fetchHelperStatus,
  postHelperBattery,
} from "./helper_client.js";

const registryReady = loadRegistry().catch((e) => {
  console.error("Failed to load /devices/devices.json:", e);
  return null;
});

const $ = (id) => document.getElementById(id);

// ======================================================================
// multi-mouse session store
// ======================================================================
const sessions = new Map();  // sessionId -> session
let activeId = null;
let systemMice = [];         // passive sysfs list from /api/status
const held = new Set();      // sliders currently being dragged
let btnSel = null, capturing = null;
let snoopEnabled = false;
let activeTab = "perf";

const cur = () => sessions.get(activeId) ?? null;
const dpiMin = (s = cur()) => s?.caps?.dpiMin ?? 100;
const dpiMax = (s = cur()) => s?.caps?.dpiMax ?? 30000;
const maxStages = (s = cur()) => s?.caps?.maxStages ?? 5;
const activeS = () => cur()?.S ?? {};

// ======================================================================
// log + toast
// ======================================================================
const logLines = [];
let logQueued = false;
function log(s, prefix = "") {
  const tag = prefix ? `[${prefix}] ` : (cur() ? `[${cur().shortName}] ` : "");
  logLines.push(new Date().toLocaleTimeString() + "  " + tag + s);
  if (logLines.length > 500) logLines.shift();
  if (!logQueued && !document.querySelector('[data-panel="device"]').hidden) {
    logQueued = true;
    requestAnimationFrame(() => { logQueued = false; paintLog(); });
  }
}
function paintLog() {
  const el = $("log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  el.textContent = logLines.join("\n");
  if (atBottom) el.scrollTop = el.scrollHeight;
}
$("logCopy").addEventListener("click", () => navigator.clipboard?.writeText(logLines.join("\n")).then(() => toast("Log copied")));
$("logClear").addEventListener("click", () => { logLines.length = 0; paintLog(); });

let toastT;
function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "show" + (err ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = ""), err ? 4200 : 2400);
}

// ======================================================================
// protocol 8: Generic HID mouse fallback (any vendor/device)
// ======================================================================
function makeGenericApi(sess) {
  const key = `mousetool.genericDpi.${hex4(sess.vid)}:${hex4(sess.pid)}`;
  const readLocal = () => {
    const v = parseInt(localStorage.getItem(key) || "800", 10) || 800;
    return [v, v];
  };
  return {
    fastKeys: [],
    slowKeys: [],
    init: async () => { sess.S.dpi = readLocal(); },
    firmware: async () => "–",
    serial: async () => sess.dev.serialNumber || "",
    getDpi: async () => readLocal(),
    setGenericDpi: (v) => { localStorage.setItem(key, String(v)); sess.S.dpi = [v, v]; },
  };
}

// ======================================================================
// per-session commit helper: write → read back → update session state
// ======================================================================
function setState(key, kind, msg = "") {
  for (const el of document.querySelectorAll(`.state[data-for="${key}"]`)) {
    clearTimeout(el._t);
    el.className = "state" + (kind ? " " + kind : "");
    el.textContent = kind === "saving" ? "Saving" : kind === "saved" ? "✓ Saved" : kind === "error" ? msg : "";
    el.title = kind === "error" ? msg : "";
    if (kind === "saved") el._t = setTimeout(() => setState(key, ""), 1800);
  }
}

async function commit(key, { write, read, want, eq = same, label, sess = cur() }) {
  if (!sess) return false;
  if (!sess.writeEnabled || (sess.tier ?? 0) < 2) {
    const msg = `Blocked: ${sess.tierLabel || tierLabel(sess.tier ?? 0)} device`;
    if (cur()?.id === sess.id) {
      setState(key, "error", msg);
      toast(`${label}: ${msg}`, true);
    }
    log(`! ${label}: ${msg}`, sess.shortName);
    return false;
  }
  sess.userBusy++; sess.pending.add(key);
  if (cur()?.id === sess.id) setState(key, "saving");
  let err = null, got;
  try { await write(); } catch (e) { err = e; log(`! ${label}: write said "${e.message}", checking…`, sess.shortName); }
  try { await sleep(30); got = await read(); } catch (e) { err ??= e; }
  sess.pending.delete(key); sess.userBusy--;
  if (got !== undefined && got !== null) sess.S[key] = got;
  if (cur()?.id === sess.id) {
    render(key);
    if (got != null && eq(got, want)) { setState(key, "saved"); markAwake(sess); renderFleet(); return true; }
    const msg = got != null ? "The mouse kept the old value" : (err?.message ?? "No answer");
    setState(key, "error", msg);
    toast(`${label}: ${msg[0].toLowerCase() + msg.slice(1)}`, true);
    if (err?.kind === "timeout") setAsleep(true, sess);
  }
  renderFleet();
  return !!(got != null && eq(got, want));
}

// ======================================================================
// acceleration & desktop pointer controller
// ======================================================================
const accelCtrl = createAccelController({
  $,
  getActiveDpi: () => activeS()?.dpi?.[0],
  isAccelTabActive: () => activeTab === "accel",
  setState,
  toast,
  paintRanges,
  onSystemMice: (mice) => { systemMice = mice; renderFleet(); },
});

// ======================================================================
// rendering
// ======================================================================
const R = {
  dpi: renderDpi, stages: renderStagesFromDevice, poll: renderPoll, idle: renderIdle, low: renderLow,
  lod: renderLod, led: renderLed, battery: renderBattery, charging: renderBattery, egg: renderTune, tune: renderTune,
};
function render(key) {
  if (key === "eggAll" || key === "compxAll" || key === "pproAll" || key === "sinoAll") {
    renderDpi(); renderStagesFromDevice(); renderPoll(); renderLod(); renderTune(); renderButtons();
    const s = cur();
    if (s) {
      $("statusNote").hidden = !s.caps.statusNote;
      $("statusNote").textContent = s.caps.statusNote || "";
      if (key === "compxAll" || key === "pproAll") renderIdle();
      if (key === "pproAll") renderLow();
      if (key === "sinoAll") {
        buildPollButtons();
        $("stagesGroup").hidden = !s.caps.stages;
        $("dpiNowRow").hidden = !s.caps.dpi;
      }
      if (key === "compxAll") {
        $("stagesHint").textContent = `Click a stage to make it active · up to ${s.caps.maxStages} · ${s.caps.dpiMin}–${s.caps.dpiMax.toLocaleString()}`;
      }
    }
    return;
  }
  if (key.startsWith("btn:")) renderButtons(); else R[key]?.();
}

function renderDpi() {
  const s = cur();
  const d = s?.S?.dpi;
  if (!d) {
    $("hDpi").textContent = "–";
    $("roDpiVal").textContent = "–";
    return;
  }
  const [x, y] = d;
  $("hDpi").innerHTML = x === y ? `${x}` : `${x}<small>x</small> ${y}<small>y</small>`;
  $("roDpiVal").textContent = x === y ? `${x} DPI` : `${x} × ${y} DPI`;
  $("genericDpiIn").value = x;
  renderTrack();
  accelCtrl.renderAccelDpiHint();
}

function renderPips() {
  const s = cur();
  const st = s?.S?.stages; const wrap = $("hPips");
  if (!st || !s?.caps?.stages) {
    wrap.innerHTML = "";
    $("hStage").textContent = s?.tier === 1 && s?.caps?.dpiRead ? "Read-only" : s?.caps?.genericDpi ? "Reference" : "";
    return;
  }
  wrap.innerHTML = st.list.map((_, i) => `<i class="${i + 1 === st.active ? "on" : ""}" title="Stage ${i + 1}"></i>`).join("");
  wrap.title = `Stage ${st.active} of ${st.list.length}`;
  $("hStage").textContent = `Stage ${st.active}/${st.list.length}`;
}

let pollRates = [];
function buildPollButtons() {
  const s = cur();
  pollRates = s?.caps?.pollRates ?? [];
  $("pollRow").hidden = !pollRates.length;
  const ro = !!s?.caps?.pollReadonly;
  $("poll").innerHTML = pollRates.map((hz) => `<button data-hz="${hz}"${ro ? " disabled" : ""}>${fmtHz(hz)}</button>`).join("");
  $("pollHint").textContent = ro
    ? "Current hardware polling rate (read-only over USB HID; change with the mouse's physical button)."
    : s?.driver === "egg"
    ? "High-speed USB polling (1k–8k Hz) stored on the mouse."
    : s?.caps?.hyper
    ? "How often the mouse reports its position (Hz). Above 1k costs battery and some CPU; 2k–4k is the sweet spot."
    : s?.driver === "hidpp"
    ? "USB / wireless report rate via Logitech HID++."
    : "How often the mouse reports its position (Hz).";
}
function renderPoll() {
  const hz = activeS().poll;
  for (const b of $("poll").children) { b.classList.toggle("sel", +b.dataset.hz === hz); b.classList.remove("want"); }
  $("hPoll").innerHTML = hz ? `${hz >= 1000 ? hz / 1000 + "k" : hz}<small>Hz · ${+(1000 / hz).toFixed(3)} ms</small>` : "–";
  const at = pollRates.indexOf(hz);
  $("hPollBars").innerHTML = pollRates.map((_, i) =>
    `<i class="${i <= at ? "on" : ""}${i === at ? " cur" : ""}" style="height:${30 + i * (70 / Math.max(1, pollRates.length - 1))}%"></i>`).join("");
}

function renderBattery() {
  const s = cur();
  const pct = activeS().battery, ch = activeS().charging;
  if (!s?.caps?.wireless && pct == null) {
    $("hBatt").innerHTML = s?.tier === 0 ? "N/A" : "Wired";
    $("hBattS").textContent = s?.tier === 0 ? "Pointer only" : "";
    $("hBattCells").innerHTML = "";
    $("chargeVal").textContent = s?.tier === 0 ? "No vendor telemetry (Tier 0)" : "USB powered (no battery)";
    return;
  }
  if (pct != null) {
    $("hBatt").innerHTML = `${Math.round(pct)}<small>%</small>`;
    const lit = Math.max(1, Math.ceil(pct / 10));
    $("hBattCells").innerHTML = Array.from({ length: 10 }, (_, i) => `<i class="${i < lit ? "on" + (pct < 20 ? " low" : "") : ""}"></i>`).join("");
  } else {
    $("hBatt").textContent = "–";
    $("hBattCells").innerHTML = "";
  }
  $("hBattS").textContent = ch ? "⚡ Charging" : "";
  if (s?.sharedBattery?.stale) {
    $("hBattS").textContent = pct == null ? "No reading" : "Last known";
    $("chargeVal").textContent = "Awaiting a fresh reading";
    return;
  }
  $("chargeVal").textContent = ch == null ? (s?.caps?.batteryRead ? "Reading…" : "N/A") : (ch ? "Charging" : "On battery");
}

function paintRanges() {
  for (const el of document.querySelectorAll('input[type="range"]'))
    el.style.setProperty("--p", ((el.value - el.min) / Math.max(1, el.max - el.min) * 100) + "%");
}
document.addEventListener("input", (e) => { if (e.target.type === "range") requestAnimationFrame(paintRanges); });
function renderIdle() {
  const idle = activeS().idle;
  if (idle == null || held.has("idle")) return;
  const m = Math.max(1, Math.round(idle / 60));
  $("idle").value = m; $("idleVal").textContent = `${m} min`; paintRanges();
}
function renderLow() {
  const low = activeS().low;
  if (low == null || held.has("low")) return;
  const p = Math.round(low / 255 * 100);
  $("low").value = p; $("lowVal").textContent = `${p}%`; paintRanges();
}
function renderLed() {
  const led = activeS().led;
  for (const b of $("dongleLed").children) b.classList.toggle("sel", +b.dataset.m === led);
}

// ---------- lift-off ----------
function renderLod() {
  const s = cur();
  const lod = activeS().lod;
  if (!s || s.caps.lodMode === "none" || !lod) { $("lodRow").hidden = true; return; }
  $("lodRow").hidden = false;
  if (s.caps.lodMode === "egg") {
    $("lodLevel").hidden = true;
    $("lodMode").hidden = true;
    $("lodAsync").hidden = true;
    $("lodEgg").hidden = false;
    const opts = (s.info.lodGlass && activeS().egg?.glassMode) ? EGG_GLASS_LOD : s.info.lodOptions;
    $("lodDesc").textContent = s.info.lodGlass && activeS().egg?.glassMode
      ? "Sensor Glass Mode is enabled — Lift-off distance uses Glass Mode options."
      : "Sensor lift-off height stored on the mouse.";
    $("lodEgg").innerHTML = opts.map((lbl, idx) =>
      `<button data-egg-lod="${idx}" class="${lod.index === idx ? "sel" : ""}">${lbl}</button>`
    ).join("");
    return;
  }
  $("lodEgg").hidden = true;
  $("lodMode").hidden = !s.caps.lodAsync;
  $("lodDesc").textContent = s.caps.lodAsync
    ? "How high you can lift the mouse before it stops tracking. Asymmetric sets lift-off and landing separately (step 1–26, higher = taller distance, Lift-off > Landing)."
    : "How high you can lift the mouse before tracking stops.";
  const mode = (s.caps.lodAsync && s.lodDraft) ? "async" : lod.mode;
  for (const b of $("lodMode").children) b.classList.toggle("sel", b.dataset.mode === mode);
  $("lodLevel").hidden = mode !== "preset";
  $("lodAsync").hidden = mode !== "async";
  for (const b of $("lodLevel").children) b.classList.toggle("sel", lod.mode === "preset" && +b.dataset.l === lod.level);
  if (mode === "async" && !held.has("lod")) {
    const v = s.lodDraft ?? lod;
    $("lodLift").value = v.lift; $("lodLand").value = v.land; lodLabels(); paintRanges();
  }
}
function lodLabels() { $("lodLiftV").textContent = `Step ${$("lodLift").value}`; $("lodLandV").textContent = `Step ${$("lodLand").value}`; paintRanges(); }

// ---------- universal sensor, click & performance tuning ----------
function renderTune() {
  const s = cur();
  if (s?.driver === "egg" && activeS().egg) {
    const cfg = activeS().egg;
    $("tuneGroup").hidden = false;
    $("tuneDebounceRow").hidden = true;
    $("tuneMotionSyncRow").hidden = false;
    $("tuneMotionSync").checked = !!cfg.motionSync;
    $("tuneAngleSnapRow").hidden = false;
    $("tuneAngleSnap").checked = !!cfg.angleSnapping;
    $("tuneRippleRow").hidden = false;
    $("tuneRipple").checked = !!cfg.rippleControl;
    $("tunePerfRow").hidden = true;
    $("tuneScan20kRow").hidden = true;
    $("tuneAntiMistouchRow").hidden = true;
    $("tuneSlamRow").hidden = false;
    $("tuneSlam").checked = !!cfg.slamclick;
    $("tuneJitterRow").hidden = !s.info.motionJitter;
    $("tuneJitter").checked = !!cfg.motionJitter;
    $("tuneGlassRow").hidden = !s.info.lodGlass;
    $("tuneGlass").checked = !!cfg.glassMode;
    $("tuneLedLiftRow").hidden = !s.info.ledLiftoff;
    $("tuneLedLift").checked = !!cfg.ledLiftoffDisabled;
    $("tuneMaxFpsRow").hidden = !s.info.forceMaxFps;
    $("tuneMaxFps").checked = !!cfg.forceMaxFps;
    $("tuneAngleTuneRow").hidden = !s.info.angleTuning;
    $("tuneAngleTune").min = -127; $("tuneAngleTune").max = 127;
    if (!held.has("tuneAngleTune")) {
      $("tuneAngleTune").value = cfg.angleTuning ?? 0;
      $("tuneAngleTuneVal").textContent = String(cfg.angleTuning ?? 0);
    }
    $("eggSpdtRow").hidden = false;
    $("eggSideMcRow").hidden = false;
    const populateCtl = (sel, val) => {
      if (!sel.options.length) {
        let opts = `<option value="241">GX Speed (SPDT)</option><option value="240">GX Safe (SPDT)</option>`;
        for (let ms = 0; ms <= 25; ms++) opts += `<option value="${ms}">Multiclick ${ms} ms</option>`;
        sel.innerHTML = opts;
      }
      sel.value = String(val >= 0xf0 ? val : Math.min(25, Math.max(0, val)));
    };
    populateCtl($("eggCtlL"), cfg.buttons[0]?.ctl ?? 8);
    populateCtl($("eggCtlR"), cfg.buttons[1]?.ctl ?? 8);
    if (!held.has("eggSideMc")) {
      const mc = Math.min(25, cfg.buttons[2]?.ctl ?? 8);
      $("eggSideMc").value = mc;
      $("eggSideMcVal").textContent = `${mc} ms`;
    }
    $("eggHanded").checked = !!cfg.leftHanded;
    paintRanges();
    return;
  }

  const tc = s?.caps?.tune;
  const anyTune = !!(tc && Object.keys(tc).some((k) => !["debMin", "debMax", "debStep", "angleMin", "angleMax"].includes(k) && tc[k]));
  $("tuneGroup").hidden = !anyTune;
  if (!anyTune) return;
  const t = activeS().tune || {};

  $("tuneDebounceRow").hidden = !tc.debounce;
  if (tc.debounce) {
    const mn = tc.debMin ?? 0, mx = tc.debMax ?? 30, st = tc.debStep ?? 1;
    $("tuneDebounce").min = mn; $("tuneDebounce").max = mx; $("tuneDebounce").step = st;
    if (!held.has("tuneDebounce")) {
      const val = Math.max(mn, Math.min(mx, t.debounceMs ?? mn));
      $("tuneDebounce").value = val;
      $("tuneDebounceVal").textContent = `${val} ms`;
    }
  }
  $("tuneMotionSyncRow").hidden = !tc.motionSync;
  $("tuneMotionSync").checked = !!t.motionSync;
  $("tuneAngleSnapRow").hidden = !tc.angleSnapping;
  $("tuneAngleSnap").checked = !!t.angleSnapping;
  $("tuneRippleRow").hidden = !tc.rippleControl;
  $("tuneRipple").checked = !!t.rippleControl;
  $("tunePerfRow").hidden = !tc.performance;
  $("tunePerf").checked = !!t.performance;
  $("tuneScan20kRow").hidden = !tc.scan20k;
  $("tuneScan20k").checked = !!t.scan20k;
  $("tuneAntiMistouchRow").hidden = !tc.antiMistouch;
  $("tuneAntiMistouch").checked = !!t.antiMistouch;
  $("tuneSlamRow").hidden = true;
  $("tuneJitterRow").hidden = true;
  $("tuneGlassRow").hidden = true;
  $("tuneLedLiftRow").hidden = true;
  $("tuneMaxFpsRow").hidden = true;
  $("eggSpdtRow").hidden = true;
  $("eggSideMcRow").hidden = true;
  $("tuneAngleTuneRow").hidden = !tc.angleTuning;
  if (tc.angleTuning) {
    $("tuneAngleTune").min = tc.angleMin ?? -30;
    $("tuneAngleTune").max = tc.angleMax ?? 30;
    if (!held.has("tuneAngleTune")) {
      $("tuneAngleTune").value = t.angleTuning ?? 0;
      $("tuneAngleTuneVal").textContent = String(t.angleTuning ?? 0);
    }
  }
  paintRanges();
}

// ---------- dpi stages ----------
const curStages = () => {
  const s = cur();
  return s?.stagesEdit ?? s?.S?.stages ?? { active: 1, list: [] };
};
const stagesDirty = () => {
  const s = cur();
  return !!(s?.stagesEdit && s?.S?.stages && !same(s.stagesEdit, s.S.stages));
};

function renderStagesFromDevice() {
  const s = cur();
  if (!s) return;
  if (s.stagesEdit && s.S.stages && same(s.stagesEdit, s.S.stages)) s.stagesEdit = null;
  renderPips();
  const typing = $("stages").contains(document.activeElement) && document.activeElement.tagName === "INPUT";
  if (!s.stagesEdit && !typing) {
    if (s.S.stages?.list.some((x) => x.x !== x.y)) $("splitXY").checked = true;
    renderStages();
  } else { markDirty(); renderTrack(); }
}

const logPos = (v) => {
  const mn = dpiMin(), mx = dpiMax();
  return (Math.log(Math.max(mn, Math.min(mx, v))) - Math.log(mn)) / Math.max(0.01, Math.log(mx) - Math.log(mn)) * 100;
};
function renderTrack() {
  const st = curStages(), mn = dpiMin(), max = dpiMax();
  const ticks = [mn, 400, 800, 1600, 3200, 6400, 12800, max].filter((t, i, a) => t >= mn && t <= max && a.indexOf(t) === i);
  let h = '<div class="rail"></div>';
  h += ticks.map((t, i) => `<span class="tl" style="left:${logPos(t)}%"></span><span class="tick${[400, 1600, 6400, 12800].includes(t) ? " minor" : ""}${i === 0 ? " first" : i === ticks.length - 1 ? " last" : ""}" style="left:${logPos(t)}%">${t >= 1000 ? t / 1000 + "k" : t}</span>`).join("");
  const dpi = activeS().dpi;
  if (dpi) h += `<span class="live" style="left:${logPos(dpi[0])}%" title="Current: ${dpi[0]}"></span>`;
  st.list.forEach((s, i) => { h += `<span class="mark${st.active === i + 1 ? " active" : ""}" style="left:${logPos(clampDpi(s.x) ?? mn)}%" title="Stage ${i + 1}: ${s.x}">${i + 1}</span>`; });
  $("track").innerHTML = h;
}

function renderStages() {
  const sess = cur();
  if (!sess || !sess.caps.stages) { $("stages").innerHTML = ""; markDirty(); return; }
  const st = curStages();
  const split = $("splitXY").checked;
  $("dpiNowY").hidden = !split;
  const mn = dpiMin(), max = dpiMax(), step = sess.caps.dpiStepLo ?? 50;
  const wrap = $("stages");
  wrap.innerHTML = "";
  st.list.forEach((s, i) => {
    const card = document.createElement("div");
    const active = st.active === i + 1;
    card.className = "stagecard" + (active ? " active" : "") + (split ? " split" : "");
    card.innerHTML = `
      <div class="sh">Stage ${String(i + 1).padStart(2, "0")}${active ? ' <span class="badge">Active</span>' : ""}
        <button class="ghost rm" aria-label="Remove stage ${i + 1}" ${st.list.length <= 1 ? "hidden" : ""}>×</button></div>
      <div class="xy">
        <label>${split ? "<span>X</span>" : ""}<input type="number" inputmode="numeric" data-k="x" min="${mn}" max="${max}" step="${step}" value="${s.x}" aria-label="Stage ${i + 1} ${split ? "X " : ""}DPI"></label>
        ${split ? `<label><span>Y</span><input type="number" inputmode="numeric" data-k="y" min="${mn}" max="${max}" step="${step}" value="${s.y}" aria-label="Stage ${i + 1} Y DPI"></label>` : ""}
      </div>`;
    card.addEventListener("click", (e) => {
      if (e.target.closest("input, button")) return;
      if (curStages().active === i + 1) return;
      edit().active = i + 1; renderStages();
    });
    card.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("input", () => {
        const v = parseInt(inp.value, 10);
        if (Number.isNaN(v)) return;
        const e = edit();
        if (inp.dataset.k === "x") { e.list[i].x = v; if (!split) e.list[i].y = v; } else e.list[i].y = v;
        markDirty(); renderTrack();
      });
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter" && stagesDirty()) $("applyStages").click(); });
      inp.addEventListener("blur", () => {
        const v = clampDpi(inp.value);
        if (v != null && String(v) !== inp.value) { inp.value = v; inp.dispatchEvent(new Event("input")); }
      });
    });
    card.querySelector(".rm").addEventListener("click", () => {
      const e = edit();
      e.list.splice(i, 1);
      if (e.active > e.list.length) e.active = e.list.length;
      else if (e.active > i + 1) e.active--;
      renderStages();
    });
    wrap.appendChild(card);
  });
  if (st.list.length < maxStages(sess)) {
    const add = document.createElement("button");
    add.className = "addcard"; add.textContent = "+ Add stage";
    add.addEventListener("click", () => {
      const e = edit();
      const last = e.list[e.list.length - 1] ?? { x: 400, y: 400 };
      e.list.push({ x: Math.min(max, last.x * 2), y: Math.min(max, last.y * 2) });
      renderStages();
      wrap.querySelectorAll(".stagecard")[e.list.length - 1]?.querySelector("input")?.select();
    });
    wrap.appendChild(add);
  }
  markDirty(); renderTrack();
}
function edit() {
  const s = cur();
  if (!s.stagesEdit) s.stagesEdit = structuredClone(s.S.stages ?? { active: 1, list: [] });
  return s.stagesEdit;
}
function markDirty() {
  const d = stagesDirty();
  $("stagesDirty").hidden = !d; $("applyStages").hidden = !d; $("revertStages").hidden = !d;
}
function clampDpi(v, s = cur()) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return null;
  if (s?.driver === "egg") return eggClampCpi(s.info, n);
  return Math.max(dpiMin(s), Math.min(dpiMax(s), n));
}

// ======================================================================
// buttons
// ======================================================================
const BTN_NAMES = { 1: "Left click", 2: "Right click", 3: "Middle click", 4: "Back", 5: "Forward",
  0x60: "DPI button", 0x61: "Profile button", 0x68: "Wheel left", 0x69: "Wheel right", 9: "Wheel up", 10: "Wheel down" };
const BTN_ORDER = [1, 2, 3, 9, 10, 0x68, 0x69, 5, 4, 0x60, 0x61];
const MOUSE_CODES = { 1: "Left click", 2: "Right click", 3: "Middle click", 4: "Back", 5: "Forward",
  9: "Wheel up", 10: "Wheel down", 0x68: "Wheel left", 0x69: "Wheel right" };
const DPI_FNS = { 1: "DPI up", 2: "DPI down", 5: "DPI clutch", 6: "DPI cycle up", 7: "DPI cycle down" };
const MEDIA = { 0xe9: "Volume up", 0xea: "Volume down", 0xe2: "Mute", 0xcd: "Play / pause", 0xb5: "Next track", 0xb6: "Previous track" };
const MODS = [[0x01, "Ctrl"], [0x02, "Shift"], [0x04, "Alt"], [0x08, "Super"], [0x10, "Right Ctrl"], [0x20, "Right Shift"], [0x40, "Right Alt"], [0x80, "Right Super"]];
const MOD_CODES = { ControlLeft: 0x01, ShiftLeft: 0x02, AltLeft: 0x04, MetaLeft: 0x08, ControlRight: 0x10, ShiftRight: 0x20, AltRight: 0x40, MetaRight: 0x80 };
const HID = (() => {
  const m = {};
  for (let i = 0; i < 26; i++) m["Key" + String.fromCharCode(65 + i)] = 0x04 + i;
  for (let i = 1; i <= 9; i++) m["Digit" + i] = 0x1d + i;
  m.Digit0 = 0x27;
  for (let i = 1; i <= 12; i++) m["F" + i] = 0x39 + i;
  for (let i = 13; i <= 24; i++) m["F" + i] = 0x68 + (i - 13);
  Object.assign(m, { Enter: 0x28, Escape: 0x29, Backspace: 0x2a, Tab: 0x2b, Space: 0x2c, Minus: 0x2d, Equal: 0x2e,
    BracketLeft: 0x2f, BracketRight: 0x30, Backslash: 0x31, Semicolon: 0x33, Quote: 0x34, Backquote: 0x35, Comma: 0x36,
    Period: 0x37, Slash: 0x38, CapsLock: 0x39, PrintScreen: 0x46, ScrollLock: 0x47, Pause: 0x48, Insert: 0x49, Home: 0x4a,
    PageUp: 0x4b, Delete: 0x4c, End: 0x4d, PageDown: 0x4e, ArrowRight: 0x4f, ArrowLeft: 0x50, ArrowDown: 0x51, ArrowUp: 0x52,
    NumLock: 0x53, NumpadDivide: 0x54, NumpadMultiply: 0x55, NumpadSubtract: 0x56, NumpadAdd: 0x57, NumpadEnter: 0x58,
    Numpad1: 0x59, Numpad2: 0x5a, Numpad3: 0x5b, Numpad4: 0x5c, Numpad5: 0x5d, Numpad6: 0x5e, Numpad7: 0x5f, Numpad8: 0x60,
    Numpad9: 0x61, Numpad0: 0x62, NumpadDecimal: 0x63, IntlBackslash: 0x64, ContextMenu: 0x65 });
  return m;
})();
const HID_NAME = Object.fromEntries(Object.entries(HID).map(([code, u]) => [u, code.replace(/^Key|^Digit/, "").replace(/([a-z])([A-Z0-9])/g, "$1 $2")]));

const defaultFor = (b, s = cur()) => {
  if (s?.driver === "egg") {
    if (s.S.egg?.leftHanded && b === 1) return { fn: 1, size: 1, data: [2] };
    if (s.S.egg?.leftHanded && b === 2) return { fn: 1, size: 1, data: [1] };
    return { fn: 1, size: 1, data: [b] };
  }
  return b === 0x60 ? { fn: 6, size: 1, data: [6] } : { fn: 1, size: 1, data: [b] };
};
const sameAction = (a, w) => a?.fn === w?.fn && Array.from({ length: w?.size ?? 0 }, (_, i) => (w.data[i] ?? 0) === a.data[i]).every(Boolean);
const isDefault = (b, a, s = cur()) => sameAction(a, defaultFor(b, s));

function describe(a) {
  if (!a) return "–";
  if (a.label) return a.label;
  const d = a.data || [];
  switch (a.fn) {
    case 0: return "Disabled";
    case 1: return MOUSE_CODES[d[0]] ?? `Mouse code ${d[0]}`;
    case 2: {
      const mods = MODS.filter(([bit]) => d[0] & bit).map(([, n]) => n);
      return (d[1] ? [...mods, HID_NAME[d[1]] ?? `Key 0x${d[1].toString(16)}`] : mods).join(" + ") || "Key";
    }
    case 6: return DPI_FNS[d[0]] ?? `DPI function ${d[0]}`;
    case 10: { const u = d[0] << 8 | d[1]; return MEDIA[u] ?? `Media 0x${u.toString(16)}`; }
    case 11: return "Double click";
    case 12: return "HyperShift";
    case 13: case 14: return "Turbo";
    default: return `Function ${a.fn} [${d.slice(0, a.size).join(",")}]`;
  }
}

const ACTIONS = [
  ["default", "Factory default"],
  ["off", "Disabled", { fn: 0, size: 0, data: [] }],
  ...[1, 2, 3, 4, 5, 9, 10, 0x68, 0x69].map((c) => [`m${c}`, MOUSE_CODES[c], { fn: 1, size: 1, data: [c] }]),
  ["dbl", "Double click", { fn: 11, size: 1, data: [1] }],
  ["hs", "HyperShift", { fn: 12, size: 0, data: [] }],
  ...[6, 7, 1, 2, 5].map((c) => [`d${c}`, DPI_FNS[c], { fn: 6, size: 1, data: [c] }]),
  ...Object.entries(MEDIA).map(([u, n]) => [`v${u}`, n, { fn: 10, size: 2, data: [+u >> 8, +u & 255] }]),
  ["key", "Keyboard key…"],
];
const ACTION_GROUPS_RAZER = [["", ["default", "off", "hs"]], ["Mouse", ["m1", "m2", "m3", "m4", "m5", "m9", "m10", "m104", "m105", "dbl"]],
  ["DPI", ["d6", "d7", "d1", "d2", "d5"]], ["Media", Object.keys(MEDIA).map((u) => "v" + u)], ["Keyboard", ["key"]]];
const ACTION_GROUPS_EGG = [["", ["default", "off"]], ["Mouse", ["m1", "m2", "m3", "m4", "m5", "m9", "m10"]],
  ["DPI", ["d6"]], ["Media", Object.keys(MEDIA).map((u) => "v" + u)], ["Keyboard", ["key"]]];

async function loadButtons(sess = cur()) {
  if (!sess) return;
  sess.lastButtons = Date.now();
  if (!sess.caps.buttons) {
    if (cur()?.id === sess.id) renderButtons();
    return;
  }
  if (sess.driver === "egg") {
    if (cur()?.id === sess.id) renderButtons();
    return;
  }
  const layer = sess.btnLayer;
  let ids;
  try { ids = await sess.api.buttonIds(); } catch (e) { log("! button ids: " + e.message, sess.shortName); }
  if (!ids) return;
  const next = {};
  for (const b of ids) {
    if (sess.pending.has(`btn:${b}`)) { next[b] = sess.S.buttons[b]; continue; }
    try { next[b] = await sess.api.getButton(b, layer); } catch (e) { log(`! button ${b}: ${e.message}`, sess.shortName); if (sess.S.buttons[b]) next[b] = sess.S.buttons[b]; }
  }
  if (layer !== sess.btnLayer || !sessions.has(sess.id)) return;
  sess.S.buttons = next;
  if (cur()?.id === sess.id) renderButtons();
}

function renderButtons() {
  if (capturing) return;
  const s = cur();
  const wrap = $("btns");
  const hasBtns = !!(s?.caps?.buttons && Object.keys(s.S.buttons || {}).length);
  $("btnLayer").hidden = !s?.caps?.layers;
  $("eggHandedWrap").hidden = !s?.caps?.eggHanded;
  $("btnResetAll").hidden = !hasBtns;
  $("btnUnavailable").hidden = hasBtns;
  $("btnFootnote").hidden = !hasBtns;
  $("btnGroupTitle").textContent = hasBtns ? "Button mapping" : "Live input tester";
  $("btnGroupNote").textContent = hasBtns ? "Stored on the mouse, works without this page" : "Test buttons, scroll, and keys on any connected mouse";
  if (!hasBtns) {
    wrap.innerHTML = "";
    for (const hs of document.querySelectorAll(".mousefig .hs")) {
      hs.classList.remove("changed", "locked");
      hs.classList.toggle("sel", btnSel === +hs.dataset.btn);
      hs.style.opacity = ".75";
    }
    return;
  }
  const groups = s.driver === "egg" ? ACTION_GROUPS_EGG : ACTION_GROUPS_RAZER;
  const isLefty = s.driver === "egg" && !!s.S.egg?.leftHanded;
  const lockedBtn = s.driver === "egg" ? (isLefty ? 2 : 1) : (s.btnLayer === 0 ? 1 : -1);
  const ids = Object.keys(s.S.buttons).map(Number).sort((a, b) => (BTN_ORDER.indexOf(a) + 99) % 99 - (BTN_ORDER.indexOf(b) + 99) % 99);
  wrap.innerHTML = "";
  for (const b of ids) {
    const a = s.S.buttons[b]; if (!a) continue;
    const def = isDefault(b, a, s), locked = b === lockedBtn;
    const label = BTN_NAMES[b] ?? `Button ${b}`;
    const row = document.createElement("div");
    row.className = "brow" + (btnSel === b ? " sel" : "");
    row.dataset.btn = b;
    const opts = groups.map(([g, vals]) => {
      const o = vals.map((v) => ACTIONS.find((x) => x[0] === v)).filter(Boolean).map(([v, n]) => `<option value="${v}">${n}</option>`).join("");
      return g ? `<optgroup label="${g}">${o}</optgroup>` : o;
    }).join("");
    row.innerHTML = `
      <span class="ix">${String(ids.indexOf(b) + 1).padStart(2, "0")}</span>
      <span class="name">${label}</span>
      <span class="cur"><span>${describe(a)}</span>${def ? "" : '<span class="tag">Changed</span>'}<span class="state" data-for="btn:${b}"></span></span>
      <div class="pick">${locked ? '<span class="muted" style="font-size:12px">Locked (primary)</span>'
        : `<select aria-label="New action for ${label}"><option value="">Change to…</option>${opts}</select>`}</div>`;
    row.addEventListener("mouseenter", () => selectBtn(b, false));
    if (!locked) {
      const sel = row.querySelector("select");
      sel.addEventListener("focus", () => selectBtn(b, false));
      sel.addEventListener("change", () => {
        const v = sel.value; if (!v) return;
        if (v === "key") { captureKey(row, b); return; }
        applyButton(b, v === "default" ? defaultFor(b, s) : ACTIONS.find((x) => x[0] === v)[2]);
      });
    }
    wrap.appendChild(row);
  }
  for (const hs of document.querySelectorAll(".mousefig .hs")) {
    const b = +hs.dataset.btn, a = s.S.buttons[b];
    hs.classList.toggle("changed", !!a && !isDefault(b, a, s));
    hs.classList.toggle("sel", btnSel === b);
    hs.classList.toggle("locked", b === lockedBtn);
    hs.style.opacity = a ? "" : ".35";
    let t = hs.querySelector("title"); if (!t) { t = document.createElementNS("http://www.w3.org/2000/svg", "title"); hs.appendChild(t); }
    t.textContent = `${BTN_NAMES[b] ?? "Button " + b}: ${a ? describe(a) : "–"}`;
  }
}

function selectBtn(b, focus) {
  btnSel = b;
  for (const r of $("btns").children) r.classList.toggle("sel", +r.dataset.btn === b);
  for (const hs of document.querySelectorAll(".mousefig .hs")) hs.classList.toggle("sel", +hs.dataset.btn === b);
  if (focus) $("btns").querySelector(`[data-btn="${b}"] select`)?.focus();
}
for (const hs of document.querySelectorAll(".mousefig .hs")) {
  hs.addEventListener("click", () => selectBtn(+hs.dataset.btn, true));
  hs.addEventListener("mouseenter", () => selectBtn(+hs.dataset.btn, false));
}

$("btnLayer").addEventListener("click", async (e) => {
  const s = cur(); if (!s || !s.caps.layers) return;
  const b = e.target.closest("button"); if (!b) return;
  const layer = +b.dataset.layer;
  if (layer === s.btnLayer) return;
  s.btnLayer = layer;
  for (const x of $("btnLayer").children) x.classList.toggle("sel", +x.dataset.layer === layer);
  $("btnFootnote").textContent = layer === 0
    ? "Primary click is locked so you can't lose clicking. Every change is read back, so the list always shows what the mouse actually has."
    : "HyperShift layer is active while holding a button mapped to HyperShift. Left click can be remapped here.";
  await loadButtons(s);
});

function captureKey(row, b) {
  const pick = row.querySelector(".pick");
  pick.innerHTML = '<button class="keycap">Press a key or combo… (Esc cancels)</button>';
  const cap = pick.firstElementChild;
  const heldMods = new Set();
  capturing = b; cap.focus();
  const modMask = () => {
    let m = 0;
    for (const c of heldMods) m |= MOD_CODES[c] ?? 0;
    return m;
  };
  const done = () => {
    capturing = null;
    window.removeEventListener("keydown", onKey, true); window.removeEventListener("keyup", onUp, true);
  };
  const onKey = (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.code === "Escape" && !e.ctrlKey && !e.shiftKey && !e.altKey && !e.metaKey) { done(); renderButtons(); return; }
    if (MOD_CODES[e.code] !== undefined) {
      heldMods.add(e.code);
      cap.textContent = [...heldMods].map((c) => c.replace(/Left|Right/, "").replace("Meta", "Super").replace("Control", "Ctrl")).join(" + ") + " + …";
      return;
    }
    const usage = HID[e.code];
    if (usage === undefined) { cap.textContent = `"${e.code}" isn't supported, try another key`; return; }
    const mods = modMask() || ((e.ctrlKey ? 1 : 0) | (e.shiftKey ? 2 : 0) | (e.altKey ? 4 : 0) | (e.metaKey ? 8 : 0));
    done(); applyButton(b, { fn: 2, size: 2, data: [mods, usage] });
  };
  const onUp = (e) => {
    if (MOD_CODES[e.code] === undefined) return;
    e.preventDefault();
    const mask = modMask() || MOD_CODES[e.code];
    heldMods.delete(e.code);
    done(); applyButton(b, { fn: 2, size: 2, data: [mask, 0] });
  };
  window.addEventListener("keydown", onKey, true);
  window.addEventListener("keyup", onUp, true);
  cap.addEventListener("blur", () => { if (capturing === b) { done(); renderButtons(); } });
}

function applyButton(b, act, s = cur()) {
  if (!s) return Promise.resolve(false);
  const layer = s.btnLayer;
  return commit(`btn:${b}`, {
    sess: s,
    label: BTN_NAMES[b] ?? `Button ${b}`, want: act,
    write: () => s.api.setButton(b, act, layer),
    read: async () => { const a = await s.api.getButton(b, layer); if (layer === s.btnLayer) s.S.buttons[b] = a; return a; },
    eq: (got) => sameAction(got, act),
  }).then((ok) => {
    if (ok && cur()?.id === s.id) toast(`${BTN_NAMES[b] ?? b} → ${describe(s.S.buttons[b])}`);
    if (cur()?.id === s.id) renderButtons();
    return ok;
  });
}

$("btnResetAll").addEventListener("click", async () => {
  const s = cur(); if (!s) return;
  const changed = Object.keys(s.S.buttons).map(Number).filter((b) => s.S.buttons[b] && !isDefault(b, s.S.buttons[b], s));
  if (!changed.length) { toast("All buttons on this layer are already at their defaults"); return; }
  if (!confirm(`Reset ${changed.length} button${changed.length === 1 ? "" : "s"} to default?`)) return;
  for (const b of changed) await applyButton(b, defaultFor(b, s), s);
});

// tester: shows what the computer receives and lights up the matching spot on the drawing
(() => {
  const t = $("tester"), out = $("testOut");
  const names = ["Left", "Middle", "Right", "Back", "Forward"], ids = [1, 3, 2, 4, 5];
  const flash = (b) => {
    const hs = document.querySelector(`.mousefig .hs[data-btn="${b}"]`); if (!hs) return;
    hs.classList.add("flash"); clearTimeout(hs._f); hs._f = setTimeout(() => hs.classList.remove("flash"), 180);
  };
  t.addEventListener("contextmenu", (e) => e.preventDefault());
  t.addEventListener("mousedown", (e) => { e.preventDefault(); t.focus(); out.textContent = (names[e.button] ?? "Button " + e.button) + " click"; flash(ids[e.button]); });
  t.addEventListener("auxclick", (e) => e.preventDefault());
  t.addEventListener("wheel", (e) => {
    e.preventDefault();
    const horiz = Math.abs(e.deltaX) > Math.abs(e.deltaY);
    out.textContent = horiz ? (e.deltaX > 0 ? "Wheel right" : "Wheel left") : (e.deltaY > 0 ? "Wheel down" : "Wheel up");
    flash(horiz ? 3 : (e.deltaY > 0 ? 10 : 9));
  }, { passive: false });
  t.addEventListener("keydown", (e) => {
    const k = [e.ctrlKey && "Ctrl", e.shiftKey && "Shift", e.altKey && "Alt", e.metaKey && "Super"].filter(Boolean);
    if (!MOD_CODES[e.code]) k.push(e.code.replace(/^Key|^Digit/, ""));
    out.textContent = "Key " + k.join(" + ");
    if (e.code !== "Tab") e.preventDefault();
  });
  window.addEventListener("mouseup", (e) => { if (e.button === 3 || e.button === 4) e.preventDefault(); });
})();

// live polling rate meter (universal across any mouse)
(() => {
  const box = $("hzBox"), val = $("hzVal"), stats = $("hzStats");
  let lastTs = null, emaHz = 0, peakHz = 0, samples = 0, active = false, decayT = null;
  function record(ts) {
    if (lastTs != null) {
      const dt = ts - lastTs;
      if (dt > 0.08 && dt < 120) {
        const inst = 1000 / dt;
        emaHz = emaHz ? emaHz * 0.85 + inst * 0.15 : inst;
        if (samples > 10 && emaHz > peakHz) peakHz = emaHz;
        samples++;
        val.innerHTML = `${Math.round(emaHz)}<small>Hz</small>`;
        stats.textContent = `Interval: ${dt.toFixed(2)} ms · Peak: ${Math.round(peakHz)} Hz · ${samples} events`;
      }
    }
    lastTs = ts;
    clearTimeout(decayT);
    decayT = setTimeout(() => { lastTs = null; box.classList.remove("on"); }, 350);
  }
  const onMove = (e) => {
    if (!active) return;
    box.classList.add("on");
    const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    if (evs.length) {
      for (const ce of evs) record(ce.timeStamp);
    } else record(e.timeStamp);
  };
  box.addEventListener("pointerenter", () => { active = true; lastTs = null; });
  box.addEventListener("pointerleave", () => { active = false; lastTs = null; box.classList.remove("on"); });
  if ("onpointerrawupdate" in window) box.addEventListener("pointerrawupdate", onMove);
  box.addEventListener("pointermove", onMove);
  $("hzReset").addEventListener("click", (e) => {
    e.stopPropagation();
    lastTs = null; emaHz = 0; peakHz = 0; samples = 0;
    val.innerHTML = `–<small>Hz</small>`;
    stats.textContent = "Move pointer inside this pad · Peak: – Hz";
  });
})();

// ======================================================================
// background sync per session
// ======================================================================
let timer = null;

async function sharedBattery(sess) {
  try {
    const { res, data: b } = await fetchHelperBattery({ signal: AbortSignal.timeout(1500) });
    if (!res.ok) throw new Error("Helper unavailable");
    const modelMatches = b.vid != null ? b.vid === sess.vid && b.pid === sess.pid
      : b.device === sess.name && b.link === sess.link;
    const serial = sess.dev.serialNumber;
    const unique = b.model_unique !== false && ![...sessions.values()].some(
      (other) => other !== sess && other.vid === sess.vid && other.pid === sess.pid);
    const serialsKnown = Boolean(b.serial && serial);
    const matched = modelMatches && (serialsKnown ? b.serial === serial : unique);
    const ambiguous = modelMatches && !serialsKnown && !unique;
    const pending = b.reason === "waiting for the first reading";
    sess.sharedBattery = null;
    if (b.watching !== false) {
      if (matched) sess.sharedBattery = b;
      else if (ambiguous || pending) sess.sharedBattery = { percent: null, charging: null, stale: true };
    }
  } catch {
    if (sess.sharedBattery) sess.sharedBattery = { ...sess.sharedBattery, stale: true };
  }
  return sess.sharedBattery;
}

async function syncKeys(sess, keys) {
  if (!sess || !sessions.has(sess.id)) return;
  if (sess.syncP) { await sess.syncP.catch(() => {}); return; }
  sess.syncP = (async () => {
    let ok = 0, timeouts = 0;
    const sample = sess.caps.batteryRead && keys.includes("battery") ? await sharedBattery(sess) : null;
    const readers = {
      dpi: () => sess.api.getDpi?.(),
      stages: () => sess.api.getStages?.(),
      poll: () => sess.api.getPoll?.(),
      lod: () => sess.api.getLod?.(),
      tune: () => sess.api.getTune?.(),
      idle: () => sess.caps.powerTune && sess.caps.wireless ? sess.api.getIdle?.() : undefined,
      low: () => sess.caps.powerTune && sess.caps.wireless ? sess.api.getLow?.() : undefined,
      led: () => sess.caps.hyper ? sess.api.getDongleLed?.() : undefined,
      battery: () => sample ? sample.percent : sess.caps.batteryRead ? sess.api.battery?.() : undefined,
      charging: () => sample ? (sample.stale ? null : sample.charging) : sess.caps.batteryRead ? sess.api.charging?.() : undefined,
      eggAll: () => sess.api.readKey?.(),
      compxAll: () => sess.api.readKey?.(),
      pproAll: () => sess.api.readKey?.(),
      sinoAll: () => sess.api.readKey?.(),
    };
    const multiKeys = new Set(["eggAll", "compxAll", "pproAll", "sinoAll"]);
    for (const k of keys) {
      if (!sessions.has(sess.id)) return;
      while (sess.userBusy) await sleep(40);
      if (sess.pending.has(k)) continue;
      if (!readers[k]) continue;
      try {
        const v = await readers[k]();
        if (v !== undefined && !sess.pending.has(k)) {
          if (!multiKeys.has(k)) sess.S[k] = v;
          if (cur()?.id === sess.id) render(k);
        }
        ok++;
      } catch (e) {
        if (e.kind === "timeout") timeouts++;
        log(`! ${k}: ${e.message}`, sess.shortName);
      }
    }
    if (ok) markAwake(sess); else if (timeouts) setAsleep(true, sess);
    renderFleet();
  })();
  try { await sess.syncP; } finally { sess.syncP = null; }
}
function syncSlowToo(sess = cur()) {
  if (!sess) return Promise.resolve();
  sess.lastSlow = Date.now();
  return syncKeys(sess, [...sess.api.fastKeys, ...sess.api.slowKeys]);
}

function markAwake(sess = cur()) {
  if (!sess) return;
  sess.lastOk = Date.now();
  if (sess.asleep) setAsleep(false, sess);
  if (cur()?.id === sess.id) renderSyncLabel();
}
function setAsleep(v, sess = cur()) {
  if (!sess) return;
  sess.asleep = v;
  if (cur()?.id === sess.id) {
    $("conn").className = "pill " + (v ? "sleep" : "on");
    $("connText").textContent = v ? "Mouse asleep — move it" : `${sess.shortName} · ${sess.link}`;
  }
}
function renderSyncLabel() {
  const s = cur();
  if (!s) { $("sync").textContent = ""; return; }
  if (s.driver === "generic") { $("sync").textContent = "Generic HID"; return; }
  const sec = Math.round((Date.now() - s.lastOk) / 1000);
  $("sync").textContent = s.lastOk ? (sec < 3 ? "In sync" : `Synced ${sec}s ago`) : "";
}

function startTimer() {
  clearInterval(timer);
  let n = 0;
  timer = setInterval(() => {
    renderSyncLabel();
    const s = cur();
    if (!s || document.hidden || s.syncP || s.userBusy || ++n % 2) return;
    if (Date.now() - s.lastSlow > 10000) syncSlowToo(s); else syncKeys(s, s.api.fastKeys);
  }, 1000);
}
function onReturn() {
  const s = cur();
  if (!s || document.hidden) return;
  if (Date.now() - s.lastSlow > 1500) syncSlowToo(s);
  if (s.caps.buttons && s.driver !== "egg" && Date.now() - s.lastButtons > 20000) loadButtons(s);
}
document.addEventListener("visibilitychange", onReturn);
window.addEventListener("focus", onReturn);

async function readAll(sess = cur()) {
  if (!sess) return;
  if (sess.api.init) await sess.api.init();
  applySessionChrome();
  await syncSlowToo(sess);
  const fw = await sess.api.firmware?.().catch(() => null);
  sess.fw = fw ?? "–";
  sess.serial = (await sess.api.serial?.().catch(() => "")) || "–";
  if (cur()?.id === sess.id) {
    $("fw").textContent = sess.fw;
    $("hLinkS").textContent = fw && fw !== "–" ? `${sess.name} · ${fw}` : sess.name;
    $("serial").textContent = sess.serial;
  }
  await loadButtons(sess);
  renderFleet();
}

// ======================================================================
// multi-mouse fleet bar + capabilities UI chrome
// ======================================================================
function applySessionChrome() {
  const s = cur();
  if (!s) return;
  const tier = s.tier ?? 0;
  const tLabel = s.tierLabel || tierLabel(tier);
  $("conn").className = "pill " + (s.asleep ? "sleep" : "on");
  $("connText").textContent = s.asleep ? "Mouse asleep — move it" : `${s.shortName} · ${s.link}`;
  $("tierBadge").hidden = false;
  $("tierBadge").className = `tierbadge tier-${tier}`;
  $("tierBadgeText").textContent = tLabel;

  $("model").textContent = s.name;
  $("protoName").textContent = s.protoLabel;
  const evNote = tier === 0
    ? "Zero vendor HID commands sent"
    : tier === 1
    ? "Reviewed read-only getters only (writes blocked at dispatch)"
    : s.evidenceStatus === "hardware_verified"
    ? "Hardware-verified operations"
    : "Legacy documented profile (migrated from baseline)";
  $("supportTier").textContent = `${tLabel} — ${evNote}`;
  $("link").textContent = s.link;
  $("usbid").textContent = `${hex4(s.vid)}:${hex4(s.pid)}`;
  $("fw").textContent = s.fw ?? "–";
  $("serial").textContent = s.serial ?? "–";
  $("hidCols").textContent = s.collectionsText;
  $("factoryResetBtn").hidden = !s.caps.factoryReset || !s.writeEnabled;

  $("statusNote").hidden = !s.caps.statusNote;
  $("statusNote").className = "accelnotice" + (tier === 0 ? " tier-0" : tier === 1 ? " tier-1" : "");
  $("statusNote").textContent = s.caps.statusNote || "";

  $("hLink").textContent = s.link;
  $("hLinkS").textContent = s.fw && s.fw !== "–" ? `${s.name} · ${s.fw}` : s.name;

  const isReadonlyDpi = tier === 1 && !!s.caps.dpiRead && !s.caps.dpiWrite;
  $("stagesGroup").hidden = !s.caps.stages;
  $("readonlyDpiGroup").hidden = !isReadonlyDpi;
  $("genericDpiGroup").hidden = !s.caps.genericDpi;
  $("dpiNowRow").hidden = !(s.caps.dpi && s.writeEnabled);
  $("splitXYWrap").hidden = !s.caps.splitXY;
  if (!s.caps.splitXY) $("splitXY").checked = false;
  $("stagesNote").textContent = s.driver === "egg"
    ? "Cycle stages (up to 4) with the underside CPI button — live changes appear instantly"
    : "The DPI button on the mouse cycles through these";
  $("stagesHint").textContent = `Click a stage to make it active · up to ${s.caps.maxStages} · ${s.caps.dpiMin}–${s.caps.dpiMax.toLocaleString()}`;
  $("dpiNow").min = s.caps.dpiMin; $("dpiNow").max = s.caps.dpiMax; $("dpiNow").step = s.caps.dpiStepLo;
  $("dpiNowY").min = s.caps.dpiMin; $("dpiNowY").max = s.caps.dpiMax; $("dpiNowY").step = s.caps.dpiStepLo;
  if (isReadonlyDpi) {
    $("roDpiNote").textContent = `Live X/Y sensor readout (${s.caps.dpiMin}–${s.caps.dpiMax.toLocaleString()} DPI) via 0xff getter — writes blocked at dispatch`;
  }

  $("idleRow").hidden = !s.caps.powerTune;
  $("lowRow").hidden = !(s.caps.powerTune && s.caps.lowBatt !== false);
  $("idle").disabled = !s.caps.wireless || !s.writeEnabled;
  $("low").disabled = !s.caps.wireless || !s.writeEnabled;
  $("idle").min = 1;
  $("idle").max = s.caps.idleMaxMin ?? 15;
  $("low").min = s.caps.lowMin ?? 5;
  $("low").max = s.caps.lowMax ?? 100;
  $("low").step = s.caps.lowStep ?? 5;
  $("powerNote").textContent = s.caps.powerTune
    ? (s.info.prio === 0 ? "Wireless settings (saved on mouse even over cable)" : "Wireless")
    : s.caps.batteryRead ? "Battery telemetry (read-only)"
    : tier === 0 ? "Pointer only — no vendor battery telemetry"
    : "Wired / no battery reported";

  $("dongleTab").hidden = !s.caps.dongle;
  if (!s.caps.dongle && activeTab === "dongle") showTab("perf");

  $("appFoot").textContent = tier === 0
    ? "Tier 0 · Pointer only. Zero vendor HID reports are sent to this device."
    : tier === 1
    ? "Tier 1 · Read-only. Only reviewed firmware and X/Y DPI getters are active; all writes are blocked at dispatch."
    : "Changes are written to the mouse straight away and read back to confirm. Settings re-sync every 10 s and when you come back to the window.";

  buildPollButtons();
  renderDpi();
  renderPips();
  renderPoll();
  renderBattery();
  renderIdle();
  renderLow();
  renderLed();
  renderLod();
  renderTune();
  renderStagesFromDevice();
  renderButtons();
  renderSyncLabel();
}

function renderFleet() {
  const bar = $("fleet");
  const openCounts = new Map();
  const cards = [];
  for (const s of sessions.values()) {
    const vk = vpKey(s.vid, s.pid);
    openCounts.set(vk, (openCounts.get(vk) || 0) + 1);
    const dpiStr = s.S.dpi ? `${s.S.dpi[0]} DPI` : "—";
    const pollStr = s.S.poll ? `${fmtHz(s.S.poll)}Hz` : "";
    const battStr = s.S.battery != null ? `${Math.round(s.S.battery)}%` : "";
    const meta = [dpiStr, pollStr, battStr].filter(Boolean).join(" · ") || s.link;
    const isAct = s.id === activeId;
    const tShort = s.tier === 1 ? "T1 · RO" : s.tier === 2 ? "T2" : "T0";
    cards.push(`
      <div class="mcard${isAct ? " active" : ""}" data-sid="${s.id}" tabindex="0" role="button" aria-pressed="${isAct}">
        <button class="mcard-close" data-close="${s.id}" title="Disconnect ${s.name}" aria-label="Disconnect ${s.name}">×</button>
        <div class="mcard-top"><i class="mcard-dot"></i><span class="mcard-name">${s.name}</span></div>
        <div class="mcard-sub"><span class="mcard-tag">${s.protoTag}</span><span class="mcard-tier tier-${s.tier ?? 0}">${tShort}</span><span>${meta}</span></div>
      </div>`);
  }
  for (const m of systemMice) {
    const vNorm = String(m.vid || "").replace(/^0x/i, "").toLowerCase().padStart(4, "0");
    const pNorm = String(m.pid || "").replace(/^0x/i, "").toLowerCase().padStart(4, "0");
    const key = `${vNorm}:${pNorm}`;
    const c = openCounts.get(key) || 0;
    if (c > 0) { openCounts.set(key, c - 1); continue; }
    cards.push(`
      <div class="mcard sys" data-sys="${key}" data-driver="${m.driver}" tabindex="0" role="button" title="Detected via Linux sysfs — click to grant browser WebHID access">
        <div class="mcard-top"><i class="mcard-dot"></i><span class="mcard-name">${m.name}</span></div>
        <div class="mcard-sub"><span class="mcard-tag">${m.driver}</span><span>Detected · Click to connect</span></div>
      </div>`);
  }
  bar.hidden = !cards.length;
  bar.innerHTML = cards.join("");
}

$("fleet").addEventListener("click", async (e) => {
  const closeBtn = e.target.closest("[data-close]");
  if (closeBtn) {
    e.stopPropagation();
    await detachSession(closeBtn.dataset.close);
    return;
  }
  const card = e.target.closest(".mcard");
  if (!card) return;
  if (card.dataset.sid) {
    selectSession(card.dataset.sid);
  } else if (card.dataset.sys) {
    const drv = card.dataset.driver;
    const [v, p] = card.dataset.sys.split(":").map((x) => parseInt(x, 16));
    if (drv === "generic") await connectGenericDevice();
    else await connectSupportedDevices([{ vendorId: v, productId: p }]);
  }
});

function selectSession(id) {
  if (!sessions.has(id) || activeId === id) return;
  activeId = id;
  btnSel = null; capturing = null;
  showConnected(true);
  applySessionChrome();
  renderFleet();
  const s = cur();
  if (s && Date.now() - s.lastOk > 4000) syncSlowToo(s);
}

// ======================================================================
// multi-device identification, grouping & connection
// ======================================================================
function groupSessionTargets(devices, allowGeneric = false) {
  const claimed = new Set();
  const existing = [];
  for (const s of sessions.values()) {
    claimed.add(s.dev);
    for (const sib of s.siblings) claimed.add(sib);
    existing.push({ vid: s.vid, pid: s.pid, serial: s.dev.serialNumber });
  }
  return groupHidTargets(devices, allowGeneric, claimed, existing);
}

const driverCtx = {
  isConnected: (sess) => sessions.has(sess.id) && sess.dev.opened,
  isCurrent: (sess) => cur()?.id === sess.id,
  log,
  render,
  renderFleet,
};

async function attachTarget({ dev: d, siblings = [], spec }) {
  try { if (!d.opened) await d.open(); }
  catch (e) {
    log("! open failed: " + e.message, d.productName || hex4(d.productId));
    $("permhint").hidden = false;
    $("connText").textContent = "Couldn't open a device";
    return null;
  }
  for (const sib of siblings) {
    try { if (!sib.opened) await sib.open(); } catch {}
  }
  $("permhint").hidden = true;
  const id = `${spec.driver}:${hex4(d.vendorId)}:${hex4(d.productId)}:${d.serialNumber || Math.random().toString(36).slice(2, 7)}`;
  const shortName = spec.info.name.replace(/^Razer |^Endgame Gear |^Logitech |^Pulsar |^Glorious |^ATK |^VXE |^Scyrox |^Teevolution |^VGN |^Lamzu /i, "");
  const tier = spec.tier ?? 0;
  const sess = {
    id, dev: d, siblings, vid: d.vendorId, pid: d.productId,
    driver: spec.driver, protoLabel: spec.protoLabel, protoTag: spec.protoTag,
    tier, tierLabel: spec.tierLabel || tierLabel(tier),
    profileId: spec.profileId || "generic_tier0",
    writeEnabled: !!(spec.writeEnabled && tier >= 2),
    evidenceStatus: spec.evidenceStatus || "none",
    operations: spec.operations || {},
    name: spec.info.name, shortName, link: spec.info.link,
    info: spec.info, caps: spec.caps,
    collectionsText: describeHidCollections(d),
    S: { buttons: {} }, pending: new Set(),
    stagesEdit: null, lodDraft: null, btnLayer: 0,
    queue: Promise.resolve(), lastSlow: 0, lastOk: 0, lastButtons: 0, asleep: false, userBusy: 0,
    fw: "–", serial: d.serialNumber || "–",
  };
  if (spec.driver === "razer") sess.api = makeRazerApi(sess, driverCtx);
  else if (spec.driver === "egg") sess.api = makeEggApi(sess, driverCtx);
  else if (spec.driver === "hidpp") sess.api = makeHidppApi(sess, driverCtx);
  else if (spec.driver === "compx" || spec.driver === "egg_we") sess.api = makeCompxApi(sess, driverCtx);
  else if (spec.driver === "pulsar_xs1") sess.api = makePulsarXs1Api(sess, driverCtx);
  else if (spec.driver === "pulsar_pro") sess.api = makePulsarProApi(sess, driverCtx);
  else if (spec.driver === "glorious_sinowealth") sess.api = makeGloriousSinowealthApi(sess, driverCtx);
  else if (spec.driver === "glorious_core2") sess.api = makeGloriousCore2Api(sess, driverCtx);
  else if (spec.driver === "glorious_classic") sess.api = makeGloriousClassicApi(sess);
  else if (spec.driver === "glorious_pixart") sess.api = makeGloriousPixartApi(sess);
  else sess.api = makeGenericApi(sess);

  sess._snoop = (e) => {
    if (!snoopEnabled) return;
    const u = new Uint8Array(e.data.buffer, e.data.byteOffset, e.data.byteLength);
    log(`⟪ HID IN 0x${e.reportId.toString(16).padStart(2, "0")}: ${hex(u.subarray(0, 24))}`, sess.shortName);
  };
  d.addEventListener("inputreport", sess._snoop);
  for (const sib of siblings) sib.addEventListener("inputreport", sess._snoop);

  sessions.set(id, sess);
  if (!activeId) activeId = id;
  showConnected(true);
  applySessionChrome();
  renderFleet();
  startTimer();
  await readAll(sess);
  if (cur()?.id === id) applySessionChrome();
  return sess;
}

async function detachSession(id, msg) {
  const s = sessions.get(id);
  if (!s) return;
  sessions.delete(id);
  if (s._onEggReport) {
    s.dev.removeEventListener("inputreport", s._onEggReport);
    for (const sib of s.siblings) sib.removeEventListener("inputreport", s._onEggReport);
  }
  if (s._onCompxReport) {
    s.dev.removeEventListener("inputreport", s._onCompxReport);
    for (const sib of s.siblings) sib.removeEventListener("inputreport", s._onCompxReport);
  }
  if (s._onPulsarProReport) s.dev.removeEventListener("inputreport", s._onPulsarProReport);
  if (s._onHidppReport) s.dev.removeEventListener("inputreport", s._onHidppReport);
  if (s._snoop) {
    s.dev.removeEventListener("inputreport", s._snoop);
    for (const sib of s.siblings) sib.removeEventListener("inputreport", s._snoop);
  }
  if (s.dev?.opened) await s.dev.close().catch(() => {});
  for (const sib of s.siblings) { if (sib?.opened) await sib.close().catch(() => {}); }
  if (activeId === id) activeId = sessions.keys().next().value ?? null;
  if (sessions.size === 0) {
    clearInterval(timer);
    showConnected(false);
    $("connText").textContent = "Not connected";
    if (msg) $("emptyTitle").textContent = msg;
  } else {
    showConnected(true);
    applySessionChrome();
  }
  renderFleet();
}

const MOUSE_TABS = ["perf", "buttons", "dongle", "device"];
function setTabAccess(mouseOn) {
  for (const t of document.querySelectorAll(".tab")) {
    if (MOUSE_TABS.includes(t.dataset.tab)) {
      t.disabled = !mouseOn;
      t.title = mouseOn ? "" : "Connect a mouse first";
    }
  }
  $("powerNote").hidden = !mouseOn;
  $("chargeVal").closest(".setting").hidden = !mouseOn;
  if (!mouseOn) {
    $("idleRow").hidden = true;
    $("lowRow").hidden = true;
  }
  $("hero").hidden = !mouseOn;
  $("appFoot").hidden = !mouseOn;
}

function showConnected(on, helperOnly = false, tab = "accel") {
  const showApp = on || helperOnly;
  $("app").hidden = !showApp;
  $("empty").hidden = showApp;
  $("refresh").hidden = !on;
  setTabAccess(on);
  if (!on) { $("conn").className = "pill"; $("tierBadge").hidden = true; renderSyncLabel(); }
  if (helperOnly && !on) showTab(tab);
  paintRanges();
  accelCtrl.syncAccelMotion();
}

async function connectSupportedDevices(filters = null) {
  try {
    await registryReady;
    const useFilters = filters || getSupportedFilters();
    const picked = await navigator.hid.requestDevice({ filters: useFilters });
    const targets = groupSessionTargets([...picked, ...(await navigator.hid.getDevices())], false);
    if (!targets.length) { if (picked.length) toast("No supported control interface found on that selection (try Any HID…)", true); return; }
    let first = null;
    for (const t of targets) {
      const s = await attachTarget(t);
      if (s && !first) first = s;
    }
    if (first) selectSession(first.id);
  } catch (e) { toast(e.message, true); }
}

async function connectGenericDevice() {
  try {
    await registryReady;
    const picked = await navigator.hid.requestDevice({ filters: [] });
    if (!picked.length) return;
    const targets = groupSessionTargets(picked, true);
    let first = null;
    for (const t of targets) {
      const s = await attachTarget(t);
      if (s && !first) first = s;
    }
    if (first) selectSession(first.id);
  } catch (e) { toast(e.message, true); }
}

async function scanExistingHid() {
  if (!("hid" in navigator)) return;
  await registryReady;
  const ds = await navigator.hid.getDevices();
  const targets = groupSessionTargets(ds, true);
  for (const t of targets) await attachTarget(t);
}

$("connect").addEventListener("click", () => connectSupportedDevices());
$("connect2").addEventListener("click", () => connectSupportedDevices());
$("connectAny").addEventListener("click", connectGenericDevice);
$("connectAny2").addEventListener("click", connectGenericDevice);
$("openPowerOnly").addEventListener("click", () => { showConnected(false, true, "power"); });
$("openAccelOnly").addEventListener("click", () => { showConnected(false, true); accelCtrl.loadAccel(); });
$("refresh").addEventListener("click", async () => {
  const s = cur(); if (!s) return;
  const b = $("refresh"); b.disabled = true;
  await readAll(s);
  b.disabled = false; toast(`Read everything from ${s.shortName}`);
});
$("roDpiRefresh").addEventListener("click", async () => {
  const s = cur(); if (!s) return;
  const b = $("roDpiRefresh"); b.disabled = true;
  await readAll(s);
  b.disabled = false; toast(`Read sensor DPI from ${s.shortName}`);
});
$("snoopToggle").addEventListener("change", () => {
  snoopEnabled = $("snoopToggle").checked;
  toast(snoopEnabled ? "HID report snooper active in protocol log" : "HID report snooper off");
});
$("factoryResetBtn").addEventListener("click", async () => {
  const s = cur(); if (!s || s.driver !== "egg") return;
  if (!confirm(`Factory reset ${s.name}? All onboard stages, filters, and button mappings will return to factory defaults.`)) return;
  try {
    await s.api.factoryReset();
    applySessionChrome();
    renderFleet();
    toast(`${s.shortName} reset to factory defaults`);
  } catch (e) { toast(e.message, true); }
});

if (!("hid" in navigator)) {
  $("nohid").hidden = false;
  $("connect").disabled = $("connect2").disabled = $("connectAny").disabled = $("connectAny2").disabled = true;
} else {
  navigator.hid.addEventListener("disconnect", (e) => {
    for (const s of [...sessions.values()]) {
      if (s.dev === e.device || s.siblings.includes(e.device)) detachSession(s.id, `${s.name} disconnected`);
    }
    fetchSystemFleet();
  });
  navigator.hid.addEventListener("connect", async () => {
    await sleep(800);
    await scanExistingHid();
    fetchSystemFleet();
  });
  scanExistingHid();
}

async function fetchSystemFleet() {
  try {
    const { data } = await fetchHelperStatus();
    systemMice = data.system_mice || [];
    renderFleet();
  } catch {}
}
setTimeout(fetchSystemFleet, 100);

// ======================================================================
// tabs
// ======================================================================
function showTab(name) {
  activeTab = name;
  for (const t of document.querySelectorAll(".tab")) t.setAttribute("aria-selected", t.dataset.tab === name);
  for (const p of document.querySelectorAll(".panel")) p.hidden = p.dataset.panel !== name;
  if (name === "device") paintLog();
  if (name === "accel") accelCtrl.loadAccel();
  if (name === "power") setTimeout(loadBatteryNotify);
  accelCtrl.syncAccelMotion();
  try { localStorage.setItem("mousetool.tab", name); } catch {}
}
for (const t of document.querySelectorAll(".tab")) t.addEventListener("click", () => { if (!t.disabled) showTab(t.dataset.tab); });
try { const t = localStorage.getItem("mousetool.tab") || localStorage.getItem("viper.tab"); if (t && document.querySelector(`[data-panel="${t}"]`)) showTab(t); } catch {}

// ======================================================================
// controls
// ======================================================================
$("poll").addEventListener("click", (e) => {
  const s = cur(); if (!s || s.caps.pollReadonly) return;
  const b = e.target.closest("button"); if (!b) return;
  const hz = +b.dataset.hz; if (hz === s.S.poll && !s.pending.has("poll")) return;
  for (const x of $("poll").children) x.classList.toggle("want", x === b);
  commit("poll", { sess: s, label: "Polling rate", want: hz, write: () => s.api.setPoll(hz), read: s.api.getPoll });
});

$("dongleLed").addEventListener("click", (e) => {
  const s = cur(); if (!s) return;
  const b = e.target.closest("button"); if (!b) return;
  const m = +b.dataset.m; if (m === s.S.led) return;
  commit("led", { sess: s, label: "Dongle LED", want: m, write: () => s.api.setDongleLed(m), read: s.api.getDongleLed });
});

// lift-off (Razer preset/async + EGG/V8S discrete)
$("lodEgg").addEventListener("click", (e) => {
  const s = cur(); if (!s || s.caps.lodMode !== "egg") return;
  const b = e.target.closest("button"); if (!b) return;
  const idx = +b.dataset.eggLod;
  commit("lod", {
    sess: s, label: "Lift-off distance", want: { mode: "egg", index: idx, glass: !!s.S.egg?.glassMode },
    write: () => s.api.setLodEgg(idx),
    read: () => s.api.getLod(),
    eq: (got, want) => got?.index === want.index,
  }).then((ok) => { if (ok) toast(`Lift-off: ${b.textContent}`); });
});
$("lodLift").addEventListener("input", () => {
  const s = cur(); if (!s) return;
  held.add("lod"); if (+$("lodLand").value >= +$("lodLift").value) $("lodLand").value = +$("lodLift").value - 1;
  lodLabels(); s.lodDraft = { mode: "async", lift: +$("lodLift").value, land: +$("lodLand").value };
});
$("lodLand").addEventListener("input", () => {
  const s = cur(); if (!s) return;
  held.add("lod"); if (+$("lodLift").value <= +$("lodLand").value) $("lodLift").value = +$("lodLand").value + 1;
  lodLabels(); s.lodDraft = { mode: "async", lift: +$("lodLift").value, land: +$("lodLand").value };
});
for (const id of ["lodLift", "lodLand"]) $(id).addEventListener("change", () => held.delete("lod"));
$("lodMode").addEventListener("click", (e) => {
  const s = cur(); if (!s || !s.caps.lodAsync) return;
  const mode = e.target.closest("button")?.dataset.mode; if (!mode) return;
  if (mode === "preset") {
    s.lodDraft = null;
    if (s.S.lod?.mode === "preset") { renderLod(); return; }
    setLod({ mode: "preset", level: 0 }, "Lift-off: low");
    return;
  }
  if (s.S.lod?.mode === "async") return;
  s.lodDraft = { mode: "async", lift: 3, land: 2 };
  renderLod();
});
$("lodLevel").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  setLod({ mode: "preset", level: +b.dataset.l }, "Lift-off: " + b.textContent.toLowerCase());
});
$("lodApply").addEventListener("click", () => {
  const lift = +$("lodLift").value, land = +$("lodLand").value;
  setLod({ mode: "async", lift, land }, `Lift-off ${lift}, landing ${land}`);
});
async function setLod(want, msg) {
  const s = cur(); if (!s) return;
  const ok = await commit("lod", {
    sess: s, label: "Lift-off", want,
    write: () => want.mode === "preset" ? s.api.setLodPreset(want.level) : s.api.setLodAsync(want.lift, want.land),
    read: async () => { await sleep(40); return s.api.getLod(); },
  });
  if (ok) { s.lodDraft = null; if (cur()?.id === s.id) { renderLod(); toast(msg); } }
}

// Universal sensor, click & Endgame Gear tuning handlers
function commitEgg(label, mutator) {
  const s = cur(); if (!s || s.driver !== "egg") return;
  return commit("eggAll", {
    sess: s, label, want: true, eq: () => true,
    write: () => s.api.updateTune(mutator),
    read: () => s.S.egg,
  });
}
function commitTune(label, patch, checkFn) {
  const s = cur(); if (!s || !s.api.setTune) return;
  return commit("tune", {
    sess: s, label, want: patch,
    write: () => s.api.setTune(patch),
    read: () => s.api.getTune(),
    eq: (got) => !checkFn || checkFn(got || {}),
  });
}
$("tuneDebounce").addEventListener("input", () => { held.add("tuneDebounce"); $("tuneDebounceVal").textContent = `${$("tuneDebounce").value} ms`; });
$("tuneDebounce").addEventListener("change", () => {
  held.delete("tuneDebounce");
  const v = +$("tuneDebounce").value;
  commitTune("Click debounce", { debounceMs: v }, (g) => g.debounceMs === v);
});
$("tuneMotionSync").addEventListener("change", () => {
  const s = cur(); if (!s) return;
  const v = $("tuneMotionSync").checked;
  if (s.driver === "egg") commitEgg("Motion Sync", (out) => { out[EGG_OFF.MOTION_SYNC] = v ? 1 : 0; });
  else commitTune("Motion Sync", { motionSync: v }, (g) => !!g.motionSync === v);
});
$("tuneAngleSnap").addEventListener("change", () => {
  const s = cur(); if (!s) return;
  const v = $("tuneAngleSnap").checked;
  if (s.driver === "egg") commitEgg("Angle Snapping", (out) => { out[EGG_OFF.ANGLE_SNAP] = v ? 1 : 0; });
  else commitTune("Angle Snapping", { angleSnapping: v }, (g) => !!g.angleSnapping === v);
});
$("tuneRipple").addEventListener("change", () => {
  const s = cur(); if (!s) return;
  const v = $("tuneRipple").checked;
  if (s.driver === "egg") commitEgg("Ripple Control", (out) => { out[EGG_OFF.RIPPLE] = v ? 1 : 0; });
  else commitTune("Ripple Control", { rippleControl: v }, (g) => !!g.rippleControl === v);
});
$("tunePerf").addEventListener("change", () => {
  const v = $("tunePerf").checked;
  commitTune("Performance mode", { performance: v }, (g) => !!g.performance === v);
});
$("tuneScan20k").addEventListener("change", () => {
  const v = $("tuneScan20k").checked;
  commitTune("20K Scan Mode", { scan20k: v }, (g) => !!g.scan20k === v);
});
$("tuneAntiMistouch").addEventListener("change", () => {
  const v = $("tuneAntiMistouch").checked;
  commitTune("Anti-mistouch", { antiMistouch: v }, (g) => !!g.antiMistouch === v);
});
$("tuneSlam").addEventListener("change", () => {
  const v = $("tuneSlam").checked;
  commitEgg("Slamclick Filter", (out) => { out[EGG_OFF.FILTERS] = v ? (out[EGG_OFF.FILTERS] | 0x01) : (out[EGG_OFF.FILTERS] & ~0x01); });
});
$("tuneJitter").addEventListener("change", () => {
  const v = $("tuneJitter").checked;
  commitEgg("Motion Jitter Filter", (out) => { out[EGG_OFF.FILTERS] = v ? (out[EGG_OFF.FILTERS] | 0x10) : (out[EGG_OFF.FILTERS] & ~0x10); });
});
$("tuneGlass").addEventListener("change", () => {
  const v = $("tuneGlass").checked;
  commitEgg("Glass Mode", (out) => {
    out[EGG_OFF.GLASS] = v ? 1 : 0;
    if (v && out[EGG_OFF.LOD] > 1) out[EGG_OFF.LOD] = 0;
  });
});
$("tuneLedLift").addEventListener("change", () => {
  const v = $("tuneLedLift").checked;
  commitEgg("LED on Lift-Off", (out) => { out[EGG_OFF.LED_LIFTOFF] = v ? 0 : 1; }); // inverted!
});
$("tuneMaxFps").addEventListener("change", () => {
  const v = $("tuneMaxFps").checked ? 1 : 0;
  commitEgg("Force Max FPS", (out) => { out[EGG_OFF.MAX_FPS] = v; });
});
$("tuneAngleTune").addEventListener("input", () => { held.add("tuneAngleTune"); $("tuneAngleTuneVal").textContent = $("tuneAngleTune").value; });
$("tuneAngleTune").addEventListener("change", () => {
  const s = cur(); held.delete("tuneAngleTune"); if (!s) return;
  const v = +$("tuneAngleTune").value;
  if (s.driver === "egg") {
    const b = Math.max(-127, Math.min(127, v)) & 0xff;
    commitEgg("Angle Tuning", (out) => { out[EGG_OFF.ANGLE_TUNE] = b; });
  } else {
    commitTune("Angle Tuning", { angleTuning: v }, (g) => g.angleTuning === v);
  }
});
$("eggCtlL").addEventListener("change", () => {
  const v = +$("eggCtlL").value & 0xff;
  commitEgg("Left Switch Mode", (out) => { out[EGG_OFF.BUTTONS] = v; });
});
$("eggCtlR").addEventListener("change", () => {
  const v = +$("eggCtlR").value & 0xff;
  commitEgg("Right Switch Mode", (out) => { out[EGG_OFF.BUTTONS + 7] = v; });
});
$("eggSideMc").addEventListener("input", () => { held.add("eggSideMc"); $("eggSideMcVal").textContent = `${$("eggSideMc").value} ms`; });
$("eggSideMc").addEventListener("change", () => {
  held.delete("eggSideMc");
  const v = Math.max(0, Math.min(25, +$("eggSideMc").value));
  commitEgg("Multiclick Filter", (out) => {
    out[EGG_OFF.BUTTONS + 14] = v; out[EGG_OFF.BUTTONS + 21] = v; out[EGG_OFF.BUTTONS + 28] = v;
  });
});
$("eggHanded").addEventListener("change", async () => {
  const s = cur(); if (!s || s.driver !== "egg") return;
  const left = $("eggHanded").checked;
  await commit("eggAll", {
    sess: s, label: "Left-handed mode", want: left, eq: () => s.S.egg?.leftHanded === left,
    write: () => s.api.setHanded(left), read: () => s.S.egg,
  });
});

// pairing
function pairPid() {
  const v = $("pairPid").value === "custom" ? $("pairCustom").value.trim() : $("pairPid").value;
  return /^[0-9a-f]{1,4}$/i.test(v) ? parseInt(v, 16) : null;
}
$("pairPid").addEventListener("change", () => { $("pairCustom").hidden = $("pairPid").value !== "custom"; });
$("pairBtn").addEventListener("click", async () => {
  const s = cur(); if (!s?.api.pair) return;
  const pid = pairPid(); if (pid == null) { toast("Enter a 4-digit hex USB ID", true); return; }
  setState("pair", "saving");
  try {
    await s.api.pair(pid);
    setState("pair", "saved");
    toast("Pairing started — switch the mouse on if it isn't");
    await sleep(3000); await readAll(s);
  } catch (err) { setState("pair", "error", err.message); toast(err.message, true); }
});
$("unpairBtn").addEventListener("click", async () => {
  const s = cur(); if (!s?.api.unpair) return;
  const pid = pairPid(); if (pid == null) { toast("Enter a 4-digit hex USB ID", true); return; }
  if (!confirm(`Unpair ${pid.toString(16).padStart(4, "0")} from the dongle? The mouse stops working wirelessly until you pair it again.`)) return;
  setState("pair", "saving");
  try {
    await s.api.unpair(pid);
    setState("pair", "saved");
    toast("Unpaired");
    await sleep(500); await readAll(s);
  } catch (err) { setState("pair", "error", err.message); toast(err.message, true); }
});

// generic DPI memory reference
$("genericDpiSave").addEventListener("click", () => {
  const s = cur(); if (!s?.api.setGenericDpi) return;
  const v = clampDpi($("genericDpiIn").value, s) ?? 800;
  $("genericDpiIn").value = v;
  s.api.setGenericDpi(v);
  renderDpi();
  renderFleet();
  toast(`DPI reference saved: ${v}`);
});

// one-off dpi
$("dpiNowSet").addEventListener("click", async () => {
  const s = cur(); if (!s || !s.caps.dpi) return;
  const x = clampDpi($("dpiNow").value, s); if (x == null) { $("dpiNow").focus(); return; }
  const split = $("splitXY").checked;
  const y = split ? (clampDpi($("dpiNowY").value, s) ?? x) : x;
  $("dpiNow").value = x;
  if (split) $("dpiNowY").value = y;
  if (await commit("dpi", { sess: s, label: "DPI", want: [x, y], write: () => s.api.setDpi(x, y), read: s.api.getDpi })) {
    toast(x === y ? `DPI set to ${x}` : `DPI set to ${x} × ${y}`);
  }
});
$("dpiNow").addEventListener("keydown", (e) => { if (e.key === "Enter") $("dpiNowSet").click(); });
$("dpiNowY").addEventListener("keydown", (e) => { if (e.key === "Enter") $("dpiNowSet").click(); });

// stages
$("splitXY").addEventListener("change", () => {
  if (!$("splitXY").checked) { const e = edit(); e.list.forEach((s) => (s.y = s.x)); }
  renderStages();
});
$("revertStages").addEventListener("click", () => { const s = cur(); if (s) { s.stagesEdit = null; renderStages(); } });
$("applyStages").addEventListener("click", async () => {
  const s = cur(); if (!s) return;
  const e = edit();
  const want = { active: e.active, list: e.list.map((st) => ({ x: clampDpi(st.x, s) ?? 800, y: clampDpi(st.y, s) ?? 800 })) };
  s.stagesEdit = want;
  const ok = await commit("stages", { sess: s, label: "DPI stages", want, write: () => s.api.setStages(want.active, want.list), read: s.api.getStages });
  if (ok) { s.stagesEdit = null; if (cur()?.id === s.id) { renderStages(); toast("DPI stages saved"); } }
  s.api.getDpi?.().then((d) => { if (d) { s.S.dpi = d; if (cur()?.id === s.id) renderDpi(); } renderFleet(); }).catch(() => {});
});

// sliders: commit on release, ignore background updates while dragging
function slider(id, key, label, fmt, toDevice) {
  const el = $(id);
  el.addEventListener("input", () => { held.add(key); $(id + "Val").textContent = fmt(+el.value); });
  el.addEventListener("change", async () => {
    const s = cur(); if (!s) return;
    held.delete(key);
    const v = +el.value, raw = toDevice(v);
    if (raw === s.S[key]) return;
    const wfn = key === "idle" ? s.api.setIdle : s.api.setLow;
    const rfn = key === "idle" ? s.api.getIdle : s.api.getLow;
    if (!wfn || !rfn) return;
    await commit(key, { sess: s, label, want: raw, write: () => wfn(raw), read: rfn });
  });
}
slider("idle", "idle", "Sleep timer", (m) => `${m} min`, (m) => m * 60);
slider("low", "low", "Low battery warning", (p) => `${p}%`, (p) => Math.max(0x0c, Math.min(0xff, Math.round(p / 100 * 255))));

// ======================================================================
// low-battery desktop notifications (sent by the local helper's battery watcher)
// ======================================================================
let batteryNotify = null;  // { enabled, low, critical } from GET /api/battery, null when the helper is offline
let batteryNotifyRequest = 0;
let batteryNotifySaving = false;
let batteryNotifyLoading = true;
function renderBatteryNotify() {
  const box = $("batteryNotify");
  box.disabled = batteryNotifyLoading || batteryNotifySaving || !batteryNotify;
  box.checked = !!batteryNotify?.enabled;
  $("notifyDesc").textContent = batteryNotifyLoading ? "Loading notification settings…" : batteryNotify
    ? `The local helper warns you at ${batteryNotify.low}% and again at ${batteryNotify.critical}%, even with this window closed.`
    : "Needs the local helper on 127.0.0.1:8766 (mousetool.service), which reads the battery and sends the warnings.";
}
async function loadBatteryNotify() {
  if (batteryNotifySaving) return;
  const request = ++batteryNotifyRequest;
  batteryNotifyLoading = true;
  renderBatteryNotify();
  try {
    const { res, data } = await fetchHelperBattery();
    if (request !== batteryNotifyRequest) return;
    if (!res.ok || !data.notifications) throw new Error("Battery API unavailable");
    batteryNotify = data.notifications;
  } catch {
    if (request !== batteryNotifyRequest) return;
    batteryNotify = null;
  }
  batteryNotifyLoading = false;
  renderBatteryNotify();
}
$("batteryNotify").addEventListener("change", async () => {
  const want = $("batteryNotify").checked;
  batteryNotifyRequest++;
  batteryNotifySaving = true;
  $("batteryNotify").disabled = true;
  setState("notify", "saving");
  try {
    const { res, data } = await postHelperBattery({ notifications: want });
    if (!res.ok || !data.notifications) throw new Error(data.error || "Failed to save");
    batteryNotify = data.notifications;
    setState("notify", batteryNotify.enabled === want ? "saved" : "error", "The helper kept the old value");
  } catch (err) {
    setState("notify", "error", err.message);
    toast(err.message, true);
  }
  batteryNotifySaving = false;
  renderBatteryNotify();
});
renderBatteryNotify();
