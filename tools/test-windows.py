"""Reversible Windows live-hardware smoke test for mousetool + Raw Accel.

Runs safely from a git checkout by staging an isolated temporary app copy with
unblocked Raw Accel binaries (or pass --allow-in-place when already running from
a standalone app copy that has rawaccel/ binaries).

Exercises:
  - /api/status (Raw Accel driver running + Guided Setup ready + LUT supported)
  - Static web assets (/, /app.js, /accel_lut.js, /devices/devices.json)
  - HTTP guardrails (403 without X-Mouse-Tool, 501 for Linux-only routes)
  - Live apply + exact driver readback for linear, natural, synchronous, and
    custom LUT (MODE="lookup") curves
  - Logon replay (python -m mousetool.accel.windows) after clearing active curve
  - Reset to no acceleration + guaranteed rollback verification in finally
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from mousetool.accel import rawaccel_export as curves
from mousetool.accel.windows import OFF, RAWACCEL_BINARIES, read_active, write_settings

DEFAULT_RAWACCEL_REL = Path("Downloads") / "RawAccel_v1.7.1" / "RawAccel"


def _find_rawaccel_dir(explicit):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        candidates.append(APP / "rawaccel")
        profile = os.environ.get("USERPROFILE")
        if profile:
            candidates.append(Path(profile) / DEFAULT_RAWACCEL_REL)
    for folder in candidates:
        if all((folder / name).is_file() for name in RAWACCEL_BINARIES):
            return folder
    searched = ", ".join(str(p) for p in candidates)
    raise SystemExit(f"Could not find Raw Accel binaries ({', '.join(RAWACCEL_BINARIES)}) in: {searched}")


def _unblock_windows_files(paths):
    if sys.platform != "win32":
        return
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    cmd = (
        "$ErrorActionPreference = 'Stop'; "
        "foreach ($p in ($env:MOUSETOOL_UNBLOCK_PATHS | ConvertFrom-Json)) { "
        "Unblock-File -LiteralPath $p -ErrorAction SilentlyContinue "
        "}"
    )
    env = dict(os.environ, MOUSETOOL_UNBLOCK_PATHS=json.dumps([str(p) for p in paths]))
    subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        env=env,
        check=True,
        capture_output=True,
        timeout=15,
    )


def _stage_temp_app(work_dir, rawaccel_dir):
    staged = Path(work_dir) / "app"
    staged.mkdir(parents=True, exist_ok=True)
    for name in ("mousetool.py", "icon.svg", "install.ps1"):
        src = APP / name
        if src.is_file():
            shutil.copy2(src, staged / name)
    for dirname in ("mousetool", "web", "devices"):
        shutil.copytree(
            APP / dirname,
            staged / dirname,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    win_dst = staged / "platform" / "windows"
    win_dst.mkdir(parents=True, exist_ok=True)
    for name in ("install.ps1", "read-rawaccel.ps1"):
        shutil.copy2(APP / "platform" / "windows" / name, win_dst / name)
    tools_dst = staged / "rawaccel"
    tools_dst.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in RAWACCEL_BINARIES:
        dst = tools_dst / name
        shutil.copy2(rawaccel_dir / name, dst)
        copied.append(dst)
    _unblock_windows_files(copied)
    return staged


def _run_smoke(app_dir, port, skip_lut=False):
    # Bind read_active / write_settings to the staged app copy so rawaccel/ resolves there.
    import mousetool.accel.windows as win_mod
    win_mod.APP = Path(app_dir)

    off = curves.maccel_to_rawaccel(OFF)[0]
    curves_to_test = [
        ("linear", dict(OFF, MODE="linear", ACCEL=0.0425, INPUT_DPI=3200, YX_RATIO=1.25)),
        ("natural", dict(OFF, MODE="natural", DECAY_RATE=0.1, OFFSET=2.0, LIMIT=1.5, INPUT_DPI=1600)),
        ("synchronous", dict(OFF, MODE="synchronous", GAMMA=1.0, SMOOTH=0.5, MOTIVITY=1.5, SYNC_SPEED=5.0, INPUT_DPI=1600)),
    ]
    if not skip_lut:
        curves_to_test.append((
            "lookup",
            dict(OFF, MODE="lookup", INPUT_DPI=1600, SENS_MULT=1.0, YX_RATIO=1.1,
                 LUT_POINTS=[[0.0, 0.9], [5.0, 1.0], [15.0, 1.45], [30.0, 1.7]]),
        ))

    with tempfile.TemporaryDirectory(prefix="mousetool-win-state-") as state_home:
        rollback = Path(state_home) / "off.json"
        rollback.write_text(curves.dumps(off), encoding="utf-8")
        env = dict(os.environ, LOCALAPPDATA=state_home)
        proc = subprocess.Popen(
            [sys.executable, str(Path(app_dir) / "mousetool.py"), "serve", "--no-browser", "--port", str(port)],
            env=env,
            cwd=str(app_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            banner = proc.stdout.readline().strip()
            m = re.fullmatch(r"mousetool listening on (http://127\.0\.0\.1:(\d+)/)", banner)
            if not m:
                err = proc.stderr.read() if proc.stderr else ""
                raise RuntimeError(f"Test server failed to start (banner={banner!r}): {err}")
            base_url = m.group(1).rstrip("/")
            bound_port = int(m.group(2))
            print(f"Server listening on {base_url}", flush=True)

            def api(path, body=None, headers=None):
                data = json.dumps(body).encode("utf-8") if body is not None else None
                hdrs = {"X-Mouse-Tool": "1"} if headers is None else headers
                with urlopen(Request(base_url + path, data=data, headers=hdrs), timeout=25) as res:
                    return res.status, json.load(res)

            try:
                status_code, root = api("/api/status")
                assert status_code == 200
                state = root["accel"]
                setup = root["setup"]
                print(
                    f"Driver: {state['driver']} | initial mode: {state.get('mode')} | "
                    f"setup ready: {setup['ready']} | lut_supported: {state.get('lut_supported')}",
                    flush=True,
                )
                assert state["driver"] == "running", state
                assert setup["platform"] == "windows" and setup["ready"], setup
                assert state.get("lut_supported") is True, state

                # Verify static web assets & HTTP boundaries against the live Windows server
                for asset_path, mime_prefix in (
                    ("/", "text/html"),
                    ("/app.js", "text/javascript"),
                    ("/accel_lut.js", "text/javascript"),
                    ("/devices/devices.json", "application/json"),
                ):
                    conn = http.client.HTTPConnection("127.0.0.1", bound_port, timeout=10)
                    conn.request("GET", asset_path)
                    resp = conn.getresponse()
                    payload = resp.read()
                    ctype = resp.getheader("Content-Type", "")
                    conn.close()
                    assert resp.status == 200 and ctype.startswith(mime_prefix) and len(payload) > 0, (
                        asset_path, resp.status, ctype
                    )
                print("PASS static assets (/, /app.js, /accel_lut.js, /devices/devices.json)", flush=True)

                try:
                    api("/api/accel", {"reset": True}, headers={})
                    raise AssertionError("Expected 403 without X-Mouse-Tool header")
                except HTTPError as exc:
                    assert exc.code == 403, exc.code
                try:
                    api("/api/desktop")
                    raise AssertionError("Expected 501 for /api/desktop on Windows")
                except HTTPError as exc:
                    assert exc.code == 501, exc.code
                print("PASS HTTP guardrails (403 CSRF header, 501 Linux-only route)", flush=True)

                # Apply and verify exact driver readback for every curve mode
                last_expected = None
                last_label = None
                for label, cfg in curves_to_test:
                    code, res = api("/api/accel?backend=rawaccel", cfg)
                    assert code == 200, res
                    expected = curves.maccel_to_rawaccel(cfg)[0]
                    actual = read_active()
                    assert actual == expected, f"{label} readback mismatch:\nactual={actual}\nexpected={expected}"
                    assert res["accel"]["mode"] == label, res["accel"]
                    assert res["accel"]["saved_state"] == "Matches saved curve", res["accel"]
                    if label == "lookup":
                        assert res["accel"]["lut_points"] == cfg["LUT_POINTS"], res["accel"]
                    print(f"PASS {label} apply + exact driver readback", flush=True)
                    last_expected = expected
                    last_label = label

                # Clear active curve directly and verify logon replay restores last saved curve (Custom LUT)
                write_settings(rollback)
                assert read_active() == off, "Failed to clear active curve before replay check"
                subprocess.run(
                    [sys.executable, "-m", "mousetool.accel.windows"],
                    env=env,
                    cwd=str(app_dir),
                    check=True,
                    timeout=30,
                )
                assert read_active() == last_expected, f"Logon replay mismatch for {last_label}"
                print(f"PASS logon replay restored saved {last_label} curve", flush=True)

                # Reset to no acceleration via API
                code, reset_res = api("/api/accel", {"reset": True})
                assert code == 200 and reset_res["accel"]["mode"] == "no_accel", reset_res
                assert read_active() == off
                print("PASS reset and saved no acceleration", flush=True)
            finally:
                write_settings(rollback)
                if read_active() != off:
                    raise RuntimeError("Rollback readback mismatch")
                print("FINAL no acceleration verified", flush=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reversible Windows hardware smoke test for mousetool + Raw Accel.")
    ap.add_argument("--rawaccel-dir", help="Path to Raw Accel directory containing writer.exe, wrapper.dll, Newtonsoft.Json.dll")
    ap.add_argument("--port", type=int, default=0, help="Local HTTP port for the test server (default: 0 = ephemeral)")
    ap.add_argument("--allow-in-place", action="store_true",
                    help="Run directly against the current directory instead of staging a temporary app copy")
    ap.add_argument("--skip-lut", action="store_true", help="Skip Custom LUT (MODE=lookup) live driver round-trip")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        raise SystemExit("Run this hardware smoke test on Windows with the Raw Accel driver running.")

    in_git_checkout = (APP / ".git").exists()
    if args.allow_in_place and all((APP / "rawaccel" / name).is_file() for name in RAWACCEL_BINARIES):
        _run_smoke(APP, args.port, skip_lut=args.skip_lut)
        return

    if not in_git_checkout and not args.rawaccel_dir and all((APP / "rawaccel" / name).is_file() for name in RAWACCEL_BINARIES):
        _run_smoke(APP, args.port, skip_lut=args.skip_lut)
        return

    rawaccel_dir = _find_rawaccel_dir(args.rawaccel_dir)
    with tempfile.TemporaryDirectory(prefix="mousetool-win-stage-") as stage_dir:
        staged_app = _stage_temp_app(stage_dir, rawaccel_dir)
        print(f"Staged isolated app copy at {staged_app} using Raw Accel from {rawaccel_dir}", flush=True)
        _run_smoke(staged_app, args.port, skip_lut=args.skip_lut)


if __name__ == "__main__":
    main()
