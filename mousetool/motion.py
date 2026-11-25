"""Live mouse motion telemetry (MotionTracker + MotionHub over /dev/mouseaccel and evdev)."""
import glob
import math
import os
import select
import struct
import sys
import threading
import time

from .accel import linux as linux_accel

EV_SYN = 0x00
SYN_REPORT = 0x00
EV_REL = 0x02
REL_X = 0x00
REL_Y = 0x01
INPUT_EVENT_STRUCT = struct.Struct("llHHi")
MACCEL_DEV = "/dev/maccel"           # legacy maccel speed device
MOUSEACCEL_DEV = "/dev/mouseaccel"   # mouseaccel speed device (same 8-byte big-endian read)
AUTO_SPEED_DEV = "auto"              # MotionHub: use the detected backend's speed device
NORMALIZED_DPI = 1000.0


def _parse_sysfs_bitmap(text):
    """Parse Linux sysfs capability bitmap (space-separated hex words, MSB first)."""
    bits = 0
    word_bits = struct.calcsize("L") * 8
    for tok in (text or "").strip().split():
        try:
            bits = (bits << word_bits) | int(tok, 16)
        except ValueError:
            return 0
    return bits


def discover_evdev_mice(sys_input_glob="/sys/class/input/event*", dev_dir="/dev/input"):
    """Return sorted /dev/input/event* paths that expose EV_REL with REL_X and REL_Y."""
    out = []
    for ev_dir in sorted(glob.glob(sys_input_glob)):
        ev_file = os.path.join(ev_dir, "device", "capabilities", "ev")
        rel_file = os.path.join(ev_dir, "device", "capabilities", "rel")
        try:
            with open(ev_file) as f:
                ev_bits = _parse_sysfs_bitmap(f.read())
            with open(rel_file) as f:
                rel_bits = _parse_sysfs_bitmap(f.read())
        except OSError:
            continue
        if (ev_bits & (1 << EV_REL)) and (rel_bits & (1 << REL_X)) and (rel_bits & (1 << REL_Y)):
            out.append(os.path.join(dev_dir, os.path.basename(ev_dir)))
    return out


