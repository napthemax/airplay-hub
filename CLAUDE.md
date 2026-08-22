# AirPlay Hub — multiroom AirPlay on Linux

Airfoil-like app: the same audio to several AirPlay devices at once, with on/off
and volume per room. PyQt6 GUI. Target platform CachyOS/Arch with PipeWire.

> Addresses in this file are examples (`10.0.0.x`), not real devices. The
> measurements are real — they come from the development machine and are kept
> because they say something about what to expect.

Write code and comments in English. Conversation with the maintainer is in
Swedish; the project is not.

## Architecture — two engines, one list

```
PlexAmp ─┐                              ┌─► loopback ─► RAOP sink ─► AirPlay 1 device
Firefox ─┼─► null sink "AirPlayHub" ────┤
Spotify ─┘                              └─► parec ─► fifo ─► OwnTone ─► AirPlay 2 device
```

- **PipeWire/RAOP** for devices without FairPlay. Driven through `pactl`: one
  `module-loopback` per active room, so rooms can be switched while playing.
- **OwnTone** for anything requiring FairPlay/AirPlay 2. Driven over its JSON API
  on `localhost:3689`. Fed raw PCM 44100/16/2 through a named pipe.

No ffmpeg anywhere — see "Dead ends" below.

**None of this shows in the window.** `rooms.py` puts a layer over both and
yields a single list of rooms. Which engine drives a room follows from what the
device can do, not from anything the user picks:

```
requires FairPlay  ->  OwnTone   (PipeWire can never reach them)
everything else    ->  PipeWire  (fewer moving parts, no fifo)
```

The same speaker often announces itself in both engines, and they are matched by
name — PipeWire's `device.description` is identical to OwnTone's output name.
One row per room, one engine per room. Showing it twice would confuse, and
switching it on in both at once sounds doubled and out of phase: RAOP admits one
sender at a time. The plumbing stays behind the info button on each row, for
whoever wonders.

Add new features to `rooms.py`, not to the GUI. The window should not need to
know which engine is involved — the day `main.py` starts importing `owntone` for
anything but error handling, the layer has begun to leak.

**There are two clients of `rooms.py`:** the window and the web interface. That
is why `sync_stream()` and `ensure_hub()` live in the facade and not in
`main.py` — both must do the same follow-up when an OwnTone room is switched on,
or one of them goes silent. Add anything that changes state to `rooms.py` and
the phone gets it for free.

## Files

| File | Role |
|---|---|
| `main.py` | PyQt6 GUI — one room list, no engine split |
| `webui.py` | Web interface for the phone, stdlib http, private networks only |
| `rooms.py` | Merges both engines into rooms and picks the engine per room |
| `pwhub.py` | PipeWire backend, everything through `pactl` |
| `owntone.py` | OwnTone JSON API client, stdlib only |
| `bridge.py` | Starts and stops the `parec` that feeds OwnTone's fifo |
| `probe-raop.sh` | Reads devices' mDNS TXT: who can be reached, and why not |
| `diagnose.sh` | Environment check when nothing is heard |
| `owntone-bridge.sh` | fifo + `parec` by hand; `--install-unit` writes a user unit |
| `setup-owntone.sh` | Makes OwnTone ready: pipe_autostart, log, fifo, PTP, start |
| `debug-owntone.sh` | Switches OwnTone to debug logging; `off` goes back |
| `raop-discover.conf` | Latency for every discovered AirPlay sink |
| `sync.sh` | Shows and adjusts the phase between the engines' rooms |
| `install.sh` | Install: packages, OwnTone, menu entry, web service |
| `packaging/` | .desktop, icon and systemd unit that install.sh puts in place |
| `README.md` | Installation and troubleshooting for the user |

## State of things

Development moved from macOS to the CachyOS machine (10.0.0.10, wlan0). Real
PipeWire and real speakers are available — do not mock anything that can be run.

Working, verified by ear on 2026-08-22:
- **HomePod through OwnTone.** The whole chain null sink → `parec` → fifo →
  OwnTone → AirPlay 2. The only thing missing was the PTP port, see "This
  machine" below. No pairing, no PIN, no Home app settings had to be touched.
