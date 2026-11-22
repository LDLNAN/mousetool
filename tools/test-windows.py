"""Reversible hardware smoke test. Run from a scratch app copy with rawaccel binaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from mousetool.accel import rawaccel_export as curves
from mousetool.accel.windows import OFF, read_active, write_settings


def main():
    scratch = Path.home() / ".t3" + "/scratch"
    app = APP
    if sys.platform != "win32" or not app.is_relative_to(scratch):
        raise SystemExit("Run the test from an app copy under the Windows user's mousetool-test-scratch")
    url = "http://127.0.0.1:8766"

    def api(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(url + path, data=data, headers={"X-Mouse-Tool": "1"}), timeout=25) as res:
            return json.load(res)

    linear = dict(OFF, MODE="linear", ACCEL=.0425, INPUT_DPI=3200, YX_RATIO=1.25)
    off = curves.maccel_to_rawaccel(OFF)[0]
    with tempfile.TemporaryDirectory(dir=scratch) as work:
        rollback = Path(work) / "off.json"
        rollback.write_text(curves.dumps(off), encoding="utf-8")
        env = dict(os.environ, LOCALAPPDATA=work)
        proc = subprocess.Popen([sys.executable, app / "mousetool.py", "serve", "--no-browser"],
                                env=env, stdout=subprocess.PIPE, text=True)
        try:
            if proc.stdout.readline().strip() != "mousetool listening on " + url + "/":
                raise RuntimeError("Scratch server failed to start")
            try:
                state = api("/api/status")["accel"]
                print("Driver:", state["driver"], "initial mode:", state.get("mode"), flush=True)
                api("/api/accel", linear)
                expected = curves.maccel_to_rawaccel(linear)[0]
                assert read_active() == expected
                print("PASS classic 0.0425, DPI 3200, Y/X 1.25; exact readback", flush=True)
                write_settings(rollback)
                subprocess.run([sys.executable, "-m", "mousetool.accel.windows"], env=env, cwd=app, check=True)
                assert read_active() == expected
                print("PASS logon replay after clearing active curve", flush=True)
                api("/api/accel", {"reset": True})
                assert read_active() == off
                print("PASS reset and saved no acceleration", flush=True)
            finally:
                write_settings(rollback)
                if read_active() != off:
                    raise RuntimeError("Rollback readback mismatch")
                print("FINAL no acceleration verified", flush=True)
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
