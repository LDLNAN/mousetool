// Logarithmic DPI stage track: rendering, pointer-capture dragging & keyboard slider control

function dpiToLogPct(v, min, max) {
  const lo = Math.max(1, min);
  const hi = Math.max(lo + 1, max);
  const clamped = Math.max(lo, Math.min(hi, v));
  return ((Math.log(clamped) - Math.log(lo)) / Math.max(0.01, Math.log(hi) - Math.log(lo))) * 100;
}

function snapDpiValue(raw, { min, max, step = 50, stepHi = null, clampFn = null }) {
  const lo = Math.max(1, min);
  const hi = Math.max(lo, max);
  const bounded = Math.max(lo, Math.min(hi, raw));
  if (typeof clampFn === "function") {
    return Math.max(lo, Math.min(hi, clampFn(Math.round(bounded))));
  }
  const s = (stepHi && stepHi !== step && bounded >= 10000) ? stepHi : Math.max(1, step || 50);
  const snapped = Math.round(bounded / s) * s;
  return Math.max(lo, Math.min(hi, snapped));
}

function logPctToDpi(pct, opts) {
  const lo = Math.max(1, opts.min);
  const hi = Math.max(lo + 1, opts.max);
  const t = Math.max(0, Math.min(1, pct / 100));
  const raw = lo * Math.pow(hi / lo, t);
  return snapDpiValue(raw, opts);
}