- **The kitchen stereo (Volumio/shairport-sync) through PipeWire.**
- **The porch through PipeWire.** The AirPort Express was replaced by a
  Raspberry Pi 3 running Volumio (10.0.0.21, `et=0,1`, shairport-sync) — the
  same setup as the kitchen. It is discovered automatically over mDNS and needs
  no config file of its own.

**Three faults kept the OwnTone path silent from the GUI**, all fixed
2026-08-22. None of them produced an error message — the app looked like it
succeeded every time.

1. **Nobody started the bridge.** `main.py` had no code running `parec`; it only
   existed in `owntone-bridge.sh`, which had to be run by hand. OwnTone
   connected to the speaker and reported `state: play` while playing silence.
   `bridge.py` handles it now, wired to `sync_stream()`.
2. **`O_NONBLOCK` leaked into parec.** The first version of `bridge.start()`
   opened the fifo non-blocking — which you must, or the call hangs until
   OwnTone opens the other end — but left the flag set when `parec` inherited
   the descriptor. `parec` then wrote non-blocking and got `EAGAIN` the moment
   the fifo filled, so samples vanished. The shell's `>` in `owntone-bridge.sh`
   is blocking, which is why running it by hand worked while the app did not.
   `start()` now clears the flag with `fcntl` before starting `parec`. Check
   with `grep flags /proc/$(pgrep -x parec)/fdinfo/1` — `O_NONBLOCK` is octal
   `04000` and must *not* be set.
3. **Nobody pressed play.** `pipe_autostart` starts playback when data first
   appears in the pipe, but never after a pause — long known, see owntone-server
   issue 465. `owntone.play()` is not reliable either. `owntone.play_pipe()`
   does what the issue recommends: point at the pipe in the queue again with
   `POST /api/queue/items/add?uris=...&clear=true&playback=start`.

The status line now prints `NO AUDIO GOING OUT` and `PAUSED`, so that silence is
never invisible again.

Useful when debugging: Volumio answers on `http://10.0.0.22/api/v1/getState` and
states outright whether it is receiving an AirPlay stream (`"trackType":"airplay"`,
title, sample rate, volume, mute). That is a harder confirmation than OwnTone
saying `state: play` — it measures the receiving end, not the sending end.

Open:
1. **The two engines cannot share a device.** PipeWire creates a RAOP sink for
   *every* mDNS-announced device, HomePods included, even though it can never
   play to them (FairPlay). Switch one of those on in the PipeWire path and it
   takes the RTSP session, leaving OwnTone with `ANNOUNCE request failed in
   session startup: 453 Not Enough Bandwidth` — which in RAOP means "busy".
   `rooms.py` picks one engine per room, which covers the normal case. What
   remains is that `module-raop-discover` is now loaded from
   `raop-discover.conf` and therefore cannot be unloaded at runtime: `pactl
   unload-module` answers `Access denied` for anything that came from a config
   file rather than from `pactl`.
2. **AirPort Express is being phased out entirely.** The porch A1392 is already
   replaced by a Raspberry Pi 3 with Volumio. Gen 1 (A1264) and the remaining
   Express units are to be sold and replaced by Pi + Volumio everywhere. That
   makes the whole FairPlay discussion and the link-local trouble moot:
   shairport-sync announces `et=0,1` and is discovered automatically.
3. A pair of Raspberry Pi 4 (1 GB) are on order for the remaining rooms. Expect
   the same power-supply trap as on the Pi 3 — do not skimp on the adapter.
4. Nothing may be hardcoded. Right now no addresses appear in the code at all:
   `raop-discover.conf` matches every device with `raop.ip = "~.*"`, and the
   concrete addresses live only in this file as notes. Keep it that way.

## The rooms must stay in phase

That is the whole point of the app, and the easiest thing to break by accident:
the two engines buffer different amounts, and the difference is heard as an echo
between rooms. Measured 2026-08-22, the kitchen ran 1.7 seconds ahead of the
bedroom.

Do not count in the milliseconds you want, but in what PipeWire actually does:

