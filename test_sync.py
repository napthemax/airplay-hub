#!/usr/bin/env python3
"""Timing guide helpers — no PipeWire, no OwnTone, no Qt required."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import owntone
import rooms
import webui


class BufferParseTests(unittest.TestCase):
    def test_reads_active_line_not_comment(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("# start_buffer_ms = 100\nstart_buffer_ms = 2500\n")
            path = Path(fh.name)
        try:
            self.assertEqual(owntone.start_buffer_ms(path), 2500)
        finally:
            path.unlink()

    def test_commented_only_uses_default(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("# start_buffer_ms = 100\n")
            path = Path(fh.name)
        try:
            self.assertEqual(owntone.start_buffer_ms(path), owntone.DEFAULT_START_BUFFER_MS)
        finally:
            path.unlink()

    def test_missing_file_uses_default(self) -> None:
        self.assertEqual(
            owntone.start_buffer_ms(Path("/no/such/owntone.conf")),
            owntone.DEFAULT_START_BUFFER_MS,
        )


class HeadroomTests(unittest.TestCase):
    def test_headroom_and_risk(self) -> None:
        self.assertEqual(rooms.offset_headroom(-2000, 2500), 500)
        self.assertFalse(rooms.offset_risky(0, 2250))
        self.assertFalse(rooms.offset_risky(400, 2250))
        self.assertTrue(rooms.offset_risky(-2000, 2250))
        self.assertTrue(rooms.offset_risky(-1800, 2250))  # 450 ms left
        self.assertFalse(rooms.offset_risky(-1500, 2500))

    def test_suggested_buffer_covers_offset(self) -> None:
        room = rooms.Room("HomePod", "owntone", "1", offset_ms=-2000, reachable=True)
        self.assertEqual(rooms.suggested_buffer_ms([room], 2250), 2500)
        pw = rooms.Room("Kitchen", "pipewire", "raop.x", reachable=True)
        self.assertEqual(rooms.suggested_buffer_ms([pw], 2250), 2250)

    def test_buffer_command_uses_floor_and_script(self) -> None:
        cmd = rooms.buffer_command(100)
        self.assertIn("owntone 500", cmd)
        self.assertTrue(cmd.startswith(str(rooms.sync_script())))
        self.assertTrue(rooms.sync_script().name == "sync.sh")


class MixedEngineTests(unittest.TestCase):
    def test_mixed_only_when_both_reachable(self) -> None:
        pw = rooms.Room("Kitchen", "pipewire", "raop.x", reachable=True)
        ot = rooms.Room("HomePod", "owntone", "1", reachable=True)
        gone = rooms.Room("HomePod", "owntone", "1", reachable=False)
        self.assertTrue(rooms.mixed_engines([pw, ot]))
        self.assertFalse(rooms.mixed_engines([pw]))
        self.assertFalse(rooms.mixed_engines([ot]))
        self.assertFalse(rooms.mixed_engines([pw, gone]))

    def test_overview_hides_offset_when_not_mixed(self) -> None:
        ot = rooms.Room("HomePod", "owntone", "1", offset_ms=200, reachable=True)
        mixed = rooms.sync_overview([
            rooms.Room("Kitchen", "pipewire", "raop.x", reachable=True),
            ot,
        ])
        self.assertTrue(mixed["mixed"])
        homepod = next(item for item in mixed["rooms"] if item["name"] == "HomePod")
        self.assertTrue(homepod["can_offset"])

        alone = rooms.sync_overview([ot])
        self.assertFalse(alone["mixed"])
        homepod = next(item for item in alone["rooms"] if item["name"] == "HomePod")
        self.assertFalse(homepod["can_offset"])

    def test_guide_copy_is_the_safe_recipe(self) -> None:
        guide = rooms.sync_guide_text(2500)
        blob = " ".join([
            str(guide["when"]),
            str(guide["when_not"]),
            " ".join(guide["steps"]),
            str(guide["buffer"]),
            str(guide["avoid"]),
        ]).lower()
        self.assertIn("airplay 1", blob)
        self.assertIn("airplay 2", blob)
        self.assertIn("later", blob)
        self.assertIn("headroom", blob)
        self.assertIn("500", blob)
        self.assertIn("silences", blob)
        self.assertIn("do not drag toward earlier", blob)
        self.assertIn("2500", blob)


class OffsetMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        os.environ["AIRPLAYHUB_CONFIG"] = self.tmp
        rooms._restored_offsets.clear()
        self.addCleanup(os.environ.pop, "AIRPLAYHUB_CONFIG", None)
        self.addCleanup(rooms._restored_offsets.clear)

    def test_set_offset_persists_by_room_name(self) -> None:
        room = rooms.Room("HomePod Sovrum", "owntone", "9", on=False, reachable=True)
        with patch.object(rooms.owntone, "set_offset") as put, \
             patch.object(rooms.owntone, "select") as select, \
             patch.object(rooms, "sync_stream", return_value=[]):
            rooms.set_offset(room, 400)
        put.assert_called_once_with("9", 400)
        select.assert_not_called()
        self.assertEqual(room.offset_ms, 400)
        self.assertEqual(rooms.remembered_offset("homepod sovrum"), 400)
        saved = json.loads((Path(self.tmp) / "offsets.json").read_text())
        self.assertEqual(saved["homepod sovrum"], 400)

    def test_reset_to_zero_is_remembered(self) -> None:
        room = rooms.Room("HomePod", "owntone", "1", on=False, reachable=True)
        with patch.object(rooms.owntone, "set_offset"), \
             patch.object(rooms, "sync_stream", return_value=[]):
            rooms.set_offset(room, -200)
            rooms.set_offset(room, 0)
        self.assertEqual(rooms.remembered_offset("homepod"), 0)

    def test_restore_writes_once_without_reselect_when_off(self) -> None:
        rooms._remember_offset("homepod", -500)
        room = rooms.Room("HomePod", "owntone", "1", on=False, offset_ms=0, reachable=True)
        with patch.object(rooms.owntone, "set_offset") as put, \
             patch.object(rooms.owntone, "select") as select:
            rooms._apply_remembered_offset(room, reselect=False)
            rooms._apply_remembered_offset(room, reselect=False)
        put.assert_called_once_with("1", -500)
        select.assert_not_called()
        self.assertEqual(room.offset_ms, -500)

    def test_set_offset_on_a_playing_room_rebuilds_the_session(self) -> None:
        room = rooms.Room("HomePod", "owntone", "1", on=True, reachable=True)
        with patch.object(rooms.owntone, "set_offset") as put, \
             patch.object(rooms.owntone, "select") as select, \
             patch.object(rooms.time, "sleep"), \
             patch.object(rooms, "sync_stream", return_value=[]) as sync:
            rooms.set_offset(room, 250)
        put.assert_called_once_with("1", 250)
        select.assert_any_call("1", False)
        select.assert_any_call("1", True)
        sync.assert_called_once()

    def test_turning_on_applies_remembered_offset_first(self) -> None:
        rooms._remember_offset("homepod", 300)
        room = rooms.Room("HomePod", "owntone", "1", on=False, offset_ms=0, reachable=True)
        with patch.object(rooms.owntone, "set_offset") as put, \
             patch.object(rooms.owntone, "select") as select:
            rooms.set_on(room, True)
        put.assert_called_once_with("1", 300)
        select.assert_called_once_with("1", True)

    def test_pipewire_room_cannot_be_trimmed(self) -> None:
        room = rooms.Room("Kitchen", "pipewire", "raop.x", reachable=True)
        with self.assertRaises(ValueError):
            rooms.set_offset(room, 100)


class ClickTrackTests(unittest.TestCase):
    def test_wav_is_pcm_stereo_44100(self) -> None:
        path = rooms.write_click_wav(seconds=2)
        try:
            with wave.open(str(path), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 2)
                self.assertEqual(wav.getsampwidth(), 2)
                self.assertEqual(wav.getframerate(), 44100)
                self.assertEqual(wav.getnframes(), 2 * 44100)
        finally:
            path.unlink()

    def test_player_prefers_paplay(self) -> None:
        wav = Path("/tmp/clicks.wav")
        with patch("rooms.shutil.which", side_effect=lambda name: "/usr/bin/" + name if name == "paplay" else None):
            cmd = rooms.hub_player_command(wav)
        self.assertEqual(cmd[0], "paplay")
        self.assertIn("AirPlayHub", cmd[1])

    def test_player_missing_is_a_clear_error(self) -> None:
        with patch("rooms.shutil.which", return_value=None):
            with self.assertRaises(rooms.SyncToneError):
                rooms.hub_player_command(Path("/tmp/clicks.wav"))


class WebMirrorTests(unittest.TestCase):
    def test_page_carries_the_guide_and_clicks(self) -> None:
        self.assertIn("Match timing", webui.PAGE)
        self.assertIn("sync-clicks", webui.PAGE)
        self.assertIn("/api/sync/clicks", webui.PAGE)
        self.assertIn("/offset", webui.PAGE)
        self.assertIn("Hold back the faster path", webui.PAGE)
        self.assertIn("/api/hubdelay", webui.PAGE)


if __name__ == "__main__":
    unittest.main()
