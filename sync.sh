#!/usr/bin/env bash
# Keeps the rooms in phase with each other.
#
#   ./sync.sh          show how things stand
#   ./sync.sh +100     make the PipeWire rooms 100 ms later
#   ./sync.sh -100     make them 100 ms earlier
#
# The two engines buffer different amounts, and the difference is audible as an
# echo between rooms. OwnTone rooms (HomePod, Apple TV) are governed by
# start_buffer_ms in /etc/owntone.conf. PipeWire rooms (Volumio, shairport-sync)
# are governed from here.
#
# Listen, adjust, listen again. If the PipeWire room is AHEAD, increase.
#
# Individual OwnTone rooms are not adjusted here but in the app, behind the info
# button on the room's row. The numbers here are the baseline; that adjustment
# catches the devices' own buffering - a HomePod holds more than a
# shairport-sync does.

set -euo pipefail
CONF=$(cd "$(dirname "$0")" && pwd)/raop-discover.conf
DEST=~/.config/pipewire/pipewire.conf.d/raop-discover.conf

# A RAOP packet is 352 samples at 44100 Hz. PipeWire rounds down to whole
# packets, and the loopback adds its own period buffer on top.
PTIME=7.981859410

configured() {
  # The active line only. The file's comments mention older values, and those
  # must not be read as settings.
  grep -v '^\s*#' "$CONF" | grep -oP 'sess\.latency\.msec\s*=\s*\K[0-9.]+' | head -1
}

loopback_ms() {
  # Loopback streams only. Other sink inputs (PlexAmp, test tones) carry buffers
  # that have nothing to do with room timing.
  local frac
  frac=$(pactl list sink-inputs 2>/dev/null \
    | awk '/Sink Input/{blk=""} {blk=blk"\n"$0} /node\.latency/{if (blk ~ /loopback/) print blk}' \
    | grep -oP 'node\.latency = "\K[0-9]+/[0-9]+' | head -1)
  [ -z "$frac" ] && { echo 0; return; }
  python3 -c "n,d='$frac'.split('/'); print(round(int(n)/int(d)*1000))"
}

sink_ms() {
  local frac
  frac=$(pactl list sinks 2>/dev/null | grep -oP 'node\.latency = "\K[0-9]+/44100' | head -1)
  [ -z "$frac" ] && { echo "?"; return; }
  python3 -c "n,d='$frac'.split('/'); print(round(int(n)/int(d)*1000))"
}

ot_ms() {
  local v
  v=$(grep -oP '^\s*start_buffer_ms\s*=\s*\K[0-9]+' /etc/owntone.conf 2>/dev/null | head -1)
  echo "${v:-2250}"   # 2250 is OwnTone's default when the line is commented out
}

show() {
  local lb sink ot total
  lb=$(loopback_ms); sink=$(sink_ms); ot=$(ot_ms)
  echo
  echo "  PipeWire rooms (Volumio, shairport-sync)"
  echo "    requested  sess.latency.msec = $(configured) ms"
  # The loopback only exists while a room is on. Counting it as zero would make
  # the total look ~133 ms too low and the difference below plain wrong, so say
  # the measurement is incomplete instead of quietly reporting a bad number.
  if [ "$sink" != "?" ] && [ "$lb" -gt 0 ]; then
    total=$((sink + lb))
    echo "    actual     $sink ms in the sink + $lb ms in the loopback = $total ms"
  elif [ "$sink" != "?" ]; then
    total=""
    echo "    actual     $sink ms in the sink, loopback not measurable"
    echo "               (switch a PipeWire room on to see the real total)"
  else
    total=""
    echo "    actual     (no room switched on - turn one on to measure)"
  fi
  echo
  echo "  OwnTone rooms (HomePod, Apple TV)"
  echo "    start_buffer_ms = $ot ms"
  # Each OwnTone output can also be shifted on its own, from the info button in
  # the app. That is the adjustment that absorbs the devices' own buffering.
  curl -fsS --max-time 3 http://localhost:3689/api/outputs 2>/dev/null | python3 -c "
import json, sys
try:
    outs = json.load(sys.stdin).get('outputs', [])
except Exception:
    sys.exit()
for o in [o for o in outs if o.get('offset_ms')]:
    v = o['offset_ms']
    print(f\"    {o['name']}: {v:+d} ms ({'later' if v > 0 else 'earlier'})\")
" || true
  if [ -n "$total" ]; then
    echo
    echo "    difference: $((total - ot)) ms"
  fi
  echo
}

if [ $# -eq 0 ]; then
  show
  exit 0
fi

delta=$1
now=$(configured)
# The rounding is done in python throughout. printf follows the locale, and on a
# Swedish system it refuses numbers with a decimal point - which is what python
# writes.
next=$(python3 -c "
wanted = float('$now') + float('$delta')
# Round to the nearest whole RAOP packet, so PipeWire does not do it quietly.
packets = max(1, round(wanted / $PTIME))
print(round(packets * $PTIME))
")

echo "sess.latency.msec: $now -> $next"
sed -i "/^[[:space:]]*#/! s/sess\.latency\.msec = [0-9.]*/sess.latency.msec = $next/" "$CONF"
cp "$CONF" "$DEST"
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 6
echo "PipeWire restarted. The rooms need switching on again in the app."
show