- `sess.latency.msec` is **rounded down to whole RAOP packets** of 352 samples.
  Ask for 2250.884 and you get 281 packets = `98912/44100` = 2242.9 ms. It shows
  as `node.latency` on the sink in `pactl list sinks`.
- **The loopback adds its own.** `latency_msec=400` came out as `6400/48000` =
  133 ms — PipeWire picks its own period size.
- **OwnTone** is governed by `start_buffer_ms` in `/etc/owntone.conf`, default
  2250 ms. The config file says 500 ms usually works, but higher values tolerate
  network trouble better.
- **The devices themselves** buffer too, by differing amounts, and it cannot be
  read off. That is why the last adjustment is manual.

Current setting: `sess.latency.msec = 2115` → 2107 + 133 = 2240 ms against
OwnTone's 2250. Ten milliseconds apart, which cannot be heard.

`./sync.sh` shows both sides and the difference; `./sync.sh +100` and `-100`
shift the PipeWire rooms and round to whole packets for you.

**But theory does not go all the way.** With PipeWire at 2240 ms and OwnTone at
2250, the kitchen and the porch were in perfect sync with each other while the
HomePod still lagged by about a second. That difference lives in the device
itself — a HomePod buffers more on its own than shairport-sync does, and it is
invisible from this side. The last stretch has to be done by ear.

Hence `offset_ms` per room: OwnTone's API takes `-2000` to `2000` ms on each
output, positive meaning later. It is reached from the info button on the room's
row, with a slider that shifts the room while the music plays. PipeWire has no
equivalent worth trusting — `latency_msec` on the loopback can be set but
PipeWire still picks its own period size (asked for 400, got 133), so those
rooms are shifted together with `sync.sh` instead. That is good enough, since
they are already in phase with each other: same engine, same buffer.

The slider is only shown when the house actually runs both engines, see
`rooms.mixed_engines()`. With only AirPlay 1 speakers, or only AirPlay 2, it
would be a knob inviting you to break something that already works.

The warning `sess.latency.msec ... should be an integer multiple of rtp.ptime`
cannot be avoided except at 3520 ms: the packet length is 3520/441 ms, and that
only lands on whole milliseconds at 441 packets. It is cosmetic — PipeWire
rounds and carries on. Do not chase it.

## The network is the bottleneck, not the code

Measured 2026-08-22, with strong signal (95) and packet loss everywhere anyway:

| Path | Packet loss | Max rtt |
|---|---|---|
| Machine → router | 3.3 % | 30 ms |
| Machine → porch (Pi 3) | 4–40 % | 107 ms |
| Machine → kitchen | 1–10 % | 47 ms |

That the router loses packets too means the fault sits before the speakers. The
machine is on 2.4 GHz channel 4, where three networks from the same access point
crowd together with neighbours on channels 1, 2, 7 and 11. Signal strength is
excellent — it is the congestion that costs. RAOP sends audio in real time, so
every dropped packet is audible.

TCP was tried as a remedy — devices announce `tp=TCP,UDP` — but shairport-sync
answered with `mod.raop-sink: timestamp: expected ... != actual` and PipeWire
dropped the sink entirely; the device vanished from `pactl`. The transport
therefore stays on udp. Fix the power and the network instead.

What is left to try, in falling order of likely effect:

1. **USB ethernet to the machine.** It is the server; all traffic passes through
   it, and it has no network port (only `wlan0`). Biggest effect of all.
2. ~~**Ethernet to the Pi.**~~ Done 2026-08-22. The porch no longer stutters and
   sits in perfect sync with the kitchen.
3. **5 GHz.** The 5 GHz network is on channel 40, but at signal 37 against 95 on
   2.4 GHz. Cleaner band, weaker coverage — worth measuring, not assuming.
4. ~~**Power supply to the Pi.**~~ Fixed 2026-08-22: swapping 1 A for 5 V/3 A
   took packet loss towards the porch Pi from 4–40 % to 0 %, and max rtt from
   107 ms to 31 ms. A Pi 3 needs 2.5 A — do not skimp here.

## Installation and packaging

`install.sh` is the only way in for a new user. It checks system packages, runs
`setup-owntone.sh` if needed, puts `raop-discover.conf` in place, writes two
launchers in `~/.local/bin`, installs the menu entry and icon, and enables the
web interface as a systemd user service.

