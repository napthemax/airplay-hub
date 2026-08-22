#!/usr/bin/env bash
# Makes OwnTone ready to serve as the AirPlay 2 engine for AirPlay Hub.
#
# Idempotent - run it as many times as you like. Needs sudo.
#
#   1. pipe_autostart = true in the library section (without it OwnTone never
#      starts playback when parec begins feeding the fifo)
#   2. a log file the owntone user can actually write to
#   3. the fifo in the library, readable by owntone
#   4. the PTP port, which AirPlay 2 devices require
#   5. the service running

set -euo pipefail

CONF=/etc/owntone.conf
LOG=/var/log/owntone.log
MUSIC=/srv/music
PIPE=$MUSIC/airplayhub.fifo

[ -f "$CONF" ] || { echo "Cannot find $CONF - is owntone-server installed?" >&2; exit 1; }

say() { printf '\n== %s\n' "$1"; }

say "pipe_autostart"
if grep -qE '^[[:space:]]*pipe_autostart[[:space:]]*=' "$CONF"; then
  echo "already set: $(grep -E '^[[:space:]]*pipe_autostart' "$CONF")"
else
  sudo cp -n "$CONF" "$CONF.bak-airplayhub"
  # The line ships commented out in the library section of the default config.
  sudo sed -i 's/^#\([[:space:]]*\)pipe_autostart[[:space:]]*=.*/\1pipe_autostart = true/' "$CONF"
  grep -qE '^[[:space:]]*pipe_autostart[[:space:]]*=[[:space:]]*true' "$CONF" \
    || { echo "Could not set pipe_autostart - add it by hand inside library { }" >&2; exit 1; }
  echo "set pipe_autostart = true (backup: $CONF.bak-airplayhub)"
fi

say "log file"
# The unit's drop-in runs OwnTone as User=owntone, and /var/log is only writable
# by root. Without this file there is nothing to debug with.
sudo install -o owntone -g owntone -m 644 /dev/null "$LOG" 2>/dev/null || sudo chown owntone:owntone "$LOG"
ls -l "$LOG"

say "library and fifo"
sudo mkdir -p "$MUSIC"
[ -p "$PIPE" ] || sudo mkfifo "$PIPE"
sudo chmod 666 "$PIPE"
ls -l "$PIPE"

say "PTP for AirPlay 2"
# HomePods and Apple TVs synchronise over PTP on ports 319/320. The unit's
# drop-in runs OwnTone as User=owntone, which may not bind ports below 1024 -
# OwnTone then logs "Could not bind to PTP event port" and falls back to NTP,
# which AirPlay 2 devices do not accept. AirPlay 1 is unaffected.
DROPIN=/etc/systemd/system/owntone.service.d/airplayhub.conf
sudo mkdir -p "$(dirname "$DROPIN")"
printf '[Service]\nAmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\n' \
  | sudo tee "$DROPIN" >/dev/null
echo "wrote $DROPIN"
sudo systemctl daemon-reload
sudo systemctl restart owntone
sleep 3

say "starting the service"
sudo systemctl enable --now owntone
sleep 2
systemctl --no-pager --lines=0 status owntone | head -5

say "API on localhost:3689"
for i in $(seq 1 15); do
  if curl -fsS --max-time 2 http://localhost:3689/api/config >/dev/null 2>&1; then
    echo "responding."
    ss -uln 2>/dev/null | grep -qE ':(319|320)\b' \
      && echo "PTP bound - AirPlay 2 can synchronise" \
      || echo "PTP still unbound - AirPlay 2 devices will stay silent"
    exit 0
  fi
  sleep 1
done

echo "OwnTone is not responding on 3689. Last lines of the log:" >&2
sudo tail -20 "$LOG" >&2
exit 1
