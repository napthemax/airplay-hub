#!/usr/bin/env python3
"""
Client for OwnTone's JSON API (localhost:3689).

OwnTone is the engine for everything that requires FairPlay/AirPlay 2 —
HomePods, Apple TVs, Macs. It handles the pairing and synchronisation that
PipeWire cannot. Standard library only, no dependencies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

BASE = "http://localhost:3689"
TIMEOUT = 2.5

# OwnTone's own limit. Outside it the API returns an error.
OFFSET_MIN = -2000
OFFSET_MAX = 2000


class OwnToneError(RuntimeError):
    """OwnTone did not answer, or answered with an error."""


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise OwnToneError(f"{method} {path}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OwnToneError(f"cannot reach OwnTone at {BASE} ({exc})") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


@dataclass
class Output:
    id: str
    name: str
    kind: str
    selected: bool
    volume: int
    offset_ms: int
    needs_pin: bool

    @property
    def is_airplay(self) -> bool:
        return "airplay" in self.kind.lower()


def available() -> bool:
    try:
        _request("GET", "/api/player")
        return True
    except OwnToneError:
        return False


def outputs() -> list[Output]:
    data = _request("GET", "/api/outputs")
    result: list[Output] = []
    for entry in data.get("outputs", []):
        result.append(
            Output(
                id=str(entry.get("id", "")),
                name=str(entry.get("name", "?")),
                kind=str(entry.get("type", "")),
                selected=bool(entry.get("selected")),
                volume=int(entry.get("volume") or 0),
                offset_ms=int(entry.get("offset_ms") or 0),
                # OwnTone flags devices waiting for a four-digit code shown on
                # the device's own screen.
                needs_pin=bool(entry.get("needs_auth_key") or entry.get("requires_auth")),
            )
        )
    return result


def select(output_id: str, on: bool) -> None:
    _request("PUT", f"/api/outputs/{output_id}", {"selected": bool(on)})


def set_volume(output_id: str, volume: int) -> None:
    _request("PUT", f"/api/outputs/{output_id}", {"volume": max(0, min(100, volume))})


def set_offset(output_id: str, offset_ms: int) -> None:
    """Shift an output in time. Positive delays, negative moves it earlier.

    This is the only way to get OwnTone rooms in phase with the PipeWire ones.
    The two engines' buffers can be calculated and matched on paper, but AirPlay
    devices add latency of their own that is invisible from this side — a
    HomePod buffers more than a shairport-sync does. The last step is done by ear.
    """
    offset = max(OFFSET_MIN, min(OFFSET_MAX, int(offset_ms)))
    _request("PUT", f"/api/outputs/{output_id}", {"offset_ms": offset})


def send_pin(output_id: str, pin: str) -> None:
    """The four-digit code shown on a HomePod or Apple TV when pairing."""
    _request("PUT", f"/api/outputs/{output_id}", {"pin": pin})


def player() -> dict:
    return _request("GET", "/api/player")


def play() -> None:
    _request("PUT", "/api/player/play")


def pause() -> None:
    _request("PUT", "/api/player/pause")


def pipe_track_uri(path: str) -> str | None:
    """OwnTone's uri for the fifo at `path`, or None if it is not indexed."""
    name = path.rsplit("/", 1)[-1]
    data = _request("GET", f"/api/search?type=tracks&query={urllib.parse.quote(name)}")
    for track in data.get("tracks", {}).get("items", []):
        if track.get("path") == path and track.get("data_kind") == "pipe":
            return str(track.get("uri") or "")
    return None


def play_pipe(path: str) -> None:
    """Put the fifo alone in the queue and start playing.

    Calling play() is not enough. OwnTone starts playback by itself when data
    first appears in the pipe, but only that first time — after a pause it never
    finds its way back. This has been known for years, see
    github.com/owntone/owntone-server issue 465, and the workaround suggested
    there is exactly this: point at the pipe in the queue again.

    clear=true keeps stale queue entries from being played instead.
    """
    uri = pipe_track_uri(path)
    if uri is None:
        raise OwnToneError(
            f"OwnTone does not have {path} in its library — does 'directories' in "
            "/etc/owntone.conf point at that folder, and has it been scanned?"
        )
    _request("POST", f"/api/queue/items/add?uris={uri}&clear=true&playback=start")
