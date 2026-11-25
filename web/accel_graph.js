// Acceleration curve math, SVG curve visualizer, live swipe telemetry & desktop pointer UI
import {
  LUT_DRAG_DX_MIN,
  LUT_DX_MIN,
  LUT_MAX_POINTS,
  LUT_MIN_POINTS,
  LUT_PRESETS,
  LUT_STARTER_POINTS,
  LUT_X_MAX,
  LUT_X_MIN,
  LUT_Y_MAX,
  LUT_Y_MIN,
  addMidpointLutPoint,
  cloneLutPoints,
  getLutPreset,
  insertLutPoint,
  lutPointsEqual,
  matchingLutPresetId,
  moveLutPoint,
  normalizeLutPoints,
  removeLutPoint,
} from "./accel_lut.js";
import {
  fetchHelperStatus,
  openHelperMotionStream,
  postHelperAccel,
  postHelperAccelReset,
  postHelperDesktop,
} from "./helper_client.js";

const MACCEL_DEFAULTS = {
  MODE: "linear", SENS_MULT: 1, YX_RATIO: 1, INPUT_DPI: 1000, ANGLE_ROTATION: 0,
  ACCEL: 0, OFFSET: 0, OUTPUT_CAP: 0,
  DECAY_RATE: 0.1, LIMIT: 1.5,
  GAMMA: 1, SMOOTH: 0.5, MOTIVITY: 1.5, SYNC_SPEED: 5,
};
const MODE_DESCS = {
  linear: "Speed rises steadily as you move faster, up to your optional cap.",
  natural: "Smooth curve that ramps up gently and levels off near your limit.",
  synchronous: "S-shaped curve that feels slower for fine aim and faster for big swipes.",
  lookup: "Draw your own curve directly on the graph with draggable control points.",
  no_accel: "No speed boost — constant sensitivity at every hand speed.",
};
const MODE_PARAMS = {
  linear: [
    { k: "ACCEL", label: "Accel", min: 0, max: 1, step: 0.005, desc: "How quickly sensitivity rises as you move faster." },
    { k: "OFFSET", label: "Offset", min: 0, max: 40, step: 0.25, desc: "Hand speed before acceleration starts (0 = immediately)." },
    { k: "OUTPUT_CAP", label: "Output cap", min: 0, max: 10, step: 0.05, desc: "Highest sensitivity multiplier allowed (0 = no limit)." },
  ],
  natural: [
    { k: "DECAY_RATE", label: "Decay rate", min: 0.001, max: 2, step: 0.001, desc: "How quickly the curve reaches your top multiplier." },
    { k: "OFFSET", label: "Offset", min: 0, max: 40, step: 0.25, desc: "Hand speed before acceleration starts (0 = immediately)." },
    { k: "LIMIT", label: "Limit", min: 1, max: 10, step: 0.05, desc: "Maximum speed boost on fast swipes." },
  ],
  synchronous: [
    { k: "SYNC_SPEED", label: "Sync speed", min: 0.1, max: 40, step: 0.05, desc: "Hand speed where sensitivity equals 1.0×." },
    { k: "MOTIVITY", label: "Motivity", min: 1.01, max: 10, step: 0.01, desc: "Maximum boost on fast swipes (slow aim scales down symmetrically)." },
    { k: "GAMMA", label: "Gamma", min: 0.05, max: 10, step: 0.05, desc: "How sharply sensitivity changes around Sync speed." },
    { k: "SMOOTH", label: "Smooth", min: 0, max: 1, step: 0.02, desc: "Softens the bend near the slowest and fastest speeds." },
  ],
  lookup: [],
  no_accel: [],
};
const GLOBAL_ACCEL_PARAMS = [
  { k: "SENS_MULT", label: "Sensitivity multiplier", min: 0.05, max: 5, step: 0.01, desc: "Scales your entire curve up or down." },
  { k: "YX_RATIO", label: "Y / X ratio", min: 0.2, max: 3, step: 0.02, desc: "Makes vertical movement faster or slower than horizontal." },
  { k: "INPUT_DPI", label: "Mouse DPI", min: 100, max: 35000, step: 50, desc: "Matches the curve to your mouse's current DPI." },
  { k: "ANGLE_ROTATION", label: "Angle rotation (°)", min: -45, max: 45, step: 0.5, desc: "Tilts horizontal movement to match your natural wrist angle." },
];
const ACCEL_MODULES = ["mouseaccel", "maccel"];
const RAWACCEL_RELEASES_URL = "https://github.com/RawAccelOfficial/rawaccel/releases";

function escHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function deriveSetupStatus(data) {
  if (data?.setup && Array.isArray(data.setup.steps)) return data.setup;
  const m = data?.accel ?? data?.maccel;
  if (!m) return null;
  const b = accelBackendInfo(m);
  if (b.name === "rawaccel") {
    const running = m.driver === "running" || !!m.loaded;
    const ready = !!(running && m.loaded && m.writable);
    return {
      platform: "windows",
      ready,
      summary: ready ? "Ready" : (m.driver === "missing" ? "Raw Accel needs setup" : "Raw Accel needs attention"),
      driver: {
        name: "rawaccel", label: "Raw Accel", installed: running,
        loaded: !!m.loaded, writable: !!m.writable, status: running ? "installed" : "needs_setup",
      },
      steps: [
        {
          id: "driver",
          title: running ? "Raw Accel driver" : "Download and install Raw Accel",
          status: running ? "installed" : "needs_setup",
          description: running
            ? "The Raw Accel driver is installed and running."
            : "Download the latest Raw Accel v1.7.x release, run installer.exe, and restart Windows.",
          command: null,
          url: running ? null : RAWACCEL_RELEASES_URL,
          url_label: running ? null : "Download Raw Accel",
        },
        {
          id: "writer",
          title: "Link your Raw Accel folder",
          status: ready ? "installed" : "needs_setup",
          description: ready
            ? "Mouse Tool can apply and verify your curve."
            : "Extract the Raw Accel zip and run this once in PowerShell:",
          command: ready ? null : 'powershell -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1 -RawAccelDir "$env:USERPROFILE\\Downloads\\RawAccel_v1.7.1\\RawAccel"',
          url: null,
          url_label: null,
        },
      ],
    };
  }
  const mod = ACCEL_MODULES.includes(b.name || b.candidate) ? (b.name || b.candidate) : "mouseaccel";
  const also = (b.also_loaded || []).filter((n) => ACCEL_MODULES.includes(n));
  const group = ACCEL_MODULES.includes(m.group) ? m.group : mod;
  const loaded = !!m.loaded;
  const paramsOk = m.parameters_available !== false;
  const writable = !!m.writable;
  const ready = loaded && paramsOk && writable && !also.length;
  return {
    platform: "linux",
    ready,
    summary: ready ? "Ready" : also.length ? "Driver conflict detected" : loaded && !writable ? "Needs save permission" : "Needs a quick setup",
    driver: {
      name: mod, label: b.label || mod, installed: !!m.installed || loaded,
      loaded, writable, status: (loaded && paramsOk && !also.length) ? "installed" : "needs_setup",
    },
    steps: [
      {
        id: "driver",
        title: (loaded && paramsOk && !also.length) ? `${b.label || mod} driver`
          : also.length ? "Unload conflicting maccel driver"
          : b.candidate ? `Load the ${mod} driver`
          : "Install the mouseaccel driver",
        status: (loaded && paramsOk && !also.length) ? "installed" : "needs_setup",
        description: (loaded && paramsOk && !also.length)
          ? "Running and ready for custom sensitivity curves."
          : also.length ? "Both mouseaccel and maccel are loaded, so changes are ignored. Unload maccel and reload mouseaccel:"
          : b.candidate ? `${mod} is installed on this PC, but not loaded yet. Load it with:`
          : "Install the Linux mouseaccel driver (recommended) or legacy maccel, then load it:",
        command: (loaded && paramsOk && !also.length) ? null
          : also.length ? "sudo rmmod maccel && sudo modprobe -r mouseaccel && sudo modprobe mouseaccel"
          : `sudo modprobe ${mod}`,
        url: (!loaded && !b.candidate) ? "https://www.maccel.org" : null,
        url_label: (!loaded && !b.candidate) ? "maccel guide (legacy)" : null,
      },
      {
        id: "permissions",
        title: (loaded && writable) ? "Save permissions" : `Allow saving without root`,
        status: (loaded && writable) ? "installed" : "needs_setup",
        description: (loaded && writable)
          ? "Mouse Tool can save acceleration changes directly."
          : mod === "maccel"
          ? "Add your user to the maccel group, then log out and back in:"
          : "Install the mouseaccel permission rule and add your user to the group (then log out and back in):",
        command: (loaded && writable) ? null
          : mod === "maccel"
          ? `sudo usermod -aG ${group} "$USER"`
          : './install.sh --mouseaccel-perms && sudo usermod -aG mouseaccel "$USER"',
        url: null,
        url_label: null,
      },
    ],
  };
}

function maccelEval(x, cfg) {
  const m = cfg.MODE || "linear";
  let s = 1;
  if (m === "linear") {
    const accel = cfg.ACCEL ?? 0, off = cfg.OFFSET ?? 0, cap = cfg.OUTPUT_CAP ?? 0;
    if (x > off) {
      let b = accel * ((x - off) * (x - off)) / x;
      let sign = 1;
      if (cap > 0) {
        let c = cap - 1;
        if (c < 0) { c = -c; sign = -1; }
        b = Math.min(b, c);
      }
      s = 1 + sign * b;
    }
  } else if (m === "natural") {
    const decay = cfg.DECAY_RATE ?? 0.1, off = cfg.OFFSET ?? 0, lim = cfg.LIMIT ?? 1.5;
    if (x > off && lim > 1 && decay > 0) {
      const l = lim - 1;
      const a = decay / Math.abs(l);
      const c = -l / a;
      const ox = off - x;
      const dec = Math.exp(a * ox);
      const out = l * (dec / a - ox) + c;
      s = out / x + 1;
    }
  } else if (m === "synchronous") {
    const gamma = cfg.GAMMA ?? 1, smooth = cfg.SMOOTH ?? 0.5, mot = Math.max(1.0001, cfg.MOTIVITY ?? 1.5), sync = Math.max(0.0001, cfg.SYNC_SPEED ?? 5);
    const logM = Math.log(mot), gc = gamma / logM, logS = Math.log(sync);
    const sharp = smooth === 0 ? 16 : 0.5 / smooth;
    if (sharp >= 16) {
      const ls = Math.max(-1, Math.min(1, gc * (Math.log(Math.max(1e-6, x)) - logS)));
      s = Math.exp(ls * logM);
    } else if (Math.abs(x - sync) < 1e-9) {
      s = 1;
    } else {
      const ld = Math.log(Math.max(1e-6, x)) - logS;
      const sign = ld > 0 ? 1 : -1;
      const ls = gc * Math.abs(ld);
      const exp = sign * Math.pow(Math.tanh(Math.pow(ls, sharp)), 1 / sharp);
      s = Math.exp(exp * logM);
    }
  } else if (m === "lookup") {
    const pts = cfg.LUT_POINTS;
    if (Array.isArray(pts) && pts.length >= 2) {
      if (x <= pts[0][0]) {
        s = pts[0][1];
      } else if (x >= pts[pts.length - 1][0]) {
        s = pts[pts.length - 1][1];
      } else {
        let lo = 0, hi = pts.length - 1;
        while (hi - lo > 1) {
          const mid = (lo + hi) >> 1;
          if (pts[mid][0] <= x) lo = mid;
          else hi = mid;
        }
        const [x0, y0] = pts[lo], [x1, y1] = pts[lo + 1];
        s = y0 + ((x - x0) * (y1 - y0)) / (x1 - x0);
      }
    }
  }
  const sx = s * (cfg.SENS_MULT ?? 1);
  return [sx, sx * (cfg.YX_RATIO ?? 1)];
}

