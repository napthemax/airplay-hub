#!/usr/bin/env bash
# probe-raop.sh - why is this particular device silent?
# Reads the TXT records each AirPlay device announces and works out whether
# PipeWire can send to it at all.

c_ok=$'\033[32m'; c_bad=$'\033[31m'; c_warn=$'\033[33m'; c_off=$'\033[0m'; c_b=$'\033[1m'

# avahi escapes characters as decimal bytes: \064 = @, \195\182 = ö. Without
# decoding them both names and comparisons come out wrong.
browse() {
  if command -v python3 >/dev/null 2>&1; then
    avahi-browse -rpt "$1" 2>/dev/null | python3 -c 'import sys, re
data = sys.stdin.buffer.read()
sys.stdout.buffer.write(re.sub(rb"\\(\d{3})", lambda m: bytes([int(m.group(1))]), data))'
  else
    avahi-browse -rpt "$1" 2>/dev/null
  fi
}

seen=""
already_seen() {  # the same device is announced once per network interface
  key=$(printf '%s@%s' "$1" "$2" | tr ' ' '_')
  case " $seen " in *" $key "*) return 0 ;; esac
  seen="$seen $key"
  return 1
}

printf '%s== Enheter som annonserar _raop._tcp ==%s\n' "$c_b" "$c_off"
found=0
while IFS=';' read -r rec iface proto name svc domain host ip port txt; do
  [ "$rec" = "=" ] || continue
  name=${name#*@}                       # RAOP annonseras som "MAC@Namn"
  already_seen "$name" "$ip" && continue
  found=1

  et=$(printf '%s' "$txt" | grep -o 'et=[0-9,]*' | head -1 | cut -d= -f2)
  cn=$(printf '%s' "$txt" | grep -o 'cn=[0-9,]*' | head -1 | cut -d= -f2)
  am=$(printf '%s' "$txt" | grep -o 'am=[^" ]*'  | head -1 | cut -d= -f2)
  pw=$(printf '%s' "$txt" | grep -o 'pw=[^" ]*'  | head -1 | cut -d= -f2)

  printf '\n%s%s%s  (%s:%s)%s\n' "$c_b" "$name" "$c_off" "$ip" "$port" "${am:+  modell: $am}"
  printf '   et=%s  cn=%s\n' "${et:-?}" "${cn:-?}"

  case ",${et}," in
    *,3,*|*,5,*) printf '   %sFairPlay required%s - PipeWire cannot do it. Use OwnTone for this one.\n' "$c_bad" "$c_off" ;;
    *,4,*)       printf '   %sMFiSAP/auth-setup%s - may work, but needs raop.encryption.type=auth_setup.\n' "$c_warn" "$c_off" ;;
    *,0,*|*,1,*) printf '   %sIngen/RSA-kryptering%s - PipeWire ska klara denna.\n' "$c_ok" "$c_off" ;;
    *)           printf '   %sUnknown et record%s - see man 7 libpipewire-module-raop-sink.\n' "$c_warn" "$c_off" ;;
  esac

  case ",${cn}," in
    *,0,*|*,1,*) : ;;
    ,,)          printf '   %sno cn record%s - unknown which codecs the device accepts.\n' "$c_warn" "$c_off" ;;
    *)           printf '   %sTar varken PCM eller ALAC%s - prova raop.audio.codec=AAC.\n' "$c_warn" "$c_off" ;;
  esac

  case "$ip" in
    169.254.*) printf '   %sLink-local address%s - the device has no DHCP lease. Fix the network\n              first, or nothing the protocol says will matter.\n' "$c_bad" "$c_off" ;;
  esac
  [ "$pw" = "true" ] && printf '   %sPassword protected%s - needs raop.password.\n' "$c_warn" "$c_off"
done < <(browse _raop._tcp)
[ "$found" = 1 ] || printf '  %singa%s\n' "$c_bad" "$c_off"

printf '\n%s== Enheter som BARA annonserar _airplay._tcp ==%s\n' "$c_b" "$c_off"
raoplist=$(browse _raop._tcp | grep '^=' | cut -d';' -f4 | sed 's/^[^@]*@//')
seen=""; any=0
while IFS=';' read -r rec iface proto name svc domain host ip port txt; do
  [ "$rec" = "=" ] || continue
  printf '%s\n' "$raoplist" | grep -qxF "$name" && continue
  already_seen "$name" "$ip" && continue
  any=1
  printf '  %s%s%s  %s:%s\n' "$c_warn" "$name" "$c_off" "$ip" "$port"
  printf '     AirPlay 2 without RAOP fallback -> PipeWire can never reach it. OwnTone required.\n'
done < <(browse _airplay._tcp)
[ "$any" = 1 ] || printf '  %snone - every AirPlay 2 device also announces RAOP%s\n' "$c_ok" "$c_off"

printf '\n%s== RAOP-sinkar i PipeWire just nu ==%s\n' "$c_b" "$c_off"
pactl list sinks 2>/dev/null | awk '
  /^Sink #/           { name=""; vol=""; mute="" }
  /^\tName: .*raop/   { name=$2 }
  /^\tMute:/          { mute=$2 }
  /^\tVolume: front/  { for(i=1;i<=NF;i++) if($i ~ /%$/) { vol=$i; break } }
  /^\tMonitor Source/ { if (name != "") printf "  %-46s vol=%-6s mute=%s\n", name, vol, mute }
'
printf '\n%s== Aktiva loopbackar (hubben -> enhet) ==%s\n' "$c_b" "$c_off"
pactl list short modules 2>/dev/null | grep "module-loopback" | grep "AirPlayHub.monitor" \
  || printf '  none - no device is switched on in the app\n'
