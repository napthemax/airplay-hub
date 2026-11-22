#!/usr/bin/env python3
"""
Delay-to-slowest at the hub — Airfoil's model, without PipeWire latency knobs.

Airfoil holds every output back to the slowest protocol. Here the two engines
split after the null sink, and AirPlay 2 (HomePod via OwnTone) usually lags
AirPlay 1 (PipeWire/RAOP). Holding the faster feed back *before* that split
aligns them in steady state.

The delay is an explicit PCM buffer, not a PipeWire latency setting. Those
were tried on real hardware and rejected:

    sess.latency.msec on RAOP     — silences shairport-sync
    loopback latency_msec         — ignored; PipeWire picks its own period
    raop.latency.ms on discover   — module reloads; the argument does not stick
    filter-chain delay sink       — loops back into AirPlayHub

Instead, only one of these two slots is filled, and only while both engines
are actually playing:

    AirPlayHub
      ├─► [PCM delay] ─► AirPlayHubDelayed ─► loopback ─► RAOP     (path=pipewire)
      └─► parec ─► [PCM delay] ─► fifo ─► OwnTone                  (path=owntone)

Same-engine houses, and mixed houses with only one engine on, get zero extra
delay.

Manual mode keeps a fixed millisecond value (set by ear for residual trim).
Auto mode estimates the engine gap that is safe to read without a microphone:

    hub_delay ≈ max(0, OwnTone start_buffer_ms − PipeWire RAOP latency_ms)

on the AirPlay 1 path. That is the bulk of the cross-engine lead when RAOP
runs at PipeWire defaults (~1500 ms) and OwnTone at its start buffer
(~2250 ms). Do not add a HomePod's arrivalToRenderLatencyMs — receivers
already self-compensate that value, and applying it over-compensates
(Music Assistant / airplay-cli). Device residual after the engines meet is
still per-room offset / ear.

Standard library only. State of the delay process is read from /proc, same
idea as bridge.py — never pgrep -f.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pwhub

RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME = CHANNELS * SAMPLE_WIDTH

MIN_MS = 0
MAX_MS = 4000

PATH_PIPEWIRE = "pipewire"
PATH_OWNTONE = "owntone"
PATHS = (PATH_PIPEWIRE, PATH_OWNTONE)
DEFAULT_PATH = PATH_PIPEWIRE  # measured: AirPlay 1 runs ahead of a HomePod

MODE_MANUAL = "manual"
MODE_AUTO = "auto"
MODES = (MODE_MANUAL, MODE_AUTO)
# Fresh installs: close the measurable engine gap without touching the slider.
DEFAULT_MODE = MODE_AUTO

# parec/paplay client name, so bridge._pids() can leave this chain alone.
CLIENT_NAME = "AirPlayHubDelay"

# How we find our own worker in /proc. The executable must be python, and
# these flags must appear as their own argv entries — a shell or editor that
# merely mentions the file in an argument is not a match.
WORKER_FLAG = "--pipewire-delay"
PCM_FLAG = "--pcm-delay"


class DelayError(RuntimeError):
    """The hub delay could not be applied."""


@dataclass(frozen=True)
class Settings:
    delay_ms: int = 0
    path: str = DEFAULT_PATH
    mode: str = DEFAULT_MODE


def config_dir() -> Path:
    """Where the delay is remembered. Override with AIRPLAYHUB_CONFIG in tests."""
    override = os.environ.get("AIRPLAYHUB_CONFIG")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "airplay-hub"


def _settings_path() -> Path:
    return config_dir() / "hubdelay.json"


def delay_frames(delay_ms: int | float, rate: int = RATE) -> int:
    """Whole PCM frames for this many milliseconds. Never fractional samples."""
    try:
        ms = float(delay_ms)
    except (TypeError, ValueError):
        return 0
    if ms <= 0:
        return 0
    ms = min(float(MAX_MS), ms)
    return int(round(ms * rate / 1000.0))


def clamp_ms(delay_ms: int | float) -> int:
    try:
        value = int(round(float(delay_ms)))
    except (TypeError, ValueError):
        return 0
    return max(MIN_MS, min(MAX_MS, value))


def clamp_path(path: str | None) -> str:
    if path in PATHS:
        return path
    return DEFAULT_PATH


def clamp_mode(mode: str | None) -> str:
    if mode in MODES:
        return mode
    return DEFAULT_MODE


def estimate_ms(start_buffer_ms: int | float, raop_latency_ms: int | float) -> int:
    """Engine-gap estimate for auto mode. Never negative; never past MAX_MS."""
    try:
        ot = int(round(float(start_buffer_ms)))
    except (TypeError, ValueError):
        ot = 0
    try:
        pw = int(round(float(raop_latency_ms)))
    except (TypeError, ValueError):
        pw = pwhub.DEFAULT_RAOP_LATENCY_MS
    return clamp_ms(max(0, ot - pw))


def resolve(
    settings: Settings | None = None,
    *,
    start_buffer_ms: int | None = None,
    raop_latency_ms: int | None = None,
) -> Settings:
    """Concrete delay/path to apply. Auto recomputes; manual returns as stored."""
    cfg = settings if settings is not None else load()
    if cfg.mode != MODE_AUTO:
        return cfg
    if start_buffer_ms is None:
        import owntone

        start_buffer_ms = owntone.start_buffer_ms()
    if raop_latency_ms is None:
        raop_latency_ms = pwhub.raop_latency_ms()
    return Settings(
        delay_ms=estimate_ms(start_buffer_ms, raop_latency_ms),
        path=PATH_PIPEWIRE,
        mode=MODE_AUTO,
    )


def load() -> Settings:
    path = _settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    # Files written before auto mode had no "mode" key. Keep their fixed
    # delay_ms as manual so an intentional 0 or ear-tuned value is not
    # overwritten by the estimate.
    if "mode" in data:
        mode = clamp_mode(data.get("mode"))
    else:
        mode = MODE_MANUAL
    return Settings(
        delay_ms=clamp_ms(data.get("delay_ms", 0)),
        path=clamp_path(data.get("path")),
        mode=mode,
    )


def save(
    delay_ms: int,
    path: str | None = None,
    mode: str | None = None,
) -> Settings:
    current = load()
    # A write that does not name a mode is a manual lock of this value —
    # moving the slider, the phone control, or an old caller. Auto is only
    # entered when mode="auto" is passed explicitly (checkbox / API).
    want_mode = clamp_mode(mode) if mode is not None else MODE_MANUAL
    settings = Settings(
        delay_ms=clamp_ms(delay_ms),
        path=clamp_path(path if path is not None else current.path),
        mode=want_mode,
    )
    if settings.mode == MODE_AUTO:
        # Auto always holds AirPlay 1; the owntone path is manual-only.
        settings = Settings(
            delay_ms=settings.delay_ms,
            path=PATH_PIPEWIRE,
            mode=MODE_AUTO,
        )
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = _settings_path()
    tmp = target.with_suffix(".json.tmp")
    payload = {
        "delay_ms": settings.delay_ms,
        "path": settings.path,
        "mode": settings.mode,
    }
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(target)
    return settings


def should_apply(mixed: bool, both_playing: bool, settings: Settings | None = None) -> bool:
    """Extra delay only while a mixed house actually uses both engines."""
    cfg = resolve(settings)
    return bool(mixed and both_playing and cfg.delay_ms > 0)


def effective_ms(mixed: bool, both_playing: bool, settings: Settings | None = None) -> int:
    cfg = resolve(settings)
    return cfg.delay_ms if should_apply(mixed, both_playing, cfg) else 0


def effective_path(mixed: bool, both_playing: bool, settings: Settings | None = None) -> str:
    cfg = resolve(settings)
    if not should_apply(mixed, both_playing, cfg):
        return cfg.path
    return cfg.path


def owntone_delay_ms(mixed: bool, both_playing: bool, settings: Settings | None = None) -> int:
    """Milliseconds to insert in front of OwnTone's fifo, or 0."""
    cfg = resolve(settings)
    if not should_apply(mixed, both_playing, cfg):
        return 0
    return cfg.delay_ms if cfg.path == PATH_OWNTONE else 0


