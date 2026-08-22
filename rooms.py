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

import time
from dataclasses import dataclass, field

import bridge
import owntone
import pwhub

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

        OwnTone has a per-output offset. PipeWire has no equivalent — the
        loopback's latency_msec can be set, but PipeWire still picks its own
        period size (asked for 400 ms, got 133), so it cannot be trusted for
        fine adjustment. PipeWire rooms are shifted together with ./sync.sh
        instead.
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
        if room.offset_ms:
            direction = "later" if room.offset_ms > 0 else "earlier"
            rows.append(("Timing", f"{room.offset_ms:+d} ms ({direction} than the others)"))
        if out is not None:
            rows.append(("OwnTone id", out.id))
    else:
        rows.append(("Audio path", "hub → loopback → speaker"))
        rows.append(("Timing", "follows the other PipeWire rooms, adjust with ./sync.sh"))
        # The sink only means anything for rooms that actually go via PipeWire.
        if sink is not None:
            rows.append(("Sink", sink.name))
    return rows


def set_on(room: Room, on: bool) -> None:
    """Switch a room on or off, whichever engine drives it."""
    if not room.reachable:
        raise ConnectionError(f"{room.name} is not answering right now")
    if room.engine == "owntone":
        owntone.select(room.target, on)
        return
    if on:
        pwhub.route_on(room.target)
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
    owntone.set_offset(room.target, offset_ms)
    if room.on:
        owntone.select(room.target, False)
        time.sleep(_RESELECT_PAUSE)
        owntone.select(room.target, True)


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
        if not bridge.is_running():
            try:
                bridge.start()
                messages.append("Started the audio feed to OwnTone.")
            except bridge.BridgeError as exc:
                messages.append(f"Audio feed did not start: {exc}")
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

    return messages


def ensure_hub() -> None:
    """The hub must exist before any room can read from it."""
    if not pwhub.hub_exists():
        pwhub.create_hub()
