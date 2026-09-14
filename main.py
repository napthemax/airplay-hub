#!/usr/bin/env python3
"""
AirPlay Hub — the same audio in several rooms, on Linux.

Run:  python main.py
       python main.py --version
Trouble:  ./diagnose.sh

The window shows rooms, not plumbing. That the kitchen stereo is reached over
PipeWire while the HomePod needs OwnTone is true but irrelevant to anyone who
just wants music in the kitchen — that distinction lives in rooms.py and only
peeks out from behind the info button.
"""

from __future__ import annotations

import argparse
import sys
from functools import partial
from pathlib import Path

from version import argparse_version, display as build_id

# --version before Qt, so `airplay-hub --version` works even if PyQt6 is
# missing. After ./install.sh this is how you see which checkout is on disk.
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AirPlay Hub")
    parser.add_argument("--version", action="version", version=argparse_version())
    parser.parse_args()

from PyQt6.QtCore import Qt, QTimer, QProcess
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

import bridge
import owntone
import pwhub
import rooms
from pwhub import PactlError

STYLE = """
QMainWindow, QDialog { background-color: #1b2733; }
QLabel { color: #ecf0f1; }
QLabel#header { font-size: 22px; font-weight: bold; color: #ecf0f1; }
QLabel#build { color: #8fa3b8; font-size: 12px; }
QLabel#status { color: #8fa3b8; font-size: 13px; }
QLabel#section {
    color: #6f8299; font-size: 11px; font-weight: bold; letter-spacing: 1px;
}
QLabel#roomname { font-size: 15px; color: #ecf0f1; }
QLabel#pct { color: #8fa3b8; font-size: 12px; }
QPlainTextEdit {
    background-color: #16202a; color: #7f95ab;
    border: 1px solid #2b3d4f; border-radius: 6px;
    font-family: monospace; font-size: 11px;
}
QPushButton {
    background-color: #2b3d4f; color: #dbe6f0; font-size: 13px;
    border: none; border-radius: 6px; padding: 7px 12px;
}
QPushButton:hover { background-color: #365068; }
QPushButton:disabled { background-color: #24323f; color: #5a6b7c; }
QComboBox {
    background-color: #2b3d4f; color: #dbe6f0;
    border-radius: 6px; padding: 6px;
}

/* A room row. No frame, no card feel - just a line in a list. */
QFrame#room { background-color: transparent; border-radius: 8px; }
QFrame#room:hover { background-color: #22303f; }
QFrame#syncbox { background-color: #22303f; border-radius: 8px; }
QLabel#warning { color: #e0a33a; font-size: 11px; }
QLabel#oknote { color: #7f95ab; font-size: 11px; }
QPushButton#sync {
    background-color: #2b3d4f; color: #dbe6f0; font-size: 12px;
    border: none; border-radius: 6px; padding: 5px 10px;
}
QPushButton#sync:hover { background-color: #365068; }

/* Rooms that do not answer: still listed, but clearly out of reach. */
QLabel#roomgone { font-size: 15px; color: #55697d; }
QLabel#gonetag { color: #55697d; font-size: 11px; }
QPushButton#speakergone {
    background-color: #22303f; color: #3f5163;
    font-size: 15px; border-radius: 6px; padding: 0px;
    min-width: 34px; max-width: 34px; min-height: 30px; max-height: 30px;
}

/* The speaker button on the left. Dark when off, blue when playing. */
QPushButton#speaker {
    background-color: #2b3d4f; color: #7f95ab;
    font-size: 15px; border-radius: 6px; padding: 0px;
    min-width: 34px; max-width: 34px; min-height: 30px; max-height: 30px;
}
QPushButton#speaker:hover { background-color: #365068; }
QPushButton#speaker:checked { background-color: #2f8fd8; color: white; }

/* The little i. Visible when looked for, not otherwise. */
QPushButton#info {
    background-color: transparent; color: #55697d;
    font-size: 12px; font-weight: bold; border: 1px solid #3a4d60;
    border-radius: 9px; padding: 0px;
    min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px;
}
QPushButton#info:hover { color: #ecf0f1; border-color: #7f95ab; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical {
    background: transparent; width: 9px; margin: 0px;
}
QScrollBar::handle:vertical {
    background: #3a4d60; border-radius: 4px; min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #4d6478; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }

QSlider::groove:horizontal { height: 4px; background: #2b3d4f; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #2f8fd8; border-radius: 2px; }
QSlider::handle:horizontal {
    background: #dbe6f0; width: 13px; margin: -5px 0; border-radius: 6px;
}
QSlider::groove:horizontal:disabled { background: #24323f; }
QSlider::sub-page:horizontal:disabled { background: #3a4d60; }
QSlider::handle:horizontal:disabled { background: #55697d; }
"""