def pipewire_delay_ms(mixed: bool, both_playing: bool, settings: Settings | None = None) -> int:
    cfg = resolve(settings)
    if not should_apply(mixed, both_playing, cfg):
        return 0
    return cfg.delay_ms if cfg.path == PATH_PIPEWIRE else 0


def pipewire_monitor(mixed: bool | None = None, both_playing: bool | None = None) -> str:
    """Which monitor PipeWire loopbacks should read right now.

    If the caller already knows mixed/both_playing, pass them. Otherwise we
    only look at whether the delay worker is actually running — that is the
    source of truth for an in-flight route.
    """
    if mixed is None or both_playing is None:
        return (
            pwhub.delayed_monitor()
            if pipewire_worker_running() and pwhub.delayed_hub_exists()
            else pwhub.hub_monitor()
        )
    if pipewire_delay_ms(mixed, both_playing) > 0 and pwhub.delayed_hub_exists():
        return pwhub.delayed_monitor()
    return pwhub.hub_monitor()


def headroom_note(
    settings: Settings | None = None,
    applied_ms: int = 0,
    *,
    start_buffer_ms: int | None = None,
    raop_latency_ms: int | None = None,
) -> str:
    """How this delay interacts with OwnTone's per-room offset / start buffer."""
    stored_cfg = settings if settings is not None else load()
    cfg = resolve(
        stored_cfg,
        start_buffer_ms=start_buffer_ms,
        raop_latency_ms=raop_latency_ms,
    )
    stored = cfg.delay_ms
    path = cfg.path
    if stored_cfg.mode == MODE_AUTO:
        ot = (
            start_buffer_ms
            if start_buffer_ms is not None
            else _start_buffer_ms_safe()
        )
        pw = (
            raop_latency_ms
            if raop_latency_ms is not None
            else pwhub.raop_latency_ms()
        )
        live = (
            f"Auto: AirPlay 1 held back by {applied_ms} ms "
            f"(OwnTone start_buffer {ot} ms − RAOP {pw} ms)."
            if applied_ms
            else (
                f"Auto: will hold AirPlay 1 back by {stored} ms once both "
                f"kinds play (OwnTone start_buffer {ot} ms − RAOP {pw} ms)."
            )
        )
        return (
            f"{live} Drag the slider to lock a manual value. Device residual "
            "after the engines meet is still the per-room OwnTone slider "
            "(behind i) — keep start_buffer_ms − |offset| around 500 ms."
        )
    if stored <= 0:
        return (
            "No extra hub delay stored. Same-engine playback stays as it is. "
            "Turn on Auto to close the measurable engine gap, or set a value "
            "by ear. The per-room OwnTone slider (behind i) is leftover trim "
            "and still eats start_buffer_ms — keep start_buffer_ms − |offset| "
            "around 500 ms."
        )
    if path == PATH_PIPEWIRE:
        live = (
            f"AirPlay 1 is held back by {applied_ms} ms at the hub."
            if applied_ms
            else f"AirPlay 1 will be held back by {stored} ms once both kinds of room play."
        )
        return (
            f"{live} That delay does not consume OwnTone's start_buffer_ms. "
            "Prefer it over dragging a HomePod toward earlier (negative offset), "
            "which eats the same buffer the start delay uses. The per-room "
            "slider remains residual trim after this."
        )
    live = (
        f"AirPlay 2 is held back by {applied_ms} ms on the way into OwnTone."
        if applied_ms
        else (
            f"AirPlay 2 will be held back by {stored} ms once both kinds of "
            "room play."
        )
    )
    return (
        f"{live} Use this path only if HomePods run ahead of AirPlay 1 rooms "
        "(unusual on the hardware this was measured on). It sits in addition "
        "to start_buffer_ms; it does not replace the per-room offset."
    )


