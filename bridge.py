#!/usr/bin/env python3
"""
The bridge that feeds OwnTone with the hub's audio.

    AirPlayHub.monitor ── parec ──► /srv/music/airplayhub.fifo ──► OwnTone

OwnTone does not play system audio by itself. It reads from a named pipe in its
library, and something has to fill that pipe. Without the bridge OwnTone happily
connects to the speaker, reports 'state: play' — and is completely silent,
because no samples ever arrive. Nothing anywhere reports an error, so look here
first.

Same principle as pwhub: state is read from the system, not held in memory.
Restart the GUI while music is playing and the bridge is found again, not
started twice.

Standard library only.
"""

from __future__ import annotations

import errno
import fcntl
import os
import signal
import subprocess
import sys
from pathlib import Path

# The format is not negotiable. OwnTone reads the pipe as raw PCM and does not
# guess — it must be 44100 Hz, 16 bit, stereo. See pwhub.create_hub().
RATE = 44100
CHANNELS = 2
FORMAT = "s16le"

FIFO = Path("/srv/music/airplayhub.fifo")
DEVICE = "AirPlayHub.monitor"


class BridgeError(RuntimeError):
    """The bridge could not be started."""


def _pids() -> list[int]:
    """Every parec process feeding our fifo, whoever started it.

    Reads /proc directly rather than using 'pgrep -f'. A pattern matching the
    whole command line also hits shells and editors that happen to have the
    text among their arguments — and stop() sends SIGTERM to whatever it finds.
    Here the process must actually be named parec and carry our device among its
    arguments, so what gets killed is always a bridge.
    """
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # process died, or is not ours to read
        if not argv or not argv[0]:
            continue
        if os.path.basename(argv[0].decode(errors="replace")) != "parec":
            continue
        args = [arg.decode(errors="replace") for arg in argv[1:]]
        # The hub-delay worker also runs parec against the same monitor.
        # It tags itself; leaving it in this list would let stop() kill the
        # AirPlay 1 delay when an OwnTone room is switched off.
        if any(arg == "--client-name=AirPlayHubDelay" for arg in args):
            continue
        if any(arg == f"--device={DEVICE}" for arg in args):
            pids.append(int(entry.name))
    return pids


def is_running() -> bool:
    return bool(_pids()) or bool(_fifo_delay_pids())


def _fifo_delay_pids() -> list[int]:
    """python hubdelay.py --pcm-delay that is writing our fifo."""
    pids: list[int] = []
    fifo = str(FIFO)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        base = os.path.basename(argv[0].decode(errors="replace"))
        if not (base == "python" or base.startswith("python3")):
            continue
        args = [a.decode(errors="replace") for a in argv[1:] if a]
        if not any(os.path.basename(a) == "hubdelay.py" for a in args):
            continue
        if "--pcm-delay" not in args:
            continue
        if "--pipewire-delay" in args:
            continue
        if _pid_writes_path(int(entry.name), fifo):
            pids.append(int(entry.name))
    return pids


def _pid_writes_path(pid: int, path: str) -> bool:
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return False
    for link in entries:
        try:
            target = os.readlink(link)
        except OSError:
            continue
        if target == path:
            return True
    return False


def feed_delay_ms() -> int:
    """Extra PCM delay in front of the fifo, or 0 for a straight parec."""
    for pid in _fifo_delay_pids():
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [a.decode(errors="replace") for a in argv if a]
        if "--pcm-delay" not in args:
            continue
        idx = args.index("--pcm-delay")
        if idx + 1 < len(args):
            try:
                return max(0, int(args[idx + 1]))
            except ValueError:
                return 0
    return 0


def fifo_ready() -> tuple[bool, str]:
    """Can the fifo be written to? The second value explains why not."""
    if not FIFO.exists():
        return False, f"{FIFO} is missing — run ./setup-owntone.sh"
    if not FIFO.is_fifo():
        return False, f"{FIFO} exists but is not a fifo"
    if not os.access(FIFO, os.W_OK):
        return False, f"{FIFO} is not writable — run ./setup-owntone.sh"
    return True, ""


def start(delay_ms: int = 0) -> None:
    """Start the bridge unless it is already running. Idempotent.

    delay_ms inserts a PCM delay line between parec and the fifo (Track B,
    path=owntone). Zero is the normal pass-through. If a feed is already
    running at a different delay, it is restarted so the new value lands.
    """
    want = max(0, int(delay_ms))
    if is_running():
        if feed_delay_ms() == want:
            return
        stop()
    ok, why = fifo_ready()
    if not ok:
        raise BridgeError(why)

    # Opening a fifo for writing blocks until someone reads the other end.
    # O_NONBLOCK saves us from hanging here when OwnTone is not listening — we
    # get ENXIO straight away and can say why.
    try:
        fd = os.open(FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise BridgeError(
                f"nothing is reading {FIFO} — is OwnTone running, and is "
                "pipe_autostart = true set in the library section of /etc/owntone.conf?"
            ) from exc
        raise BridgeError(f"could not open {FIFO}: {exc}") from exc

    # And then clear O_NONBLOCK again, before parec inherits the descriptor.
    # Leave it set and parec writes non-blocking: the moment the fifo fills up
    # it gets EAGAIN and samples are dropped. OwnTone then sees a broken stream
    # and either never starts or plays silence — with nothing logged anywhere.
    # This is the difference from owntone-bridge.sh, where the shell's '>' is
    # blocking.
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)

    parec_cmd = [
        "parec",
        f"--device={DEVICE}",
        f"--format={FORMAT}",
        f"--rate={RATE}",
        f"--channels={CHANNELS}",
    ]
    try:
        if want <= 0:
            subprocess.Popen(
                parec_cmd,
                stdout=fd,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            # parec → hubdelay.py --pcm-delay → fifo. The delay process is
            # the session leader so stop() can tear the pair down together.
            helper = Path(__file__).resolve().parent / "hubdelay.py"
            delay_proc = subprocess.Popen(
                [sys.executable, str(helper), "--pcm-delay", str(want)],
                stdin=subprocess.PIPE,
                stdout=fd,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                bufsize=0,
            )
            try:
                subprocess.Popen(
                    parec_cmd,
                    stdout=delay_proc.stdin,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                delay_proc.kill()
                raise
            if delay_proc.stdin is not None:
                delay_proc.stdin.close()
    except FileNotFoundError as exc:
        os.close(fd)
        raise BridgeError("parec is missing — install libpulse") from exc
    except OSError as exc:
        os.close(fd)
        raise BridgeError(f"could not start parec: {exc}") from exc
    os.close(fd)


def stop() -> None:
    """Stop the bridge. Does nothing if it is already down."""
    for pid in _fifo_delay_pids():
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    for pid in _pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
