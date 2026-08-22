#!/usr/bin/env bash
# Checks everything that must hold for AirPlay Hub to be audible. Run: ./diagnose.sh
ok(){ printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad(){ printf '  \033[31mFEL\033[0m  %s\n' "$1"; }
warn(){ printf '  \033[33m??\033[0m   %s\n' "$1"; }
sect(){ printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

sect "Tools"
for tool in pactl avahi-browse pw-cli python; do
  if command -v "$tool" >/dev/null 2>&1; then ok "$tool present"; else bad "$tool missing"; fi
done
python -c "import PyQt6" 2>/dev/null && ok "PyQt6 installed" || bad "PyQt6 missing  ->  sudo pacman -S python-pyqt6"

sect "Sound server"
if pactl info >/dev/null 2>&1; then
  pactl info | grep -E "Server Name|Default Sink|Server Version"
  pactl info | grep -qi pipewire && ok "PipeWire running" || warn "Looks like real PulseAudio - the RAOP module is still called module-raop-discover"
else
  bad "pactl info is not answering - is the sound server running at all?"
fi

sect "The RAOP module in PipeWire"
if ls /usr/lib/pipewire-*/libpipewire-module-raop-*.so >/dev/null 2>&1; then
  ok "libpipewire-module-raop-{discover,sink}.so present on disk"
  pacman -Qq pipewire-zeroconf >/dev/null 2>&1 && ok "the pipewire-zeroconf package is installed"
else
  bad "RAOP-modulerna saknas - de ligger INTE i 'pipewire' utan i eget paket:"
  echo "         sudo pacman -S pipewire-zeroconf"
  echo "       (utan det svarar 'pactl load-module module-raop-discover' med"
  echo "        'Failure: No such entity' - .so-filen finns helt enkelt inte)"
fi

sect "Avahi (mDNS - without it no devices are found)"
if systemctl is-active --quiet avahi-daemon; then ok "avahi-daemon running"
else bad "avahi-daemon is stopped  ->  sudo systemctl enable --now avahi-daemon"; fi

sect "Loaded modules"
pactl list short modules 2>/dev/null | grep -E "raop|null-sink|loopback" || warn "no raop/null-sink/loopback loaded yet (the app loads them at startup)"

sect "AirPlay sinks right now"
pactl list short sinks 2>/dev/null | grep -i raop || warn "no raop sinks - run: pactl load-module module-raop-discover"

sect "What is announced on the network?"
echo "-- _raop._tcp (AirPlay 1 audio, what PipeWire can send to):"
avahi-browse -rpt _raop._tcp 2>/dev/null | grep "^=" | awk -F';' '{print "   " $4 "  " $8 ":" $9 "  " $10}' || warn "nothing"
echo "-- _airplay._tcp (AirPlay 2):"
avahi-browse -rpt _airplay._tcp 2>/dev/null | grep "^=" | awk -F';' '{print "   " $4 "  " $8 ":" $9}' || warn "nothing"

sect "Firewall (block the timing port and nothing is heard)"
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  warn "firewalld is active - RAOP needs inbound UDP (timing/control). Try: sudo firewall-cmd --add-service=mdns"
elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  warn "ufw is active - allow UDP from the speakers' addresses"
else
  ok "no active firewall found"
fi

sect "OwnTone (the engine for HomePod, Apple TV and Macs)"
if ! command -v owntone >/dev/null 2>&1; then
  warn "owntone missing - HomePods and Apple TVs cannot be reached  ->  paru -S owntone-server"
else
  ok "owntone installed"
  if systemctl is-active --quiet owntone 2>/dev/null; then
    ok "service running"
  else
    bad "service is down  ->  sudo systemctl start owntone"
  fi

  # Without pipe_autostart OwnTone never starts playback when audio begins to
  # arrive, and everything is silent without anything looking broken.
  if grep -qE '^[[:space:]]*pipe_autostart[[:space:]]*=[[:space:]]*true' /etc/owntone.conf 2>/dev/null; then
    ok "pipe_autostart is set"
  else
    bad "pipe_autostart missing from /etc/owntone.conf  ->  ./setup-owntone.sh"
  fi

  if [ -p /srv/music/airplayhub.fifo ]; then
    ok "the audio pipe exists"
  else
    bad "/srv/music/airplayhub.fifo missing  ->  ./setup-owntone.sh"
  fi

  # AirPlay 2 devices require PTP sync. Without the port OwnTone falls back to
  # NTP, and HomePods refuse to play.
  if ss -uln 2>/dev/null | grep -qE ':(319|320)\b'; then
    ok "PTP port bound - AirPlay 2 can synchronise"
  else
    bad "PTP port 319 unbound - HomePods stay silent  ->  ./setup-owntone.sh"
  fi

  if curl -fsS --max-time 3 http://localhost:3689/api/player >/dev/null 2>&1; then
    ok "API responding on localhost:3689"
  else
    bad "API not responding - see: journalctl -u owntone -n 30"
  fi
fi

sect "The audio feed to OwnTone"
# Only relevant when an AirPlay 2 room is on - otherwise it should be down.
if pgrep -x parec >/dev/null 2>&1; then
  ok "parec is feeding the pipe"
  pid=$(pgrep -x parec | head -1)
  flags=$(grep -oP '^flags:\s*\K[0-7]+' "/proc/$pid/fdinfo/1" 2>/dev/null || echo "")
  if [ -n "$flags" ] && [ $(( 8#$flags & 8#4000 )) -ne 0 ]; then
    bad "O_NONBLOCK is set on the output - samples are dropped and audio goes silent"
  elif [ -n "$flags" ]; then
    ok "the write is blocking, as it should be"
  fi
else
  ok "no feed running (normal when no HomePod room is on)"
fi

sect "The web interface for your phone"
if systemctl --user is-active --quiet airplay-hub-web.service 2>/dev/null; then
  ok "service running"
  address=$(python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('10.255.255.255', 1)); print(s.getsockname()[0])
except OSError:
    print('127.0.0.1')
finally:
    s.close()" 2>/dev/null)
  if curl -fsS --max-time 3 -o /dev/null "http://127.0.0.1:8730/api/rooms" 2>/dev/null; then
    ok "responding - open http://$address:8730 on your phone"
  else
    bad "service running but not responding  ->  journalctl --user -u airplay-hub-web -n 20"
  fi
else
  warn "not running  ->  systemctl --user enable --now airplay-hub-web"
fi

sect "The rooms as the app sees them"
python - <<'PY' 2>/dev/null || echo "   (could not read the rooms - run from the project directory)"
import sys
sys.path.insert(0, ".")
import rooms
current = rooms.list_rooms()
if not current:
    print("   no rooms found")
for r in current:
    state = "ON " if r.on else "off"
    status = "" if r.reachable else "  (not answering)"
    print(f"   {state} {r.name:20} {r.engine:9} {r.volume:3}%{status}")
PY