class InfoDialog(QDialog):
    """The plumbing behind a room, and the fine adjustment of its timing."""

    def __init__(
        self,
        room: rooms.Room,
        window: "MainWindow",
        parent: QWidget | None = None,
        show_timing: bool = True,
    ):
        super().__init__(parent)
        self.room = room
        self.show_timing = show_timing
        self.window_ref = window
        self._committed = room.offset_ms
        self.setWindowTitle(room.name)
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        title = QLabel(room.name)
        title.setObjectName("header")
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(1, 1)
        for row, (label, value) in enumerate(room.details):
            key = QLabel(label)
            key.setStyleSheet("color: #6f8299; font-size: 12px;")
            key.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            val = QLabel(str(value))
            val.setStyleSheet("color: #dbe6f0; font-size: 12px;")
            val.setWordWrap(True)
            grid.addWidget(key, row, 0)
            grid.addWidget(val, row, 1)
        layout.addLayout(grid)

        # Timing controls belong in houses running both engines. See
        # rooms.mixed_engines(). Same-kind rooms stay in step on their own.
        if self.show_timing:
            if room.can_offset:
                layout.addWidget(self._sync_section())
            else:
                layout.addWidget(self._pipewire_note())

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        if self.show_timing:
            guide = QPushButton("Timing guide")
            guide.setObjectName("sync")
            guide.clicked.connect(self._open_guide)
            row.addWidget(guide)
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _open_guide(self) -> None:
        SyncGuideDialog(self.window_ref, self).exec()

    def _pipewire_note(self) -> QWidget:
        box = QFrame()
        box.setObjectName("syncbox")
        col = QVBoxLayout(box)
        col.setContentsMargins(13, 11, 13, 12)
        note = QLabel(
            "This room stays in step with the other AirPlay 1 rooms. There is "
            "no safe per-room delay on this path — it would silence the "
            "speaker. Hold the whole AirPlay 1 feed back with the hub delay "
            "under the room list. If an AirPlay 2 room is ahead of this one, "
            "delay that room from its own info button. If a HomePod lags, "
            "use the hub delay first, then the timing guide — not a PipeWire "
            "latency knob."
        )
        note.setObjectName("oknote")
        note.setWordWrap(True)
        col.addWidget(note)
        return box

    def _sync_section(self) -> QWidget:
        """The control that shifts an AirPlay 2 room in time.

        OwnTone reads offset_ms once, when the session is built. The slider
        therefore commits on release (and on keyboard changes), not while
        dragging. Positive = later. Negative eats the start buffer.
        """
        box = QFrame()
        box.setObjectName("syncbox")
        col = QVBoxLayout(box)
        col.setContentsMargins(13, 11, 13, 12)
        col.setSpacing(7)

        heading = QLabel("Delay this room if it is ahead")
        heading.setStyleSheet("color: #dbe6f0; font-size: 13px; font-weight: bold;")
        col.addWidget(heading)

        help_text = QLabel(
            "Play in several rooms, then drag toward later and release. "
            "A room that lags cannot be pulled forward — there is no earlier "
            "audio in the pipe, and pushing left far enough clips, then "
            "stops. Delay the rooms that are ahead instead."
        )
        help_text.setStyleSheet("color: #6f8299; font-size: 11px;")
        help_text.setWordWrap(True)
        col.addWidget(help_text)

        row = QHBoxLayout()
        row.setSpacing(9)
        earlier = QLabel("earlier\n(limited)")
        earlier.setStyleSheet("color: #55697d; font-size: 10px;")
        earlier.setToolTip(
            "Limited by OwnTone's start buffer. Push too far and the audio "
            "clips, then stops. Raise the buffer with ./sync.sh owntone N "
            "if you truly need more headroom."
        )
        row.addWidget(earlier)

        self.offset = QSlider(Qt.Orientation.Horizontal)
        self.offset.setRange(rooms.OFFSET_MIN, rooms.OFFSET_MAX)
        self.offset.setSingleStep(25)
        self.offset.setPageStep(100)
        self.offset.setValue(self.room.offset_ms)
        self.offset.valueChanged.connect(self._on_offset_preview)
        self.offset.sliderReleased.connect(self._on_offset_commit)
        row.addWidget(self.offset, 1)

        later = QLabel("later")
        later.setStyleSheet("color: #55697d; font-size: 10px;")
        row.addWidget(later)
        col.addLayout(row)

        bottom = QHBoxLayout()
        self.offset_label = QLabel()
        self.offset_label.setStyleSheet("color: #dbe6f0; font-size: 12px;")
        bottom.addWidget(self.offset_label)
        bottom.addStretch(1)
        reset = QPushButton("Reset")
        reset.clicked.connect(self._on_offset_reset)
        bottom.addWidget(reset)
        col.addLayout(bottom)

        self.headroom = QLabel()
        self.headroom.setWordWrap(True)
        col.addWidget(self.headroom)

        self._show_offset(self.room.offset_ms)
        return box

    def _show_offset(self, value: int) -> None:
        buffer_ms = rooms.start_buffer_ms()
        headroom = rooms.offset_headroom(value, buffer_ms)
        if value == 0:
            self.offset_label.setText("in step with the other AirPlay 2 rooms")
        else:
            direction = "later" if value > 0 else "earlier"
            self.offset_label.setText(f"{value:+d} ms — {direction}")

        if rooms.offset_risky(value, buffer_ms):
            self.headroom.setObjectName("warning")
            if value <= rooms.CLIP_WARN_MS:
                self.headroom.setText(
                    f"This far earlier will clip or go silent. Drag toward "
                    f"later. Buffer is {buffer_ms} ms; keep about "
                    f"{rooms.HEADROOM_MS} ms of headroom "
                    f"(start_buffer_ms − |offset|)."
                )
            else:
                need = abs(value) + rooms.HEADROOM_MS
                self.headroom.setText(
                    f"Headroom {headroom} ms — below ~{rooms.HEADROOM_MS} ms "
                    f"the audio clips. Raise the buffer to at least {need} ms "
                    f"({rooms.buffer_command(need)}) or drag toward later."
                )
        else:
            self.headroom.setObjectName("oknote")
            self.headroom.setText(
                f"OwnTone start buffer {buffer_ms} ms · "
                f"{headroom} ms headroom left (want ≥ {rooms.HEADROOM_MS} ms)."
            )
        self.headroom.style().unpolish(self.headroom)
        self.headroom.style().polish(self.headroom)

    def _on_offset_preview(self, value: int) -> None:
        self._show_offset(value)
        # Keyboard and Reset change the value without a mouse release.
        if not self.offset.isSliderDown() and value != self._committed:
            self._on_offset_commit()

    def _on_offset_commit(self) -> None:
        value = self.offset.value()
        if value == self._committed:
            return
        try:
            rooms.set_offset(self.room, value)
            self._committed = value
            self.window_ref.log(f"{self.room.name}: timing {value:+d} ms")
        except (owntone.OwnToneError, ValueError) as exc:
            self.window_ref.log(f"Timing change failed: {exc}")

    def _on_offset_reset(self) -> None:
        self.offset.setValue(0)


