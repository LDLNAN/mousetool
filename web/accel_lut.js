// Custom acceleration lookup-table (LUT) math, validation, presets & point editing helpers
// Matches mouseaccel (MA-003, MODE=4 LUT_POINTS) and Raw Accel v1.7.1 Sensitivity LUT exactly.

const LUT_MIN_POINTS = 2;
const LUT_MAX_POINTS = 64;
const LUT_X_MIN = 0;
const LUT_X_MAX = 1000;
const LUT_DX_MIN = 0.001;
const LUT_DRAG_DX_MIN = 0.1;
const LUT_Y_MIN = 0.05;
const LUT_Y_MAX = 10.0;

const LUT_DEFAULT_POINTS = [
  [0, 1.0],
  [10, 1.0],
];

const LUT_STARTER_POINTS = [
  [0, 1.0],
  [5, 1.0],
  [15, 1.4],
  [30, 1.6],
];

const LUT_PRESETS = [
  {
    id: "flat",
    label: "Flat 1×",
    points: [
      [0, 1.0],
      [10, 1.0],
    ],
  },
  {
    id: "ramp",
    label: "Ramp",
    points: [
      [0, 1.0],
      [5, 1.0],
      [15, 1.4],
      [30, 1.6],
    ],
  },
  {
    id: "scurve",
    label: "S-Curve",
    points: [
      [0, 0.75],
      [4, 0.85],
      [10, 1.0],
      [18, 1.35],
      [28, 1.55],
      [36, 1.6],
    ],
  },
  {
    id: "stepcap",
    label: "Step Cap",
    points: [
      [0, 0.9],
      [8, 0.9],
      [16, 1.5],
      [32, 1.5],
    ],
  },
];

function _round(v, digits = 4) {
  const f = 10 ** digits;
  return Math.round(v * f) / f;
}

function _snap(v, step) {
  if (!step || step <= 0) return _round(v, 4);
  return _round(Math.round(v / step) * step, 4);
}

function cloneLutPoints(points) {
  if (!Array.isArray(points) || points.length < LUT_MIN_POINTS) {
    return LUT_STARTER_POINTS.map(([x, y]) => [x, y]);
  }
  return points.map(([x, y]) => [_round(Number(x), 4), _round(Number(y), 4)]);
}

function normalizeLutPoints(points) {
  if (!Array.isArray(points) || points.length < LUT_MIN_POINTS) return null;
  const out = [];
  for (const item of points.slice(0, LUT_MAX_POINTS)) {
    if (!Array.isArray(item) || item.length !== 2) return null;
    const x = Number(item[0]);
    const y = Number(item[1]);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    if (x < LUT_X_MIN || x > LUT_X_MAX || y < LUT_Y_MIN || y > LUT_Y_MAX) return null;
    if (out.length > 0 && x - out[out.length - 1][0] < LUT_DX_MIN - 1e-12) return null;
    out.push([_round(x, 4), _round(y, 4)]);
  }
  return out.length >= LUT_MIN_POINTS ? out : null;
}

function lutPointsEqual(a, b, eps = 1e-4) {
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (Math.abs(a[i][0] - b[i][0]) > eps || Math.abs(a[i][1] - b[i][1]) > eps) return false;
  }
  return true;
}

function evalLutSens(x, points) {
  if (!Array.isArray(points) || points.length < LUT_MIN_POINTS) return 1;
  if (x <= points[0][0]) return points[0][1];
  const last = points.length - 1;
  if (x >= points[last][0]) return points[last][1];
  let lo = 0;
  let hi = last;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= x) lo = mid;
    else hi = mid;
  }
  const [x0, y0] = points[lo];
  const [x1, y1] = points[lo + 1];
  return y0 + ((x - x0) * (y1 - y0)) / (x1 - x0);
}

function pointXBounds(points, idx, { minGap = LUT_DRAG_DX_MIN, maxX = LUT_X_MAX } = {}) {
  const gap = Math.max(LUT_DX_MIN, minGap);
  const lo = idx <= 0 ? LUT_X_MIN : points[idx - 1][0] + gap;
  const hi = idx >= points.length - 1 ? Math.min(LUT_X_MAX, maxX) : points[idx + 1][0] - gap;
  if (hi < lo) {
    // Fallback to exact minimum spacing if points are tightly packed
    const tightLo = idx <= 0 ? LUT_X_MIN : points[idx - 1][0] + LUT_DX_MIN;
    const tightHi = idx >= points.length - 1 ? Math.min(LUT_X_MAX, maxX) : points[idx + 1][0] - LUT_DX_MIN;
    return [tightLo, Math.max(tightLo, tightHi)];
  }
  return [lo, hi];
}

function clampLutPoint(points, idx, rawX, rawY, opts = {}) {
  const {
    shiftKey = false,
    snap = true,
    stepX = shiftKey ? 0.05 : 0.25,
    stepY = shiftKey ? 0.01 : 0.02,
    minGap = LUT_DRAG_DX_MIN,
    maxX = LUT_X_MAX,
  } = opts;
  const [loX, hiX] = pointXBounds(points, idx, { minGap, maxX });
  let x = Number.isFinite(rawX) ? rawX : points[idx][0];
  let y = Number.isFinite(rawY) ? rawY : points[idx][1];
  if (snap) {
    x = _snap(x, stepX);
    y = _snap(y, stepY);
  } else {
    x = _round(x, 4);
    y = _round(y, 4);
  }
  x = _round(Math.max(loX, Math.min(hiX, x)), 4);
  y = _round(Math.max(LUT_Y_MIN, Math.min(LUT_Y_MAX, y)), 4);
  return [x, y];
}