def _start_buffer_ms_safe() -> int:
    try:
        import owntone

        return int(owntone.start_buffer_ms())
    except Exception:
        return 2250


def status(
    *,
    mixed: bool,
    both_playing: bool,
    start_buffer_ms: int | None = None,
    raop_latency_ms: int | None = None,
) -> dict:
    """Snapshot both UIs share."""
    stored_cfg = load()
    if start_buffer_ms is None:
        start_buffer_ms = _start_buffer_ms_safe()
    if raop_latency_ms is None:
        raop_latency_ms = pwhub.raop_latency_ms()
    cfg = resolve(
        stored_cfg,
        start_buffer_ms=start_buffer_ms,
        raop_latency_ms=raop_latency_ms,
    )
    apply = should_apply(mixed, both_playing, cfg)
    applied_ms = effective_ms(mixed, both_playing, cfg)
    pw_running = pipewire_worker_running()
    auto_ms = estimate_ms(start_buffer_ms, raop_latency_ms)
    return {
        "mixed": mixed,
        "both_playing": both_playing,
        "applied": apply,
        "applied_ms": applied_ms,
        "stored_ms": cfg.delay_ms,
        "manual_ms": stored_cfg.delay_ms,
        "mode": stored_cfg.mode,
        "auto": stored_cfg.mode == MODE_AUTO,
        "auto_ms": auto_ms,
        "start_buffer_ms": int(start_buffer_ms),
        "raop_latency_ms": int(raop_latency_ms),
        "path": cfg.path,
        "path_label": _path_label(cfg.path),
        "min_ms": MIN_MS,
        "max_ms": MAX_MS,
        "pipewire_worker": pw_running,
        "delayed_sink": pwhub.DELAYED_SINK,
        "note": headroom_note(
            stored_cfg,
            applied_ms,
            start_buffer_ms=start_buffer_ms,
            raop_latency_ms=raop_latency_ms,
        ),
        "audio_path": _audio_path_text(cfg.path, applied_ms, mixed),
        "idle_reason": _idle_reason(mixed, both_playing, cfg),
    }


