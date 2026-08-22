#!/usr/bin/env bash
# Feeds OwnTone with the hub's audio through a named pipe.
#
#   AirPlayHub.monitor --parec--> /srv/music/airplayhub.fifo --> OwnTone --> HomePods
#
# OwnTone wants raw PCM 44100 Hz / 16 bit / stereo, and starts playback by
# itself if 'pipe_autostart = true' is set in the library section of
# owntone.conf.
#
# The app does this on its own through bridge.py. This script is for running the
# feed by hand while debugging.

set -u
PIPE=${PIPE:-/srv/music/airplayhub.fifo}
DEVICE=${DEVICE:-AirPlayHub.monitor}
UNIT="$HOME/.config/systemd/user/owntone-bridge.service"

if [ "${1:-}" = "--install-unit" ]; then
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=Feeds OwnTone with AirPlay Hub's audio
After=pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
Environment=PIPE=$PIPE
Environment=DEVICE=$DEVICE
ExecStart="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
UNITEOF
  echo "Wrote $UNIT"
  echo "Enable with:  systemctl --user daemon-reload && systemctl --user enable --now owntone-bridge"
  exit 0
fi

command -v parec >/dev/null 2>&1 || { echo "parec missing - install libpulse" >&2; exit 1; }

if [ ! -p "$PIPE" ]; then
  dir=$(dirname "$PIPE")
  [ -d "$dir" ] || { echo "Directory $dir does not exist - is that OwnTone's library?" >&2; exit 1; }
  if ! mkfifo "$PIPE" 2>/dev/null; then
    echo "Could not create $PIPE. Run:" >&2
    echo "  sudo mkfifo $PIPE && sudo chmod 666 $PIPE" >&2
    exit 1
  fi
  chmod 666 "$PIPE" 2>/dev/null || true
  echo "Created $PIPE"
fi

echo "Feeding $PIPE from $DEVICE (44100/16/2). Stop with Ctrl-C."
# parec blocks until OwnTone opens the other end of the pipe - that is intended.
exec parec --device="$DEVICE" --format=s16le --rate=44100 --channels=2 > "$PIPE"