function accelSnapshot(m) {
  const loadedLut = normalizeLutPoints(m?.lut_points);
  return {
    MODE: m?.mode || "linear",
    ...Object.fromEntries(
      Object.keys(MACCEL_DEFAULTS)
        .filter((k) => k !== "MODE")
        .map((k) => [k, +(m?.params?.[k] ?? MACCEL_DEFAULTS[k]).toFixed(6)])
    ),
    LUT_POINTS: loadedLut ? cloneLutPoints(loadedLut) : cloneLutPoints(LUT_STARTER_POINTS),
  };
}

function accelBackendInfo(m) {
  const b = m?.backend;
  if (b && (b.name || b.candidate !== undefined || b.installed_backends)) return b;
  return m?.loaded
    ? { name: "maccel", label: "maccel (legacy)", legacy: true, loaded: true,
        params_path: "/sys/module/maccel/parameters", speed_device: "/dev/maccel", also_loaded: [] }
    : { name: null, label: null, loaded: false, candidate: m?.installed ? "maccel" : null, also_loaded: [] };
}

function accelModuleCode(name) {
  const n = ACCEL_MODULES.includes(name) ? name : "mouseaccel";
  return n === "maccel" ? "<code>maccel</code> (legacy)" : "<code>mouseaccel</code>";
}

