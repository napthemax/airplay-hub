#!/usr/bin/env python3
"""
A room is a room. Which engine happens to drive it is the house's problem.

The app has two engines under the hood — PipeWire for plain AirPlay 1
receivers, OwnTone for anything requiring FairPlay — but that is a technical
circumstance, not something the user should have to choose between. This module
merges them into a single list of rooms and decides for itself who drives what.

The rule is simple and follows from what the devices can actually do:

    requires FairPlay  ->  OwnTone   (PipeWire can never reach them)
    everything else    ->  PipeWire  (fewer moving parts, no fifo)

The same speaker often shows up in both engines. Listing it twice is confusing,
and switching it on in both at once sounds doubled and out of phase — RAOP only
admits one sender at a time anyway. Hence: one row per room, one engine per room.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import bridge
import hubdelay
import owntone
import pwhub

# Re-exported so the window does not have to import owntone for the slider.
OFFSET_MIN = owntone.OFFSET_MIN
OFFSET_MAX = owntone.OFFSET_MAX
HEADROOM_MS = 500
# Measured on a HomePod: clipping around -1850 ms, silence at -2000.
CLIP_WARN_MS = -1850
CLICK_SECONDS = 6

# OwnTone lists the server's own sound card as an "output". That is not a room.
NOT_A_ROOM = {"computer"}


@dataclass
class Room:
    name: str
    engine: str                 # "pipewire" or "owntone"
    target: str                 # sink name, or OwnTone output id
    on: bool = False
    volume: int = 100
    ip: str | None = None
    protocol: str = ""          # "AirPlay 1" / "AirPlay 2"
    note: str = ""              # why this particular engine
    needs_pin: bool = False     # waiting for a code from the device's screen
    reachable: bool = True      # False = seen earlier, not answering now
    offset_ms: int = 0          # timing trim, OwnTone rooms only
    details: list[tuple[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.name.strip().lower()

    @property
    def can_offset(self) -> bool:
        """Can this room be shifted in time on its own?

        OwnTone has a per-output offset. PipeWire has no equivalent that both
        moves the audio and leaves shairport-sync playing — those latency
        overrides were tried and they silence AirPlay 1 rooms. PipeWire rooms
        of the same kind stay in phase with each other on their own.
        """
        return self.engine == "owntone" and self.reachable


def _owntone_outputs() -> dict[str, owntone.Output]:
    """Keyed by name. Empty dict if OwnTone is down — then we run on PipeWire."""
    try:
        outs = owntone.outputs()
    except owntone.OwnToneError:
        return {}
    result: dict[str, owntone.Output] = {}
    for out in outs:
        key = out.name.strip().lower()
        if key in NOT_A_ROOM:
            continue
        # The same speaker may announce both AirPlay 1 and 2. The latter wins,
        # since that is what the device would rather speak.
        previous = result.get(key)
        if previous is None or ("2" in out.kind and "2" not in previous.kind):
            result[key] = out
    return result


def _pipewire_sinks() -> dict[str, pwhub.Sink]:
    """Keyed by description, one sink per room."""
    result: dict[str, pwhub.Sink] = {}
    for sink in pwhub.list_sinks():
        if not sink.is_raop:
            continue
        key = sink.description.strip().lower()
        if not key:
            continue
        previous = result.get(key)
        # Devices show up over both IPv4 and IPv6, sometimes on several ports.
        # The one with a readable IPv4 address is the one we can say something
        # useful about.
        if previous is None or (pwhub.sink_ip(sink.name) and not pwhub.sink_ip(previous.name)):
            result[key] = sink
    return result


# Offsets already written back to OwnTone this process, so a playing room is
# not torn down again on every refresh.
_restored_offsets: set[str] = set()


# Rooms seen during this run. A speaker that loses power disappears from both
# mDNS and the engines, and its row would simply vanish — which looks like the
# device never existed. Remembering it and showing it greyed out distinguishes
# "not in this house" from "unplugged right now".
_seen: dict[str, Room] = {}


def forget_unreachable() -> None:
    """Drop rooms that are not answering. For clearing the list by hand."""
    for key in [k for k, r in _seen.items() if not r.reachable]:
        del _seen[key]


def list_rooms() -> list[Room]:
    # Discovery must be running or every AirPlay 1 room silently falls back to
    # OwnTone — which holds the session and reports play, but shairport-sync
    # receivers never produce sound from it. That was the very first mystery of
    # this project, and it must not be able to come back quietly.
    try:
        pwhub.ensure_raop_discover()
    except pwhub.PactlError:
        pass

    ot = _owntone_outputs()
    pw = _pipewire_sinks()
    routes = pwhub.active_routes()

    # Loopbacks whose sink died (a failed RAOP handshake takes the sink with
    # it) linger invisibly and hold state. Clean them before deciding anything.
    try:
        pwhub.prune_orphan_routes({s.name for s in pwhub.list_sinks()}, routes)
    except pwhub.PactlError:
        pass

    found: list[Room] = []
    for key in sorted(set(ot) | set(pw)):
        out = ot.get(key)
        sink = pw.get(key)
        name = (out.name if out else sink.description).strip()

        fairplay = bool(out and "2" in out.kind)
        if fairplay or sink is None:
            if out is None:
                continue
            room = Room(
                name=name,
                engine="owntone",
                target=out.id,
                on=out.selected,
                volume=out.volume,
                protocol=out.kind,
                needs_pin=out.needs_pin,
                offset_ms=out.offset_ms,
                note=(
                    "Requires FairPlay pairing, which only OwnTone can do."
                    if fairplay
                    else "No PipeWire sink found — OwnTone fallback. NOTE: "
                         "shairport-sync receivers stay silent on this path."
                ),
            )
            _apply_remembered_offset(room)
        else:
            room = Room(
                name=name,
                engine="pipewire",
                target=sink.name,
                on=sink.name in routes,
                volume=sink.volume_pct,
                ip=pwhub.sink_ip(sink.name),
                protocol=out.kind if out else "AirPlay 1",
                note="Open AirPlay 1 receiver — reached directly, no detours.",
            )
        room.details = _details(room, out, sink)
        found.append(room)

    present = {r.key for r in found}
    for room in found:
        _seen[room.key] = room

    # Bring back what is missing, but only what we have actually seen this run.
    for key, remembered in _seen.items():
        if key in present:
            continue
        gone = Room(
            name=remembered.name,
            engine=remembered.engine,
            target=remembered.target,
            on=False,
            volume=remembered.volume,
            ip=remembered.ip,
            protocol=remembered.protocol,
            note=remembered.note,
            offset_ms=remembered.offset_ms,
            reachable=False,
        )
        gone.details = [
            ("Status", "Not answering — unplugged, disconnected or rebooting"),
            *[(k, v) for k, v in remembered.details if k != "Feed"],
        ]
        found.append(gone)

    found.sort(key=lambda r: (not r.reachable, r.name.lower()))
    return found


def _details(room: Room, out: owntone.Output | None, sink: pwhub.Sink | None) -> list[tuple[str, str]]:
    """What hides behind the info button. Nobody should need it to listen."""
    rows = [("Protocol", room.protocol or "unknown")]

    # OwnTone does not report which address it uses, but PipeWire has seen the
    # same device over mDNS and put the IP in the sink name. Good enough to show
    # even for rooms that OwnTone drives.
    ip = room.ip or (pwhub.sink_ip(sink.name) if sink is not None else None)
    if ip:
        rows.append(("Address", ip))

    rows.append(("Engine", "OwnTone" if room.engine == "owntone" else "PipeWire"))
    rows.append(("Why", room.note))

    if room.engine == "owntone":
        rows.append(("Audio path", "hub → parec → fifo → OwnTone → speaker"))
        rows.append(("Feed", "running" if bridge.is_running() else "idle"))
        delay = hubdelay.load()
        if delay.delay_ms and delay.path == hubdelay.PATH_OWNTONE:
            rows.append((
                "Hub delay",
                f"{delay.delay_ms} ms on this path when AirPlay 1 and AirPlay 2 "
                "play together (PCM into the fifo, not a PipeWire latency knob)",
            ))
        if room.offset_ms:
            direction = "later" if room.offset_ms > 0 else "earlier"
            rows.append(("Timing", f"{room.offset_ms:+d} ms ({direction} than the others)"))
        buffer_ms = start_buffer_ms()
        rows.append(("OwnTone start buffer", f"{buffer_ms} ms"))
        if out is not None:
            rows.append(("OwnTone id", out.id))
    else:
        rows.append(("Audio path", "hub → loopback → speaker"))
        delay = hubdelay.load()
        if delay.delay_ms and delay.path == hubdelay.PATH_PIPEWIRE:
            rows.append((
                "Hub delay",
                f"{delay.delay_ms} ms on this path when AirPlay 1 and AirPlay 2 "
                "play together (PCM before the loopbacks, not sess.latency.msec)",
            ))
        rows.append((
            "Timing",
            "follows the other AirPlay 1 rooms — they stay in step with each "
            "other. There is no safe per-room delay on this path. The hub "
            "delay holds the whole AirPlay 1 feed back together.",
        ))
        # The sink only means anything for rooms that actually go via PipeWire.
        if sink is not None:
            rows.append(("Sink", sink.name))
    return rows


def set_on(room: Room, on: bool) -> None:
    """Switch a room on or off, whichever engine drives it."""
    if not room.reachable:
        raise ConnectionError(f"{room.name} is not answering right now")
    if room.engine == "owntone":
        # PUT the stored offset before the session is built, so it actually
        # lands. Reselect is unnecessary — select() below starts a new session.
        if on:
            _apply_remembered_offset(room, reselect=False)
        owntone.select(room.target, on)
        return
    if on:
        pwhub.route_on(room.target, source=hubdelay.pipewire_monitor())
    else:
        pwhub.route_off(room.target)


# How long OwnTone needs between dropping a session and accepting a new one.
# Shorter and the speaker sometimes answers the ANNOUNCE with 453 Not Enough
# Bandwidth, because the old session has not been torn down yet.
_RESELECT_PAUSE = 0.4


def set_offset(room: Room, offset_ms: int) -> None:
    """Shift a room in time. Positive = later, negative = earlier.

    The value has to be stored *and* made to take effect, and those are two
    different things. OwnTone reads offset_ms exactly once, in session_make():

        session->offset_samples = device->offset_ms * quality.sample_rate / 1000

    After that the session only carries offset_samples, so changing offset_ms
    while a room is playing does nothing at all — the API accepts it, stores it,
    reports it back, and the sound never moves. It only lands the next time a
    session is built.

    So if the room is on, take it off and on again. That costs a short gap in
    that room, which is why it happens when the slider is released rather than
    while it is being dragged.
    """
    if not room.can_offset:
        raise ValueError(f"{room.name} cannot be trimmed from here")
    offset = max(OFFSET_MIN, min(OFFSET_MAX, int(offset_ms)))
    owntone.set_offset(room.target, offset)
    _remember_offset(room.key, offset)
    room.offset_ms = offset
    _restored_offsets.add(room.key)
    if room.on:
        owntone.select(room.target, False)
        time.sleep(_RESELECT_PAUSE)
        owntone.select(room.target, True)
        # Dropping the last OwnTone output can pause playback; put it back.
        sync_stream([room])


def send_pin(room: Room, pin: str) -> None:
    """The code some Apple TVs show on screen the first time."""
    if room.engine != "owntone":
        raise ValueError("only OwnTone rooms can be paired")
    owntone.send_pin(room.target, pin)


def set_volume(room: Room, volume: int) -> None:
    volume = max(0, min(100, volume))
    if room.engine == "owntone":
        owntone.set_volume(room.target, volume)
    else:
        pwhub.set_sink_volume(room.target, volume)


def any_owntone_on(current: list[Room]) -> bool:
    return any(r.on and r.engine == "owntone" for r in current)


def mixed_engines(current: list[Room] | None = None) -> bool:
    """Is the house running both engines at once?

    Only then can rooms drift apart, and only then does anyone need a timing
    control. With nothing but AirPlay 1 speakers, or nothing but AirPlay 2,
    every room shares the same audio path and the same buffer — and the control
    would just be a knob inviting you to break something that already works.
    """
    if current is None:
        current = list_rooms()
    engines = {r.engine for r in current if r.reachable}
    return len(engines) > 1


def both_engines_playing(current: list[Room] | None = None) -> bool:
    """Are AirPlay 1 and AirPlay 2 rooms actually playing at the same time?

    A mixed house with only one engine on is same-engine playback: extra hub
    delay would just add latency for no one to match.
    """
    if current is None:
        current = list_rooms()
    engines = {r.engine for r in current if r.on and r.reachable}
    return "pipewire" in engines and "owntone" in engines


def hub_delay_status(current: list[Room] | None = None) -> dict:
    """Facts both the window and the phone UI share about delay-to-slowest."""
    if current is None:
        current = list_rooms()
    return hubdelay.status(
        mixed=mixed_engines(current),
        both_playing=both_engines_playing(current),
    )


def set_hub_delay(delay_ms: int, path: str | None = None,
                  current: list[Room] | None = None) -> list[str]:
    """Persist the hub delay and apply it if both engines are playing."""
    settings = hubdelay.save(delay_ms, path)
    try:
        ensure_hub()
    except pwhub.PactlError as extra:
        return [
            f"Hub delay stored: {settings.delay_ms} ms on {settings.path}.",
            f"Could not apply it: {extra}",
        ]
    if current is None:
        current = list_rooms()
    messages = sync_stream(current)
    messages.insert(
        0,
        f"Hub delay stored: {settings.delay_ms} ms on {settings.path}.",
    )
    return messages


def sync_stream(current: list[Room] | None = None) -> list[str]:
    """Make sure audio actually goes out. Returns lines worth logging.

    Two things must hold for OwnTone rooms, and both are silent when they do
    not: the bridge must feed the fifo, and OwnTone must be in 'play'. PipeWire
    rooms need neither — the loopback reads the hub directly.

    This lives here rather than in the GUI, because the web interface switches
    on the same rooms and needs exactly the same follow-up.
    """
    if current is None:
        current = list_rooms()
    messages: list[str] = []

    if any_owntone_on(current):
        ot_delay = hubdelay.owntone_delay_ms(
            mixed_engines(current), both_engines_playing(current)
        )
        if not bridge.is_running():
            try:
                bridge.start(delay_ms=ot_delay)
                if ot_delay:
                    messages.append(
                        f"Started the audio feed to OwnTone with a {ot_delay} ms hub delay."
                    )
                else:
                    messages.append("Started the audio feed to OwnTone.")
            except bridge.BridgeError as exc:
                messages.append(f"Audio feed did not start: {exc}")
                return messages
        elif bridge.feed_delay_ms() != ot_delay:
            try:
                bridge.stop()
                bridge.start(delay_ms=ot_delay)
                if ot_delay:
                    messages.append(
                        f"OwnTone feed now delayed by {ot_delay} ms at the hub."
                    )
                else:
                    messages.append("OwnTone feed is undelayed again.")
            except bridge.BridgeError as exc:
                messages.append(f"Audio feed did not restart: {exc}")
                return messages
        try:
            if owntone.player().get("state") != "play":
                owntone.play_pipe(str(bridge.FIFO))
        except owntone.OwnToneError as exc:
            messages.append(f"Could not start playback: {exc}")
    elif bridge.is_running():
        try:
            owntone.pause()
        except owntone.OwnToneError:
            pass
        bridge.stop()

    try:
        messages.extend(
            hubdelay.sync(
                mixed=mixed_engines(current),
                both_playing=both_engines_playing(current),
            )
        )
    except hubdelay.DelayError as exc:
        messages.append(f"Hub delay: {exc}")

    return messages


def ensure_hub() -> None:
    """The hub must exist before any room can read from it."""
    if not pwhub.hub_exists():
        pwhub.create_hub()


# ---------------------------------------------------------------------------
# Mixed-engine timing — facts both the window and the phone UI share.
# ---------------------------------------------------------------------------

class SyncToneError(RuntimeError):
    """The click track could not be played through the hub."""


def config_dir() -> Path:
    """Where remembered offsets live. Override with AIRPLAYHUB_CONFIG in tests."""
    override = os.environ.get("AIRPLAYHUB_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "airplay-hub"


def _offsets_path() -> Path:
    return config_dir() / "offsets.json"


def remembered_offsets() -> dict[str, int]:
    path = _offsets_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = max(OFFSET_MIN, min(OFFSET_MAX, int(value)))
        except (TypeError, ValueError):
            continue
    return result


def remembered_offset(key: str) -> int | None:
    return remembered_offsets().get(key)


def _remember_offset(key: str, offset_ms: int) -> None:
    data = remembered_offsets()
    data[key] = max(OFFSET_MIN, min(OFFSET_MAX, int(offset_ms)))
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = _offsets_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def start_buffer_ms() -> int:
    """OwnTone's start_buffer_ms, or its shipped default if the file is missing."""
    return owntone.start_buffer_ms()


