"""Interface between the virtual Halo and the outside world (the phone bridge).

The device side calls these hooks from the Lua thread; implementations must be
thread-safe and must not block. ``NullHooks`` is used for headless runs and
tests: no phone, the camera falls back to a generated test pattern.
"""
from __future__ import annotations

import concurrent.futures
from typing import Any


class DeviceHooks:
    def request_photo(self) -> "concurrent.futures.Future[bytes | None]":
        """Ask the phone for a fresh camera frame (any JPEG). Result None = no phone."""
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(None)
        return fut

    def mic_state(self, on: bool) -> None:
        """Tell the phone to start or stop streaming its microphone."""

    def speaker_state(self, on: bool, cfg: dict[str, Any] | None = None) -> None:
        """Speaker stream started/stopped (cfg: sample_rate, channels, volume)."""

    def speaker_pcm(self, pcm16: bytes, sample_rate: int, channels: int) -> None:
        """Signed 16-bit little-endian PCM for the phone to play."""

    def speaker_volume(self, volume: int) -> None:
        """Speaker volume 0-100 changed."""

    def play_sfx(self, name: str, opts: dict[str, Any]) -> None:
        """frame.sound preset to synthesize on the phone."""

    def camera_flash(self) -> None:
        """A capture happened (phone shows a shutter indicator)."""

    def log(self, line: str) -> None:
        """A line for the phone console (Lua prints, errors, system notes)."""


NullHooks = DeviceHooks
