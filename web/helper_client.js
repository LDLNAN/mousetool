// Localhost helper API client (127.0.0.1:8766 or current loopback origin)

export const HELPER =
  typeof location !== "undefined" &&
  location.protocol === "http:" &&
  (location.hostname === "127.0.0.1" || location.hostname === "localhost")
    ? location.origin
    : "http://127.0.0.1:8766";

export async function fetchHelperStatus() {
  const res = await fetch(`${HELPER}/api/status`, { cache: "no-store" });
  const data = await res.json();
  return { res, data };
}

export async function postHelperAccel(backend, payload) {
  const body = JSON.stringify(payload);
  const post = (path) => fetch(`${HELPER}${path}?backend=${encodeURIComponent(backend)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mouse-Tool": "1" },
    body,
  });
  let res = await post("/api/accel");
  if (res.status === 404 && backend === "maccel") res = await post("/api/maccel"); // helper from before /api/accel
  const data = await res.json();
  return { res, data };
}

export async function postHelperAccelReset() {
  const res = await fetch(`${HELPER}/api/accel?backend=rawaccel`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mouse-Tool": "1" },
    body: JSON.stringify({ reset: true }),
  });
  return res;
}

export async function postHelperDesktop(updates) {
  const res = await fetch(`${HELPER}/api/desktop`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mouse-Tool": "1" },
    body: JSON.stringify(updates),
  });
  const data = await res.json();
  return { res, data };
}

export async function fetchHelperBattery(opts = {}) {
  const res = await fetch(`${HELPER}/api/battery`, { cache: "no-store", ...opts });
  const data = await res.json();
  return { res, data };
}

export async function postHelperBattery(patch) {
  const res = await fetch(`${HELPER}/api/battery`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mouse-Tool": "1" },
    body: JSON.stringify(patch),
  });
  const data = await res.json();
  return { res, data };
}

export function openHelperMotionStream() {
  return new EventSource(`${HELPER}/api/motion`);
}