function createAccelController({ $, getActiveDpi, isAccelTabActive, setState, toast, paintRanges, onSystemMice }) {
  let accelLive = null;   // { maccel, desktop } from helper
  let accelDraftBackend = null;
  let accelDraft = null;  // working copy of mode + params
  let accelXMax = 40;
  let accelLoading = false;
  let desktopSaving = false;
  let selectedLutIdx = 2;
  let lutDrag = null;

  const accelMotion = {
    es: null,
    retryTimer: null,
    statusTimer: null,
    rafId: 0,
    ptrRafId: 0,
    ptrOn: false,
    connected: false,
    state: {
      speed: 0, raw_speed: 0, peak: 0, peak_age_ms: 0, peak_hold_ms: 2800,
      swipe_min: 0, swipe_max: 0, swipe_id: 0, active: false, dpi: 1000, source: "none", devices: 0,
    },
    ptr: { lastTs: 0, lastEv: 0, lastMot: 0, speed: 0, peak: 0, peakTs: 0, min: 0, max: 0, inSwipe: false, swipeId: 0 },
  };

  function _svgShow(el, on) {
    if (el) el.style.display = on ? "" : "none";
  }

  function _fmtX(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "0";
    return n.toFixed(2).replace(/\.?0+$/, "") || "0";
  }

  function _fmtY(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "1.00";
    return Math.abs(n * 100 - Math.round(n * 100)) > 1e-3 ? n.toFixed(3).replace(/0+$/, "") : n.toFixed(2);
  }

  function isLutMode() {
    return accelDraft?.MODE === "lookup";
  }

  function ensureDraftLutPoints() {
    if (!accelDraft) return LUT_STARTER_POINTS.map(([x, y]) => [x, y]);
    if (!Array.isArray(accelDraft.LUT_POINTS) || accelDraft.LUT_POINTS.length < LUT_MIN_POINTS) {
      accelDraft.LUT_POINTS = cloneLutPoints(LUT_STARTER_POINTS);
    }
    selectedLutIdx = Math.max(0, Math.min(selectedLutIdx, accelDraft.LUT_POINTS.length - 1));
    return accelDraft.LUT_POINTS;
  }

  function canApplyCurrentMode() {
    const m = accelLive?.maccel;
    if (!m?.loaded || !m?.writable || !accelDraft) return false;
    if (accelDraft.MODE === "lookup") {
      return !!(m.lut_supported && m.lut_writable !== false);
    }
    return true;
  }

  function renderLutNotice() {
    const el = $("accelLutNotice");
    if (!el) return;
    if (!isLutMode()) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    const m = accelLive?.maccel;
    if (m?.loaded && m?.lut_supported && m?.lut_writable !== false) {
      el.hidden = true;
      el.innerHTML = "";
      return;
    }
    const b = m ? accelBackendInfo(m) : null;
    let msg;
    if (!m?.loaded) {
      msg = "Preview active — start Mouse Tool's helper with the updated mouseaccel driver (or Raw Accel on Windows) to save Custom curves.";
    } else if (m.lut_supported && m.lut_writable === false) {
      const grp = ACCEL_MODULES.includes(m.group) ? m.group : "mouseaccel";
      msg = `Preview active — no write access to LUT_POINTS. Add your user to the ${grp} group and log back in to save Custom curves.`;
    } else if (b?.legacy || b?.name === "maccel") {
      msg = "Preview active — maccel (legacy) does not support Custom curves. Update to the mouseaccel driver (or use Raw Accel on Windows) to save.";
    } else {
      msg = "Preview active — the loaded mouseaccel driver does not expose LUT_POINTS yet. Update and reload mouseaccel (or use Raw Accel on Windows) to save.";
    }
    el.innerHTML = `<span><strong>Custom curve preview</strong> · ${escHtml(msg)}</span>`;
    el.hidden = false;
  }

  function accelDirty() {
    if (!accelLive?.maccel?.loaded || !accelDraft) return false;
    const orig = accelSnapshot(accelLive.maccel);
    const scalarChanged = Object.keys(MACCEL_DEFAULTS).some((k) =>
      k === "MODE" ? orig[k] !== accelDraft[k] : Math.abs(orig[k] - accelDraft[k]) > 1e-5
    );
    if (scalarChanged) return true;
    if (orig.MODE === "lookup" || accelDraft.MODE === "lookup") {
      return !lutPointsEqual(orig.LUT_POINTS, ensureDraftLutPoints());
    }
    return false;
  }

  function renderAccelDirty() {
    const d = accelDirty();
    const canWrite = canApplyCurrentMode();
    $("accelDirty").hidden = !d;
    $("accelRevert").hidden = !d;
    $("accelApply").hidden = !d || !canWrite;
    $("accelApply").disabled = !canWrite;
    renderLutNotice();
  }

  function setAccelLiveBadge(mode, text, title) {
    const badge = $("accelLiveBadge");
    const label = $("accelLiveSrc");
    if (!badge || !label) return;
    badge.className = "livebadge" + (mode ? ` ${mode}` : "");
    label.textContent = text;
    if (title) badge.title = title;
  }

  function _placeOverPill(grp, text, g, cy) {
    const rect = grp.querySelector("rect");
    const txt = grp.querySelector("text");
    txt.textContent = text;
    const w = Math.max(42, text.length * 6.2 + 10);
    const h = 15;
    const rx = g.W - g.R - w - 6;
    const ry = Math.max(g.T + 2, Math.min(g.H - g.B - h - 2, cy - h / 2));
    rect.setAttribute("x", rx.toFixed(1));
    rect.setAttribute("y", ry.toFixed(1));
    rect.setAttribute("width", w.toFixed(1));
    rect.setAttribute("height", h);
    txt.setAttribute("x", (rx + w - 4).toFixed(1));
    txt.setAttribute("y", (ry + 10.5).toFixed(1));
    _svgShow(grp, true);
    return ry;
  }

  function renderAccelLiveOverlay() {
    const svg = $("accelSvg");
    const g = svg?._geom;
    if (!g || !accelDraft || !accelMotion) return;

    const st = accelMotion.state;
    const isPtr = st.source === "pointer";
    const streamDpi = st.dpi > 0 ? st.dpi : 1000;
    const draftDpi = accelDraft.INPUT_DPI > 0 ? accelDraft.INPUT_DPI : streamDpi;
    const dpiScale = streamDpi / draftDpi;
    if (isPtr || st.source === "maccel" || !accelMotion.connected) {
      for (const id of ["accelSwipeGroup", "accelSwipeGroupY", "accelPeakGroup", "accelLiveGroup"]) {
        _svgShow($(id), false);
      }
      let text = "Awaiting motion source";
      if (isPtr) {
        text = `Approx. window motion · Now ${st.speed.toFixed(1)} · Peak ${st.peak.toFixed(1)} browser units/ms`;
      } else if (st.source === "maccel" && accelMotion.connected) {
        const last = Number.isFinite(st.last_speed) ? (st.last_speed * dpiScale).toFixed(1) + " c/ms" : "—";
        text = `Last ${ACCEL_MODULES.includes(st.backend) ? st.backend : "kernel"} value ${last} · Motion timing unknown`;
      }
      $("accelLiveReadout").textContent = text;
      return;
    }

    const speed = Math.max(0, (st.speed || 0) * dpiScale);
    const peak = Math.max(0, (st.peak || 0) * dpiScale);
    const swipeMin = Math.max(0, (st.swipe_min || 0) * dpiScale);
    const swipeMax = Math.max(0, (st.swipe_max || 0) * dpiScale);

    const [nowSx, nowSy] = maccelEval(Math.max(0.05, speed), accelDraft);
    const hasPeak = peak > 0.05;
    const [peakSx, peakSy] = hasPeak ? maccelEval(Math.max(0.05, peak), accelDraft) : [0, 0];
    const clampY = (y) => Math.max(g.T, Math.min(g.H - g.B, y));

    const holdMs = st.peak_hold_ms || 2800;
    const remMs = Math.max(0, holdMs - (st.peak_age_ms || 0));
    const fade = st.active ? 1 : Math.max(0.18, Math.min(1, remMs / 500));

    // Swipe band + highlighted curve segment
    const swipeGrp = $("accelSwipeGroup");
    const swipeGrpY = $("accelSwipeGroupY");
    if (swipeGrp) {
      if (hasPeak) {
        const lo = Math.min(accelXMax, Math.max(0, Math.min(swipeMin, peak)));
        const hi = Math.min(accelXMax, Math.max(lo, Math.max(swipeMax, peak)));
        const x1 = g.px(lo), x2 = g.px(hi);
        const bandW = Math.max(2, x2 - x1);
        const band = $("accelSwipeBand");
        band.setAttribute("x", x1.toFixed(1));
        band.setAttribute("width", bandW.toFixed(1));
        const clipY = $("accelSwipeClipYRect");
        if (clipY) {
          clipY.setAttribute("x", x1.toFixed(1));
          clipY.setAttribute("width", bandW.toFixed(1));
        }

        const steps = 36;
        const sampleXs = [];
        for (let i = 0; i <= steps; i++) {
          sampleXs.push(Math.max(0.05, lo + (i / steps) * (hi - lo)));
        }
        if (isLutMode()) {
          for (const [kx] of ensureDraftLutPoints()) {
            if (kx > lo && kx < hi) sampleXs.push(kx);
          }
          sampleXs.sort((a, b) => a - b);
        }
        const dX = [];
        for (let i = 0; i < sampleXs.length; i++) {
          const xv = sampleXs[i];
          const [sx] = maccelEval(xv, accelDraft);
          const cmd = i ? "L" : "M";
          dX.push(`${cmd}${g.px(xv).toFixed(1)},${clampY(g.py(sx)).toFixed(1)}`);
        }
        $("accelSwipeTrail").setAttribute("d", dX.join(" "));
        swipeGrp.style.opacity = fade.toFixed(2);
        _svgShow(swipeGrp, true);
        if (swipeGrpY) {
          swipeGrpY.style.opacity = fade.toFixed(2);
          _svgShow(swipeGrpY, g.hasY);
        }
      } else {
        _svgShow(swipeGrp, false);
        _svgShow(swipeGrpY, false);
      }
    }

    // Peak hold marker
    let peakPillY = null;
    const peakGrp = $("accelPeakGroup");
    if (peakGrp) {
      if (hasPeak) {
        const peakOver = peak > accelXMax;
        const pxVal = Math.min(peak, accelXMax);
        const [pPlotSx, pPlotSy] = maccelEval(Math.max(0.05, pxVal), accelDraft);
        const pcx = g.px(pxVal), pcy = clampY(g.py(pPlotSx)), pcyY = clampY(g.py(pPlotSy));
        const pl = $("accelPeakLine");
        pl.setAttribute("x1", pcx.toFixed(1)); pl.setAttribute("x2", pcx.toFixed(1));
        const d = 8;
        $("accelPeakDot").setAttribute(
          "points",
          `${pcx.toFixed(1)},${(pcy - d).toFixed(1)} ${(pcx + d).toFixed(1)},${pcy.toFixed(1)} ${pcx.toFixed(1)},${(pcy + d).toFixed(1)} ${(pcx - d).toFixed(1)},${pcy.toFixed(1)}`
        );
        const pdY = $("accelPeakDotY");
        if (g.hasY) {
          const dy = 7;
          pdY.setAttribute(
            "points",
            `${pcx.toFixed(1)},${(pcyY - dy).toFixed(1)} ${(pcx + dy).toFixed(1)},${pcyY.toFixed(1)} ${pcx.toFixed(1)},${(pcyY + dy).toFixed(1)} ${(pcx - dy).toFixed(1)},${pcyY.toFixed(1)}`
          );
        }
        _svgShow(pdY, g.hasY);
        const pOver = $("accelPeakOver");
        if (peakOver) {
          peakPillY = _placeOverPill(pOver, `>${peak.toFixed(1)} ▸`, g, pcy);
        } else {
          _svgShow(pOver, false);
        }
        peakGrp.style.opacity = fade.toFixed(2);
        _svgShow(peakGrp, true);
      } else {
        _svgShow(peakGrp, false);
      }
    }

    // Live current-position marker
    const liveGrp = $("accelLiveGroup");
    if (liveGrp) {
      if (accelMotion.connected) {
        const moving = speed >= 0.05;
        const liveOver = speed > accelXMax;
        const lxVal = Math.min(speed, accelXMax);
        const [lPlotSx, lPlotSy] = maccelEval(Math.max(0.05, lxVal), accelDraft);
        const lcx = g.px(lxVal), lcy = clampY(g.py(lPlotSx)), lcyY = clampY(g.py(lPlotSy));
        const ll = $("accelLiveLine");
        ll.setAttribute("x1", lcx.toFixed(1)); ll.setAttribute("x2", lcx.toFixed(1));
        const halo = $("accelLiveHalo");
        halo.setAttribute("cx", lcx.toFixed(1)); halo.setAttribute("cy", lcy.toFixed(1));
        halo.setAttribute("r", (8 + Math.min(4, speed / 15)).toFixed(1));
        _svgShow(halo, moving);
        const dot = $("accelLiveDot");
        dot.setAttribute("cx", lcx.toFixed(1)); dot.setAttribute("cy", lcy.toFixed(1));
        const dotY = $("accelLiveDotY");
        if (g.hasY) {
          dotY.setAttribute("cx", lcx.toFixed(1)); dotY.setAttribute("cy", lcyY.toFixed(1));
        }
        _svgShow(dotY, g.hasY);
        const lOver = $("accelLiveOver");
        if (liveOver) {
          let targetY = lcy;
          if (peakPillY !== null && Math.abs((targetY - 7.5) - peakPillY) < 18) {
            targetY = peakPillY + 26 <= g.H - g.B - 10 ? peakPillY + 25.5 : peakPillY - 10.5;
          }
          _placeOverPill(lOver, `>${speed.toFixed(1)} ▸`, g, targetY);
        } else {
          _svgShow(lOver, false);
        }
        liveGrp.style.opacity = moving ? "1" : "0.48";
        _svgShow(liveGrp, true);
      } else {
        _svgShow(liveGrp, false);
      }
    }

    // Telemetry numeric readout
    const ro = $("accelLiveReadout");
    if (ro) {
      const nowOverTag = speed > accelXMax ? `<span class="lv-over">&gt;</span>` : "";
      const nowYTag = g.hasY ? ` <span class="lv-dim">(Y ${nowSy.toFixed(2)}×)</span>` : "";
      const nowHtml = `<span class="lv-now">Now: ${nowOverTag}<b>${speed.toFixed(1)}</b> c/ms → <b>${nowSx.toFixed(2)}×</b>${nowYTag}</span>`;
      let peakHtml;
      if (hasPeak) {
        const peakOverTag = peak > accelXMax ? `<span class="lv-over">&gt;</span>` : "";
        const peakYTag = g.hasY ? ` <span class="lv-dim">(Y ${peakSy.toFixed(2)}×)</span>` : "";
        peakHtml = `<span class="lv-peak">Peak: ${peakOverTag}<b>${peak.toFixed(1)}</b> c/ms → <b>${peakSx.toFixed(2)}×</b>${peakYTag}</span>`;
      } else {
        peakHtml = `<span class="lv-peak lv-dim">Peak: —</span>`;
      }
      ro.innerHTML = `${nowHtml}<span class="lv-sep">·</span>${peakHtml}`;
    }
  }

  function _curvePaths(g, pts) {
    const pathX = pts.map(([x, sx], i) => `${i ? "L" : "M"}${g.px(x).toFixed(1)},${g.py(sx).toFixed(1)}`).join(" ");
    const areaX = `${pathX} L${g.px(accelXMax).toFixed(1)},${g.py(g.yMin).toFixed(1)} L${g.L},${g.py(g.yMin).toFixed(1)} Z`;
    const pathY = g.hasY ? pts.map(([x, , sy], i) => `${i ? "L" : "M"}${g.px(x).toFixed(1)},${g.py(sy).toFixed(1)}`).join(" ") : "";
    return { pathX, areaX, pathY };
  }

  function _sampleCurvePoints() {
    const pts = [];
    if (isLutMode()) {
      const lut = ensureDraftLutPoints();
      const xs = [0, accelXMax];
      for (const [kx] of lut) {
        if (kx > 0 && kx < accelXMax) xs.push(kx);
      }
      xs.sort((a, b) => a - b);
      for (let i = 0; i < xs.length; i++) {
        if (i > 0 && Math.abs(xs[i] - xs[i - 1]) < 1e-9) continue;
        const x = xs[i];
        const [sx, sy] = maccelEval(x, accelDraft);
        pts.push([x, sx, sy]);
      }
      return pts;
    }
    const N = 160;
    for (let i = 0; i <= N; i++) {
      const x = Math.max(0.05, (i / N) * accelXMax);
      const [sx, sy] = maccelEval(x, accelDraft);
      pts.push([x, sx, sy]);
    }
    return pts;
  }

  function _placeLutBadge(badge, cx, cy, xVal, yVal, g) {
    if (!badge) return;
    const rect = badge.querySelector("rect");
    const txt = badge.querySelector("text");
    if (!rect || !txt) return;
    const label = `${_fmtX(xVal)} c/ms · ${_fmtY(yVal)}×`;
    txt.textContent = label;
    const w = Math.max(82, label.length * 6.3 + 14);
    const h = 20;
    const rx = Math.max(g.L + 4, Math.min(g.W - g.R - w - 4, cx - w / 2));
    const aboveY = cy - h - 11;
    const ry = aboveY >= g.T + 2 ? aboveY : Math.min(g.H - g.B - h - 2, cy + 12);
    rect.setAttribute("x", rx.toFixed(1));
    rect.setAttribute("y", ry.toFixed(1));
    rect.setAttribute("width", w.toFixed(1));
    rect.setAttribute("height", String(h));
    txt.setAttribute("x", (rx + w / 2).toFixed(1));
    txt.setAttribute("y", (ry + 13.5).toFixed(1));
    _svgShow(badge, true);
  }

  function _updateLutDragVisuals() {
    const svg = $("accelSvg");
    const g = lutDrag?.geom || svg?._geom;
    if (!g || !accelDraft || !isLutMode()) return;
    const lut = ensureDraftLutPoints();
    const sensMult = accelDraft.SENS_MULT ?? 1;
    const pts = _sampleCurvePoints();
    const { pathX, areaX, pathY } = _curvePaths(g, pts);
    const lineX = $("accelLineX");
    if (lineX) lineX.setAttribute("d", pathX);
    const areaEl = svg.querySelector(".area-x");
    if (areaEl) areaEl.setAttribute("d", areaX);
    if (g.hasY) {
      const lineY = $("accelLineY");
      if (lineY) lineY.setAttribute("d", pathY);
      const trailY = $("accelSwipeTrailY");
      if (trailY) trailY.setAttribute("d", pathY);
    }
    const idx = lutDrag ? lutDrag.idx : selectedLutIdx;
    const pt = lut[idx];
    if (pt) {
      const [xv, yv] = pt;
      const cx = g.px(Math.max(0, Math.min(accelXMax, xv)));
      const cy = Math.max(g.T, Math.min(g.H - g.B, g.py(yv * sensMult)));
      const nodeG = svg.querySelector(`.lut-pt[data-lut-idx="${idx}"]`);
      if (nodeG) {
        for (const c of nodeG.querySelectorAll("circle")) {
          c.setAttribute("cx", cx.toFixed(1));
          c.setAttribute("cy", cy.toFixed(1));
        }
        nodeG.setAttribute("aria-label", `Point ${idx + 1}: ${_fmtX(xv)} c/ms, ${_fmtY(yv)}×`);
        nodeG.setAttribute("aria-valuenow", String(yv));
        nodeG.setAttribute("aria-valuetext", `${_fmtX(xv)} counts/ms, ${_fmtY(yv)}×`);
      }
      const guide = $("accelLutGuide");
      if (guide) {
        guide.setAttribute("x1", cx.toFixed(1));
        guide.setAttribute("x2", cx.toFixed(1));
        guide.setAttribute("y1", cy.toFixed(1));
        _svgShow(guide, xv <= accelXMax);
      }
      _placeLutBadge($("accelLutBadge"), cx, cy, xv, yv, g);
      const [effX, effY] = maccelEval(xv, accelDraft);
      const effNote = Math.abs(sensMult - 1) > 1e-4 ? ` (eff ${effX.toFixed(2)}×)` : "";
      const yNote = Math.abs(effX - effY) > 1e-4 ? ` (Y: ${effY.toFixed(2)}×)` : "";
      $("accelProbe").textContent = `Pt ${idx + 1}: ${_fmtX(xv)} c/ms → ${_fmtY(yv)}×${effNote}${yNote}`;
    }
    renderAccelLiveOverlay();
  }

  function renderAccelCurve() {
    if (!accelDraft) return;
    const svg = $("accelSvg");
    const isLut = isLutMode();
    const W = 640, H = 240, L = 46, R = 16, T = 16, B = 28;
    const PW = W - L - R, PH = H - T - B;
    const pts = _sampleCurvePoints();
    let yMax = 2.0, yMin = 0.5;
    if (lutDrag?.geom) {
      yMin = lutDrag.geom.yMin;
      yMax = lutDrag.geom.yMax;
    } else {
      for (const [, sx, sy] of pts) {
        yMax = Math.max(yMax, sx, sy);
        yMin = Math.min(yMin, sx, sy);
      }
      yMax = Math.ceil((yMax * (isLut ? 1.15 : 1.1)) * 4) / 4;
      yMin = Math.max(0, Math.floor((yMin * 0.9) * 4) / 4);
      if (yMax - yMin < 0.5) yMax = yMin + 0.5;
    }
    const px = (x) => L + (x / accelXMax) * PW;
    const py = (y) => T + (1 - (y - yMin) / (yMax - yMin)) * PH;
    const hasY = Math.abs((accelDraft.YX_RATIO ?? 1) - 1) > 1e-4;
    const geom = { W, H, L, R, T, B, PW, PH, yMin, yMax, hasY, px, py };

    let g = "";
    for (let i = 0; i <= 4; i++) {
      const yv = yMin + (i / 4) * (yMax - yMin), yy = py(yv);
      g += `<line class="gridline" x1="${L}" y1="${yy.toFixed(1)}" x2="${W - R}" y2="${yy.toFixed(1)}"/>`;
      g += `<text class="ax" x="${L - 6}" y="${(yy + 3).toFixed(1)}" text-anchor="end">${yv.toFixed(2)}×</text>`;
    }
    for (let i = 0; i <= 4; i++) {
      const xv = (i / 4) * accelXMax, xx = px(xv);
      g += `<line class="gridline" x1="${xx.toFixed(1)}" y1="${T}" x2="${xx.toFixed(1)}" y2="${H - B}"/>`;
      g += `<text class="ax" x="${xx.toFixed(1)}" y="${H - 10}" text-anchor="middle">${xv}</text>`;
    }
    const hasBase = yMin <= 1 && 1 <= yMax;
    if (hasBase) {
      g += `<line class="base" x1="${L}" y1="${py(1).toFixed(1)}" x2="${W - R}" y2="${py(1).toFixed(1)}"/>`;
    }
    const { pathX, areaX, pathY } = _curvePaths(geom, pts);
    $("accelLegendY").hidden = !hasY;
    if ($("accelLegendBase")) $("accelLegendBase").hidden = !hasBase;
    if ($("accelCurveMeta")) $("accelCurveMeta").classList.toggle("has-y", hasY);

    g += `<defs><clipPath id="accelSwipeClipY"><rect id="accelSwipeClipYRect" x="${L}" y="${T - 4}" width="0" height="${PH + 8}"/></clipPath></defs>`;
    g += `<path class="area-x" d="${areaX}"/>`;
    g += `<g id="accelSwipeGroup" style="display:none">`;
    g += `<rect class="swipe-band" id="accelSwipeBand" x="${L}" y="${T}" width="0" height="${PH}"/>`;
    g += `<path class="swipe-trail" id="accelSwipeTrail" d=""/>`;
    g += `</g>`;
    if (hasY) {
      g += `<path class="line-y" id="accelLineY" d="${pathY}"/>`;
      g += `<g id="accelSwipeGroupY" style="display:none"><path class="swipe-trail-y" id="accelSwipeTrailY" d="${pathY}" clip-path="url(#accelSwipeClipY)"/></g>`;
    }
    g += `<path class="line-x" id="accelLineX" d="${pathX}"/>`;
    g += `<g id="accelPeakGroup" style="display:none">`;
    g += `<line class="peak-line" id="accelPeakLine" x1="0" y1="${T}" x2="0" y2="${H - B}"/>`;
    g += `<polygon class="peak-dot-y" id="accelPeakDotY" points="0,0" style="display:none"/>`;
    g += `<polygon class="peak-dot" id="accelPeakDot" points="0,0"/>`;
    g += `<g id="accelPeakOver" style="display:none"><rect class="over-pill" rx="3" x="0" y="0" width="44" height="15"/><text class="over-txt" x="0" y="0" text-anchor="end"></text></g>`;
    g += `</g>`;
    g += `<g id="accelLiveGroup" style="display:none">`;
    g += `<line class="live-line" id="accelLiveLine" x1="0" y1="${T}" x2="0" y2="${H - B}"/>`;
    g += `<circle class="live-halo" id="accelLiveHalo" cx="0" cy="0" r="9" style="display:none"/>`;
    g += `<circle class="live-dot-y" id="accelLiveDotY" cx="0" cy="0" r="3.5" style="display:none"/>`;
    g += `<circle class="live-dot" id="accelLiveDot" cx="0" cy="0" r="4.5"/>`;
    g += `<g id="accelLiveOver" style="display:none"><rect class="over-pill live" rx="3" x="0" y="0" width="44" height="15"/><text class="over-txt live" x="0" y="0" text-anchor="end"></text></g>`;
    g += `</g>`;
    g += `<line class="cross" id="accelCross" x1="0" y1="${T}" x2="0" y2="${H - B}" style="display:none"/>`;
    g += `<circle class="dot-x" id="accelDot" cx="0" cy="0" r="4" style="display:none"/>`;

    if (isLut) {
      const lut = ensureDraftLutPoints();
      const sensMult = accelDraft.SENS_MULT ?? 1;
      const selPt = lut[selectedLutIdx];
      const selVisible = selPt && selPt[0] >= 0 && selPt[0] <= accelXMax;
      const selCx = selVisible ? px(selPt[0]) : 0;
      const selCy = selVisible ? Math.max(T, Math.min(H - B, py(selPt[1] * sensMult))) : 0;
      g += `<g id="accelLutGroup">`;
      g += `<line class="lut-guide" id="accelLutGuide" x1="${selCx.toFixed(1)}" y1="${selCy.toFixed(1)}" x2="${selCx.toFixed(1)}" y2="${H - B}"${selVisible ? "" : ' style="display:none"'}/>`;
      for (let i = 0; i < lut.length; i++) {
        const [xv, yv] = lut[i];
        if (xv < 0 || xv > accelXMax) continue;
        const cx = px(xv);
        const cy = Math.max(T, Math.min(H - B, py(yv * sensMult)));
        const isSel = i === selectedLutIdx;
        const isDrag = lutDrag?.idx === i;
        const cls = `lut-pt${isSel ? " sel" : ""}${isDrag ? " dragging" : ""}`;
        const label = `Point ${i + 1}: ${_fmtX(xv)} c/ms, ${_fmtY(yv)}×`;
        g += `<g class="${cls}" data-lut-idx="${i}" tabindex="0" role="slider" `
          + `aria-label="${escHtml(label)}" aria-valuemin="${LUT_Y_MIN}" aria-valuemax="${LUT_Y_MAX}" `
          + `aria-valuenow="${yv}" aria-valuetext="${escHtml(`${_fmtX(xv)} counts/ms, ${_fmtY(yv)}×`)}">`
          + `<circle class="lut-hit" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="13"/>`
          + `<circle class="lut-ring" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="9.5"/>`
          + `<circle class="lut-node" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${isSel ? 6 : 5}"/>`
          + `</g>`;
      }
      g += `<g class="lut-badge" id="accelLutBadge" style="display:none"><rect class="lut-badge-bg" rx="3" x="0" y="0" width="90" height="20"/><text class="lut-badge-txt" x="0" y="0" text-anchor="middle"></text></g>`;
      g += `</g>`;
    }

    svg.innerHTML = g;
    svg.classList.toggle("is-lut", isLut);
    svg.classList.toggle("is-dragging", !!lutDrag);
    svg._geom = geom;
    if (isLut) {
      const lut = ensureDraftLutPoints();
      const selPt = lut[selectedLutIdx];
      if (selPt && selPt[0] >= 0 && selPt[0] <= accelXMax) {
        const sensMult = accelDraft.SENS_MULT ?? 1;
        const cx = px(selPt[0]);
        const cy = Math.max(T, Math.min(H - B, py(selPt[1] * sensMult)));
        _placeLutBadge($("accelLutBadge"), cx, cy, selPt[0], selPt[1], geom);
      }
      if (!lutDrag && $("accelProbe")) {
        $("accelProbe").textContent = "Click graph to add points · drag handles to shape";
      }
    } else if ($("accelProbe") && $("accelProbe").textContent.startsWith("Click graph")) {
      $("accelProbe").textContent = "Hover curve to inspect speed → multiplier";
    }
    renderAccelLiveOverlay();
  }

  function scheduleAccelOverlay() {
    if (accelMotion.rafId) return;
    accelMotion.rafId = requestAnimationFrame(() => {
      accelMotion.rafId = 0;
      renderAccelLiveOverlay();
    });
  }

  function _ptrTick() {
    accelMotion.ptrRafId = 0;
    if (!accelMotion.ptrOn) return;
    const p = accelMotion.ptr;
    const now = performance.now();
    const idleMs = p.lastEv ? (now - p.lastEv) : 1e9;
    let curSpeed = 0;
    let active = false;
    if (idleMs <= 45) {
      curSpeed = p.speed;
      active = curSpeed > 0.05;
    } else if (p.speed > 0) {
      curSpeed = p.speed * Math.exp(-(idleMs - 45) / 35);
      if (curSpeed < 0.05) { curSpeed = 0; p.speed = 0; p.inSwipe = false; }
    }
    if (idleMs >= 220) p.inSwipe = false;
    let peakAge = 0;
    if (p.peak > 0 && p.peakTs) {
      peakAge = Math.max(0, now - p.peakTs);
      if (!active && peakAge >= 2800) {
        p.peak = 0; p.min = 0; p.max = 0; p.peakTs = 0; peakAge = 0;
      }
    }
    accelMotion.state = {
      speed: curSpeed, raw_speed: active ? curSpeed : 0, peak: p.peak,
      peak_age_ms: Math.round(peakAge), peak_hold_ms: 2800,
      swipe_min: p.min, swipe_max: p.max, swipe_id: p.swipeId,
      active, dpi: accelDraft?.INPUT_DPI || 1000, source: "pointer", devices: 0,
    };
    renderAccelLiveOverlay();
    if (active || curSpeed > 0 || p.peak > 0) {
      accelMotion.ptrRafId = requestAnimationFrame(_ptrTick);
    }
  }

  function _onPointerMotion(e) {
    if (!accelMotion.ptrOn) return;
    const p = accelMotion.ptr;
    const evs = typeof e.getCoalescedEvents === "function" ? e.getCoalescedEvents() : [e];
    const list = evs.length ? evs : [e];
    let moved = false;
    for (const ev of list) {
      const ts = ev.timeStamp || performance.now();
      const dx = ev.movementX || 0, dy = ev.movementY || 0;
      if (p.lastTs > 0 && (dx !== 0 || dy !== 0)) {
        const dt = ts - p.lastTs;
        if (dt > 0 && dt < 250) {
          const inst = Math.hypot(dx, dy) / dt;
          const now = performance.now();
          const gap = p.lastEv ? (now - p.lastEv) : 1e9;
          if (p.speed <= 0.05 || gap >= 45) p.speed = inst;
          else p.speed += (1 - Math.exp(-Math.max(0.1, dt) / 10)) * (inst - p.speed);
          p.lastEv = now;
          if (p.speed >= 0.35) {
            const isNew = (!p.inSwipe && (!p.lastMot || now - p.lastMot >= 220)) || p.peak <= 0;
            if (isNew) {
              p.swipeId++; p.peak = p.speed; p.peakTs = now; p.min = p.speed; p.max = p.speed; p.inSwipe = true;
            } else {
              p.inSwipe = true;
              if (p.speed > p.peak) p.peak = p.speed;
              p.peakTs = now;
              p.min = Math.min(p.min, p.speed);
              p.max = Math.max(p.max, p.speed);
            }
            p.lastMot = now;
          }
          moved = true;
        }
      }
      p.lastTs = ts;
    }
    if (moved && !accelMotion.ptrRafId) accelMotion.ptrRafId = requestAnimationFrame(_ptrTick);
  }

  function enablePointerFallback(reason) {
    if (accelMotion.ptrOn) return;
    accelMotion.ptrOn = true;
    accelMotion.connected = true;
    accelMotion.ptr = { lastTs: 0, lastEv: 0, lastMot: 0, speed: 0, peak: 0, peakTs: 0, min: 0, max: 0, inSwipe: false, swipeId: 0 };
    accelMotion.state = {
      speed: 0, raw_speed: 0, peak: 0, peak_age_ms: 0, peak_hold_ms: 2800,
      swipe_min: 0, swipe_max: 0, swipe_id: 0, active: false, dpi: accelDraft?.INPUT_DPI || 1000, source: "pointer", devices: 0,
    };
    window.addEventListener("pointermove", _onPointerMotion, { passive: true });
    setAccelLiveBadge("fallback", "Window only", reason || "Fallback: browser pointermove (window-only)");
    scheduleAccelOverlay();
  }

  function disablePointerFallback() {
    if (!accelMotion.ptrOn) return;
    accelMotion.ptrOn = false;
    window.removeEventListener("pointermove", _onPointerMotion);
    if (accelMotion.ptrRafId) { cancelAnimationFrame(accelMotion.ptrRafId); accelMotion.ptrRafId = 0; }
  }

  function shouldStreamMotion() {
    return accelLive?.maccel?.backend?.name !== "rawaccel" && !document.hidden
      && isAccelTabActive()
      && !$("app").hidden
      && !$("accelBody").hidden
      && !!accelDraft;
  }

  function stopAccelMotion() {
    if (!accelMotion) return;
    clearTimeout(accelMotion.statusTimer);
    if (accelMotion.retryTimer) { clearTimeout(accelMotion.retryTimer); accelMotion.retryTimer = null; }
    if (accelMotion.es) { accelMotion.es.close(); accelMotion.es = null; }
    disablePointerFallback();
    if (accelMotion.rafId) { cancelAnimationFrame(accelMotion.rafId); accelMotion.rafId = 0; }
    accelMotion.connected = false;
    accelMotion.state.speed = 0;
    accelMotion.state.active = false;
    setAccelLiveBadge("", "Idle", "Live telemetry pauses when leaving the Acceleration tab");
    renderAccelLiveOverlay();
  }

  function startAccelMotion() {
    if (!accelMotion || !shouldStreamMotion()) return;
    if (accelMotion.es) return;
    if (accelMotion.retryTimer) { clearTimeout(accelMotion.retryTimer); accelMotion.retryTimer = null; }
    if (!accelMotion.ptrOn) setAccelLiveBadge("", "Connecting…", "Connecting to local helper motion stream");
    const es = openHelperMotionStream();
    accelMotion.es = es;
    es.onmessage = (ev) => {
      if (accelMotion.es !== es) return;
      let data;
      try { data = JSON.parse(ev.data); } catch { return; }
      if (data.source === "none") {
        enablePointerFallback("Calibrated motion unavailable. Showing approximate browser movement.");
        return;
      }
      disablePointerFallback();
      accelMotion.connected = true;
      accelMotion.state = data;
      const devs = data.devices || 0;
      const mod = ACCEL_MODULES.includes(data.backend) ? data.backend : "kernel";
      if (data.source === "maccel+evdev") {
        setAccelLiveBadge("on", `Live · ${mod}`, `Latest global speed from /dev/${mod === "kernel" ? "maccel" : mod}, sampled on movement from ${devs} evdev device(s). Short peaks may be missed.`);
      } else if (data.source === "evdev") {
        setAccelLiveBadge("on", "Live · evdev", `Raw evdev mouse motion (${devs} device${devs === 1 ? "" : "s"})`);
      } else if (data.source === "maccel") {
        setAccelLiveBadge("", "Last sample", `${mod === "kernel" ? "The kernel module" : mod} provides one global value without a sample timestamp.`);
      }
      scheduleAccelOverlay();
    };
    es.onerror = () => {
      if (accelMotion.es !== es) return;
      es.close();
      accelMotion.es = null;
      if (!shouldStreamMotion()) return;
      enablePointerFallback("Helper motion stream unavailable — using window-only pointermove fallback");
      accelMotion.retryTimer = setTimeout(() => {
        accelMotion.retryTimer = null;
        if (shouldStreamMotion() && !accelMotion.es) startAccelMotion();
      }, 10000);
    };
  }

  function syncAccelMotion() {
    if (!accelMotion) return;
    if (shouldStreamMotion()) startAccelMotion();
    else stopAccelMotion();
  }

  function renderAccelDpiHint() {
    const b = $("accelUseMouseDpi"); if (!b || !accelDraft) return;
    const mdpi = getActiveDpi();
    b.hidden = !mdpi || mdpi === accelDraft.INPUT_DPI || !accelLive?.maccel?.writable;
    if (mdpi) b.textContent = `Use mouse DPI (${mdpi})`;
  }

  function _syncLutNumericInputs() {
    if (!isLutMode() || !accelDraft) return;
    const lut = ensureDraftLutPoints();
    const pt = lut[selectedLutIdx];
    const countEl = $("lutPointCount");
    if (countEl) countEl.textContent = `${lut.length} / ${LUT_MAX_POINTS}`;
    const offEl = $("lutOffAxisTag");
    if (offEl) {
      const offCount = lut.filter(([x]) => x > accelXMax).length;
      offEl.hidden = offCount === 0;
      if (offCount > 0) offEl.textContent = `${offCount} > ${accelXMax} c/ms`;
    }
    const selEl = $("lutSelLabel");
    if (selEl) selEl.textContent = `Pt ${selectedLutIdx + 1}`;
    const xIn = $("lutPtX");
    const yIn = $("lutPtY");
    if (pt && xIn && yIn) {
      if (document.activeElement !== xIn) xIn.value = _fmtX(pt[0]);
      if (document.activeElement !== yIn) yIn.value = _fmtY(pt[1]);
    }
    const addBtn = $("lutAddPt");
    if (addBtn) addBtn.disabled = lut.length >= LUT_MAX_POINTS;
    const delBtn = $("lutDelPt");
    if (delBtn) delBtn.disabled = lut.length <= LUT_MIN_POINTS;
    const presetRow = $("lutPresets");
    if (presetRow) {
      const activePreset = matchingLutPresetId(lut);
      for (const b of presetRow.querySelectorAll("button[data-preset]")) {
        b.classList.toggle("sel", b.dataset.preset === activePreset);
      }
    }
  }

  function _autoFitLutXMax() {
    if (!isLutMode()) return;
    const lut = ensureDraftLutPoints();
    const maxPtX = lut[lut.length - 1]?.[0] ?? 0;
    if (maxPtX > accelXMax) {
      const nextMax = maxPtX <= 40 ? 40 : 80;
      if (nextMax !== accelXMax) {
        accelXMax = nextMax;
        for (const x of $("accelXMax").children) x.classList.toggle("sel", +x.dataset.x === accelXMax);
      }
    }
  }

  function renderAccelParams() {
    if (!accelDraft) return;
    const mode = accelDraft.MODE || "linear";
    for (const b of $("accelMode").children) b.classList.toggle("sel", b.dataset.mode === mode);
    $("accelModeDesc").textContent = MODE_DESCS[mode] ?? "";
    const ro = !accelLive?.maccel?.writable;
    const list = [...(MODE_PARAMS[mode] ?? []), ...GLOBAL_ACCEL_PARAMS];
    const wrap = $("accelParams");
    wrap.innerHTML = "";
    renderLutNotice();

    if (mode === "lookup") {
      const lut = ensureDraftLutPoints();
      const pt = lut[selectedLutIdx] ?? lut[0];
      const ptRow = document.createElement("div");
      ptRow.className = "setting";
      ptRow.id = "lutPointRow";
      ptRow.innerHTML = `
        <div class="label">
          <b>Control points<span class="lut-count" id="lutPointCount">${lut.length} / ${LUT_MAX_POINTS}</span> <span class="lut-offaxis" id="lutOffAxisTag" hidden></span></b>
          <p>Click graph to add points · drag to shape · right-click or Delete to remove</p>
        </div>
        <div class="control">
          <span class="lut-sel-tag" id="lutSelLabel">Pt ${selectedLutIdx + 1}</span>
          <label class="lut-field"><span>Speed</span><input type="number" id="lutPtX" min="${LUT_X_MIN}" max="${LUT_X_MAX}" step="0.1" value="${_fmtX(pt[0])}" aria-label="Selected point speed (counts/ms)"><span class="lut-unit">c/ms</span></label>
          <label class="lut-field"><span>Mult</span><input type="number" id="lutPtY" min="${LUT_Y_MIN}" max="${LUT_Y_MAX}" step="0.02" value="${_fmtY(pt[1])}" aria-label="Selected point sensitivity multiplier"><span class="lut-unit">×</span></label>
          <button type="button" id="lutAddPt" title="Insert a new control point">+ Add</button>
          <button type="button" class="ghost danger" id="lutDelPt" title="Remove selected point (Delete)">Remove</button>
        </div>`;
      wrap.appendChild(ptRow);

      const presetRow = document.createElement("div");
      presetRow.className = "setting";
      presetRow.id = "lutPresetRow";
      const activePreset = matchingLutPresetId(lut);
      const presetBtns = LUT_PRESETS.map(
        (p) => `<button type="button" data-preset="${p.id}" class="${p.id === activePreset ? "sel" : ""}">${escHtml(p.label)}</button>`
      ).join("");
      presetRow.innerHTML = `
        <div class="label">
          <b>Curve presets</b>
          <p>Start from a shape and sculpt it directly on the graph.</p>
        </div>
        <div class="control">
          <div class="lut-presets" id="lutPresets">
            ${presetBtns}
            <button type="button" class="ghost" id="lutResetCurve">Reset</button>
          </div>
        </div>`;
      wrap.appendChild(presetRow);

      const xIn = $("lutPtX");
      const yIn = $("lutPtY");
      const applyNumeric = () => {
        const vx = parseFloat(xIn.value);
        const vy = parseFloat(yIn.value);
        if (!Number.isFinite(vx) && !Number.isFinite(vy)) return;
        const curPt = ensureDraftLutPoints()[selectedLutIdx];
        if (!curPt) return;
        accelDraft.LUT_POINTS = moveLutPoint(
          accelDraft.LUT_POINTS,
          selectedLutIdx,
          Number.isFinite(vx) ? vx : curPt[0],
          Number.isFinite(vy) ? vy : curPt[1],
          { snap: false, minGap: LUT_DX_MIN, maxX: LUT_X_MAX }
        );
        _autoFitLutXMax();
        renderAccelCurve();
        _syncLutNumericInputs();
        renderAccelDirty();
      };
      xIn.addEventListener("input", applyNumeric);
      yIn.addEventListener("input", applyNumeric);
      xIn.addEventListener("change", () => _syncLutNumericInputs());
      yIn.addEventListener("change", () => _syncLutNumericInputs());
      xIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && accelDirty() && canApplyCurrentMode()) $("accelApply").click(); });
      yIn.addEventListener("keydown", (e) => { if (e.key === "Enter" && accelDirty() && canApplyCurrentMode()) $("accelApply").click(); });

      $("lutAddPt").addEventListener("click", () => {
        const res = addMidpointLutPoint(ensureDraftLutPoints(), selectedLutIdx, accelXMax);
        if (!res.added) {
          toast(res.reason === "max" ? `Maximum ${LUT_MAX_POINTS} control points reached` : "No room for another point here", true);
          return;
        }
        accelDraft.LUT_POINTS = res.points;
        selectedLutIdx = res.index;
        renderAccelCurve();
        _syncLutNumericInputs();
        renderAccelDirty();
      });

      $("lutDelPt").addEventListener("click", () => {
        const res = removeLutPoint(ensureDraftLutPoints(), selectedLutIdx);
        if (!res.removed) {
          toast(`Custom curve needs at least ${LUT_MIN_POINTS} points`);
          return;
        }
        accelDraft.LUT_POINTS = res.points;
        selectedLutIdx = res.index;
        renderAccelCurve();
        _syncLutNumericInputs();
        renderAccelDirty();
      });

      $("lutPresets").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-preset]");
        if (!btn) return;
        const pts = getLutPreset(btn.dataset.preset);
        if (!pts) return;
        accelDraft.LUT_POINTS = pts;
        selectedLutIdx = Math.min(2, pts.length - 1);
        _autoFitLutXMax();
        renderAccelCurve();
        _syncLutNumericInputs();
        renderAccelDirty();
      });

      $("lutResetCurve").addEventListener("click", () => {
        accelDraft.LUT_POINTS = cloneLutPoints(LUT_STARTER_POINTS);
        selectedLutIdx = Math.min(2, accelDraft.LUT_POINTS.length - 1);
        renderAccelCurve();
        _syncLutNumericInputs();
        renderAccelDirty();
      });

      _syncLutNumericInputs();
    }

    for (const p of list) {
      const val = accelDraft[p.k] ?? MACCEL_DEFAULTS[p.k];
      const row = document.createElement("div");
      row.className = "setting";
      const syncBtn = p.k === "INPUT_DPI" ? `<button class="ghost" id="accelUseMouseDpi" hidden>Use mouse DPI</button>` : "";
      row.innerHTML = `
        <div class="label"><b>${p.label}</b><p>${p.desc}</p></div>
        <div class="control">
          ${syncBtn}
          <input type="range" min="${p.min}" max="${p.max}" step="${p.step}" value="${Math.max(p.min, Math.min(p.max, val))}" data-pk="${p.k}" ${ro ? "disabled" : ""} aria-label="${p.label} slider">
          <input type="number" step="${p.step}" value="${val}" data-nk="${p.k}" ${ro ? "disabled" : ""} aria-label="${p.label}">
        </div>`;
      const rng = row.querySelector('input[type="range"]');
      const num = row.querySelector('input[type="number"]');
      const setVal = (v) => {
        if (!Number.isFinite(v)) return;
        accelDraft[p.k] = v;
        rng.value = Math.max(p.min, Math.min(p.max, v));
        paintRanges();
        renderAccelCurve();
        renderAccelDirty();
      };
      rng.addEventListener("input", () => { const v = parseFloat(rng.value); num.value = v; setVal(v); });
      num.addEventListener("input", () => { const v = parseFloat(num.value); if (Number.isFinite(v)) setVal(v); });
      num.addEventListener("keydown", (e) => { if (e.key === "Enter" && accelDirty() && canApplyCurrentMode()) $("accelApply").click(); });
      wrap.appendChild(row);
    }
    const sb = $("accelUseMouseDpi");
    if (sb) sb.addEventListener("click", () => {
      const mdpi = getActiveDpi();
      if (!mdpi) return;
      accelDraft.INPUT_DPI = mdpi;
      renderAccelParams(); renderAccelCurve(); renderAccelDirty();
    });
    renderAccelDpiHint();
    paintRanges();
  }

  function renderDesktop() {
    const n = accelLive?.desktop;
    $("desktopGroup").hidden = !n;
    if (!n) return;
    $("desktopHeading").textContent = `Desktop pointer settings (${n.label})`;
    const other = n.accel_profile && !["flat", "adaptive"].includes(n.accel_profile);
    $("desktopNote").textContent = n.message + (other ? ` · Current profile: ${n.accel_profile}` : "");
    for (const row of $("desktopGroup").querySelectorAll(".setting")) row.hidden = n.accel_speed == null;
    if (n.accel_speed == null) return;
    for (const b of $("desktopProfile").children) {
      b.disabled = !n.writable || desktopSaving;
      b.classList.toggle("sel", b.dataset.prof === n.accel_profile);
      b.setAttribute("aria-pressed", String(b.dataset.prof === n.accel_profile));
    }
    $("desktopSpeed").disabled = !n.writable || desktopSaving;
    $("desktopSpeed").value = n.accel_speed;
    $("desktopSpeedVal").textContent = n.accel_speed.toFixed(2);
    paintRanges();
  }

  function renderAccelSetup(setup, m) {
    const box = $("accelSetup");
    if (!box) return;
    if (!setup || setup.ready) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    const isWin = setup.platform === "windows";
    const title = isWin ? "Set up Raw Accel on Windows" : "Set up mouse acceleration";
    const intro = isWin
      ? "Follow these steps once so Mouse Tool can apply your acceleration curve across Windows and games."
      : m?.loaded && !m?.writable
      ? "Your acceleration driver is running in preview mode. Finish the permission step below to save changes."
      : "Complete these quick steps once to enable custom mouse acceleration in every app and game.";
    const pillClass = setup.ready ? "ok" : "warn";
    const pillText = setup.ready ? "Installed" : "Needs setup";
    const stepsHtml = (setup.steps || []).map((step, i) => {
      const done = step.status === "installed";
      const badgeClass = done ? "ok" : "warn";
      const badgeLabel = done ? "Installed" : "Needs setup";
      const safeUrl = typeof step.url === "string" && step.url.startsWith("https://") ? step.url : null;
      const linkClass = isWin ? "setup-link" : "setup-link secondary";
      const linkHtml = safeUrl
        ? `<div class="setup-actions"><a class="${linkClass}" href="${escHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escHtml(step.url_label || "Download")} ↗</a></div>`
        : "";
      const cmdHtml = step.command
        ? `<div class="setup-cmd"><code>${escHtml(step.command)}</code><button type="button" class="setup-copy" data-copy="${escHtml(step.command)}">Copy</button></div>`
        : "";
      return `
        <li class="setup-step${done ? " done" : ""}">
          <span class="setup-num" aria-hidden="true">${done ? "✓" : i + 1}</span>
          <div class="setup-step-body">
            <div class="setup-step-top">
              <span class="setup-step-title">${escHtml(step.title)}</span>
              <span class="setup-pill ${badgeClass}">${badgeLabel}</span>
            </div>
            <p class="setup-step-desc">${escHtml(step.description)}</p>
            ${linkHtml}
            ${cmdHtml}
          </div>
        </li>`;
    }).join("");
    box.innerHTML = `
      <div class="setup-head">
        <h3 class="setup-title">${escHtml(title)}</h3>
        <span class="setup-pill ${pillClass}">${pillText}</span>
        <div class="spacer"></div>
        <button type="button" id="accelSetupCheck">Check again</button>
      </div>
      <p class="setup-intro">${escHtml(intro)}</p>
      <ol class="setup-steps">${stepsHtml}</ol>`;
    box.hidden = false;
    const checkBtn = $("accelSetupCheck");
    if (checkBtn) {
      checkBtn.addEventListener("click", () => loadAccel(true));
    }
  }

  function renderAccelBackend(m) {
    const el = $("accelBackend");
    if (!el) return;
    const b = m ? accelBackendInfo(m) : null;
    const sub = $("accelSubtitle") || el.nextElementSibling;
    el.classList.toggle("legacy", !!(b?.loaded && b.legacy));
    el.classList.toggle("none", !b?.loaded);
    $("accelBackendName").textContent = !m ? "mouseaccel / maccel" : (b.loaded ? (b.label || b.name) : "Needs setup");
    if (b?.name === "rawaccel") {
      $("accelBackendName").textContent = m.loaded ? b.label : "Raw Accel · Needs setup";
      el.title = "Raw Accel driver: " + (m.driver || "missing");
      $("accelHeading").textContent = "Mouse acceleration";
      if (sub) sub.textContent = "Applies your curve across Windows and games with Raw Accel";
      const showWinBar = !!m.loaded;
      $("windowsAccel").hidden = !showWinBar;
      $("windowsReset").disabled = !m.loaded;
      if (showWinBar) {
        $("windowsAccel").firstElementChild.textContent = [
          "Raw Accel running", m.saved_state, m.last_apply, m.message,
        ].filter(Boolean).join(" · ");
      }
      if ($("accelTechInfo")) {
        $("accelTechInfo").textContent = `Driver: ${m.driver || "unknown"} · ${m.saved_state || "No saved curve"}`;
      }
      $("accelLiveBadge").parentElement.hidden = true;
      for (const marker of document.querySelectorAll(".legend .live, .legend .peak")) {
        const legend = marker.closest(".legend");
        if (legend) legend.hidden = true;
      }
      return;
    }
    $("windowsAccel").hidden = true;
    $("accelHeading").textContent = "Mouse acceleration";
    if (sub) sub.textContent = "Smoothly adjusts pointer speed everywhere, including games";
    if ($("accelTechInfo")) {
      const pathHint = b?.loaded && b.params_path
        ? `Active driver: ${b.label || b.name} (${b.params_path}). Export your current curve for Windows Raw Accel v1.7.1:`
        : "Export your current curve for Windows Raw Accel v1.7.1:";
      $("accelTechInfo").textContent = pathHint;
    }
    el.title = !m ? "Mouse acceleration driver (helper offline)"
      : b.loaded ? `Module ${b.name}${b.legacy ? " (legacy; mouseaccel replaces it)" : ""}\nParameters: ${b.params_path || "?"}\nSpeed device: ${b.speed_device || "?"}`
      : "No mouse acceleration driver is running yet";
  }

  async function loadAccel(isManualRecheck = false) {
    if (accelLoading) return;
    accelLoading = true;
    const checkBtn = $("accelSetupCheck");
    if (checkBtn) { checkBtn.disabled = true; checkBtn.textContent = "Checking…"; }
    try {
      const { res, data } = await fetchHelperStatus();
      if (!res.ok) throw new Error("Status unavailable");
      const dirty = accelDirty();
      accelLive = data;
      clearTimeout(accelMotion.statusTimer);
      if (Array.isArray(data.system_mice)) {
        onSystemMice(data.system_mice);
      }
      $("accelOffline").hidden = true;
      const m = data.accel ?? data.maccel;
      accelLive.maccel = m;
      renderAccelBackend(m);
      const b = accelBackendInfo(m);
      const setup = deriveSetupStatus(data);
      renderAccelSetup(setup, m);
      const keepDraft = dirty && accelDraftBackend === b.name;
      accelDraftBackend = b.name;
      if (!m?.loaded || m.parameters_available === false) {
        accelDraft = null;
        renderAccelDirty();
        $("accelBody").hidden = true;
        $("accelPerm").hidden = true;
      } else {
        $("accelBody").hidden = false;
        $("accelPerm").hidden = true;
        if (!keepDraft) {
          accelDraft = accelSnapshot(m);
          selectedLutIdx = Math.min(2, ensureDraftLutPoints().length - 1);
          _autoFitLutXMax();
        }
        renderAccelParams();
        renderAccelCurve();
        renderAccelDirty();
      }
      renderDesktop();
      if (isManualRecheck) {
        toast(setup?.ready ? "Acceleration setup is ready" : `Checked — ${setup?.summary || "still needs setup"}`);
      }
    } catch {
      $("accelOffline").hidden = false;
      $("accelPerm").hidden = true;
      if ($("accelSetup")) $("accelSetup").hidden = true;
      const windows = accelLive?.maccel?.backend?.name === "rawaccel";
      if (windows) {
        $("windowsAccel").hidden = true;
        $("accelBackendName").textContent = "Raw Accel (Windows) · Offline";
        $("accelBackend").classList.add("none");
        $("accelBackend").title = "Driver state unavailable";
        $("accelOffline").firstElementChild.textContent = "Background helper is offline. Open mousetool from Start and check again.";
      } else {
        renderAccelBackend(null);
      }
      if (accelLive?.maccel) accelLive.maccel.writable = false;
      accelDraft ??= { ...MACCEL_DEFAULTS, LUT_POINTS: cloneLutPoints(LUT_STARTER_POINTS) };
      $("accelBody").hidden = false;
      renderAccelParams();
      renderAccelCurve();
      renderAccelDirty();
      clearTimeout(accelMotion.statusTimer);
      if (shouldStreamMotion()) accelMotion.statusTimer = setTimeout(loadAccel, 10000);
      $("desktopGroup").hidden = true;
      if (isManualRecheck) toast("Helper is still offline", true);
    } finally {
      accelLoading = false;
      const btn = $("accelSetupCheck");
      if (btn) { btn.disabled = false; btn.textContent = "Check again"; }
      syncAccelMotion();
    }
  }

  async function postDesktop(updates) {
    if (desktopSaving || !accelLive?.desktop?.writable) return;
    desktopSaving = true;
    renderDesktop();
    setState("desktop", "saving");
    try {
      const { res, data } = await postHelperDesktop(updates);
      if (data.desktop) accelLive.desktop = data.desktop;
      if (!res.ok) throw new Error(data.error || "Failed to update desktop settings");
      setState("desktop", "saved");
    } catch (err) {
      setState("desktop", "error", err.message);
      toast(err.message, true);
    } finally {
      desktopSaving = false;
      renderDesktop();
    }
  }

  function _svgPointerToCurve(e, g) {
    const svg = $("accelSvg");
    const rect = svg.getBoundingClientRect();
    const sx = rect.width > 0 ? ((e.clientX - rect.left) / rect.width) * g.W : g.L;
    const sy = rect.height > 0 ? ((e.clientY - rect.top) / rect.height) * g.H : g.T;
    const rawX = ((sx - g.L) / g.PW) * accelXMax;
    const plotY = g.yMin + (1 - (sy - g.T) / g.PH) * (g.yMax - g.yMin);
    const sensMult = Math.max(0.01, accelDraft?.SENS_MULT ?? 1);
    const rawY = plotY / sensMult;
    const inPlot = sx >= g.L - 6 && sx <= g.W - g.R + 6 && sy >= g.T - 6 && sy <= g.H - g.B + 6;
    return { sx, sy, rawX, rawY, inPlot };
  }

  function _finishLutDrag(pointerId, commit) {
    if (!lutDrag) return;
    const finished = lutDrag;
    lutDrag = null;
    const svg = $("accelSvg");
    try { svg.releasePointerCapture?.(pointerId); } catch {}
    svg.classList.remove("is-dragging");
    if (!commit) {
      accelDraft.LUT_POINTS = finished.startPoints;
      selectedLutIdx = Math.min(finished.idx, accelDraft.LUT_POINTS.length - 1);
    }
    renderAccelCurve();
    _syncLutNumericInputs();
    renderAccelDirty();
    if (finished.focusAfter) {
      svg.querySelector(`.lut-pt[data-lut-idx="${selectedLutIdx}"]`)?.focus?.({ preventScroll: true });
    }
  }

  // Wire event listeners for acceleration & desktop pointer panels
  document.addEventListener("visibilitychange", () => {
    if (shouldStreamMotion() && !$("accelOffline").hidden) loadAccel();
    else syncAccelMotion();
  });
  window.addEventListener("pagehide", () => stopAccelMotion());

  $("accelSvg").addEventListener("pointerdown", (e) => {
    if (!isLutMode() || !accelDraft) return;
    const svg = $("accelSvg");
    const g = svg._geom;
    if (!g) return;
    const ptEl = e.target?.closest?.(".lut-pt[data-lut-idx]");
    if (ptEl) {
      if (typeof e.button === "number" && e.button !== 0) return;
      const idx = Number(ptEl.dataset.lutIdx);
      const lut = ensureDraftLutPoints();
      if (!Number.isInteger(idx) || idx < 0 || idx >= lut.length) return;
      e.preventDefault();
      selectedLutIdx = idx;
      lutDrag = {
        idx,
        pointerId: e.pointerId,
        geom: { ...g },
        startPoints: cloneLutPoints(lut),
        moved: false,
        focusAfter: true,
      };
      renderAccelCurve();
      _syncLutNumericInputs();
      svg.querySelector(`.lut-pt[data-lut-idx="${idx}"]`)?.focus?.({ preventScroll: true });
      try { svg.setPointerCapture?.(e.pointerId); } catch {}
      svg.classList.add("is-dragging");
      _svgShow($("accelCross"), false);
      _svgShow($("accelDot"), false);
      return;
    }

    if (typeof e.button === "number" && e.button !== 0) return;
    const { rawX, rawY, inPlot } = _svgPointerToCurve(e, g);
    if (!inPlot) return;
    e.preventDefault();
    const prevPoints = cloneLutPoints(ensureDraftLutPoints());
    const res = insertLutPoint(prevPoints, rawX, rawY, {
      shiftKey: e.shiftKey,
      maxX: accelXMax,
      minGap: LUT_DRAG_DX_MIN,
    });
    if (!res.added) {
      if (res.reason === "max") toast(`Maximum ${LUT_MAX_POINTS} control points reached`, true);
      else if (res.index >= 0) {
        selectedLutIdx = res.index;
        renderAccelCurve();
        _syncLutNumericInputs();
      }
      return;
    }
    accelDraft.LUT_POINTS = res.points;
    selectedLutIdx = res.index;
    lutDrag = {
      idx: res.index,
      pointerId: e.pointerId,
      geom: { ...g },
      startPoints: prevPoints,
      moved: true,
      focusAfter: true,
    };
    renderAccelCurve();
    _syncLutNumericInputs();
    renderAccelDirty();
    svg.querySelector(`.lut-pt[data-lut-idx="${res.index}"]`)?.focus?.({ preventScroll: true });
    try { svg.setPointerCapture?.(e.pointerId); } catch {}
    svg.classList.add("is-dragging");
    _svgShow($("accelCross"), false);
    _svgShow($("accelDot"), false);
  });

  $("accelSvg").addEventListener("pointermove", (e) => {
    if (!lutDrag || e.pointerId !== lutDrag.pointerId) return;
    const { rawX, rawY } = _svgPointerToCurve(e, lutDrag.geom);
    lutDrag.moved = true;
    accelDraft.LUT_POINTS = moveLutPoint(accelDraft.LUT_POINTS, lutDrag.idx, rawX, rawY, {
      shiftKey: e.shiftKey,
      maxX: accelXMax,
      minGap: LUT_DRAG_DX_MIN,
    });
    _updateLutDragVisuals();
    _syncLutNumericInputs();
    renderAccelDirty();
  });

  $("accelSvg").addEventListener("pointerup", (e) => {
    if (!lutDrag || e.pointerId !== lutDrag.pointerId) return;
    _finishLutDrag(e.pointerId, true);
  });

  $("accelSvg").addEventListener("pointercancel", (e) => {
    if (!lutDrag || e.pointerId !== lutDrag.pointerId) return;
    _finishLutDrag(e.pointerId, false);
  });

  $("accelSvg").addEventListener("contextmenu", (e) => {
    if (!isLutMode() || !accelDraft) return;
    const ptEl = e.target?.closest?.(".lut-pt[data-lut-idx]");
    if (!ptEl) return;
    e.preventDefault();
    const idx = Number(ptEl.dataset.lutIdx);
    const res = removeLutPoint(ensureDraftLutPoints(), idx);
    if (!res.removed) {
      toast(`Custom curve needs at least ${LUT_MIN_POINTS} points`);
      return;
    }
    accelDraft.LUT_POINTS = res.points;
    selectedLutIdx = res.index;
    renderAccelCurve();
    _syncLutNumericInputs();
    renderAccelDirty();
  });

  $("accelSvg").addEventListener("keydown", (e) => {
    if (!isLutMode() || !accelDraft) return;
    const ptEl = e.target?.closest?.(".lut-pt[data-lut-idx]");
    if (!ptEl) return;
    const idx = Number(ptEl.dataset.lutIdx);
    const lut = ensureDraftLutPoints();
    if (!Number.isInteger(idx) || idx < 0 || idx >= lut.length) return;
    selectedLutIdx = idx;

    if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      const res = removeLutPoint(lut, idx);
      if (!res.removed) {
        toast(`Custom curve needs at least ${LUT_MIN_POINTS} points`);
        return;
      }
      accelDraft.LUT_POINTS = res.points;
      selectedLutIdx = res.index;
      renderAccelCurve();
      _syncLutNumericInputs();
      renderAccelDirty();
      $("accelSvg").querySelector(`.lut-pt[data-lut-idx="${selectedLutIdx}"]`)?.focus?.({ preventScroll: true });
      return;
    }

    const stepX = e.shiftKey ? 0.05 : 0.5;
    const stepY = e.shiftKey ? 0.01 : 0.05;
    let [nx, ny] = lut[idx];
    if (e.key === "ArrowLeft") nx -= stepX;
    else if (e.key === "ArrowRight") nx += stepX;
    else if (e.key === "ArrowUp") ny += stepY;
    else if (e.key === "ArrowDown") ny -= stepY;
    else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      renderAccelCurve();
      _syncLutNumericInputs();
      return;
    } else {
      return;
    }

    e.preventDefault();
    accelDraft.LUT_POINTS = moveLutPoint(lut, idx, nx, ny, {
      snap: false,
      minGap: LUT_DX_MIN,
      maxX: Math.max(accelXMax, lut[lut.length - 1][0]),
    });
    renderAccelCurve();
    _syncLutNumericInputs();
    renderAccelDirty();
    $("accelSvg").querySelector(`.lut-pt[data-lut-idx="${idx}"]`)?.focus?.({ preventScroll: true });
  });

  $("accelSvg").addEventListener("mousemove", (e) => {
    if (lutDrag) return;
    const g = $("accelSvg")._geom; if (!g || !accelDraft) return;
    const rect = $("accelSvg").getBoundingClientRect();
    const sx = ((e.clientX - rect.left) / rect.width) * g.W;
    const xv = Math.max(isLutMode() ? 0 : 0.05, Math.min(accelXMax, ((sx - g.L) / g.PW) * accelXMax));
    const [vx, vy] = maccelEval(xv, accelDraft);
    const cx = g.px(xv), cy = g.py(vx);
    const cross = $("accelCross"), dot = $("accelDot");
    if (cross && dot) {
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); _svgShow(cross, true);
      dot.setAttribute("cx", cx); dot.setAttribute("cy", cy); _svgShow(dot, true);
    }
    $("accelProbe").textContent = `${xv.toFixed(1)} counts/ms → ${vx.toFixed(3)}×` + (Math.abs(vx - vy) > 1e-4 ? ` (Y: ${vy.toFixed(3)}×)` : "");
  });
  $("accelSvg").addEventListener("mouseleave", () => {
    if (lutDrag) return;
    _svgShow($("accelCross"), false);
    _svgShow($("accelDot"), false);
    $("accelProbe").textContent = isLutMode()
      ? "Click graph to add points · drag handles to shape"
      : "Hover curve to inspect speed → multiplier";
  });

  $("accelRefresh").addEventListener("click", () => { accelDraft = null; loadAccel(false); });
  $("accelRetry").addEventListener("click", () => loadAccel(true));
  $("accelMode").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || !accelDraft) return;
    const nextMode = b.dataset.mode;
    if (!accelLive?.maccel?.writable && nextMode !== "lookup" && accelDraft.MODE !== "lookup") return;
    accelDraft.MODE = nextMode;
    if (nextMode === "lookup") {
      ensureDraftLutPoints();
      _autoFitLutXMax();
    }
    renderAccelParams(); renderAccelCurve(); renderAccelDirty();
  });
  $("accelXMax").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    accelXMax = +b.dataset.x;
    for (const x of $("accelXMax").children) x.classList.toggle("sel", +x.dataset.x === accelXMax);
    renderAccelCurve();
    _syncLutNumericInputs();
  });
  $("accelRevert").addEventListener("click", () => {
    if (!accelLive?.maccel) return;
    accelDraft = accelSnapshot(accelLive.maccel);
    selectedLutIdx = Math.min(2, ensureDraftLutPoints().length - 1);
    renderAccelParams(); renderAccelCurve(); renderAccelDirty();
  });
  $("windowsReset").addEventListener("click", async () => {
    if (!confirm("Replace the active Raw Accel configuration and saved curve with no acceleration?")) return;
    try {
      const res = await postHelperAccelReset();
      if (!res.ok) throw new Error((await res.json()).error);
      accelDraft = null;
    } catch (err) { toast(err.message, true); }
    await loadAccel();
  });
  $("accelDefaults").addEventListener("click", () => {
    if (!accelLive?.maccel?.writable && !isLutMode()) return;
    accelDraft = { ...MACCEL_DEFAULTS, LUT_POINTS: cloneLutPoints(LUT_STARTER_POINTS) };
    if (accelDraftBackend === "rawaccel") Object.assign(accelDraft, { MODE: "no_accel", ACCEL: 0.005 });
    selectedLutIdx = Math.min(2, accelDraft.LUT_POINTS.length - 1);
    renderAccelParams(); renderAccelCurve(); renderAccelDirty();
  });
  $("accelApply").addEventListener("click", async () => {
    if (!accelDraftBackend || !accelDraft || !canApplyCurrentMode()) return;
    const backend = accelDraftBackend;
    const payload = Object.fromEntries(
      Object.keys(MACCEL_DEFAULTS).map((k) => [k, accelDraft[k]])
    );
    if (accelDraft.MODE === "lookup") {
      payload.LUT_POINTS = cloneLutPoints(ensureDraftLutPoints());
    }
    setState("maccel", "saving");
    try {
      const { res, data } = await postHelperAccel(backend, payload);
      if (!res.ok) {
        if (res.status === 409) await loadAccel();
        throw new Error(data.error || "Failed to save curve");
      }
      const st = data.accel ?? data.maccel;
      accelLive.maccel = st;
      renderAccelBackend(st);
      accelDraftBackend = accelBackendInfo(st).name;
      accelDraft = accelSnapshot(st);
      selectedLutIdx = Math.min(selectedLutIdx, ensureDraftLutPoints().length - 1);
      renderAccelParams(); renderAccelCurve(); renderAccelDirty();
      setState("maccel", "saved");
      toast("Acceleration curve saved");
    } catch (err) {
      if (backend === "rawaccel") await loadAccel();
      setState("maccel", "error", err.message);
      toast(err.message, true);
    }
  });

  $("desktopProfile").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    const prof = b.dataset.prof;
    if (accelLive?.desktop?.accel_profile === prof) return;
    postDesktop({ accel_profile: prof });
  });
  $("desktopSpeed").addEventListener("input", () => { $("desktopSpeedVal").textContent = (+$("desktopSpeed").value).toFixed(2); });
  $("desktopSpeed").addEventListener("change", () => { postDesktop({ accel_speed: +$("desktopSpeed").value }); });

  return {
    loadAccel,
    syncAccelMotion,
    stopAccelMotion,
    renderAccelDpiHint,
  };
}

export {
  MACCEL_DEFAULTS,
  MODE_DESCS,
  MODE_PARAMS,
  GLOBAL_ACCEL_PARAMS,
  ACCEL_MODULES,
  maccelEval,
  accelSnapshot,
  accelBackendInfo,
  accelModuleCode,
  deriveSetupStatus,
  createAccelController,
};