def offset_headroom(offset_ms: int, buffer_ms: int | None = None) -> int:
    """Milliseconds of start buffer left after this offset. Negative = starving."""
    buf = start_buffer_ms() if buffer_ms is None else buffer_ms
    return int(buf) - abs(int(offset_ms))


def offset_risky(offset_ms: int, buffer_ms: int | None = None) -> bool:
    """True when this offset is likely to clip or starve the OwnTone buffer."""
    if offset_ms <= CLIP_WARN_MS:
        return True
    return offset_headroom(offset_ms, buffer_ms) < HEADROOM_MS


def suggested_buffer_ms(current: list[Room], buffer_ms: int | None = None) -> int:
    """Smallest start_buffer_ms that still leaves HEADROOM_MS around every offset."""
    buf = start_buffer_ms() if buffer_ms is None else buffer_ms
    needed = owntone.MIN_START_BUFFER_MS
    for room in current:
        if room.can_offset:
            needed = max(needed, abs(room.offset_ms) + HEADROOM_MS)
    return max(buf, needed)


def sync_script() -> Path:
    return Path(__file__).resolve().parent / "sync.sh"


def buffer_command(milliseconds: int) -> str:
    """What to run in a terminal to change OwnTone's start buffer (needs sudo)."""
    ms = max(owntone.MIN_START_BUFFER_MS, int(milliseconds))
    return f"{sync_script()} owntone {ms}"


