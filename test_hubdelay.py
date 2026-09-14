#!/usr/bin/env python3
"""Hub delay-to-slowest: PCM delay, persistence, mixed-engine gating."""

from __future__ import annotations

import io
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hubdelay
import rooms
import webui


def _room(name: str, engine: str, on: bool = True, reachable: bool = True) -> rooms.Room:
    return rooms.Room(
        name=name,
        engine=engine,
        target=name,
        on=on,
        reachable=reachable,
        protocol="AirPlay 2" if engine == "owntone" else "AirPlay 1",
    )


class DelayPcmTests(unittest.TestCase):
    def test_zero_is_passthrough(self) -> None:
        pack = struct.Struct("<hh").pack
        audio = b"".join(pack(i, -i) for i in range(200))
        out = io.BytesIO()
        hubdelay.delay_pcm(io.BytesIO(audio), out, delay_ms=0)
        self.assertEqual(out.getvalue(), audio)

    def test_silence_then_shifted_audio(self) -> None:
        frames = 20
        pack = struct.Struct("<hh").pack
        audio = b"".join(pack(i, i) for i in range(frames))
        delay_frames = 5
        delay_ms = delay_frames * 1000 / hubdelay.RATE
        out = io.BytesIO()
        hubdelay.delay_pcm(io.BytesIO(audio), out, delay_ms=delay_ms)
        got = out.getvalue()
        pad = delay_frames * hubdelay.FRAME
        self.assertEqual(got[:pad], b"\x00" * pad)
        self.assertEqual(got[pad:], audio)
        self.assertEqual(len(got), pad + len(audio))

    def test_delay_frames_rounds_to_whole_samples(self) -> None:
        n = hubdelay.delay_frames(100)
        self.assertEqual(n, round(100 * 44100 / 1000))
        self.assertEqual(hubdelay.delay_frames(0), 0)
        self.assertEqual(hubdelay.delay_frames(-50), 0)
        self.assertEqual(hubdelay.delay_frames(99999), hubdelay.delay_frames(hubdelay.MAX_MS))


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))

    def test_default_is_zero_on_pipewire(self) -> None:
        cfg = hubdelay.load()
        self.assertEqual(cfg.delay_ms, 0)
        self.assertEqual(cfg.path, "pipewire")

    def test_roundtrip_and_clamp(self) -> None:
        hubdelay.save(800, "pipewire")
        cfg = hubdelay.load()
        self.assertEqual(cfg.delay_ms, 800)
        self.assertEqual(cfg.path, "pipewire")
        path = Path(self.tmp.name) / "hubdelay.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["delay_ms"], 800)
        hubdelay.save(-10, "nope")
        cfg = hubdelay.load()
        self.assertEqual(cfg.delay_ms, 0)
        self.assertEqual(cfg.path, "pipewire")
        hubdelay.save(50000, "owntone")
        cfg = hubdelay.load()
        self.assertEqual(cfg.delay_ms, hubdelay.MAX_MS)
        self.assertEqual(cfg.path, "owntone")

    def test_corrupt_file_is_default(self) -> None:
        Path(self.tmp.name).mkdir(parents=True, exist_ok=True)
        (Path(self.tmp.name) / "hubdelay.json").write_text("{not json", encoding="utf-8")
        cfg = hubdelay.load()
        self.assertEqual(cfg.delay_ms, 0)


class GatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))
        hubdelay.save(800, "pipewire")

    def test_same_engine_is_noop(self) -> None:
        self.assertFalse(hubdelay.should_apply(mixed=False, both_playing=False))
        self.assertEqual(hubdelay.effective_ms(False, False), 0)
        self.assertEqual(hubdelay.pipewire_delay_ms(False, True), 0)
        self.assertEqual(hubdelay.owntone_delay_ms(False, True), 0)

    def test_mixed_but_only_one_engine_playing_is_noop(self) -> None:
        self.assertFalse(hubdelay.should_apply(mixed=True, both_playing=False))
        self.assertEqual(hubdelay.effective_ms(True, False), 0)
        self.assertEqual(hubdelay.pipewire_delay_ms(True, False), 0)

    def test_both_playing_applies_pipewire_path(self) -> None:
        self.assertTrue(hubdelay.should_apply(mixed=True, both_playing=True))
        self.assertEqual(hubdelay.pipewire_delay_ms(True, True), 800)
        self.assertEqual(hubdelay.owntone_delay_ms(True, True), 0)

    def test_owntone_path_does_not_delay_pipewire(self) -> None:
        hubdelay.save(500, "owntone")
        self.assertEqual(hubdelay.pipewire_delay_ms(True, True), 0)
        self.assertEqual(hubdelay.owntone_delay_ms(True, True), 500)

    def test_stored_zero_never_applies(self) -> None:
        hubdelay.save(0, "pipewire")
        self.assertFalse(hubdelay.should_apply(True, True))
        self.assertEqual(hubdelay.pipewire_delay_ms(True, True), 0)


class RoomsFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))

    def test_mixed_and_both_playing(self) -> None:
        only_pw = [_room("Kitchen", "pipewire"), _room("Porch", "pipewire")]
        self.assertFalse(rooms.mixed_engines(only_pw))
        self.assertFalse(rooms.both_engines_playing(only_pw))

        mixed_idle = [
            _room("Kitchen", "pipewire", on=False),
            _room("Bedroom", "owntone", on=False),
        ]
        self.assertTrue(rooms.mixed_engines(mixed_idle))
        self.assertFalse(rooms.both_engines_playing(mixed_idle))

        one_on = [
            _room("Kitchen", "pipewire", on=True),
            _room("Bedroom", "owntone", on=False),
        ]
        self.assertTrue(rooms.mixed_engines(one_on))
        self.assertFalse(rooms.both_engines_playing(one_on))

        both = [
            _room("Kitchen", "pipewire", on=True),
            _room("Bedroom", "owntone", on=True),
        ]
        self.assertTrue(rooms.both_engines_playing(both))

    def test_status_hidden_when_same_engine(self) -> None:
        hubdelay.save(800, "pipewire")
        st = rooms.hub_delay_status([_room("Kitchen", "pipewire")])
        self.assertFalse(st["mixed"])
        self.assertEqual(st["applied_ms"], 0)
        self.assertEqual(st["stored_ms"], 800)
        self.assertEqual(st["idle_reason"], "same-engine")

    def test_status_stored_until_both_play(self) -> None:
        hubdelay.save(800, "pipewire")
        current = [
            _room("Kitchen", "pipewire", on=True),
            _room("Bedroom", "owntone", on=False),
        ]
        st = rooms.hub_delay_status(current)
        self.assertTrue(st["mixed"])
        self.assertFalse(st["applied"])
        self.assertEqual(st["applied_ms"], 0)
        self.assertEqual(st["idle_reason"], "one-engine-playing")
        self.assertIn("start_buffer_ms", st["note"])
        self.assertIn("AirPlay 1", st["note"])

    def test_status_applied_when_both_play(self) -> None:
        hubdelay.save(1200, "pipewire")
        current = [
            _room("Kitchen", "pipewire"),
            _room("Bedroom", "owntone"),
        ]
        st = rooms.hub_delay_status(current)
        self.assertTrue(st["applied"])
        self.assertEqual(st["applied_ms"], 1200)
        self.assertIn("AirPlayHubDelayed", st["audio_path"])
        self.assertIn("undelayed", st["audio_path"])
        self.assertIn("does not consume OwnTone", st["note"])

    def test_owntone_path_copy(self) -> None:
        hubdelay.save(400, "owntone")
        current = [
            _room("Kitchen", "pipewire"),
            _room("Bedroom", "owntone"),
        ]
        st = rooms.hub_delay_status(current)
        self.assertIn("fifo", st["audio_path"])
        self.assertIn("PCM delay 400 ms", st["audio_path"])
        self.assertIn("only if HomePods run ahead", st["note"])

    def test_pipewire_monitor_without_worker_is_hub(self) -> None:
        self.assertEqual(hubdelay.pipewire_monitor(False, False), "AirPlayHub.monitor")


class SafetyTests(unittest.TestCase):
    def test_source_does_not_set_raop_latency(self) -> None:
        text = Path(hubdelay.__file__).read_text(encoding="utf-8")
        self.assertIn("silences shairport-sync", text)
        self.assertIn("filter-chain delay sink", text)
        self.assertNotIn("load-module module-filter-chain", text)
        self.assertNotIn("sess.latency.msec=", text)
        pwhub_text = Path("pwhub.py").read_text(encoding="utf-8")
        self.assertIn("The hub delay lives in hubdelay.py", pwhub_text)
        self.assertIn("DELAYED_SINK", pwhub_text)
        self.assertIn("Not a filter-chain", pwhub_text)

    def test_web_markup_and_api_route(self) -> None:
        self.assertIn('id="hubdelay"', webui.PAGE)
        self.assertIn("Hold back the faster path", webui.PAGE)
        self.assertIn("/api/hubdelay", webui.PAGE)
        self.assertIn("usually ahead", webui.PAGE)
        handler_src = Path("webui.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/hubdelay"', handler_src)
        self.assertIn("hub_delay_status", handler_src)

    def test_readme_ties_b_to_offset_trim(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("Delay the faster path", readme)
        self.assertIn("leftover trim", readme)
        self.assertIn("sess.latency.msec", readme)
        self.assertIn("hubdelay.json", readme)
        self.assertIn("Same-kind setup", readme)

    def test_sync_stream_passes_owntone_delay_to_bridge(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))
        hubdelay.save(300, "owntone")
        current = [
            _room("Kitchen", "pipewire"),
            _room("Bedroom", "owntone"),
        ]
        with (
            patch("rooms.hubdelay.sync", return_value=[]),
            patch("rooms.bridge.is_running", return_value=False),
            patch("rooms.bridge.start") as start,
            patch("rooms.owntone.player", return_value={"state": "play"}),
        ):
            rooms.sync_stream(current)
        start.assert_called_once_with(delay_ms=300)

    def test_set_hub_delay_persists_if_hub_missing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))
        with patch("rooms.ensure_hub", side_effect=rooms.pwhub.PactlError("no pactl")):
            lines = rooms.set_hub_delay(750, "pipewire", current=[])
        self.assertEqual(hubdelay.load().delay_ms, 750)
        self.assertTrue(any("stored: 750 ms" in line for line in lines))
        self.assertTrue(any("Could not apply" in line for line in lines))

    def test_sync_stream_zero_delay_on_same_engine(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.environ["AIRPLAYHUB_CONFIG"] = tmp.name
        self.addCleanup(lambda: os.environ.pop("AIRPLAYHUB_CONFIG", None))
        hubdelay.save(300, "pipewire")
        current = [_room("Kitchen", "pipewire")]
        with (
            patch("rooms.hubdelay.sync", return_value=[]) as sync,
            patch("rooms.bridge.is_running", return_value=False),
            patch("rooms.bridge.start") as start,
        ):
            rooms.sync_stream(current)
        start.assert_not_called()
        sync.assert_called_once_with(mixed=False, both_playing=False)


if __name__ == "__main__":
    unittest.main()