class SyncGuideDialog(QDialog):
    """When mixed engines can drift, and how to trim without clipping."""

    def __init__(self, window: "MainWindow", parent: QWidget | None = None):
        super().__init__(parent)
        self.window_ref = window
        self._proc: QProcess | None = None
        self._wav: Path | None = None
        self.setWindowTitle("Match timing")
        self.setMinimumWidth(480)
        self.setMinimumHeight(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(10)

        title = QLabel("Match timing")
        title.setObjectName("header")
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 8, 0)
        self.body.setSpacing(10)
        self._fill()
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        self.btn_clicks = QPushButton("Play six clicks")
        self.btn_clicks.setToolTip(
            "A click each second through the hub. Walk the rooms and note "
            "which one you hear first — that one is ahead."
        )
        self.btn_clicks.clicked.connect(self._play_clicks)
        buttons.addWidget(self.btn_clicks)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def _label(self, text: str, kind: str = "body") -> QLabel:
        widget = QLabel(text)
        widget.setWordWrap(True)
        if kind == "heading":
            widget.setStyleSheet("color: #dbe6f0; font-size: 13px; font-weight: bold;")
        elif kind == "warn":
            widget.setObjectName("warning")
        else:
            widget.setStyleSheet("color: #8fa3b8; font-size: 12px;")
        return widget

    def _fill(self) -> None:
        overview = rooms.sync_overview(list(self.window_ref.rooms.values()))
        guide = overview["guide"]

        self.body.addWidget(self._label(str(guide["when_title"]), "heading"))
        self.body.addWidget(self._label(str(guide["when"])))
        self.body.addWidget(self._label(str(guide["when_not"])))

        self.body.addWidget(self._label(str(guide["steps_title"]), "heading"))
        for i, step in enumerate(guide["steps"], 1):
            self.body.addWidget(self._label(f"{i}. {step}"))

        self.body.addWidget(self._label(str(guide["buffer_title"]), "heading"))
        self.body.addWidget(self._label(str(guide["buffer"])))

        buffer_box = QFrame()
        buffer_box.setObjectName("syncbox")
        box = QVBoxLayout(buffer_box)
        box.setContentsMargins(13, 11, 13, 12)
        box.setSpacing(6)
        current = (
            f"Start buffer now: {overview['buffer_ms']} ms. "
            f"Suggested for your sliders: {overview['suggested_buffer_ms']} ms."
        )
        if overview["suggested_buffer_ms"] > overview["buffer_ms"]:
            box.addWidget(self._label(current, "warn"))
        else:
            box.addWidget(self._label(current))
        cmd = str(overview["buffer_command"])
        cmd_row = QHBoxLayout()
        cmd_label = QLabel(cmd)
        cmd_label.setStyleSheet(
            "color: #dbe6f0; font-size: 11px; font-family: monospace;"
        )
        cmd_label.setWordWrap(True)
        cmd_row.addWidget(cmd_label, 1)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(cmd))
        cmd_row.addWidget(copy)
        box.addLayout(cmd_row)
        box.addWidget(self._label(
            "That command asks for your password, edits /etc/owntone.conf and "
            "restarts OwnTone. AirPlay 2 rooms then need switching on again. "
            "The app does not run sudo itself."
        ))
        self.body.addWidget(buffer_box)

        trims = [
            item for item in overview["rooms"]
            if item.get("can_offset") or item["engine"] == "owntone"
        ]
        if trims:
            self.body.addWidget(self._label("AirPlay 2 rooms right now", "heading"))
            for item in trims:
                offset = item["offset_ms"]
                extra = "in step" if offset == 0 else f"{offset:+d} ms"
                if item.get("risky"):
                    extra += " — little headroom, risk of clipping"
                self.body.addWidget(self._label(f"{item['name']}: {extra}"))

        self.body.addWidget(self._label("What not to do", "heading"))
        self.body.addWidget(self._label(str(guide["avoid"])))
        self.body.addStretch(1)

    def _play_clicks(self) -> None:
        if self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning:
            return
        try:
            rooms.ensure_hub()
            self._wav = rooms.write_click_wav()
            cmd = rooms.hub_player_command(self._wav)
        except (rooms.SyncToneError, PactlError) as exc:
            self.window_ref.log(str(exc))
            return
        self.btn_clicks.setEnabled(False)
        self.btn_clicks.setText("Playing clicks…")
        self._proc = QProcess(self)
        self._proc.finished.connect(self._clicks_finished)
        self._proc.errorOccurred.connect(self._clicks_failed)
        self._proc.start(cmd[0], cmd[1:])

    def _clicks_finished(self, *args) -> None:
        self._cleanup_wav()
        self.btn_clicks.setEnabled(True)
        self.btn_clicks.setText("Play six clicks")
        self.window_ref.log("Played clicks through the hub.")

    def _clicks_failed(self, *args) -> None:
        self._cleanup_wav()
        self.btn_clicks.setEnabled(True)
        self.btn_clicks.setText("Play six clicks")
        self.window_ref.log("Could not play the click track.")

    def _cleanup_wav(self) -> None:
        if self._wav is not None:
            try:
                self._wav.unlink()
            except OSError:
                pass
            self._wav = None


