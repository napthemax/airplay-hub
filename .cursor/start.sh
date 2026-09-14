#!/usr/bin/env bash
# Cloud Agent per-boot startup for AirPlay Hub.
#
# Brings up the audio + mDNS infrastructure the app reads from, then returns.
# Idempotent: safe to run again, never starts a second copy of anything.
#
#   dbus (system)  -> avahi needs it
#   avahi-daemon   -> mDNS discovery of AirPlay/RAOP devices (pwhub.browse_airplay)
#   dbus (session) -> wireplumber's policy/device reservation
#   pipewire       -> the audio graph
#   wireplumber    -> session manager (links, devices)
#   pipewire-pulse -> the PulseAudio-compatible socket that `pactl`/`parec` use
#
# There are no real AirPlay speakers on a Cloud VM, so discovery finds nothing —
# but every command the app issues (pactl load-module null-sink / loopback /
# raop-discover, set-sink-volume, avahi-browse) runs against a real server.
set -euo pipefail

UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="/run/user/${UID_NUM}"
sudo mkdir -p "$XDG_RUNTIME_DIR"
sudo chown "$(id -u):$(id -g)" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

ENVFILE="${XDG_RUNTIME_DIR}/airplay-hub.env"

# --- system bus + avahi (mDNS) -------------------------------------------
sudo mkdir -p /run/dbus
sudo sh -c '[ -S /run/dbus/system_bus_socket ] || dbus-daemon --system --fork'
sudo sh -c 'pidof avahi-daemon >/dev/null 2>&1 || avahi-daemon -D' || true

# --- session bus (for wireplumber) ---------------------------------------
# Reuse a live one if this boot already started it, otherwise launch it and
# remember the address so terminals/tools can reach the same graph.
if [ -f "$ENVFILE" ]; then
  # shellcheck disable=SC1090
  source "$ENVFILE"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] || ! dbus-send --session --dest=org.freedesktop.DBus \
      --type=method_call --print-reply /org/freedesktop/DBus \
      org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
  eval "$(dbus-launch --sh-syntax)"
  echo "export XDG_RUNTIME_DIR='${XDG_RUNTIME_DIR}'" > "$ENVFILE"
  echo "export DBUS_SESSION_BUS_ADDRESS='${DBUS_SESSION_BUS_ADDRESS}'" >> "$ENVFILE"
fi

# --- PipeWire stack -------------------------------------------------------
start_once() {
  local name="$1"; shift
  if pgrep -u "$UID_NUM" -x "$name" >/dev/null 2>&1; then
    echo "  $name already running"
  else
    nohup "$@" >"${XDG_RUNTIME_DIR}/${name}.log" 2>&1 &
    echo "  $name started"
  fi
}

start_once pipewire       pipewire
sleep 1
start_once wireplumber    wireplumber
sleep 1
start_once pipewire-pulse pipewire-pulse

# --- readiness ------------------------------------------------------------
for _ in $(seq 1 20); do
  if pactl info >/dev/null 2>&1; then
    echo "PipeWire ready: $(pactl info | awk -F': ' '/Server Name/{print $2}')"
    exit 0
  fi
  sleep 0.5
done

echo "PipeWire did not become ready in time" >&2
cat "${XDG_RUNTIME_DIR}/pipewire.log" "${XDG_RUNTIME_DIR}/pipewire-pulse.log" 2>/dev/null >&2 || true
exit 1