def _path_label(path: str) -> str:
    if path == PATH_OWNTONE:
        return "AirPlay 2 (OwnTone) — only if HomePods lead"
    return "AirPlay 1 (PipeWire) — usually ahead"


def _audio_path_text(path: str, applied_ms: int, mixed: bool) -> str:
    if not mixed:
        return "One kind of speaker — no hub delay."
    if applied_ms <= 0:
        if path == PATH_OWNTONE:
            return (
                "AirPlayHub → loopback → AirPlay 1, and "
                "AirPlayHub → parec → fifo → OwnTone (no extra delay yet)."
            )
        return (
            "AirPlayHub → loopback → AirPlay 1, and "
            "AirPlayHub → parec → fifo → OwnTone (no extra delay yet)."
        )
    if path == PATH_OWNTONE:
        return (
            f"AirPlayHub → loopback → AirPlay 1 (undelayed). "
            f"AirPlayHub → parec → PCM delay {applied_ms} ms → fifo → OwnTone."
        )
    return (
        f"AirPlayHub → PCM delay {applied_ms} ms → {pwhub.DELAYED_SINK} → "
        f"loopback → AirPlay 1. "
        f"AirPlayHub → parec → fifo → OwnTone (undelayed)."
    )


def _idle_reason(mixed: bool, both_playing: bool, cfg: Settings) -> str:
    if not mixed:
        return "same-engine"
    resolved = resolve(cfg)
    if resolved.delay_ms <= 0:
        return "stored-zero"
    if not both_playing:
        return "one-engine-playing"
    return ""


# ---------------------------------------------------------------------------
# PCM delay line
# ---------------------------------------------------------------------------

