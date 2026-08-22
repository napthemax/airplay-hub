#!/usr/bin/env bash
# Installs AirPlay Hub so it starts from the menu, not from a terminal.
#
#   ./install.sh              install
#   ./install.sh --uninstall  remove everything it put there
#
# What happens:
#   1. missing system packages (asks before installing anything)
#   2. OwnTone made ready - pipe_autostart, log file, fifo, the PTP port
#   3. the latency setting for AirPlay devices goes into PipeWire's config
#   4. two launchers in ~/.local/bin and a menu entry
#   5. the web interface as a service, so your phone works without the window open
#
# The script is idempotent - re-run it whenever you like.

set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd)
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UNITS="$HOME/.config/systemd/user"
PWCONF="$HOME/.config/pipewire/pipewire.conf.d"

blue() { printf '\033[1;34m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }

if [ "${1:-}" = "--uninstall" ]; then
  blue "Removing AirPlay Hub"
  systemctl --user disable --now airplay-hub-web.service 2>/dev/null || true
  rm -f "$UNITS/airplay-hub-web.service"
  rm -f "$BIN/airplay-hub" "$BIN/airplay-hub-web"
  rm -f "$APPS/airplay-hub.desktop"
  rm -f "$ICONS/airplay-hub.svg"
  rm -f "$PWCONF/raop-discover.conf"
  systemctl --user daemon-reload 2>/dev/null || true
  update-desktop-database "$APPS" 2>/dev/null || true
  green "Done. System packages and the OwnTone configuration are left in place."
  echo "To remove those too:  sudo pacman -Rns owntone-server"
  exit 0
fi

# ------------------------------------------------------------- 1. packages
blue "1/5  Checking system packages"

missing=()
need() {
  # $1 = pacman package, $2 = command or file to look for
  if [ -e "$2" ] || command -v "$2" >/dev/null 2>&1; then
    printf '  ✓ %s\n' "$1"
  else
    printf '  ✗ %s\n' "$1"
    missing+=("$1")
  fi
}

need pipewire            pipewire
need pipewire-pulse      pw-cat
if find /usr/lib -maxdepth 2 -name 'libpipewire-module-raop-discover.so' 2>/dev/null | grep -q .; then
  printf '  ✓ %s\n' "pipewire-zeroconf"
else
  printf '  ✗ %s\n' "pipewire-zeroconf"
  saknas+=("pipewire-zeroconf")
fi
need wireplumber         wireplumber
need libpulse            parec
need avahi               avahi-browse
if python -c "import PyQt6.QtWidgets" 2>/dev/null; then
  printf '  ✓ %s\n' "python-pyqt6"
else
  printf '  ✗ %s\n' "python-pyqt6"
  saknas+=("python-pyqt6")
fi
need owntone-server      owntone

if [ ${#missing[@]} -gt 0 ]; then
  echo
  yellow "Missing: ${missing[*]}"
  aur=""
  official=()
  for p in "${missing[@]}"; do
    [ "$p" = "owntone-server" ] && aur="$p" || official+=("$p")
  done
  if [ ${#official[@]} -gt 0 ]; then
    echo "Install with:"
    echo "  sudo pacman -S --needed ${official[*]}"
  fi
  if [ -n "$aur" ]; then
    echo "OwnTone lives in the AUR:"
    echo "  paru -S $aur     (or: yay -S $aur)"
  fi
  echo
  read -rp "Run the installation for you now? [y/N] " answer
  if [[ "$answer" =~ ^[yY]$ ]]; then
    [ ${#official[@]} -gt 0 ] && sudo pacman -S --needed "${official[@]}"
    if [ -n "$aur" ]; then
      if command -v paru >/dev/null; then paru -S "$aur"
      elif command -v yay >/dev/null; then yay -S "$aur"
      else yellow "No AUR helper found. Install $aur by hand."; fi
    fi
  else
    yellow "Skipping. Re-run ./install.sh once the packages are in place."
    exit 1
  fi
fi

# -------------------------------------------------------------- 2. OwnTone
blue "2/5  Making OwnTone ready"
ready=true
grep -qE '^[[:space:]]*pipe_autostart[[:space:]]*=[[:space:]]*true' /etc/owntone.conf 2>/dev/null || ready=false
[ -f /var/log/owntone.log ] || ready=false
[ -p /srv/music/airplayhub.fifo ] || ready=false
systemctl is-active --quiet owntone 2>/dev/null || ready=false
ss -uln 2>/dev/null | grep -qE ':(319|320)\b' || ready=false

if [ "$ready" = true ]; then
  echo "  already set up - skipping (run setup-owntone.sh by hand if needed)"
elif [ -x "$SRC/setup-owntone.sh" ]; then
  echo "  running setup-owntone.sh (needs sudo)"
  "$SRC/setup-owntone.sh"
else
  yellow "  setup-owntone.sh missing - skipping"
fi

# ------------------------------------------------------------- 3. PipeWire
blue "3/5  PipeWire cleanup"
# Earlier versions installed a raop-discover.conf with a sess.latency.msec
# override. That override silences shairport-sync receivers (sessions come up,
# no audio comes out), and a config-file module cannot be unloaded at runtime.
# The app now loads RAOP discovery itself, with defaults, via pactl.
if [ -f "$PWCONF/raop-discover.conf" ]; then
  rm -f "$PWCONF/raop-discover.conf"
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
  echo "  removed old raop-discover.conf (the app handles discovery itself now)"
else
  echo "  nothing to do — the app handles discovery itself"
fi

# ------------------------------------------------------------ 4. launchers
blue "4/5  Menu entry and launchers"
mkdir -p "$BIN" "$APPS" "$ICONS"

cat > "$BIN/airplay-hub" <<EOF
#!/usr/bin/env bash
# Starts the window. Written by install.sh - do not edit by hand.
exec python "$SRC/main.py" "\$@"
EOF
chmod +x "$BIN/airplay-hub"

cat > "$BIN/airplay-hub-web" <<EOF
#!/usr/bin/env bash
# Starts the web interface. Written by install.sh - do not edit by hand.
exec python "$SRC/webui.py" "\$@"
EOF
chmod +x "$BIN/airplay-hub-web"

cp "$SRC/packaging/airplay-hub.svg" "$ICONS/"
# The menu launches applications without ~/.local/bin on PATH, so Exec has to be
# absolute or KDE reports "could not find the program". Icon likewise: an
# absolute path works no matter which icon theme is active or how stale its
# cache is.
sed -e "s|__BIN__|$BIN|" -e "s|__ICON__|$ICONS/airplay-hub.svg|" \
  "$SRC/packaging/airplay-hub.desktop" > "$APPS/airplay-hub.desktop"
update-desktop-database "$APPS" 2>/dev/null || true
gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
echo "  airplay-hub and airplay-hub-web in $BIN"
echo "  menu entry and icon installed"

# The menu entry calls the full path and works regardless, but a terminal will
# not find `airplay-hub` unless ~/.local/bin is on PATH. Offer to fix it for
# whichever shells are actually installed - fish keeps PATH in a universal
# variable, the others in an rc file.
path_marker="# Added by airplay-hub"
path_block="
$path_marker — put ~/.local/bin on PATH if it is not there already
case \":\$PATH:\" in
  *\":\$HOME/.local/bin:\"*) ;;
  *) export PATH=\"\$HOME/.local/bin:\$PATH\" ;;
esac"

add_to_rc() {
  local rc=$1
  [ -e "$rc" ] || return 0
  if grep -q "$path_marker" "$rc" 2>/dev/null; then
    echo "  $(basename "$rc"): already set"
  else
    printf '%s\n' "$path_block" >> "$rc"
    echo "  $(basename "$rc"): PATH added"
  fi
}

if ! printf '%s' ":$PATH:" | grep -q ":$BIN:"; then
  echo
  yellow "$BIN is not on your PATH."
  echo "The menu entry works anyway, but typing 'airplay-hub' in a terminal will not."
  read -rp "Add it to your shell configuration? [Y/n] " answer
  if [[ ! "$answer" =~ ^[nN]$ ]]; then
    add_to_rc "$HOME/.zshrc"
    add_to_rc "$HOME/.bashrc"
    if command -v fish >/dev/null 2>&1; then
      # fish_add_path is idempotent and writes a universal variable, so it
      # survives without touching config.fish.
      fish -c "fish_add_path -g $BIN" 2>/dev/null && echo "  fish: PATH added"
    fi
    echo "  Open a new terminal for it to take effect."
  fi
fi

# ------------------------------------------------------- 5. the web service
blue "5/5  The web interface as a service"
mkdir -p "$UNITS"
sed "s|__INSTALL_DIR__|$BIN|" "$SRC/packaging/airplay-hub-web.service" > "$UNITS/airplay-hub-web.service"
systemctl --user daemon-reload
systemctl --user enable --now airplay-hub-web.service
sleep 2

if systemctl --user is-active --quiet airplay-hub-web.service; then
  echo "  service running"
else
  yellow "  service did not start - see: journalctl --user -u airplay-hub-web -n 20"
fi

# A firewall will silently drop the phone's connection — the browser reports a
# timeout, which looks like the server is down rather than blocked. Offer to
# open the port for the local subnet only.
subnet=$(ip -4 route 2>/dev/null | awk '/proto kernel/ && /src/ {print $1; exit}')
if systemctl is-active --quiet ufw 2>/dev/null; then
  if ! sudo ufw status 2>/dev/null | grep -q "8730"; then
    echo
    yellow "ufw is active and will block your phone from reaching the web interface."
    read -rp "Open port 8730 for ${subnet:-your local network}? [Y/n] " answer
    if [[ ! "$answer" =~ ^[nN]$ ]] && [ -n "$subnet" ]; then
      sudo ufw allow from "$subnet" to any port 8730 proto tcp comment 'AirPlay Hub web'
      echo "  opened for $subnet only — not reachable from the internet"
    fi
  fi
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  echo
  yellow "firewalld is active. To let your phone reach the web interface:"
  echo "  sudo firewall-cmd --permanent --add-port=8730/tcp --zone=home"
  echo "  sudo firewall-cmd --reload"
fi

# So the machine does not have to be logged in for the phone to work.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
  echo
  yellow "Want to control it from your phone even when not logged in?"
  echo "  sudo loginctl enable-linger $USER"
fi

echo
green "Done."
echo
echo "  Window:  look for \"AirPlay Hub\" in your application menu"
# hostname -I comes back empty on a machine with only wifi and no hostname
# answer. Ask the kernel which address is used towards the outside instead.
address=$(python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('10.255.255.255', 1)); print(s.getsockname()[0])
except OSError:
    print('your-machines-ip')
finally:
    s.close()
" 2>/dev/null || echo "your-machines-ip")
echo "  Phone:   http://$address:8730"
echo
echo "Trouble:  $SRC/diagnose.sh"