class RoomRow(QFrame):
    """A row in the room list: on/off, name, volume, info button."""

    def __init__(self, room: rooms.Room, window: "MainWindow"):
        super().__init__()
        self.setObjectName("room")
        self.window_ref = window
        self.room = room

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(11)

        self.speaker = QPushButton("♪")
        self.speaker.setObjectName("speaker")
        self.speaker.setCheckable(True)
        self.speaker.setToolTip("Switch this room on or off")
        self.speaker.clicked.connect(partial(window.on_room_toggle, room.key))
        layout.addWidget(self.speaker)

        self.name = QLabel(room.name)
        self.name.setObjectName("roomname")
        self.name.setMinimumWidth(150)
        layout.addWidget(self.name)

        layout.addStretch(1)

        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(140)
        self.volume.sliderReleased.connect(partial(window.on_room_volume, room.key))
        layout.addWidget(self.volume)

        self.pct = QLabel()
        self.pct.setObjectName("pct")
        self.pct.setFixedWidth(34)
        self.pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.pct)

        self.gone = QLabel("not answering")
        self.gone.setObjectName("gonetag")
        self.gone.setFixedWidth(174)   # the slider (140) + the percentage (34)
        self.gone.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.gone.hide()
        layout.addWidget(self.gone)

        self.info = QPushButton("i")
        self.info.setObjectName("info")
        self.info.setToolTip("How this room is reached")
        self.info.clicked.connect(partial(window.on_room_info, room.key))
        layout.addWidget(self.info)

        self.apply(room)

    def _restyle(self) -> None:
        """Qt does not restyle by itself when objectName changes."""
        for widget in (self.name, self.speaker):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def apply(self, room: rooms.Room) -> None:
        self.room = room
        self.speaker.setChecked(room.on)
        if not self.volume.isSliderDown():
            self.volume.setValue(room.volume)
        self.pct.setText(f"{room.volume}%")

        if not room.reachable:
            # Kept in the list so "does not exist" differs from "is down".
            self.name.setText(room.name)
            self.name.setObjectName("roomgone")
            self.speaker.setObjectName("speakergone")
            self.speaker.setEnabled(False)
            self.volume.hide()
            self.pct.hide()
            self.gone.show()
            self._restyle()
            return

        self.name.setObjectName("roomname")
        self.speaker.setObjectName("speaker")
        self.speaker.setEnabled(True)
        self.gone.hide()
        self.volume.show()
        self.pct.show()
        self._restyle()

        if room.needs_pin:
            # The device wants a code before letting us in. Volume is
            # meaningless then - show what needs doing instead.
            self.name.setText(f"{room.name}   ·   tap to pair")
            self.name.setStyleSheet("font-size: 15px; color: #e0a33a;")
            self.volume.setEnabled(False)
        else:
            self.name.setText(room.name)
            self.name.setStyleSheet("")
            self.volume.setEnabled(True)


