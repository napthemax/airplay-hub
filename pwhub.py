#!/usr/bin/env python3
"""
Backend for AirPlay Hub — all talk to PipeWire/PulseAudio goes through pactl.

The audio path (no ffmpeg involved):

    PlexAmp ─┐
    Firefox ─┼─► null sink "AirPlayHub" ─► one loopback per active device
    Spotify ─┘                           ─► PipeWire's RAOP sink ─► the speaker

One loopback per device means the same audio reaches every selected device at
once, and that devices can be switched on and off while playing without cutting
the stream to the others.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

HUB_SINK = "AirPlayHub"
HUB_DESC = "AirPlay Hub"
DEFAULT_LATENCY_MS = 400


class PactlError(RuntimeError):
    """pactl/avahi did not answer as expected."""


def run(args: list[str], timeout: int = 15) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise PactlError(f"Command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PactlError(f"Timeout: {' '.join(args)}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise PactlError(msg or f"{args[0]} exited with code {proc.returncode}")
    return proc.stdout


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

@dataclass
class Sink:
    index: int
    name: str
    description: str
    volume_pct: int = 100
    muted: bool = False

    @property
    def is_raop(self) -> bool:
        return "raop" in self.name.lower()


def list_sinks() -> list[Sink]:
    try:
        data = json.loads(run(["pactl", "-f", "json", "list", "sinks"]))
    except (PactlError, json.JSONDecodeError):
        return _list_sinks_text()

    sinks: list[Sink] = []
    for entry in data:
        pcts = []
        for channel in (entry.get("volume") or {}).values():
            raw = str(channel.get("value_percent", "")).rstrip("%")
            if raw.isdigit():
                pcts.append(int(raw))
        sinks.append(
            Sink(
                index=int(entry["index"]),
                name=entry["name"],
                description=entry.get("description") or entry["name"],
                volume_pct=max(pcts) if pcts else 100,
                muted=bool(entry.get("mute")),
            )
        )
    return sinks


def _list_sinks_text() -> list[Sink]:
    """Fallback when pactl has no -f json (older libpulse)."""
    text = run(["pactl", "list", "sinks"])
    sinks: list[Sink] = []
    cur: Sink | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sink #"):
            if cur is not None:
                sinks.append(cur)
            cur = Sink(index=int(stripped.split("#", 1)[1]), name="", description="")
        elif cur is None:
            continue
        elif stripped.startswith("Name:"):
            cur.name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:"):
            cur.description = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Mute:"):
            cur.muted = stripped.split(":", 1)[1].strip() == "yes"
        elif stripped.startswith("Volume:"):
            found = re.findall(r"(\d+)%", stripped)
            if found:
                cur.volume_pct = int(found[0])
    if cur is not None:
        sinks.append(cur)
    for sink in sinks:
        if not sink.description:
            sink.description = sink.name
    return sinks


def raop_sinks() -> list[Sink]:
    return [s for s in list_sinks() if s.is_raop]


def sink_exists(name: str) -> bool:
    return any(s.name == name for s in list_sinks())


def set_sink_volume(name: str, pct: int) -> None:
    run(["pactl", "set-sink-volume", name, f"{max(0, min(150, pct))}%"])


def set_sink_mute(name: str, muted: bool) -> None:
    run(["pactl", "set-sink-mute", name, "1" if muted else "0"])


# --------------------------------------------------------------------------
# Modules
# --------------------------------------------------------------------------

@dataclass
class Module:
    index: int
    name: str
    argument: str


def list_modules() -> list[Module]:
    modules: list[Module] = []
    for line in run(["pactl", "list", "short", "modules"]).splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        modules.append(
            Module(
                index=int(parts[0].strip()),
                name=parts[1].strip(),
                argument=parts[2].strip() if len(parts) > 2 else "",
            )
        )
    return modules


def _arg_value(argument: str, key: str) -> str | None:
    for token in argument.split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1].strip('"')
    return None


def unload_module(index: int) -> None:
    run(["pactl", "unload-module", str(index)])


# --------------------------------------------------------------------------
# RAOP discovery (creates one sink per AirPlay device on the network)
# --------------------------------------------------------------------------

def raop_discover_loaded() -> bool:
    return any(m.name == "module-raop-discover" for m in list_modules())


def load_raop_discover() -> None:
    run(["pactl", "load-module", "module-raop-discover"])


def ensure_raop_discover() -> bool:
    """Load RAOP discovery if it is missing. Returns True if it had to.

    Loaded via pactl, with defaults, on purpose. Loading it from a config file
    was tried and rejected twice over: a config-file module cannot be unloaded
    at runtime (`pactl unload-module` answers Access denied), and the file
    carried a sess.latency.msec override that turned out to kill shairport-sync
    receivers — sessions established, metadata flowed, no audio came out, and
    eventually the sink vanished with "timestamp: expected ... != actual".

    Called from rooms.list_rooms(), so discovery self-heals after a PipeWire
    restart without anyone having to remember it.
    """
    if raop_discover_loaded():
        return False
    load_raop_discover()
    return True


# --------------------------------------------------------------------------
# The hub
# --------------------------------------------------------------------------

def hub_exists() -> bool:
    return sink_exists(HUB_SINK)


def create_hub() -> None:
    # The format is not cosmetic. OwnTone reads the fifo as raw PCM 44100/16/2,
    # and a null sink has no clock of its own - it runs off the system timer.
    # Leave the sink at 48000 Hz float32 and parec has to resample; that small
    # drift is enough to starve OwnTone: "Source is not providing sufficient
    # data, temporarily suspending playback". The audio goes quiet without
    # anything looking broken. With the hub in the same format end to end,
    # nothing has to convert anything.
    run(
        [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={HUB_SINK}",
            "rate=44100",
            "channels=2",
            "format=s16le",
            f'sink_properties=device.description="{HUB_DESC}",node.pause-on-idle=false',
        ]
    )


def destroy_hub() -> None:
    for module in list_modules():
        if module.name == "module-null-sink" and _arg_value(module.argument, "sink_name") == HUB_SINK:
            unload_module(module.index)


def active_routes() -> dict[str, int]:
    """{raop sink name: module index} for every loopback fed by the hub."""
    routes: dict[str, int] = {}
    for module in list_modules():
        if module.name != "module-loopback":
            continue
        if _arg_value(module.argument, "source") != f"{HUB_SINK}.monitor":
            continue
        sink = _arg_value(module.argument, "sink")
        if sink:
            routes[sink] = module.index
    return routes


def route_on(sink_name: str, latency_ms: int = DEFAULT_LATENCY_MS) -> int:
    out = run(
        [
            "pactl",
            "load-module",
            "module-loopback",
            f"source={HUB_SINK}.monitor",
            f"sink={sink_name}",
            f"latency_msec={latency_ms}",
            "source_dont_move=true",
            "sink_dont_move=true",
        ]
    )
    digits = [line.strip() for line in out.splitlines() if line.strip().isdigit()]
    return int(digits[-1]) if digits else -1


def route_off(sink_name: str) -> None:
    index = active_routes().get(sink_name)
    if index is not None:
        unload_module(index)


def route_off_all() -> None:
    for index in active_routes().values():
        try:
            unload_module(index)
        except PactlError:
            pass


# --------------------------------------------------------------------------
# Applications (sink inputs) — this is where PlexAmp gets moved
# --------------------------------------------------------------------------

@dataclass
class Stream:
    index: int
    app: str
    sink_index: int


def list_streams() -> list[Stream]:
    try:
        data = json.loads(run(["pactl", "-f", "json", "list", "sink-inputs"]))
    except (PactlError, json.JSONDecodeError):
        return []
    streams: list[Stream] = []
    for entry in data:
        props = entry.get("properties") or {}
        app = (
            props.get("application.name")
            or props.get("media.name")
            or props.get("node.name")
            or f"stream {entry.get('index')}"
        )
        sink = entry.get("sink")
        streams.append(
            Stream(
                index=int(entry["index"]),
                app=str(app),
                sink_index=int(sink) if isinstance(sink, int) else -1,
            )
        )
    return streams


def move_stream(stream_index: int, sink_name: str) -> None:
    run(["pactl", "move-sink-input", str(stream_index), sink_name])


def default_sink() -> str:
    for line in run(["pactl", "info"]).splitlines():
        if line.startswith("Default Sink:"):
            return line.split(":", 1)[1].strip()
    return ""


def set_default_sink(name: str) -> None:
    run(["pactl", "set-default-sink", name])


def grab_all_audio() -> int:
    """Make the hub the default output and move everything playing to it."""
    set_default_sink(HUB_SINK)
    hub = next((s for s in list_sinks() if s.name == HUB_SINK), None)
    moved = 0
    for stream in list_streams():
        if hub is not None and stream.sink_index == hub.index:
            continue
        try:
            move_stream(stream.index, HUB_SINK)
            moved += 1
        except PactlError:
            pass
    return moved


# --------------------------------------------------------------------------
# mDNS — what is actually out there?
# --------------------------------------------------------------------------

def _unescape(value: str) -> str:
    """avahi escapes as decimal bytes: \\064 = @, \\195\\182 = ö (UTF-8)."""
    raw = re.sub(
        rb"\\(\d{3})",
        lambda m: bytes([int(m.group(1))]),
        value.encode("utf-8", "surrogateescape"),
    )
    return raw.decode("utf-8", "replace")


def browse_airplay() -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for service in ("_raop._tcp", "_airplay._tcp"):
        try:
            out = run(["avahi-browse", "-rpt", service], timeout=25)
        except PactlError:
            continue
        for line in out.splitlines():
            if not line.startswith("="):
                continue
            fields = line.split(";")
            if len(fields) < 10:
                continue
            name = _unescape(fields[3])
            if service == "_raop._tcp" and "@" in name:
                # RAOP announces as "AABBCCDDEEFF@Kitchen" - the MAC is noise here.
                name = name.split("@", 1)[1]
            found.append(
                {
                    "service": service,
                    "name": name,
                    "host": fields[6],
                    "ip": fields[7],
                    "port": fields[8],
                    "txt": fields[9],
                }
            )
    return found


# --------------------------------------------------------------------------
# What can the device do? (ties the mDNS finding to PipeWire's sink)
# --------------------------------------------------------------------------

_IP_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def sink_ip(sink_name: str) -> str | None:
    """PipeWire puts the IP in the sink name: raop_sink.Name.local.10.0.0.5.7000"""
    match = _IP_RE.search(sink_name)
    return match.group(1) if match else None


def _txt_field(txt: str, key: str) -> str:
    match = re.search(rf"\b{key}=([0-9,]+)", txt)
    return match.group(1) if match else ""


def capabilities(results: list[dict[str, str]]) -> dict[str, dict[str, object]]:
    """{ip: what the device requires} from the TXT records in a browse_airplay() list."""
    caps: dict[str, dict[str, object]] = {}
    for entry in results:
        if entry["service"] != "_raop._tcp":
            continue
        et = _txt_field(entry["txt"], "et").split(",")
        cn = _txt_field(entry["txt"], "cn").split(",")
        caps[entry["ip"]] = {
            "name": entry["name"],
            "et": ",".join(x for x in et if x),
            "fairplay": any(code in et for code in ("3", "5")),
            "auth_setup": "4" in et and not any(code in et for code in ("3", "5")),
            "pcm_or_alac": any(code in cn for code in ("0", "1")),
        }
    return caps


def prune_orphan_routes(known_sinks: set[str], routes: dict[str, int]) -> list[str]:
    """Loopbacks whose device has vanished (e.g. when the RAOP handshake fails)
    linger and are invisible in the interface. Clean them up."""
    removed: list[str] = []
    for sink_name, index in routes.items():
        if sink_name in known_sinks:
            continue
        try:
            unload_module(index)
            removed.append(sink_name)
        except PactlError:
            pass
    return removed
