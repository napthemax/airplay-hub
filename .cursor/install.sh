#!/usr/bin/env bash
# Cloud Agent install for AirPlay Hub.
#
# Installs the system packages the app drives at runtime. It is deliberately
# NOT the repository's own ./install.sh, which is Arch/pacman + systemd-user
# specific and sets up a real household of AirPlay speakers. Here we prepare an
# Ubuntu Cloud Agent VM so the code runs end to end:
#
#   - PyQt6            the desktop window (main.py)
#   - PipeWire + pactl the audio backend the app talks to (pwhub.py)
#   - parec            feeds OwnTone's fifo (bridge.py)
#   - avahi            mDNS discovery of AirPlay devices
#   - Xvfb             a virtual display so the GUI can run headless
#
# Idempotent: apt-get install is a no-op when the packages are already present.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update

# --no-install-recommends keeps the image lean; every package here is used.
sudo apt-get install -y --no-install-recommends \
  python3-pyqt6 python-is-python3 \
  pulseaudio-utils \
  pipewire pipewire-pulse pipewire-audio wireplumber \
  libpipewire-0.3-modules libspa-0.2-modules \
  avahi-utils avahi-daemon \
  xvfb x11-utils xauth dbus-x11 \
  libgl1 libegl1 libxkbcommon0

# Fail loudly here rather than with a blank window later.
python -c "import PyQt6.QtWidgets, PyQt6.QtCore; print('PyQt6', PyQt6.QtCore.PYQT_VERSION_STR, 'OK')"
for tool in pactl parec avahi-browse pipewire wireplumber Xvfb; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

echo "AirPlay Hub install complete."