def parse_evdev_events(data, pending=None):
    """Parse raw struct input_event bytes into (ts_sec, dx, dy) tuples emitted on SYN_REPORT."""
    if pending is None:
        pending = {"dx": 0, "dy": 0, "buf": b""}
    buf = pending.get("buf", b"") + data
    sz = INPUT_EVENT_STRUCT.size
    n_complete = (len(buf) // sz) * sz
    frames = []
    dx = pending.get("dx", 0)
    dy = pending.get("dy", 0)
    for off in range(0, n_complete, sz):
        tv_sec, tv_usec, ev_type, code, value = INPUT_EVENT_STRUCT.unpack_from(buf, off)
        if ev_type == EV_REL:
            if code == REL_X:
                dx += value
            elif code == REL_Y:
                dy += value
        elif ev_type == EV_SYN and code == SYN_REPORT:
            if dx != 0 or dy != 0:
                frames.append((tv_sec + tv_usec * 1e-6, dx, dy))
                dx = 0
                dy = 0
    pending["dx"] = dx
    pending["dy"] = dy
    pending["buf"] = buf[n_complete:]
    return frames, pending


def _read_maccel_fd(fd):
    """Read the module's LAST_INPUT_MOUSE_SPEED (big-endian Q32.32 fixed-point counts/ms).

    /dev/mouseaccel and /dev/maccel use the same format.
    """
    try:
        raw = os.read(fd, 8)
    except OSError:
        return None
    if len(raw) == 8:
        val = struct.unpack(">q", raw)[0]
        return val, max(0.0, val / linux_accel.FIX)
    if len(raw) == 4:
        val = struct.unpack(">i", raw)[0]
        return val, max(0.0, val / (1 << 16))
    return None


def accel_speed_device(backend=None):
    """Speed device path of the detected backend (/dev/mouseaccel or /dev/maccel), or None."""
    b = backend or linux_accel.detect_accel_backend()
    return b.get("speed_device") if b.get("loaded") else None


_O_RDONLY_NONBLOCK = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def read_maccel_speed(dev_path=None):
    """Read (raw_fixed, speed_counts_per_ms) from the detected speed device, or None if unreadable."""
    if dev_path is None:
        dev_path = accel_speed_device()
        if dev_path is None:
            return None
    try:
        fd = os.open(dev_path, _O_RDONLY_NONBLOCK)
    except OSError:
        return None
    try:
        return _read_maccel_fd(fd)
    except OSError:
        return None
    finally:
        os.close(fd)


def compute_normalized_speed(dx, dy, dt_ms, input_dpi=NORMALIZED_DPI):
    """Compute maccel-equivalent input speed in counts/ms normalized to 1000 DPI.

    Matches maccel accel.h / speed.h:
      dpi_factor = 1000.0 / input_dpi
      speed = hypot(dx * dpi_factor, dy * dpi_factor) / dt_ms
    """
    if dt_ms is None or dt_ms <= 0 or not math.isfinite(dt_ms):
        return 0.0
    if input_dpi is None or input_dpi <= 0 or not math.isfinite(input_dpi):
        return 0.0
    dpi_factor = NORMALIZED_DPI / float(input_dpi)
    return math.hypot(dx * dpi_factor, dy * dpi_factor) / float(dt_ms)


class MotionTracker:
    """Tracks live smoothed speed, active swipe range, idle decay, and recent swipe peak hold."""

    def __init__(self, input_dpi=NORMALIZED_DPI, smooth_tau_ms=10.0, idle_ms=45.0,
                 decay_tau_ms=35.0, swipe_gap_s=0.22, swipe_threshold=0.35, peak_hold_s=2.8):
        self.input_dpi = float(input_dpi) if input_dpi and input_dpi > 0 else NORMALIZED_DPI
        self.smooth_tau_ms = float(smooth_tau_ms)
        self.idle_ms = float(idle_ms)
        self.decay_tau_ms = float(decay_tau_ms)
        self.swipe_gap_s = float(swipe_gap_s)
        self.swipe_threshold = float(swipe_threshold)
        self.peak_hold_s = float(peak_hold_s)
        self.raw_speed = 0.0
        self._speed = 0.0
        self._last_event_ts = None
        self._last_motion_ts = None
        self._peak = 0.0
        self._peak_ts = None
        self._swipe_min = 0.0
        self._swipe_max = 0.0
        self._in_swipe = False
        self.swipe_id = 0

    def set_input_dpi(self, dpi):
        if dpi and math.isfinite(dpi) and dpi > 0:
            scale = self.input_dpi / dpi
            self._speed *= scale
            self.raw_speed *= scale
            self._peak *= scale
            self._swipe_min *= scale
            self._swipe_max *= scale
            self.input_dpi = float(dpi)

    def record_delta(self, dx, dy, dt_ms, now=None):
        spd = compute_normalized_speed(dx, dy, dt_ms, self.input_dpi)
        self.record_speed(spd, dt_ms=dt_ms, now=now)
        return spd

    def record_speed(self, inst_speed, dt_ms=None, now=None):
        if inst_speed is None or not math.isfinite(inst_speed):
            return
        inst = max(0.0, float(inst_speed))
        now = time.monotonic() if now is None else float(now)
        gap_s = (now - self._last_event_ts) if self._last_event_ts is not None else float("inf")
        if dt_ms is None or not math.isfinite(dt_ms) or dt_ms <= 0:
            eff_dt = min(50.0, max(0.1, gap_s * 1000.0)) if math.isfinite(gap_s) else self.smooth_tau_ms
        else:
            eff_dt = float(dt_ms)

        self.raw_speed = inst
        if self._speed <= 0.05 or gap_s >= self.idle_ms / 1000.0:
            self._speed = inst
        else:
            alpha = 1.0 - math.exp(-max(0.1, eff_dt) / self.smooth_tau_ms)
            self._speed = self._speed + alpha * (inst - self._speed)
        self._last_event_ts = now

        if self._speed >= self.swipe_threshold:
            new_swipe = (
                not self._in_swipe
                and (self._last_motion_ts is None or (now - self._last_motion_ts) >= self.swipe_gap_s)
            ) or self._peak <= 0.0
            if new_swipe:
                self.swipe_id += 1
                self._peak = self._speed
                self._peak_ts = now
                self._swipe_min = self._speed
                self._swipe_max = self._speed
                self._in_swipe = True
            else:
                self._in_swipe = True
                if self._speed > self._peak:
                    self._peak = self._speed
                self._peak_ts = now
                self._swipe_min = min(self._swipe_min, self._speed)
                self._swipe_max = max(self._swipe_max, self._speed)
            self._last_motion_ts = now

    def snapshot(self, now=None, dpi=None):
        now = time.monotonic() if now is None else float(now)
        idle_s = (now - self._last_event_ts) if self._last_event_ts is not None else float("inf")
        idle_ms = idle_s * 1000.0
        active = False

        if self._last_event_ts is None:
            cur_speed = 0.0
        elif idle_ms <= self.idle_ms:
            cur_speed = self._speed
            active = cur_speed > 0.05
        else:
            decay_ms = idle_ms - self.idle_ms
            cur_speed = self._speed * math.exp(-decay_ms / self.decay_tau_ms)
            if cur_speed < 0.05:
                cur_speed = 0.0
                self._speed = 0.0
                self.raw_speed = 0.0
                self._in_swipe = False

        if idle_s >= self.swipe_gap_s:
            self._in_swipe = False

        peak_age_ms = 0
        if self._peak > 0.0 and self._peak_ts is not None:
            peak_age_s = max(0.0, now - self._peak_ts)
            if not active and peak_age_s >= self.peak_hold_s:
                self._peak = 0.0
                self._swipe_min = 0.0
                self._swipe_max = 0.0
                self._peak_ts = None
            else:
                peak_age_ms = int(round(peak_age_s * 1000.0))

        base_dpi = self.input_dpi if self.input_dpi > 0 else NORMALIZED_DPI
        out_dpi = float(dpi) if (dpi is not None and math.isfinite(dpi) and dpi > 0) else base_dpi
        scale = base_dpi / out_dpi
        return {
            "speed": round(cur_speed * scale, 3),
            "raw_speed": round((self.raw_speed if active else 0.0) * scale, 3),
            "peak": round(self._peak * scale, 3),
            "peak_age_ms": peak_age_ms,
            "peak_hold_ms": int(round(self.peak_hold_s * 1000.0)),
            "swipe_min": round(self._swipe_min * scale, 3),
            "swipe_max": round(self._swipe_max * scale, 3),
            "swipe_id": self.swipe_id,
            "active": active,
            "dpi": round(out_dpi, 2),
        }


def _default_kernel_dpi():
    try:
        st = linux_accel.maccel_state()
        if st.get("loaded"):
            d = st.get("params", {}).get("INPUT_DPI")
            if d and math.isfinite(d) and d > 0:
                return float(d)
    except Exception:
        pass
    return NORMALIZED_DPI


def _raw_evdev_available(sys_module=None):
    # Only confirmed absent modules permit interpreting REL events as input: either module rewrites them.
    sys_module = sys_module or linux_accel.SYS_MODULE
    for b in linux_accel.ACCEL_BACKENDS:
        try:
            os.stat(os.path.join(sys_module, b["name"]))
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return os.path.isdir(sys_module)


class MotionHub:
    """Lazy, hotplug-safe live mouse motion hub shared by SSE subscribers.

    Starts the evdev + kernel speed device reader thread on the first subscriber and
    stops it cleanly (closing all descriptors) when the last subscriber leaves.
    """

    def __init__(self, evdev_discover=discover_evdev_mice, maccel_dev=AUTO_SPEED_DEV,
                 dpi_provider=_default_kernel_dpi, raw_evdev_available=_raw_evdev_available):
        self._evdev_discover = evdev_discover
        self._maccel_dev = maccel_dev     # path, None (disabled) or AUTO_SPEED_DEV (detected backend)
        self._backend = None              # module name whose speed device is open
        self._dpi_provider = dpi_provider
        self._raw_evdev_available = raw_evdev_available
        self._lifecycle = threading.Lock()
        self._last_kernel_sample = None
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._tracker = MotionTracker(input_dpi=NORMALIZED_DPI)
        self._subscribers = 0
        self._thread = None
        self._stop = threading.Event()
        self._wake_r = None
        self._wake_w = None
        self._source = "none"
        self._device_count = 0
        self._last_dpi_refresh = 0.0
        self._seq = 0

    @property
    def subscriber_count(self):
        with self._lock:
            return self._subscribers

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def refresh_dpi(self):
        dpi = self._dpi_provider()
        with self._lock:
            self._tracker.set_input_dpi(dpi)
            self._last_dpi_refresh = time.monotonic()

    def _wake_reader(self):
        w = self._wake_w
        if w is not None:
            try:
                os.write(w, b"x")
            except OSError:
                pass

    def subscribe(self):
        with self._lifecycle, self._lock:
            self._subscribers += 1
            if self._subscribers == 1 and (self._thread is None or not self._thread.is_alive()):
                self._stop.clear()
                self._wake_r, self._wake_w = os.pipe()
                os.set_blocking(self._wake_r, False)
                os.set_blocking(self._wake_w, False)
                self._tracker.set_input_dpi(self._dpi_provider())
                self._last_dpi_refresh = time.monotonic()
                t = threading.Thread(target=self._run, name="mousetool-motion", daemon=True)
                self._thread = t
                t.start()

    def unsubscribe(self):
        with self._lifecycle:
            self._unsubscribe()

    def _unsubscribe(self):
        t = None
        with self._lock:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0 and self._thread is not None:
                self._stop.set()
                self._wake_reader()
                t = self._thread
                self._cond.notify_all()
        if t is not None and t is not threading.current_thread():
            t.join()
        with self._lock:
            if self._subscribers == 0:
                self._thread = None
                self._tracker = MotionTracker()
                self._source = "none"
                self._device_count = 0
                self._last_kernel_sample = None
                for fd in (self._wake_r, self._wake_w):
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                self._wake_r = None
                self._wake_w = None

    def snapshot(self, dpi=None, now=None):
        with self._lock:
            snap = self._tracker.snapshot(now=now, dpi=dpi)
            if self._source == "maccel":
                last = self._last_kernel_sample
                snap["last_speed"] = (last[0] * last[1] / snap["dpi"]
                                      if last is not None else None)
            snap["source"] = self._source
            snap["backend"] = self._backend if self._source.startswith("maccel") else None
            snap["devices"] = self._device_count
            return snap

    def wait_update(self, last_seq, timeout=1.0):
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout=timeout)
            return self._seq

    def _open_maccel_fd(self):
        path, name = self._maccel_dev, None
        if path == AUTO_SPEED_DEV:
            b = linux_accel.detect_accel_backend()
            path, name = accel_speed_device(b), b.get("name")
        elif path:
            base = os.path.basename(path)
            name = base if base in {x["name"] for x in linux_accel.ACCEL_BACKENDS} else None
        if not path:
            return None
        try:
            fd = os.open(path, _O_RDONLY_NONBLOCK)
        except OSError:
            return None
        with self._lock:
            self._backend = name
        return fd

    def _sync_evdev_fds(self, fds_by_path):
        wanted = set(self._evdev_discover() or [])
        for path in list(fds_by_path):
            if path not in wanted:
                fd, _ = fds_by_path.pop(path)
                try:
                    os.close(fd)
                except OSError:
                    pass
        for path in sorted(wanted):
            if path in fds_by_path:
                continue
            try:
                fd = os.open(path, _O_RDONLY_NONBLOCK)
            except OSError:
                continue
            fds_by_path[path] = (fd, {"dx": 0, "dy": 0, "buf": b"", "last_ts": None})

    def _update_source(self, has_evdev, has_maccel, dev_count):
        if has_evdev and has_maccel:
            src = "maccel+evdev"
        elif has_evdev and self._raw_evdev_available():
            src = "evdev"
        elif has_maccel:
            src = "maccel"
        else:
            src = "none"
        with self._cond:
            changed = (src != self._source) or (dev_count != self._device_count)
            if src != self._source:
                self._last_kernel_sample = None
                self._tracker = MotionTracker(input_dpi=self._tracker.input_dpi)
            self._source = src
            self._device_count = dev_count
            if changed:
                self._seq += 1
                self._cond.notify_all()

    def _run(self):
        fds_by_path = {}
        maccel_fd = None
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_scan >= 2.0:
                    self._sync_evdev_fds(fds_by_path)
                    if maccel_fd is None:
                        maccel_fd = self._open_maccel_fd()
                    self._update_source(bool(fds_by_path), maccel_fd is not None, len(fds_by_path))
                    last_scan = now

                if now - self._last_dpi_refresh >= 0.5:
                    self.refresh_dpi()

                wake_r = self._wake_r
                if wake_r is None:
                    break

                if fds_by_path:
                    fd_to_path = {fd: p for p, (fd, _) in fds_by_path.items()}
                    snap = self.snapshot(now=now)
                    busy = snap["active"] or snap["speed"] > 0 or snap["peak"] > 0
                    timeout = 0.016 if busy else 2.0
                    try:
                        rlist, _, _ = select.select([wake_r] + list(fd_to_path), [], [], timeout)
                    except (OSError, ValueError):
                        last_scan = 0.0
                        continue

                    if wake_r in rlist:
                        try:
                            os.read(wake_r, 64)
                        except OSError:
                            pass
                        if self._stop.is_set():
                            break

                    now = time.monotonic()
                    any_motion = False
                    rescan = False
                    for fd in rlist:
                        if fd == wake_r:
                            continue
                        path = fd_to_path.get(fd)
                        if not path or path not in fds_by_path:
                            continue
                        _, st = fds_by_path[path]
                        try:
                            chunk = os.read(fd, 4096)
                        except BlockingIOError:
                            continue
                        except OSError:
                            chunk = b""
                        if not chunk:
                            fds_by_path.pop(path, None)
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            rescan = True
                            continue

                        frames, _ = parse_evdev_events(chunk, st)
                        if not frames:
                            continue

                        if maccel_fd is not None:
                            res = _read_maccel_fd(maccel_fd)
                            if res is not None:
                                _, spd = res
                                last_ts = st["last_ts"]
                                end_ts = frames[-1][0]
                                dt_ms = (end_ts - last_ts) * 1000.0 if last_ts is not None else None
                                st["last_ts"] = end_ts
                                with self._lock:
                                    self._tracker.record_speed(spd, dt_ms=dt_ms, now=now)
                                any_motion = True
                            else:
                                os.close(maccel_fd)
                                maccel_fd = None
                                self._update_source(True, False, len(fds_by_path))

                        elif self._source == "evdev" and self._raw_evdev_available():
                            with self._lock:
                                for ts_s, dx, dy in frames:
                                    last_ts = st["last_ts"]
                                    st["last_ts"] = ts_s
                                    if last_ts is not None:
                                        dt_ms = (ts_s - last_ts) * 1000.0
                                        if dt_ms > 0:
                                            self._tracker.record_delta(dx, dy, dt_ms, now=now)
                                            any_motion = True

                    if any_motion:
                        with self._cond:
                            self._seq += 1
                            self._cond.notify_all()
                    if rescan:
                        last_scan = 0.0
                elif maccel_fd is not None:
                    if sys.platform == "win32":
                        if self._stop.wait(0.016):
                            break
                    else:
                        try:
                            rlist, _, _ = select.select([wake_r], [], [], 0.016)
                        except (OSError, ValueError):
                            break
                        if wake_r in rlist:
                            try:
                                os.read(wake_r, 64)
                            except OSError:
                                pass
                            if self._stop.is_set():
                                break
                    res = _read_maccel_fd(maccel_fd)
                    if res is None:
                        os.close(maccel_fd)
                        maccel_fd = None
                        self._update_source(False, False, 0)
                        continue
                    _, speed = res
                    with self._cond:
                        if self._last_kernel_sample is None or speed != self._last_kernel_sample[0]:
                            self._last_kernel_sample = (speed, self._tracker.input_dpi)
                            self._seq += 1
                            self._cond.notify_all()
                else:
                    if sys.platform == "win32":
                        if self._stop.wait(2.0):
                            break
                    else:
                        try:
                            rlist, _, _ = select.select([wake_r], [], [], 2.0)
                        except (OSError, ValueError):
                            break
                        if wake_r in rlist:
                            try:
                                os.read(wake_r, 64)
                            except OSError:
                                pass
        finally:
            for fd, _ in fds_by_path.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            fds_by_path.clear()
            if maccel_fd is not None:
                try:
                    os.close(maccel_fd)
                except OSError:
                    pass


MOTION_HUB = MotionHub()