function moveLutPoint(points, idx, rawX, rawY, opts = {}) {
  if (!Array.isArray(points) || idx < 0 || idx >= points.length) return cloneLutPoints(points);
  const next = cloneLutPoints(points);
  next[idx] = clampLutPoint(next, idx, rawX, rawY, opts);
  return next;
}

function insertLutPoint(points, rawX, rawY, opts = {}) {
  const cur = cloneLutPoints(points);
  if (cur.length >= LUT_MAX_POINTS) {
    return { points: cur, index: -1, added: false, reason: "max" };
  }
  const {
    shiftKey = false,
    snap = true,
    stepX = shiftKey ? 0.05 : 0.25,
    stepY = shiftKey ? 0.01 : 0.02,
    minGap = LUT_DRAG_DX_MIN,
    maxX = LUT_X_MAX,
  } = opts;
  const gap = Math.max(LUT_DX_MIN, minGap);
  let x = Number.isFinite(rawX) ? rawX : 0;
  let y = Number.isFinite(rawY) ? rawY : 1;
  if (snap) {
    x = _snap(x, stepX);
    y = _snap(y, stepY);
  } else {
    x = _round(x, 4);
    y = _round(y, 4);
  }
  x = _round(Math.max(LUT_X_MIN, Math.min(Math.min(LUT_X_MAX, maxX), x)), 4);
  y = _round(Math.max(LUT_Y_MIN, Math.min(LUT_Y_MAX, y)), 4);

  // Find sorted insertion position
  let idx = cur.findIndex(([px]) => px > x);
  if (idx === -1) idx = cur.length;

  const prevX = idx > 0 ? cur[idx - 1][0] : null;
  const nextX = idx < cur.length ? cur[idx][0] : null;
  if (prevX !== null && x - prevX < gap) {
    // Try nudging into available space if there's room
    const lo = prevX + gap;
    const hi = nextX !== null ? nextX - gap : Math.min(LUT_X_MAX, maxX);
    if (lo <= hi) x = _round(lo, 4);
    else return { points: cur, index: idx - 1, added: false, reason: "spacing" };
  } else if (nextX !== null && nextX - x < gap) {
    const hi = nextX - gap;
    const lo = prevX !== null ? prevX + gap : LUT_X_MIN;
    if (lo <= hi) x = _round(hi, 4);
    else return { points: cur, index: idx, added: false, reason: "spacing" };
  }

  cur.splice(idx, 0, [x, y]);
  return { points: cur, index: idx, added: true, reason: null };
}

function removeLutPoint(points, idx) {
  const cur = cloneLutPoints(points);
  if (cur.length <= LUT_MIN_POINTS) {
    return { points: cur, index: Math.max(0, Math.min(idx, cur.length - 1)), removed: false, reason: "min" };
  }
  if (idx < 0 || idx >= cur.length) {
    return { points: cur, index: 0, removed: false, reason: "bounds" };
  }
  cur.splice(idx, 1);
  const nextIdx = Math.max(0, Math.min(idx, cur.length - 1));
  return { points: cur, index: nextIdx, removed: true, reason: null };
}

function addMidpointLutPoint(points, selectedIdx = 0, xMax = 40) {
  const cur = cloneLutPoints(points);
  if (cur.length >= LUT_MAX_POINTS) {
    return { points: cur, index: selectedIdx, added: false, reason: "max" };
  }
  const sel = Math.max(0, Math.min(selectedIdx, cur.length - 1));
  // Prefer midpoint of segment right after selected point, else extend right if room, else widest gap
  let targetX = null;
  if (sel < cur.length - 1 && cur[sel + 1][0] - cur[sel][0] >= LUT_DRAG_DX_MIN * 2) {
    targetX = (cur[sel][0] + cur[sel + 1][0]) / 2;
  } else if (cur[cur.length - 1][0] + 2 <= xMax) {
    targetX = Math.min(xMax, cur[cur.length - 1][0] + Math.max(2, (xMax - cur[cur.length - 1][0]) / 2));
  } else {
    let bestGap = 0;
    for (let i = 0; i < cur.length - 1; i++) {
      const gap = cur[i + 1][0] - cur[i][0];
      if (gap > bestGap) {
        bestGap = gap;
        targetX = (cur[i][0] + cur[i + 1][0]) / 2;
      }
    }
  }
  if (targetX === null) {
    return { points: cur, index: sel, added: false, reason: "spacing" };
  }
  const y = evalLutSens(targetX, cur);
  return insertLutPoint(cur, targetX, y, { snap: true, stepX: 0.25, stepY: 0.01, maxX: Math.max(xMax, cur[cur.length - 1][0]) });
}

function getLutPreset(id) {
  const p = LUT_PRESETS.find((item) => item.id === id);
  return p ? cloneLutPoints(p.points) : null;
}

function matchingLutPresetId(points) {
  for (const p of LUT_PRESETS) {
    if (lutPointsEqual(points, p.points, 1e-3)) return p.id;
  }
  return null;
}

export {
  LUT_MIN_POINTS,
  LUT_MAX_POINTS,
  LUT_X_MIN,
  LUT_X_MAX,
  LUT_DX_MIN,
  LUT_DRAG_DX_MIN,
  LUT_Y_MIN,
  LUT_Y_MAX,
  LUT_DEFAULT_POINTS,
  LUT_STARTER_POINTS,
  LUT_PRESETS,
  cloneLutPoints,
  normalizeLutPoints,
  lutPointsEqual,
  evalLutSens,
  pointXBounds,
  clampLutPoint,
  moveLutPoint,
  insertLutPoint,
  removeLutPoint,
  addMidpointLutPoint,
  getLutPreset,
  matchingLutPresetId,
};
