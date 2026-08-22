#!/usr/bin/env python3
"""
AirPlay Hub — the same audio in several rooms, on Linux.

Run:  python main.py
Trouble:  ./diagnose.sh

The window shows rooms, not plumbing. That the kitchen stereo is reached over
PipeWire while the HomePod needs OwnTone is true but irrelevant to anyone who
just wants music in the kitchen — that distinction lives in rooms.py and only
peeks out from behind the info button.
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
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

        # The control only belongs in houses running both engines. See
        # rooms.mixed_engines().
        if room.can_offset and self.show_timing:
            layout.addWidget(self._sync_section())

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)

    def _sync_section(self) -> QWidget:
        """The control that shifts the room in time.

        The two engines can be matched on paper, but AirPlay devices buffer
        differing amounts on their own and that is invisible from here. The last
        step has to be done by ear, while the music plays.
        """
        box = QFrame()
        box.setObjectName("syncbox")
        col = QVBoxLayout(box)
        col.setContentsMargins(13, 11, 13, 12)
        col.setSpacing(7)

        rubrik = QLabel("Timing against the other rooms")
        rubrik.setStyleSheet("color: #dbe6f0; font-size: 13px; font-weight: bold;")
        col.addWidget(rubrik)

        hjalp = QLabel(
            "Play music in several rooms and drag while listening.\n"
            "Only works for a room that is AHEAD of the others — drag right to "
            "hold it back. A room that lags behind cannot be pulled forward; "
            "there is no earlier audio to play. Delay the other rooms with "
            "./sync.sh instead."
        )
        hjalp.setStyleSheet("color: #6f8299; font-size: 11px;")
        hjalp.setWordWrap(True)
        col.addWidget(hjalp)

        rad = QHBoxLayout()
        rad.setSpacing(9)
        tidigare = QLabel("earlier\n(limited)")
        tidigare.setStyleSheet("color: #55697d; font-size: 10px;")
        tidigare.setToolTip(
            "Limited by the buffer. Push too far and the audio clips, then stops."
        )
        rad.addWidget(tidigare)

        self.offset = QSlider(Qt.Orientation.Horizontal)
        self.offset.setRange(owntone.OFFSET_MIN, owntone.OFFSET_MAX)
        self.offset.setSingleStep(25)
        self.offset.setPageStep(100)
        self.offset.setValue(self.room.offset_ms)
        self.offset.valueChanged.connect(self._on_offset_preview)
        self.offset.sliderReleased.connect(self._on_offset_commit)
        rad.addWidget(self.offset, 1)

        senare = QLabel("later")
        senare.setStyleSheet("color: #55697d; font-size: 10px;")
        rad.addWidget(senare)
        col.addLayout(rad)

        botten = QHBoxLayout()
        self.offset_label = QLabel()
        self.offset_label.setStyleSheet("color: #dbe6f0; font-size: 12px;")
        botten.addWidget(self.offset_label)
        botten.addStretch(1)
        nolla = QPushButton("Reset")
        nolla.clicked.connect(self._on_offset_reset)
        botten.addWidget(nolla)
        col.addLayout(botten)

        self._visa_offset(self.room.offset_ms)
        return box

    def _visa_offset(self, value: int) -> None:
        if value == 0:
            self.offset_label.setText("in step with the others")
        else:
            direction = "later" if value > 0 else "earlier"
            self.offset_label.setText(f"{value:+d} ms — {direction}")

    def _on_offset_preview(self, value: int) -> None:
        self._visa_offset(value)

    def _on_offset_commit(self) -> None:
        value = self.offset.value()
        try:
            rooms.set_offset(self.room, value)
            self.window_ref.log(f"{self.room.name}: timing {value:+d} ms")
        except (owntone.OwnToneError, ValueError) as exc:
            self.window_ref.log(f"Timing change failed: {exc}")

    def _on_offset_reset(self) -> None:
        self.offset.setValue(0)
        self._on_offset_commit()


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

        header = QLabel("AirPlay Hub")
        header.setObjectName("header")
        root.addWidget(header)

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

        section = QLabel("ROOMS")
        section.setObjectName("section")
        root.addWidget(section)

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
