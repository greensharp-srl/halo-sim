"""Simulator versions of halo-emulator's frame.* stubs.

They extend the official stubs (so validation stays firmware-faithful) and
add what a phone can provide: live microphone, audible speaker and sound
effects, and the firmware's break/restart semantics.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import lupa.lua54 as lupa
import numpy as np

from halo_emulator.event_queue import Event
from halo_emulator.stubs.bluetooth import BluetoothStub
from halo_emulator.stubs.microphone import MicrophoneStub
from halo_emulator.stubs.sound import SoundStub
from halo_emulator.stubs.speaker import SpeakerStub
from halo_emulator.stubs.system import EmulatorStopException, SystemStub

from halo_sim.audio import Lc3Decoder, Lc3Encoder, level_dbfs, resample_16k
from halo_sim.hooks import DeviceHooks

INTERRUPTED = "interrupted"
RESTART = "restart"

WAKE_SOURCES = {
    "ble": "ble",
    "button_single": "button",
    "button_double": "button",
    "button_long": "button",
    "imu_tap": "imu",
    "aad": "microphone",
}

MIC_RING_BYTES = 32 * 1024  # firmware mic ring buffer is ~32 KB


def _opt(cfg: Any, name: str, default: Any) -> Any:
    if cfg is None:
        return default
    try:
        val = cfg[name]
    except Exception:
        return default
    return default if val is None else val


class BreakState:
    """Out-of-band control signals from the host (0x03 break, 0x04 restart)."""

    def __init__(self) -> None:
        self.busy = False  # a REPL chunk is executing
        self._break = threading.Event()
        self._restart = threading.Event()

    def request_break(self) -> None:
        if self.busy:
            self._break.set()

    def request_restart(self) -> None:
        if self.busy:
            self._restart.set()

    def restart_pending(self) -> bool:
        return self._restart.is_set()

    def clear(self) -> None:
        self._break.clear()
        self._restart.clear()

    def check(self) -> str | None:
        """Called from the Lua thread. Break is raised once, restart every time."""
        if self._restart.is_set():
            return RESTART
        if self._break.is_set():
            self._break.clear()
            return INTERRUPTED
        return None


class SimSystem(SystemStub):
    """sleep/yield/standby return a marker on break; a Lua shim turns it into
    ``error('interrupted')`` so app ``pcall``s see the firmware's string error."""

    def __init__(self, *args: Any, brk: BreakState, on_callback_error: Callable[[str], None], **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._brk = brk
        self._on_cb_error = on_callback_error
        self.power_off_requested = False  # deep sleep / ship mode

    def _drain(self) -> str | None:
        for event in self._event_queue.drain():
            if event.type == "stop":
                raise EmulatorStopException("Emulator stopped")
            try:
                self._dispatch_fn(event)
            except lupa.LuaError as exc:
                msg = str(exc).split("\nstack traceback")[0]
                if msg.endswith(INTERRUPTED) or msg.endswith(RESTART):
                    return INTERRUPTED
                self._on_cb_error(msg)
        return None

    def _poll(self, seconds: float) -> str | None:  # type: ignore[override]
        deadline = time.monotonic() + seconds
        while True:
            r = self._brk.check() or self._drain()
            if r:
                return r
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

    def _wait_for_wake(self, seconds: float | None) -> str:  # type: ignore[override]
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            r = self._brk.check()
            if r:
                return r
            for event in self._event_queue.drain():
                if event.type == "stop":
                    raise EmulatorStopException("Emulator stopped")
                source = WAKE_SOURCES.get(event.type)
                if source is not None:
                    return source
            time.sleep(0.001)
        return "timeout"

    # Lua-facing (wrapped by shims in device.py) ------------------------------

    def sleep(self, seconds: float | None = None) -> str | None:  # type: ignore[override]
        if not seconds:
            # Deep sleep: BLE drops and the device reboots on wake (button).
            self.power_off_requested = True
            self._brk._restart.set()
            return RESTART
        return self._poll(float(seconds))

    def reboot(self) -> str:  # type: ignore[override]
        self._brk._restart.set()
        return RESTART

    def ship_mode(self) -> str:  # type: ignore[override]
        self.power_off_requested = True
        self._brk._restart.set()
        return RESTART

    def yield_(self) -> str | None:  # type: ignore[override]
        return self._brk.check() or self._drain()

    def standby(self, seconds: float | None = None) -> str | None:  # type: ignore[override]
        src = self._wait_for_wake(float(seconds) if seconds else None)
        if src in (INTERRUPTED, RESTART):
            return src
        self._wakeup_src = src
        return None

    def light_sleep(self, seconds: float | None = None) -> str | None:  # type: ignore[override]
        src = self._wait_for_wake(float(seconds) if seconds else None)
        if src in (INTERRUPTED, RESTART):
            return src
        self._wakeup_src = src
        # Wake = Lua VM restart, main.lua from the top (handled by device.py)
        self._brk._restart.set()
        return RESTART

    def check_break(self) -> str | None:
        return self._brk.check()


class SimBluetooth(BluetoothStub):
    def __init__(self, mtu: int) -> None:
        super().__init__()
        self._max_len = mtu - 4

    def send(self, data: object) -> None:
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("latin-1")
        if len(raw) > self._max_len:
            raise ValueError(f"payload length {len(raw)} exceeds max_length() {self._max_len}")
        super().send(data)


class SimMicrophone(MicrophoneStub):
    """Fed with live phone audio (16 kHz mono PCM) and converted to the
    configured encoder / rate / bit depth / channel count."""

    def __init__(self, hooks: DeviceHooks, post_event: Callable[[Event], None]) -> None:
        super().__init__()
        self._hooks = hooks
        self._post_event = post_event
        self._cfg: dict[str, Any] = {}
        self._lc3: Lc3Encoder | None = None
        self._aad_threshold = 90.0
        self._aad_silent_ms = 1000
        self._aad_last = 0.0
        self.last_level_db = -120.0

    def start(self, cfg: Any = None) -> None:  # type: ignore[override]
        super().start(cfg)
        self._cfg = {
            "encoder": str(_opt(cfg, "encoder", "pcm")),
            "sample_rate": int(_opt(cfg, "sample_rate", 8000)),
            "bit_depth": int(_opt(cfg, "bit_depth", 16)),
            "channels": int(_opt(cfg, "channels", 1)),
            "duration": int(_opt(cfg, "duration", 1000)),
            "bitrate": int(_opt(cfg, "bitrate", 16000)),
        }
        self._lc3 = None
        if self._cfg["encoder"] == "lc3":
            self._lc3 = Lc3Encoder(self._cfg["sample_rate"], self._cfg["channels"],
                                   self._cfg["duration"], self._cfg["bitrate"])
        with self._buffer_lock:
            self._buffer.clear()
        self._hooks.mic_state(True)

    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        self._hooks.mic_state(self._aad_cb is not None)

    def aad_callback(self, func: Callable | None, threshold: Any = 90, silent_period: Any = 1000) -> None:  # type: ignore[override]
        t = float(threshold if threshold is not None else 90)
        sp = int(silent_period if silent_period is not None else 1000)
        if func is not None and not 60 <= t <= 100:
            raise ValueError("threshold must be between 60 and 100")
        if not 0 <= sp <= 10000:
            raise ValueError("silent_period must be between 0 and 10000")
        super().aad_callback(func, t, sp)
        self._aad_threshold, self._aad_silent_ms = t, sp
        self._hooks.mic_state(self._streaming or func is not None)

    def fire_aad(self) -> None:
        if self._aad_cb is not None:
            self._aad_cb()

    def feed_phone_pcm(self, pcm16: bytes) -> None:
        """Called from the server thread with 16 kHz mono int16 PCM."""
        x = np.frombuffer(pcm16, dtype="<i2")
        self.last_level_db = level_dbfs(x)
        # AAD: map dBFS to an approximate dB SPL (MEMS mic: ~94 dB SPL = -26 dBFS)
        if self._aad_cb is not None:
            spl = self.last_level_db + 120.0
            now = time.monotonic()
            if spl >= self._aad_threshold and (now - self._aad_last) * 1000 >= self._aad_silent_ms:
                self._aad_last = now
                self._post_event(Event(type="aad"))
        if not self._streaming:
            return
        cfg = self._cfg
        y = resample_16k(x, cfg["sample_rate"])
        if cfg["channels"] == 2:
            y = np.repeat(y, 2)
        if self._lc3 is not None:
            out = self._lc3.encode(y)
        elif cfg["bit_depth"] == 8:
            out = (y >> 8).astype(np.int8).tobytes()
        else:
            out = y.astype("<i2").tobytes()
        if out:
            with self._buffer_lock:
                self._buffer.extend(out)
                over = len(self._buffer) - MIC_RING_BYTES
                if over > 0:
                    del self._buffer[:over + (over % 2)]

    def read(self, byte_count: int) -> str | None:  # type: ignore[override]
        if self._cfg.get("encoder") == "lc3":
            # LC3 reads are byte-accurate (no even-size truncation)
            n = int(byte_count)
            if n <= 0 or n % 2 != 0:
                raise ValueError("Bytes must be positive and even")
            if n > 4096:
                raise ValueError("Bytes exceeds maximum of 4096")
            if not self._streaming:
                return None
            with self._buffer_lock:
                chunk = bytes(self._buffer[:n])
                del self._buffer[:n]
            return chunk.decode("latin-1")
        return super().read(byte_count)


class SimSpeaker(SpeakerStub):
    """Plays PCM/LC3 from frame.speaker.play() and the host AUDIO TX channel on the phone."""

    def __init__(self, hooks: DeviceHooks) -> None:
        super().__init__()
        self._hooks = hooks
        self._lock = threading.Lock()
        self._cfg: dict[str, Any] = {}
        self._dec: Lc3Decoder | None = None

    def start(self, cfg: Any = None) -> None:  # type: ignore[override]
        super().start(cfg)
        c = {
            "encoder": "lc3" if str(_opt(cfg, "encoder", "pcm")) == "lc3" else "pcm",
            "sample_rate": int(_opt(cfg, "sample_rate", 8000)),
            "channels": int(_opt(cfg, "channels", 1)),
            "duration": int(_opt(cfg, "duration", 1000)),
            "bitrate": int(_opt(cfg, "bitrate", 16000)),
            "volume": int(_opt(cfg, "volume", 50)),
        }
        with self._lock:
            self._cfg = c
            self._dec = (Lc3Decoder(c["sample_rate"], c["channels"], c["duration"], c["bitrate"])
                         if c["encoder"] == "lc3" else None)
        self._hooks.speaker_state(True, c)

    def play(self, data: Any = None) -> None:  # type: ignore[override]
        super().play(data)
        raw = data if isinstance(data, bytes) else str(data).encode("latin-1")
        self.feed(raw)

    def feed(self, data: bytes) -> None:
        """Audio from Lua play() or the host AUDIO TX characteristic."""
        with self._lock:
            if not self._streaming or not data:
                return
            c, dec = self._cfg, self._dec
            pcm = dec.decode(data) if dec is not None else data[: len(data) - len(data) % 2]
        if pcm:
            self._hooks.speaker_pcm(pcm, c["sample_rate"], c["channels"])

    def volume(self, val: int | None = None) -> int | None:  # type: ignore[override]
        r = super().volume(val)
        if val is not None:
            self._hooks.speaker_volume(int(val))
        return r

    def stop(self) -> None:  # type: ignore[override]
        was = self._streaming
        super().stop()
        if was:
            self._hooks.speaker_state(False)


class SimSound(SoundStub):
    def __init__(self, stop_event: threading.Event, hooks: DeviceHooks) -> None:
        super().__init__(stop_event)
        self._hooks = hooks

    def _start(self, name: str, opts: Any):  # type: ignore[override]
        err = super()._start(name, opts)
        if err is None:
            rate, dur, vol = self._read_options(opts)
            seed = _opt(opts, "seed", None)
            self._hooks.play_sfx(str(name), {"sample_rate": rate, "duration_ms": dur,
                                             "volume": vol, "seed": seed})
        return err

    def stop(self) -> None:  # type: ignore[override]
        super().stop()
        self._hooks.play_sfx("", {"stop": True})