function createDpiTrackController({
  trackEl,
  getConfig,
  getStages,
  getLiveDpi,
  onPreviewDpi,
  onSelectStage,
  onCommitStages,
  onRevertDrag,
}) {
  let drag = null;
  let kbdTimer = null;
  let kbdDirty = false;

  function isDragging() {
    return !!(drag && drag.moved);
  }

  function isInteracting() {
    return drag !== null;
  }

  function _markText(idx, dpi) {
    return `Stage ${idx + 1}: ${dpi} DPI (drag to adjust)`;
  }

  function render() {
    if (!trackEl || drag) return;
    const focusedIdx = trackEl.contains?.(trackEl.ownerDocument?.activeElement)
      ? trackEl.ownerDocument.activeElement?.dataset?.stageIdx
      : null;
    const cfg = getConfig();
    if (!cfg) { trackEl.innerHTML = ""; return; }
    const { min, max, canWrite } = cfg;
    const st = getStages() || { active: 1, list: [] };
    const ticks = [min, 400, 800, 1600, 3200, 6400, 12800, max]
      .filter((t, i, a) => t >= min && t <= max && a.indexOf(t) === i);
    let h = '<div class="rail" aria-hidden="true"></div>';
    h += ticks.map((t, i) =>
      `<span class="tl" aria-hidden="true" style="left:${dpiToLogPct(t, min, max)}%"></span>` +
      `<span class="tick${[400, 1600, 6400, 12800].includes(t) ? " minor" : ""}${i === 0 ? " first" : i === ticks.length - 1 ? " last" : ""}" aria-hidden="true" style="left:${dpiToLogPct(t, min, max)}%">${t >= 1000 ? t / 1000 + "k" : t}</span>`
    ).join("");
    const dpi = getLiveDpi?.();
    if (dpi) {
      h += `<span class="live" aria-hidden="true" style="left:${dpiToLogPct(dpi[0], min, max)}%" title="Current: ${dpi[0]}"></span>`;
    }
    st.list.forEach((stage, i) => {
      const val = snapDpiValue(stage.x, cfg);
      const pct = dpiToLogPct(val, min, max);
      const active = st.active === i + 1;
      h += `<span class="mark${active ? " active" : ""}${canWrite ? " draggable" : ""}" ` +
        `role="slider" tabindex="0" ` +
        `aria-label="Stage ${i + 1} DPI" ` +
        `aria-valuemin="${min}" aria-valuemax="${max}" ` +
        `aria-valuenow="${val}" aria-valuetext="${val} DPI" ` +
        `aria-disabled="${canWrite ? "false" : "true"}" ` +
        `data-stage-idx="${i}" data-dpi="${val}" ` +
        `style="left:${pct}%" ` +
        `title="${_markText(i, val)}">${i + 1}</span>`;
    });
    trackEl.innerHTML = h;
    if (focusedIdx != null) {
      trackEl.querySelector?.(`.mark[data-stage-idx="${focusedIdx}"]`)?.focus?.({ preventScroll: true });
    }
  }

  function _updateMarkVisual(mark, stageIdx, dpi, cfg) {
    const pct = dpiToLogPct(dpi, cfg.min, cfg.max);
    mark.style.left = `${pct}%`;
    mark.dataset.dpi = String(dpi);
    mark.setAttribute("aria-valuenow", String(dpi));
    mark.setAttribute("aria-valuetext", `${dpi} DPI`);
    mark.title = _markText(stageIdx, dpi);
  }

  function _finishDrag(mark, pointerId, commit) {
    if (!drag) return;
    const finished = drag;
    drag = null;
    try { mark.releasePointerCapture?.(pointerId); } catch {}
    mark.classList.remove("dragging");
    trackEl.classList.remove("is-dragging");

    if (!finished.moved) {
      if (commit) onSelectStage?.(finished.stageIdx);
      return;
    }
    if (!commit) {
      onRevertDrag?.(finished.stageIdx, finished.startX, finished.startY);
      return;
    }
    if (finished.lastDpi !== finished.startX) {
      onCommitStages?.();
    } else {
      render();
    }
  }

  function _flushKeyboardCommit() {
    if (kbdTimer) { clearTimeout(kbdTimer); kbdTimer = null; }
    if (!kbdDirty) return;
    kbdDirty = false;
    onCommitStages?.();
  }

  trackEl?.addEventListener("pointerdown", (e) => {
    const mark = e.target?.closest?.(".mark[data-stage-idx]");
    if (!mark) return;
    if (typeof e.button === "number" && e.button !== 0) return;
    const cfg = getConfig();
    if (!cfg || !cfg.canWrite) return;
    const st = getStages();
    const stageIdx = Number(mark.dataset.stageIdx);
    const curStage = st?.list?.[stageIdx];
    if (!curStage) return;

    e.preventDefault();
    if (typeof mark.focus === "function") mark.focus({ preventScroll: true });
    try { mark.setPointerCapture?.(e.pointerId); } catch {}
    drag = {
      mark,
      pointerId: e.pointerId,
      stageIdx,
      startClientX: e.clientX,
      startX: curStage.x,
      startY: curStage.y,
      lastDpi: curStage.x,
      moved: false,
    };
  });

  trackEl?.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.startClientX) < 4) return;
      drag.moved = true;
      drag.mark.classList.add("dragging");
      trackEl.classList.add("is-dragging");
    }
    const cfg = getConfig();
    if (!cfg) return;
    const rect = trackEl.getBoundingClientRect();
    const pct = rect.width > 0 ? ((e.clientX - rect.left) / rect.width) * 100 : 0;
    const nextDpi = logPctToDpi(pct, cfg);
    if (nextDpi === drag.lastDpi) return;
    drag.lastDpi = nextDpi;
    _updateMarkVisual(drag.mark, drag.stageIdx, nextDpi, cfg);
    onPreviewDpi?.(drag.stageIdx, nextDpi);
  });

  trackEl?.addEventListener("pointerup", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    _finishDrag(drag.mark, e.pointerId, true);
  });

  trackEl?.addEventListener("pointercancel", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    _finishDrag(drag.mark, e.pointerId, false);
  });

  trackEl?.addEventListener("keydown", (e) => {
    const mark = e.target?.closest?.(".mark[data-stage-idx]");
    if (!mark) return;
    const cfg = getConfig();
    if (!cfg || !cfg.canWrite) return;
    const st = getStages();
    const stageIdx = Number(mark.dataset.stageIdx);
    const curStage = st?.list?.[stageIdx];
    if (!curStage) return;

    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelectStage?.(stageIdx);
      return;
    }

    const baseStep = (cfg.stepHi && cfg.stepHi !== cfg.step && curStage.x >= 10000)
      ? cfg.stepHi
      : Math.max(1, cfg.step || 50);
    const delta = (e.shiftKey ? 5 : 1) * baseStep;
    let target = null;
    if (e.key === "ArrowRight" || e.key === "ArrowUp") target = curStage.x + delta;
    else if (e.key === "ArrowLeft" || e.key === "ArrowDown") target = curStage.x - delta;
    else if (e.key === "Home") target = cfg.min;
    else if (e.key === "End") target = cfg.max;
    else return;

    e.preventDefault();
    const nextDpi = snapDpiValue(target, cfg);
    if (nextDpi === curStage.x) return;
    _updateMarkVisual(mark, stageIdx, nextDpi, cfg);
    onPreviewDpi?.(stageIdx, nextDpi);
    kbdDirty = true;
    if (kbdTimer) clearTimeout(kbdTimer);
    kbdTimer = setTimeout(_flushKeyboardCommit, 250);
  });

  trackEl?.addEventListener("keyup", (e) => {
    if (!kbdDirty) return;
    if (["ArrowRight", "ArrowUp", "ArrowLeft", "ArrowDown", "Home", "End"].includes(e.key)) {
      _flushKeyboardCommit();
    }
  });

  return {
    render,
    isDragging,
    isInteracting,
  };
}

export { dpiToLogPct, logPctToDpi, snapDpiValue, createDpiTrackController };