def _apply_remembered_offset(room: Room, reselect: bool | None = None) -> None:
    """Write a stored offset back to OwnTone if it does not already have it.

    OwnTone forgets the slider across a service restart. We remember it by
    room name so the next session is built with the same trim.
    """
    if not room.can_offset:
        return
    want = remembered_offset(room.key)
    if want is None or want == room.offset_ms:
        return
    try:
        owntone.set_offset(room.target, want)
        room.offset_ms = want
    except owntone.OwnToneError:
        return
    should_reselect = room.on if reselect is None else reselect
    if should_reselect and room.key not in _restored_offsets:
        try:
            owntone.select(room.target, False)
            time.sleep(_RESELECT_PAUSE)
            owntone.select(room.target, True)
        except owntone.OwnToneError:
            pass
    _restored_offsets.add(room.key)


def sync_guide_text(buffer_ms: int | None = None) -> dict[str, str | list[str]]:
    """Plain-language copy for the in-app guide. Shared by the window and phone."""
    buf = start_buffer_ms() if buffer_ms is None else buffer_ms
    return {
        "when_title": "When this matters",
        "when": (
            "Only when AirPlay 1 rooms (Volumio, shairport-sync) play together "
            "with AirPlay 2 rooms (HomePod, Apple TV). The two paths buffer "
            "differently, and that is heard as an echo between rooms."
        ),
        "when_not": (
            "A house with only one kind of speaker stays in step by itself. "
            "This guide stays hidden then — there is nothing to trim."
        ),
        "steps_title": "How to trim",
        "steps": [
            "Play the same audio in the rooms you want to match. The click track "
            "is easier to judge than music.",
            "Start with Hold back the faster path (below the room list): delay "
            "AirPlay 1 at the hub until the rooms meet. That does not eat "
            "OwnTone's start buffer.",
            "A room that is ahead (you hear it first): delay it. On an AirPlay 2 "
            "room, open i, drag toward later, then release. The change takes "
            "effect after a short gap in that room.",
            "A room that lags (often a HomePod): do not drag toward earlier "
            "until the sound clips or goes silent. Negative offset eats OwnTone's "
            "start buffer — there is no earlier audio to play. Delay the rooms "
            "that are ahead instead (hub delay first, then this slider).",
            "If a HomePod still lags after that, raise OwnTone's start buffer "
            "so there is headroom, then try a modest earlier offset. Changing "
            "the buffer needs sudo and restarts OwnTone.",
        ],
        "buffer_title": "The start buffer",
        "buffer": (
            f"OwnTone currently starts after buffering {buf} ms. The per-room "
            f"slider draws from that same buffer. Keep at least {HEADROOM_MS} ms "
            "of headroom: start_buffer_ms minus the absolute offset should stay "
            f"around {HEADROOM_MS} ms or more. An offset of −2000 ms therefore "
            "wants a buffer around 2500 ms."
        ),
        "avoid": (
            "Do not look for a PipeWire latency setting to slow AirPlay 1 rooms. "
            "Overriding sess.latency.msec or loopback latency silences "
            "shairport-sync receivers. Use the hub delay instead."
        ),
    }