class HubDelayBox(QFrame):
    """Delay-to-slowest: hold the faster hub feed back, by ear.

    Shown only when the house has both engines. The value is stored even when
    only one engine is playing; it takes effect once both are on. Lives here
    rather than behind i so it is clearly a hub setting, not a per-room trim.
    """

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window_ref = window
        self.setObjectName("syncbox")
        self._busy = False
        col = QVBoxLayout(self)
        col.setContentsMargins(13, 11, 13, 12)
        col.setSpacing(7)

        title = QLabel("Hold back the faster path")
        title.setStyleSheet("color: #dbe6f0; font-size: 13px; font-weight: bold;")
        col.addWidget(title)

        self.explain = QLabel()
        self.explain.setStyleSheet("color: #6f8299; font-size: 11px;")
        self.explain.setWordWrap(True)
        col.addWidget(self.explain)

        path_row = QHBoxLayout()
        path_lab = QLabel("Delay")
        path_lab.setObjectName("pct")
        path_row.addWidget(path_lab)
        self.path = QComboBox()
        self.path.addItem("AirPlay 1 (PipeWire) — usually ahead", "pipewire")
        self.path.addItem("AirPlay 2 (OwnTone) — only if HomePods lead", "owntone")
        self.path.currentIndexChanged.connect(self._on_path)
        path_row.addWidget(self.path, 1)
        col.addLayout(path_row)

        slide = QHBoxLayout()
        slide.setSpacing(9)
        zero = QLabel("0")
        zero.setStyleSheet("color: #55697d; font-size: 10px;")
        slide.addWidget(zero)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 4000)
        self.slider.setSingleStep(50)
        self.slider.setPageStep(100)
        self.slider.setTickInterval(500)
        self.slider.valueChanged.connect(self._on_preview)
        self.slider.sliderReleased.connect(self._on_commit)
        slide.addWidget(self.slider, 1)
        later = QLabel("later")
        later.setStyleSheet("color: #55697d; font-size: 10px;")
        slide.addWidget(later)
        col.addLayout(slide)

        self.value = QLabel()
        self.value.setStyleSheet("color: #dbe6f0; font-size: 12px;")
        col.addWidget(self.value)

        self.path_line = QLabel()
        self.path_line.setStyleSheet("color: #6f8299; font-size: 11px;")
        self.path_line.setWordWrap(True)
        col.addWidget(self.path_line)
        self.hide()

    def sync(self, current: list[rooms.Room]) -> None:
        st = rooms.hub_delay_status(current)
        self.setVisible(bool(st.get("mixed")))
        if not st.get("mixed"):
            return
        self._busy = True
        try:
            self.slider.setRange(int(st["min_ms"]), int(st["max_ms"]))
            if not self.slider.isSliderDown():
                self.slider.setValue(int(st["stored_ms"]))
            idx = self.path.findData(st["path"])
            if idx >= 0:
                self.path.setCurrentIndex(idx)
            self.explain.setText(str(st["note"]))
            self.path_line.setText(str(st["audio_path"]))
            self._render_value(st)
        finally:
            self._busy = False

    def _render_value(self, st: dict | None = None) -> None:
        if st is None:
            st = rooms.hub_delay_status(list(self.window_ref.rooms.values()))
        stored = int(st.get("stored_ms") or 0)
        applied = int(st.get("applied_ms") or 0)
        if stored == 0:
            self.value.setText("no extra delay")
        elif applied:
            self.value.setText(f"{applied} ms applied to {st.get('path_label', '')}")
        else:
            reason = st.get("idle_reason")
            if reason == "one-engine-playing":
                self.value.setText(f"{stored} ms stored — applies when both kinds play")
            else:
                self.value.setText(f"{stored} ms stored")

    def _on_preview(self, value: int) -> None:
        if self._busy:
            return
        self.value.setText(f"{value} ms")

    def _on_path(self) -> None:
        if self._busy:
            return
        self._commit()

    def _on_commit(self) -> None:
        if self._busy:
            return
        self._commit()

    def _commit(self) -> None:
        path = self.path.currentData()
        try:
            for line in rooms.set_hub_delay(self.slider.value(), path):
                self.window_ref.log(line)
        except Exception as exc:  # backend must not take the window down
            self.window_ref.log(f"Hub delay failed: {exc}")
        self.window_ref.refresh()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("AirPlay Hub")
        self.resize(520, 640)

        self.rows: dict[str, RoomRow] = {}
        self.rooms: dict[str, rooms.Room] = {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(11)

        heading = QVBoxLayout()
        heading.setSpacing(2)
        header = QLabel("AirPlay Hub")
        header.setObjectName("header")
        heading.addWidget(header)
        # Same string as install.sh / --version / the phone page. Under the
        # title so it is actually readable — no About menu.
        build = QLabel(build_id())
        build.setObjectName("build")
        build.setToolTip("Installed build")
        heading.addWidget(build)
        root.addLayout(heading)

        self.status = QLabel("Starting…")
        self.status.setObjectName("status")
        root.addWidget(self.status)

        # Master volume for everything at once, like Airfoil's top slider.
        master_row = QHBoxLayout()
        master_row.setSpacing(11)
        master_label = QLabel("All rooms")
        master_label.setObjectName("pct")
        master_label.setFixedWidth(64)
        master_row.addWidget(master_label)
        self.master = QSlider(Qt.Orientation.Horizontal)
        self.master.setRange(0, 100)
        self.master.setValue(100)
        self.master.sliderReleased.connect(self.on_master)
        master_row.addWidget(self.master)
        root.addLayout(master_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_grab = QPushButton("Send system audio here")
        self.btn_grab.clicked.connect(self.on_grab)
        buttons.addWidget(self.btn_grab)
        self.app_box = QComboBox()
        self.app_box.setMinimumWidth(140)
        buttons.addWidget(self.app_box, 1)
        self.btn_move = QPushButton("Move")
        self.btn_move.clicked.connect(self.on_move_app)
        buttons.addWidget(self.btn_move)
        root.addLayout(buttons)

        section_row = QHBoxLayout()
        section = QLabel("ROOMS")
        section.setObjectName("section")
        section_row.addWidget(section)
        section_row.addStretch(1)
        self.btn_sync = QPushButton("Match timing…")
        self.btn_sync.setObjectName("sync")
        self.btn_sync.setToolTip(
            "AirPlay 1 and AirPlay 2 rooms can drift. Open a short guide."
        )
        self.btn_sync.clicked.connect(self.on_sync_guide)
        self.btn_sync.hide()
        section_row.addWidget(self.btn_sync)
        root.addLayout(section_row)

        self.room_host = QWidget()
        self.room_layout = QVBoxLayout(self.room_host)
        self.room_layout.setContentsMargins(0, 0, 0, 0)
        self.room_layout.setSpacing(1)
        self.room_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.room_host)
        root.addWidget(scroll, 1)

        self.delay_box = HubDelayBox(self)
        root.addWidget(self.delay_box)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setFixedHeight(64)
        root.addWidget(self.logbox)

        self.bootstrap()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(3000)

    # ---------------------------------------------------------------- start
    def bootstrap(self) -> None:
        try:
            if not pwhub.hub_exists():
                rooms.ensure_hub()
                self.log("Created the hub.")
        except PactlError as exc:
            self.log(f"Could not create the hub: {exc}")
        self.refresh()

    # ---------------------------------------------------------------- rooms
    def refresh(self) -> None:
        try:
            found = rooms.list_rooms()
        except PactlError as exc:
            self.status.setText(f"PipeWire is not answering: {exc}")
            return

        self.rooms = {r.key: r for r in found}
        self.delay_box.sync(found)

        for room in found:
            row = self.rows.get(room.key)
            if row is None:
                row = RoomRow(room, self)
                self.rows[room.key] = row
                self.room_layout.insertWidget(self.room_layout.count() - 1, row)
            else:
                row.apply(room)

        for key in list(self.rows):
            if key not in self.rooms:
                row = self.rows.pop(key)
                row.setParent(None)
                row.deleteLater()

        self.sync_stream(found)
        self.reload_apps()

        on = sum(1 for r in found if r.on)
        waiting = sum(1 for r in found if r.needs_pin)
        gone = sum(1 for r in found if not r.reachable)
        reachable = len(found) - gone
        text = f"{reachable} rooms · {on} playing" if found else "No rooms found yet"
        if gone:
            text += f" · {gone} not answering"
        if waiting:
            text += f" · {waiting} waiting for pairing code"
        if on and rooms.any_owntone_on(found):
            if not bridge.is_running():
                text += " · NO AUDIO GOING OUT"
            else:
                try:
                    if owntone.player().get("state") != "play":
                        text += " · PAUSED"
                except owntone.OwnToneError:
                    pass
        self.status.setText(text)
        mixed = rooms.mixed_engines(found)
        self.btn_sync.setVisible(mixed)

    def sync_stream(self, found: list[rooms.Room]) -> None:
        for rad in rooms.sync_stream(found):
            self.log(rad)

    def on_room_toggle(self, key: str, checked: bool) -> None:
        room = self.rooms.get(key)
        if room is None:
            return

        if room.needs_pin and checked:
            self.on_room_pair(room)
            return

        # The hub must exist before any room can read from it.
        if checked:
            try:
                rooms.ensure_hub()
            except PactlError as exc:
                self.log(f"Could not create the hub: {exc}")
                return

        try:
            rooms.set_on(room, checked)
            self.log(f"{room.name}: {'on' if checked else 'off'}")
        except ConnectionError as exc:
            self.log(str(exc))
        except (owntone.OwnToneError, PactlError) as exc:
            self.log(f"{room.name}: {exc}")
        self.refresh()

    def on_room_volume(self, key: str) -> None:
        room = self.rooms.get(key)
        row = self.rows.get(key)
        if room is None or row is None:
            return
        value = row.volume.value()
        row.pct.setText(f"{value}%")
        try:
            rooms.set_volume(room, value)
        except (owntone.OwnToneError, PactlError) as exc:
            self.log(f"Volume {room.name}: {exc}")

    def on_room_info(self, key: str) -> None:
        room = self.rooms.get(key)
        if room is not None:
            InfoDialog(
                room, self, self, show_timing=rooms.mixed_engines(list(self.rooms.values()))
            ).exec()

    def on_sync_guide(self) -> None:
        SyncGuideDialog(self, self).exec()

    def on_room_pair(self, room: rooms.Room) -> None:
        pin, ok = QInputDialog.getText(
            self, "Pairing", f"The code shown on {room.name}:"
        )
        if not ok or not pin.strip():
            self.refresh()
            return
        try:
            rooms.send_pin(room, pin.strip())
            self.log(f"Sent pairing code to {room.name}.")
        except (owntone.OwnToneError, ValueError) as exc:
            self.log(f"Pairing failed: {exc}")
        self.refresh()

    # -------------------------------------------------------------- sources
    def reload_apps(self) -> None:
        try:
            streams = pwhub.list_streams()
        except PactlError:
            return
        current = self.app_box.currentData()
        self.app_box.clear()
        for stream in streams:
            # The loopbacks are the app's own pipes out to the speakers, not
            # something the user wants to move. Listing them invites breaking
            # your own audio path.
            if "loopback" in stream.app.lower():
                continue
            self.app_box.addItem(stream.app, stream.index)
        index = self.app_box.findData(current)
        if index >= 0:
            self.app_box.setCurrentIndex(index)
        self.btn_move.setEnabled(self.app_box.count() > 0)

    def on_grab(self) -> None:
        try:
            moved = pwhub.grab_all_audio()
            self.log(f"The hub is now the default output. Moved {moved} stream(s) here.")
        except PactlError as exc:
            self.log(f"Could not take over the audio: {exc}")
        self.refresh()

    def on_move_app(self) -> None:
        index = self.app_box.currentData()
        if index is None:
            return
        try:
            pwhub.move_stream(int(index), pwhub.HUB_SINK)
            self.log(f"Moved {self.app_box.currentText()} to the hub.")
        except PactlError as exc:
            self.log(f"Move failed: {exc}")
        self.refresh()

    def on_master(self) -> None:
        try:
            pwhub.set_sink_volume(pwhub.HUB_SINK, self.master.value())
        except PactlError as exc:
            self.log(f"Master volume failed: {exc}")

    # ------------------------------------------------------------------ log
    def log(self, message: str) -> None:
        self.logbox.appendPlainText(message)

    # -------------------------------------------------------------- shutdown
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # The streams live on. Close the window mid-song and the music keeps
        # playing - the backends read their state from the system, so the next
        # start finds its way back to the same rooms.
        event.accept()


ICON = Path(__file__).resolve().parent / "packaging" / "airplay-hub.svg"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AirPlay Hub")

    # Deliberately NOT calling setDesktopFileName(). Qt then tries to register
    # the app with the desktop portal, which only succeeds when it was launched
    # from its .desktop entry - starting it from a terminal prints
    # "Failed to register with host portal ... App info not found". The name
    # buys nothing here and the warning is pure noise.
    #
    # Nor setting an icon theme. A "kf.iconthemes: Icon theme X not found"
    # warning comes from the user's own theme inheriting a theme that is not
    # installed - check `Inherits=` in its index.theme. Overriding the theme
    # from inside the app would silence the message by changing how every icon
    # looks, which is not ours to do.

    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))

    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
