"""Unit tests for static web/ asset serving, MIME types, cwd-independence, and path-traversal rejection."""
import http.client
import os
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from mousetool.accel import rawaccel_export as curves
from mousetool.accel.windows import OFF, WindowsAccel, WindowsRoutes
from mousetool.server import Api, PathTraversalError, resolve_static_asset


EXPECTED_ASSETS = [
    ("/", "text/html; charset=utf-8"),
    ("/index.html", "text/html; charset=utf-8"),
    ("/styles.css", "text/css; charset=utf-8"),
    ("/app.js", "text/javascript; charset=utf-8"),
    ("/accel_graph.js", "text/javascript; charset=utf-8"),
    ("/helper_client.js", "text/javascript; charset=utf-8"),
    ("/polling_meter.js", "text/javascript; charset=utf-8"),
    ("/drivers/common.js", "text/javascript; charset=utf-8"),
    ("/drivers/registry.js", "text/javascript; charset=utf-8"),
    ("/drivers/classifier.js", "text/javascript; charset=utf-8"),
    ("/drivers/razer.js", "text/javascript; charset=utf-8"),
    ("/drivers/egg.js", "text/javascript; charset=utf-8"),
    ("/drivers/logitech.js", "text/javascript; charset=utf-8"),
    ("/drivers/pulsar.js", "text/javascript; charset=utf-8"),
    ("/drivers/glorious.js", "text/javascript; charset=utf-8"),
    ("/devices/devices.json", "application/json; charset=utf-8"),
    ("/devices/schema.json", "application/json; charset=utf-8"),
    ("/icon.svg", "image/svg+xml"),
]

TRAVERSAL_PATHS = [
    "/../mousetool.py",
    "/../../etc/passwd",
    "/%2e%2e/mousetool.py",
    "/%2e%2e%2fmousetool.py",
    "/%252e%252e/mousetool.py",
    "/%252e%252e%252fmousetool.py",
    "/drivers/../../mousetool.py",
    "/drivers/%2e%2e/%2e%2e/README.md",
    "/devices/../mousetool.py",
    "/devices/%2e%2e/mousetool.py",
    "/devices/%252e%252e/mousetool.py",
    "/devices/..%5cmousetool.py",
    "/..%5cmousetool.py",
    "/drivers/..%5c..%5cmousetool.py",
    "/app.js%00.png",
    "//etc/passwd",
    "/./index.html",
]


class StaticAssetServingTests(unittest.TestCase):
    def _start_server(self, handler_cls):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def _get(self, addr, path, headers=None):
        conn = http.client.HTTPConnection(*addr, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, resp.getheader("Content-Type"), resp.getheader("X-Content-Type-Options"), body
        finally:
            conn.close()

    def test_linux_api_serves_all_web_assets_with_correct_mime(self):
        srv = self._start_server(Api)
        for path, expected_mime in EXPECTED_ASSETS:
            with self.subTest(path=path):
                status, ctype, nosniff, body = self._get(srv.server_address, path)
                self.assertEqual(status, 200)
                self.assertEqual(ctype, expected_mime)
                self.assertEqual(nosniff, "nosniff")
                self.assertGreater(len(body), 0)

    def test_windows_routes_serves_all_web_assets_with_correct_mime(self):
        class Handler(WindowsRoutes, Api):
            pass

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        active = curves.maccel_to_rawaccel(OFF)[0]
        srv = self._start_server(Handler)
        srv.acceleration = WindowsAccel(tmp.name, lambda: active, lambda p: None, lambda: "running")

        for path, expected_mime in EXPECTED_ASSETS:
            with self.subTest(path=path):
                status, ctype, nosniff, body = self._get(srv.server_address, path)
                self.assertEqual(status, 200)
                self.assertEqual(ctype, expected_mime)
                self.assertEqual(nosniff, "nosniff")
                self.assertGreater(len(body), 0)

    def test_cwd_independence(self):
        srv = self._start_server(Api)
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as other_dir:
            os.chdir(other_dir)
            try:
                for path, expected_mime in (("/", "text/html; charset=utf-8"), ("/app.js", "text/javascript; charset=utf-8")):
                    status, ctype, _, body = self._get(srv.server_address, path)
                    self.assertEqual(status, 200)
                    self.assertEqual(ctype, expected_mime)
                    self.assertGreater(len(body), 0)
            finally:
                os.chdir(old_cwd)

    def _raw_get_status(self, addr, raw_path):
        """Send an unsanitized HTTP/1.1 GET request over a raw socket so client normalization cannot hide bugs."""
        host = f"{addr[0]}:{addr[1]}"
        req = f"GET {raw_path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode("latin1")
        with socket.create_connection(addr, timeout=5) as sock:
            sock.sendall(req)
            data = b""
            while b"\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        status_line = data.split(b"\r\n", 1)[0].decode("latin1")
        return int(status_line.split(" ", 2)[1])

    def test_path_traversal_rejected_with_403_on_linux_and_windows(self):
        class WinHandler(WindowsRoutes, Api):
            pass

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        active = curves.maccel_to_rawaccel(OFF)[0]

        linux_srv = self._start_server(Api)
        win_srv = self._start_server(WinHandler)
        win_srv.acceleration = WindowsAccel(tmp.name, lambda: active, lambda p: None, lambda: "running")

        for label, srv in (("linux", linux_srv), ("windows", win_srv)):
            for path in TRAVERSAL_PATHS:
                with self.subTest(server=label, path=path):
                    status = self._raw_get_status(srv.server_address, path)
                    self.assertEqual(status, 403, f"{label} did not return 403 for {path}")

    def test_missing_and_non_asset_paths_return_404(self):
        srv = self._start_server(Api)
        for path in ("/nonexistent.js", "/drivers/does_not_exist.js", "/rawaccel/writer.exe", "/api/nonexistent"):
            with self.subTest(path=path):
                status, _, _, _ = self._get(srv.server_address, path)
                self.assertEqual(status, 404)

    def test_symlink_escaping_web_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            outside = root / "secret.js"
            outside.write_text("console.log('secret');", encoding="utf-8")
            (web / "ok.js").write_text("export const ok = 1;", encoding="utf-8")
            (web / "escape.js").symlink_to(outside)

            resolved = resolve_static_asset("/ok.js", web_dir=web, repo_root=root)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved[1], "text/javascript; charset=utf-8")

            with self.assertRaises(PathTraversalError):
                resolve_static_asset("/escape.js", web_dir=web, repo_root=root)

            # Symlink escaping repo root for /icon.svg must also be rejected
            outside_svg = root / "outside.svg"
            outside_svg.write_text("<svg/>", encoding="utf-8")
            nested_repo = root / "repo"
            nested_web = nested_repo / "web"
            nested_web.mkdir(parents=True)
            (nested_repo / "icon.svg").symlink_to(outside_svg)
            with self.assertRaises(PathTraversalError):
                resolve_static_asset("/icon.svg", web_dir=nested_web, repo_root=nested_repo)

            # Symlink escaping devices root must also be rejected
            dev_dir = nested_repo / "devices"
            dev_dir.mkdir()
            outside_json = root / "secret.json"
            outside_json.write_text('{"secret":true}', encoding="utf-8")
            (dev_dir / "escape.json").symlink_to(outside_json)
            with self.assertRaises(PathTraversalError):
                resolve_static_asset("/devices/escape.json", web_dir=nested_web, repo_root=nested_repo, devices_dir=dev_dir)


if __name__ == "__main__":
    unittest.main()