def delay_pcm(src, dst, delay_ms: int, rate: int = RATE) -> None:
    """Copy raw s16le stereo from src to dst, delayed by delay_ms.

    A running delay line, not a one-shot sleep: output at time t is input at
    t − delay. The first delay_ms of output is silence. On EOF the remainder
    of the buffer is flushed so the tail of the stream is not eaten.
    """
    nframes = delay_frames(delay_ms, rate)
    buf = bytearray(nframes * FRAME)
    leftover = bytearray()
    while True:
        try:
            data = src.read(4096)
        except (OSError, ValueError):
            break
        if not data:
            break
        leftover.extend(data)
        aligned = (len(leftover) // FRAME) * FRAME
        if aligned == 0:
            continue
        buf.extend(leftover[:aligned])
        del leftover[:aligned]
        emit = len(buf) - nframes * FRAME
        if emit <= 0:
            continue
        try:
            dst.write(buf[:emit])
            dst.flush()
        except (OSError, ValueError, BrokenPipeError):
            return
        del buf[:emit]
    if buf:
        try:
            dst.write(buf)
            dst.flush()
        except (OSError, ValueError, BrokenPipeError):
            return


# ---------------------------------------------------------------------------
# PipeWire-side worker (parec → delay → paplay into AirPlayHubDelayed)
# ---------------------------------------------------------------------------

def _argv(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    if not raw or not raw[0]:
        return None
    return [part.decode(errors="replace") for part in raw if part]


def _is_python(executable: str) -> bool:
    base = os.path.basename(executable)
    return base == "python" or base.startswith("python3")


def _this_script(argv: list[str]) -> bool:
    for arg in argv[1:]:
        if os.path.basename(arg) == "hubdelay.py":
            return True
    return False


def _iter_pids() -> list[int]:
    pids: list[int] = []
    try:
        entries = Path("/proc").iterdir()
    except OSError:
        return pids
    for entry in entries:
        if entry.name.isdigit():
            pids.append(int(entry.name))
    return pids


def _worker_pids() -> list[int]:
    found: list[int] = []
    for pid in _iter_pids():
        argv = _argv(pid)
        if not argv or not _is_python(argv[0]):
            continue
        if not _this_script(argv):
            continue
        if WORKER_FLAG in argv:
            found.append(pid)
    return found


def pipewire_worker_running() -> bool:
    return bool(_worker_pids())


def running_pipewire_delay_ms() -> int:
    for pid in _worker_pids():
        argv = _argv(pid) or []
        if WORKER_FLAG not in argv:
            continue
        idx = argv.index(WORKER_FLAG)
        if idx + 1 < len(argv):
            try:
                return clamp_ms(argv[idx + 1])
            except (TypeError, ValueError):
                return 0
    return 0


def _client_name_pids() -> list[int]:
    """parec / paplay tagged with our client name."""
    marker = f"--client-name={CLIENT_NAME}"
    found: list[int] = []
    for pid in _iter_pids():
        argv = _argv(pid)
        if not argv:
            continue
        base = os.path.basename(argv[0])
        if base not in ("parec", "paplay", "pw-play"):
            continue
        if marker in argv[1:] or any(a == CLIENT_NAME for a in argv[1:]):
            found.append(pid)
    return found


def stop_pipewire_delay() -> None:
    """Stop the delay worker and any leftover parec/paplay it started."""
    for pid in _worker_pids():
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    for pid in _client_name_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def start_pipewire_delay(delay_ms: int) -> None:
    """Idempotent. Restarts if the running delay does not match."""
    want = clamp_ms(delay_ms)
    if want <= 0:
        stop_pipewire_delay()
        return
    if pipewire_worker_running() and running_pipewire_delay_ms() == want:
        if pwhub.delayed_hub_exists():
            return
        stop_pipewire_delay()
    else:
        stop_pipewire_delay()

    if not pwhub.hub_exists():
        raise DelayError("the hub does not exist yet")
    try:
        pwhub.ensure_delayed_hub()
    except pwhub.PactlError as exc:
        raise DelayError(f"could not create {pwhub.DELAYED_SINK}: {exc}") from exc

    if not _paplay_cmd():
        raise DelayError(
            "paplay is missing — install libpulse (the same package that provides parec)"
        )

    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG, str(want)],
            start_new_session=True,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DelayError(f"could not start the hub delay: {exc}") from exc


def _paplay_cmd() -> list[str] | None:
    from shutil import which

    if which("paplay"):
        return [
            "paplay",
            "--raw",
            f"--device={pwhub.DELAYED_SINK}",
            f"--format=s16le",
            f"--rate={RATE}",
            f"--channels={CHANNELS}",
            f"--client-name={CLIENT_NAME}",
        ]
    return None


def _run_pipewire_worker(delay_ms: int) -> int:
    """Child process: parec from the hub, delay, paplay into the delayed sink."""
    play = _paplay_cmd()
    if not play:
        return 1
    try:
        paplay = subprocess.Popen(
            play,
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        parec = subprocess.Popen(
            [
                "parec",
                f"--device={pwhub.HUB_SINK}.monitor",
                f"--format=s16le",
                f"--rate={RATE}",
                f"--channels={CHANNELS}",
                f"--client-name={CLIENT_NAME}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except FileNotFoundError:
        return 1
    except OSError:
        return 1
    if paplay.stdin is None or parec.stdout is None:
        return 1
    try:
        delay_pcm(parec.stdout, paplay.stdin, delay_ms)
    finally:
        try:
            paplay.stdin.close()
        except OSError:
            pass
        for proc in (parec, paplay):
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
    return 0


def sync(
    *,
    mixed: bool,
    both_playing: bool,
    start_buffer_ms: int | None = None,
    raop_latency_ms: int | None = None,
) -> list[str]:
    """Make the PipeWire side match the resolved delay. Returns log lines.

    OwnTone-side delay is applied by bridge.start(delay_ms=...), because that
    process already owns the fifo. This function only starts or stops the
    extra null sink + PCM worker, and retargets loopbacks.
    """
    messages: list[str] = []
    cfg = resolve(
        load(),
        start_buffer_ms=start_buffer_ms,
        raop_latency_ms=raop_latency_ms,
    )
    want_pw = pipewire_delay_ms(mixed, both_playing, cfg)
    want_source = pwhub.delayed_monitor() if want_pw else pwhub.hub_monitor()

    if want_pw:
        try:
            start_pipewire_delay(want_pw)
        except DelayError as exc:
            messages.append(f"Hub delay did not start: {exc}")
            want_source = pwhub.hub_monitor()
        except pwhub.PactlError as exc:
            messages.append(f"Hub delay sink failed: {exc}")
            want_source = pwhub.hub_monitor()
    else:
        if pipewire_worker_running():
            stop_pipewire_delay()
            messages.append("Stopped the hub delay on AirPlay 1.")

    try:
        moved = pwhub.retarget_hub_routes(want_source)
    except pwhub.PactlError as exc:
        messages.append(f"Could not retarget AirPlay 1 rooms: {exc}")
        moved = 0
    else:
        if moved and want_pw:
            messages.append(
                f"AirPlay 1 rooms now follow a {want_pw} ms hub delay "
                f"(delayed sink {pwhub.DELAYED_SINK})."
            )
        elif moved:
            messages.append("AirPlay 1 rooms read the hub directly again.")

    if not want_pw:
        try:
            pwhub.destroy_delayed_hub()
        except pwhub.PactlError:
            pass

    return messages


# ---------------------------------------------------------------------------
# CLI — used as the delay worker, and as a stdin/stdout filter in tests.
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == PCM_FLAG:
        ms = clamp_ms(argv[2] if len(argv) > 2 else 0)
        delay_pcm(sys.stdin.buffer, sys.stdout.buffer, ms)
        return 0
    if len(argv) >= 2 and argv[1] == WORKER_FLAG:
        ms = clamp_ms(argv[2] if len(argv) > 2 else 0)
        return _run_pipewire_worker(ms)
    sys.stderr.write(
        "Usage: hubdelay.py --pcm-delay <ms>     # raw s16le stereo on stdin/stdout\n"
        "       hubdelay.py --pipewire-delay <ms>  # internal worker, do not start by hand\n"
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))
