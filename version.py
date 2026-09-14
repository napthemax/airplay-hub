#!/usr/bin/env python3
"""The installed build — VERSION plus the short git commit when we have one.

ROOT is this file's directory (the checkout install.sh pointed the wrappers
at), never the process cwd. install.sh, the window, --version and the phone
page all call display() so the string cannot drift between them.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = ROOT / "VERSION"


def version() -> str:
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


def git_sha() -> str | None:
    if not (ROOT / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _with_v(number: str) -> str:
    if number.startswith("v") or number == "unknown":
        return number
    return f"v{number}"


def display() -> str:
    shown = _with_v(version())
    sha = git_sha()
    if sha:
        return f"{shown} · {sha}"
    return f"{shown} · SHA unknown"


def argparse_version() -> str:
    return f"AirPlay Hub {display()}"


if __name__ == "__main__":
    print(display())