def sync_overview(current: list[Room] | None = None) -> dict:
    """Snapshot both UIs use to explain mixed-engine timing."""
    if current is None:
        current = list_rooms()
    mixed = mixed_engines(current)
    buffer_ms = start_buffer_ms()
    rooms_out: list[dict] = []
    for room in current:
        if not room.reachable:
            continue
        item: dict = {
            "key": room.key,
            "name": room.name,
            "engine": room.engine,
            "protocol": room.protocol,
            "on": room.on,
            "can_offset": room.can_offset and mixed,
            "offset_ms": room.offset_ms,
        }
        if room.can_offset:
            item["headroom_ms"] = offset_headroom(room.offset_ms, buffer_ms)
            item["min_buffer_ms"] = abs(room.offset_ms) + HEADROOM_MS
            item["risky"] = offset_risky(room.offset_ms, buffer_ms)
        rooms_out.append(item)
    suggested = suggested_buffer_ms(current, buffer_ms)
    return {
        "mixed": mixed,
        "buffer_ms": buffer_ms,
        "headroom_ms": HEADROOM_MS,
        "offset_min": OFFSET_MIN,
        "offset_max": OFFSET_MAX,
        "clip_warn_ms": CLIP_WARN_MS,
        "suggested_buffer_ms": suggested,
        "buffer_command": buffer_command(suggested),
        "min_buffer_ms": owntone.MIN_START_BUFFER_MS,
        "guide": sync_guide_text(buffer_ms),
        "rooms": rooms_out,
    }


