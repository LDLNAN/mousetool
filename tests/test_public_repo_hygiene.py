#!/usr/bin/env python3
"""Public-repository privacy, secret-leak, template-substitution, and README/CHANGELOG hygiene tests."""
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[Path]:
    """Return tracked repository files via `git ls-files`, falling back to a directory walk."""
    try:
        out = subprocess.check_output(
            ["git", "ls-files"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        )
        paths = [REPO_ROOT / line for line in out.splitlines() if line.strip()]
        if paths:
            return [p for p in paths if p.is_file()]
    except (OSError, subprocess.CalledProcessError):
        pass
    skip_dirs = {".git", "__pycache__", ".tephra", ".t3", "target"}
    result = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in sorted(files):
            result.append(Path(root) / f)
    return result


class TestPublicRepoHygiene(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tracked = _tracked_files()
        cls.text_files = [p for p in cls.tracked if p.suffix.lower() != ".png"]
        cls.png_files = [p for p in cls.tracked if p.suffix.lower() == ".png"]

    def test_no_personal_paths_usernames_or_hostnames_in_tracked_files(self):
        # Build patterns dynamically so this test file itself contains no literal matches.
        personal_user = "lucas" + "donald"
        forbidden = [
            ("personal_username", re.compile(re.escape(personal_user), re.IGNORECASE)),
            ("linux_home_dir", re.compile(r"/home/" + r"[A-Za-z0-9._-]+")),
            ("windows_user_dir", re.compile(r"C:\\+" + r"Users\\+", re.IGNORECASE)),
            ("personal_github_dir", re.compile(r"Documents[/\\]+GitHub", re.IGNORECASE)),
            ("t3_scratch_path", re.compile(r"\.t3[/\\]+scratch", re.IGNORECASE)),
            ("windows_desktop_hostname", re.compile(r"\bDESKTOP-" + r"[A-Z0-9]{4,}\b")),
        ]
        violations = []
        for path in self.text_files:
            rel = path.relative_to(REPO_ROOT)
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                for label, pattern in forbidden:
                    if pattern.search(line):
                        violations.append(f"{rel}:{lineno} [{label}]: {line.strip()[:120]}")
        self.assertEqual(violations, [], "Personal path/username/hostname leak(s) found:\n" + "\n".join(violations))

    def test_no_secrets_or_private_keys_in_tracked_files(self):
        secret_patterns = [
            ("github_pat_classic", re.compile(r"\bghp_" + r"[A-Za-z0-9]{20,}")),
            ("github_pat_fine", re.compile(r"\bgithub_pat_" + r"[A-Za-z0-9_]{20,}")),
            ("aws_access_key", re.compile(r"\bAKIA" + r"[0-9A-Z]{16}\b")),
            ("private_key_pem", re.compile(r"-----BEGIN " + r"[A-Z0-9 ]*PRIVATE KEY-----")),
            ("openai_or_sk_key", re.compile(r"\bsk-" + r"[A-Za-z0-9]{20,}")),
            ("google_api_key", re.compile(r"\bAIza" + r"[0-9A-Za-z_-]{35}\b")),
            ("slack_token", re.compile(r"\bxox[baprs]-" + r"[A-Za-z0-9-]{10,}")),
        ]
        violations = []
        for path in self.text_files:
            rel = path.relative_to(REPO_ROOT)
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                for label, pattern in secret_patterns:
                    if pattern.search(line):
                        violations.append(f"{rel}:{lineno} [{label}]")
        self.assertEqual(violations, [], "Secret/API key pattern(s) found:\n" + "\n".join(violations))

    def test_png_screenshots_contain_no_metadata_or_personal_text_chunks(self):
        self.assertGreaterEqual(len(self.png_files), 6)
        for path in self.png_files:
            rel = path.relative_to(REPO_ROOT)
            data = path.read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", f"{rel} is not a valid PNG")
            pos = 8
            text_chunks = []
            while pos < len(data):
                length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
                cdata = data[pos + 8:pos + 8 + length]
                pos += 12 + length
                ctype_s = ctype.decode("latin1")
                if ctype_s in ("tEXt", "zTXt", "iTXt"):
                    text_chunks.append((ctype_s, cdata))
            self.assertEqual(text_chunks, [], f"{rel} contains unexpected PNG text metadata chunks: {text_chunks}")

    def test_linux_templates_use_install_dir_placeholder_and_install_cleanly(self):
        desktop_tpl = (REPO_ROOT / "platform/linux/mousetool.desktop").read_text(encoding="utf-8")
        service_tpl = (REPO_ROOT / "platform/linux/mousetool.service").read_text(encoding="utf-8")

        self.assertIn("Exec=/usr/bin/env python3 @INSTALL_DIR@/mousetool.py open", desktop_tpl)
        self.assertIn("Icon=@INSTALL_DIR@/icon.svg", desktop_tpl)
        self.assertIn("ExecStart=/usr/bin/env python3 @INSTALL_DIR@/mousetool.py serve", service_tpl)

        if sys.platform == "win32":
            return

        with tempfile.TemporaryDirectory(prefix="mousetool-install-test-") as tmp:
            tmp_path = Path(tmp)
            xdg_data = tmp_path / "data"
            xdg_config = tmp_path / "config"
            env = dict(
                os.environ,
                XDG_DATA_HOME=str(xdg_data),
                XDG_CONFIG_HOME=str(xdg_config),
                MOUSETOOL_NO_SYSTEMCTL="1",
            )
            proc = subprocess.run(
                ["bash", str(REPO_ROOT / "platform/linux/install.sh")],
                cwd=str(REPO_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"install.sh failed:\n{proc.stderr}")

            installed_desktop = (xdg_data / "applications/mousetool.desktop").read_text(encoding="utf-8")
            installed_service = (xdg_config / "systemd/user/mousetool.service").read_text(encoding="utf-8")

            self.assertNotIn("@INSTALL_DIR@", installed_desktop)
            self.assertNotIn("@INSTALL_DIR@", installed_service)
            self.assertIn(f"Exec=/usr/bin/env python3 {REPO_ROOT}/mousetool.py open", installed_desktop)
            self.assertIn(f"Icon={REPO_ROOT}/icon.svg", installed_desktop)
            self.assertIn(f"ExecStart=/usr/bin/env python3 {REPO_ROOT}/mousetool.py serve", installed_service)

    def test_readme_and_changelog_are_clean_and_public_ready(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

        forbidden_readme_snippets = (
            "synthetic fixture",
            "fake serial",
            "capture_screenshots.py",
            "Select an image to open it at full resolution",
            "01.3 Sensor",
            "one-off DPI",
            "That verification did not include a real desktop session",
            ".t3" + "\\scratch",
            ".t3" + "/scratch",
            "such as Noctalia",
        )
        for snippet in forbidden_readme_snippets:
            self.assertNotIn(snippet, readme, f"Stale or internal-only snippet remained in README.md: {snippet!r}")

        forbidden_changelog_snippets = (
            "Tephra ledger",
            "same-family reviewer",
            "cross-provider reviewer",
            "/100",
        )
        for snippet in forbidden_changelog_snippets:
            self.assertNotIn(snippet, changelog, f"Internal review metadata remained in CHANGELOG.md: {snippet!r}")

        self.assertIn(".tephra/", gitignore)
        self.assertIn(".env", gitignore)


if __name__ == "__main__":
    unittest.main()
