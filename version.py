#!/usr/bin/env python3
"""The installed build — VERSION plus the short git commit when we have one.

install.sh, airplay-hub --version and the phone page all call display() so
the string cannot drift between them.
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


def display() -> str:
    sha = git_sha()
    if sha:
        return f"{version()} · {sha}"
    return f"{version()} · SHA unknown"


def argparse_version() -> str:
    return f"AirPlay Hub {display()}"


if __name__ == "__main__":
    print(display())