AppImage was rejected deliberately. It can bundle Python and PyQt6, but not
PipeWire, `parec` or OwnTone — and those are the awkward ones. An AppImage would
have started and been silent, which is precisely this project's worst failure
mode. See "Dead ends".

The target platform is Linux. macOS has neither PipeWire nor `pactl`, so a Mac
version would be a new project. The "Mac feel" ambition is about usability: a
menu entry, no terminal requirement, everything set up by a script.

## This machine

- `owntone-server` 29.3 from the AUR. System service, runs as `User=owntone` via
  `/usr/lib/systemd/system/owntone.service.d/override.conf`.
- Two things the Arch package does not handle, which `setup-owntone.sh` fixes:
  - `/var/log/owntone.log` must exist and be owned by `owntone`. Otherwise the
    unit logs `Failed to set ownership on logfile` and you debug blind.
  - `AmbientCapabilities=CAP_NET_BIND_SERVICE` in its own drop-in. Without it
    OwnTone cannot bind PTP port 319, logs `Could not bind to PTP event port`
    and falls back to NTP — which AirPlay 2 devices do not accept. AirPlay 1 is
    unaffected.
- The log goes to the journal: `journalctl -u owntone -f`. Filter out
  `Running query` in debug mode, or SQL drowns everything.
- **`403 Forbidden` on PIN start does not mean HomePods are locked.** That error
  came from a *Mac*, whose AirPlay receiver is governed by System Settings →
  General → AirDrop & Handoff and has nothing to do with the Home app. HomePods
  let us in immediately once PTP worked. Do not generalise from a Mac to a
  HomePod.

## Dead ends — do not go back here

- **`ffmpeg -f airplay` does not exist.** Never has, in any FFmpeg build. Early
  project documentation claimed the muxer was missing from the Arch package.
  Wrong.
- **Raw RTP does not work.** AirPlay receivers require an RTSP handshake first;
  packets that simply appear are discarded. That is why processes "started
  without errors" while everything was silent.
- **The RAOP modules live in `pipewire-zeroconf`**, not in `pipewire`. Without
  it `pactl load-module module-raop-discover` answers `Failure: No such entity`.
- **Do not leave the null sink on its default format.** 48000 Hz float32 forces
  `parec` to resample, and a null sink has no clock of its own — it runs off the
  system timer. The drift is enough to starve OwnTone with `Source is not
  providing sufficient data, temporarily suspending playback`. Nothing looks
  broken, it just goes quiet. `create_hub()` therefore sets `rate=44100
  channels=2 format=s16le`, the same format OwnTone reads the fifo with.
- **AppImage solves the wrong problem.** The hard dependencies are not Python's
  but the system's: PipeWire with `pipewire-zeroconf`, `parec`, avahi and a
  configured OwnTone. None of that goes into an AppImage. The result would be an
  app that starts and is silent — use `install.sh` instead.
- **FairPlay cannot be worked around.** `et=0,3,5` in the TXT record means
  PipeWire can never reach the device. HomeKit pairing does not help; the
  pairing must be done by the sender, and OwnTone is the one that can.

## How to work here

- Devices are never hardcoded. Everything is discovered over mDNS/`pactl`.
- Never hunt processes with `pgrep -f` or `pkill -f` when something is to be
  killed. The pattern matches the whole command line and hits shells and editors
  that happen to have the text in their arguments. `bridge._pids()` reads
  `/proc` and requires the process to actually be named `parec`.
- The backends read their state from the system (`pactl list`), not from memory,
  so the GUI can be restarted without losing the streams.
- **This machine has real PipeWire and real speakers — run for real instead of
  mocking.** Earlier development happened on macOS without PipeWire, so the
  tests were built against mocked `pactl`/`avahi-browse` and a mocked OwnTone.
- OwnTone saying `state: play` proves nothing. Measure the receiving end, or ask
  whoever is standing in the room.
- Test through `main.py`, not just against the API with `curl`. All three faults
  above lived in the app, not in the engine, and were therefore invisible from
  the command line. That something works by hand says nothing about whether it
  works in the GUI.