def write_click_wav(seconds: int = CLICK_SECONDS, path: Path | None = None,
                    rate: int = 44100) -> Path:
    """A few sharp clicks, one per second, 44100 Hz s16le stereo.

    Easier to hear which room is ahead than a music track. Standard library
    only — no extra decoder.
    """
    seconds = max(1, min(12, int(seconds)))
    if path is None:
        fd, name = tempfile.mkstemp(prefix="airplayhub-clicks-", suffix=".wav")
        os.close(fd)
        path = Path(name)

    nframes = seconds * rate
    click_len = max(1, int(rate * 0.012))
    buf = bytearray()
    pack = struct.Struct("<hh").pack
    two_pi = 2 * math.pi
    for i in range(nframes):
        pos = i % rate
        if pos < click_len:
            env = 1.0 - (pos / click_len)
            sample = int(20000 * env * math.sin(two_pi * 1000 * i / rate))
        else:
            sample = 0
        buf.extend(pack(sample, sample))

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(buf))
    return path


def hub_player_command(wav: Path) -> list[str]:
    """paplay or pw-play aimed at the hub. Raises SyncToneError if neither exists."""
    target = pwhub.HUB_SINK
    if shutil.which("paplay"):
        return ["paplay", f"--device={target}", str(wav)]
    if shutil.which("pw-play"):
        return ["pw-play", "--target", target, str(wav)]
    raise SyncToneError(
        "Need paplay or pw-play to play the test clicks "
        "(install libpulse or pipewire-pulse)."
    )


def play_sync_clicks(seconds: int = CLICK_SECONDS) -> str:
    """Play the click track through the hub so every selected room hears it."""
    ensure_hub()
    wav = write_click_wav(seconds)
    try:
        cmd = hub_player_command(wav)
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=seconds + 8
            )
        except FileNotFoundError as exc:
            raise SyncToneError(f"Player vanished: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SyncToneError("The click track did not finish in time") from exc
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or f"{cmd[0]} failed").strip()
            raise SyncToneError(msg)
    finally:
        try:
            wav.unlink()
        except OSError:
            pass
    return f"Played {seconds} clicks through the hub."
