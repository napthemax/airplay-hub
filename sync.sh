#!/usr/bin/env bash
# Shows and adjusts the timing between rooms.
#
#   ./sync.sh              show how things stand
#   ./sync.sh owntone 1750 set OwnTone's start_buffer_ms (needs sudo)
#
# How the levers work — hard-earned knowledge, do not relearn it the loud way:
#
#   * AirPlay 1 rooms (shairport-sync/Volumio) run over PipeWire at DEFAULT
#     latency. Overriding sess.latency.msec was tried: sessions came up,
#     metadata flowed, and no audio ever came out — followed by
#     "timestamp: expected ... != actual" and the sink vanishing. There is no
#     working per-stream latency lever on the PipeWire side. Leave it alone.
#
#   * OwnTone rooms (HomePod, Apple TV) are governed by start_buffer_ms in
#     /etc/owntone.conf (default 2250). Lowering it makes those rooms EARLIER.
#     That is the lever for a HomePod that lags behind the others.
#
#   * The per-room slider in the app (info button) shifts one OwnTone room
#     relative to the rest — but it draws on the same buffer, so keep
#     start_buffer_ms - |offset| at 500 ms or more, or the audio clips.

set -euo pipefail

# Below this OwnTone starves: playback stutters and starts unreliably. The
# shipped config notes 500 ms as the practical floor, and 0 makes it unusable.
MIN_BUFFER=500

ot_ms() {
  local v
  v=$(grep -oP '^\s*start_buffer_ms\s*=\s*\K[0-9]+' /etc/owntone.conf 2>/dev/null | head -1)
  echo "${v:-2250}"   # 2250 is OwnTone's default when the line is commented out
}

show() {
  echo
  echo "  AirPlay 1 rooms (shairport-sync, Volumio)"
  echo "    PipeWire defaults — no knob here, and that is deliberate."
  echo
  echo "  OwnTone rooms (HomePod, Apple TV)"
  echo "    start_buffer_ms = $(ot_ms) ms   (lower = earlier)"
  curl -fsS --max-time 3 http://localhost:3689/api/outputs 2>/dev/null | python3 -c "
import json, sys
try:
    outs = json.load(sys.stdin).get('outputs', [])
except Exception:
    sys.exit()
for o in [o for o in outs if o.get('offset_ms')]:
    v = o['offset_ms']
    print(f\"    {o['name']}: slider {v:+d} ms ({'later' if v > 0 else 'earlier'})\")
" || true
  echo
  local down up
  down=$(( $(ot_ms) - 250 )); [ "$down" -lt "$MIN_BUFFER" ] && down=$MIN_BUFFER
  up=$(( $(ot_ms) + 250 ))
  echo "  Lower (earlier): ./sync.sh owntone $down"
  echo "  Higher:          ./sync.sh owntone $up"
  echo
  echo "  Note: start_buffer_ms is what OwnTone buffers BEFORE starting, not"
  echo "  running latency. It affects how playback begins more than where the"
  echo "  room sits in time. For steady-state timing use the per-room slider"
  echo "  in the app; it is the only lever that reliably moves an OwnTone room."
  echo
}

if [ $# -eq 0 ]; then
  show
  exit 0
fi

if [ "$1" = "owntone" ]; then
  want=${2:-}
  case "$want" in
    ''|*[!0-9]*) echo "Usage: ./sync.sh owntone <milliseconds>, e.g. 1750" >&2; exit 1 ;;
  esac
  if [ "$want" -lt "$MIN_BUFFER" ]; then
    echo "Refusing $want ms — below $MIN_BUFFER OwnTone starves and playback" >&2
    echo "stutters. Pass $MIN_BUFFER or more." >&2
    exit 1
  fi
  echo "start_buffer_ms: $(ot_ms) -> $want"
  if grep -qE '^[[:space:]]*start_buffer_ms[[:space:]]*=' /etc/owntone.conf; then
    sudo sed -i "s/^\([[:space:]]*\)start_buffer_ms[[:space:]]*=.*/\1start_buffer_ms = $want/" /etc/owntone.conf
  else
    sudo sed -i "s/^#\([[:space:]]*\)start_buffer_ms[[:space:]]*=.*/\1start_buffer_ms = $want/" /etc/owntone.conf
  fi
  grep -qE "^[[:space:]]*start_buffer_ms[[:space:]]*=[[:space:]]*$want" /etc/owntone.conf \
    || { echo "Could not set it - edit /etc/owntone.conf by hand" >&2; exit 1; }
  sudo systemctl restart owntone
  sleep 4
  echo "OwnTone restarted. Its rooms need switching on again in the app."
  show
  exit 0
fi

echo "Unknown argument: $1" >&2
echo "The old ./sync.sh +N mode is gone — PipeWire latency overrides silence" >&2
echo "shairport-sync receivers. Use ./sync.sh owntone <ms> instead." >&2
exit 1
