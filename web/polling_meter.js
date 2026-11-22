// Universal live mouse polling-rate estimator (immune to event-source doubling,
// 0.1 ms Chromium timestamp quantization, and OS/compositor vblank batching).

const DEFAULTS = {
  windowMs: 360,
  minLiveSpanMs: 100,
  minLiveIntervals: 8,
  minPeakSpanMs: 250,
  minPeakIntervals: 20,
  gapResetMs: 80,
};

function extractEventTimestamps(ev) {
  if (!ev) return [];
  const rawList = typeof ev.getCoalescedEvents === "function" ? ev.getCoalescedEvents() : null;
  const list = rawList && rawList.length ? rawList : [ev];
  const out = [];
  for (const item of list) {
    if (item && typeof item.timeStamp === "number" && Number.isFinite(item.timeStamp)) {
      out.push(item.timeStamp);
    }
  }
  return out;
}

function createPollingMeter(options = {}) {
  const cfg = { ...DEFAULTS, ...options };
  let timestamps = [];
  let totalEvents = 0;
  let peakHz = 0;
  let lastHz = 0;
  let lastIntervalMs = 0;
  let seenRaw = false;
  let activeSource = null;

  function resetStats() {
    timestamps = [];
    totalEvents = 0;
    peakHz = 0;
    lastHz = 0;
    lastIntervalMs = 0;
    activeSource = null;
  }

  function resetWindow() {
    timestamps = [];
  }

  function pushTimestamp(ts) {
    if (typeof ts !== "number" || !Number.isFinite(ts) || ts < 0) return snapshot(false);

    if (timestamps.length > 0) {
      const prev = timestamps[timestamps.length - 1];
      if (ts < prev) {
        // Ignore out-of-order timestamps (never pull the cursor backward).
        return snapshot(false);
      }
      if (ts - prev > cfg.gapResetMs) {
        // New continuous sweep segment; do not treat hand pause as a low polling rate.
        timestamps = [ts];
        totalEvents += 1;
        return snapshot(false);
      }
    }

    timestamps.push(ts);
    totalEvents += 1;

    while (timestamps.length > 2 && ts - timestamps[0] > cfg.windowMs) {
      timestamps.shift();
    }

    const intervals = timestamps.length - 1;
    const spanMs = intervals > 0 ? timestamps[timestamps.length - 1] - timestamps[0] : 0;

    if (intervals >= cfg.minLiveIntervals && spanMs >= cfg.minLiveSpanMs) {
      const hz = (intervals * 1000) / spanMs;
      const avgIntervalMs = spanMs / intervals;
      lastHz = hz;
      lastIntervalMs = avgIntervalMs;
      if (intervals >= cfg.minPeakIntervals && spanMs >= cfg.minPeakSpanMs && hz > peakHz) {
        peakHz = hz;
      }
      return snapshot(true);
    }

    return snapshot(false);
  }

  function ingestPointerEvent(ev, sourceKind = "move") {
    if (sourceKind === "raw") {
      if (!seenRaw) {
        seenRaw = true;
        if (activeSource === "move") {
          // Discard preliminary pointermove-only timestamps before switching to raw updates.
          timestamps = [];
        }
      }
      activeSource = "raw";
    } else {
      if (seenRaw) {
        // Chromium fires both pointerrawupdate and pointermove for the same motion;
        // once pointerrawupdate is active, ignore pointermove so samples are never doubled.
        return snapshot(false);
      }
      activeSource = "move";
    }

    const list = extractEventTimestamps(ev);
    let result = snapshot(false);
    for (const ts of list) {
      result = pushTimestamp(ts);
    }
    return result;
  }

  function snapshot(ready) {
    const intervals = Math.max(0, timestamps.length - 1);
    const spanMs = intervals > 0 ? timestamps[timestamps.length - 1] - timestamps[0] : 0;
    return {
      ready,
      hz: lastHz,
      avgIntervalMs: lastIntervalMs,
      peakHz,
      totalEvents,
      windowEvents: timestamps.length,
      spanMs,
      source: activeSource,
    };
  }

  return {
    pushTimestamp,
    ingestPointerEvent,
    resetWindow,
    resetStats,
    getState: () => snapshot(lastHz > 0),
  };
}

export {
  DEFAULTS as POLLING_METER_DEFAULTS,
  extractEventTimestamps,
  createPollingMeter,
};
