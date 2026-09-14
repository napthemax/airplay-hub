#!/usr/bin/env python3
"""VERSION + git SHA — cwd must not matter; the window must actually show it."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import version


class VersionFileTests(unittest.TestCase):
    def test_reads_version_next_to_this_file(self) -> None:
        self.assertRegex(version.version(), r"^\d+\.\d+\.\d+$")
        self.assertEqual(version.VERSION_FILE, version.ROOT / "VERSION")
        self.assertEqual(version.ROOT, Path(version.__file__).resolve().parent)

    def test_ignores_process_cwd(self) -> None:
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            decoy = Path(tmp) / "VERSION"
            decoy.write_text("9.9.9\n", encoding="utf-8")
            os.chdir(tmp)
            try:
                self.assertNotEqual(version.version(), "9.9.9")
                self.assertEqual(
                    version.version(),
                    version.VERSION_FILE.read_text(encoding="utf-8").strip(),
                )
            finally:
                os.chdir(here)


class DisplayTests(unittest.TestCase):
    def test_v_prefix_and_sha(self) -> None:
        with (
            patch.object(version, "version", return_value="0.2.1"),
            patch.object(version, "git_sha", return_value="abc1234"),
        ):
            self.assertEqual(version.display(), "v0.2.1 · abc1234")
            self.assertEqual(version.argparse_version(), "AirPlay Hub v0.2.1 · abc1234")

    def test_unknown_sha(self) -> None:
        with (
            patch.object(version, "version", return_value="0.2.1"),
            patch.object(version, "git_sha", return_value=None),
        ):
            self.assertEqual(version.display(), "v0.2.1 · SHA unknown")

    def test_does_not_double_v(self) -> None:
        with (
            patch.object(version, "version", return_value="v0.2.1"),
            patch.object(version, "git_sha", return_value="abc1234"),
        ):
            self.assertEqual(version.display(), "v0.2.1 · abc1234")


class WindowShowsVersionTests(unittest.TestCase):
    def test_main_window_renders_build_id_without_a_menu(self) -> None:
        text = Path(__file__).resolve().parent.joinpath("main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from version import argparse_version, display as build_id", text)
        self.assertIn("QLabel(build_id())", text)
        self.assertNotIn("setMenuBar", text)
        self.assertNotIn("QMenuBar", text)
        self.assertNotIn("QMenu", text)

    def test_web_page_uses_the_same_helper(self) -> None:
        text = Path(__file__).resolve().parent.joinpath("webui.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('PAGE.replace("__BUILD__", build_id())', text)


if __name__ == "__main__":
    unittest.main()
