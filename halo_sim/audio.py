"""PCM conversion and LC3 helpers.

The phone always streams 16 kHz mono signed 16-bit PCM. The simulated
microphone converts it to whatever ``frame.microphone.start()`` asked for,
and the simulated speaker decodes LC3 from the host back to PCM for the phone.
"""
from __future__ import annotations

import math

import numpy as np

try:  # lc3py — Google's liblc3 bindings (same codec the Halo firmware uses)
    import lc3 as _lc3
except Exception:  # pragma: no cover - optional at runtime
    _lc3 = None

PHONE_RATE = 16000


def lc3_available() -> bool:
    return _lc3 is not None


def lc3_frame_bytes(bitrate: int, duration: int) -> int:
    """Bytes per LC3 frame. ``duration`` is in firmware units of 10 µs (750/1000)."""
    return int(bitrate * (duration * 10) / 8 / 1_000_000)


def resample_16k(pcm: np.ndarray, rate: int) -> np.ndarray:
    """int16 mono at 16 kHz -> int16 mono at ``rate`` (8000 or 16000)."""
    if rate == PHONE_RATE:
        return pcm
    if rate == 8000:
        # 2:1 decimation with a short averaging low-pass (good enough for speech)
        n = len(pcm) - (len(pcm) % 2)
        x = pcm[:n].astype(np.int32)
        return ((x[0::2] + x[1::2]) // 2).astype(np.int16)
    raise ValueError(f"unsupported sample rate {rate}")


def level_dbfs(pcm: np.ndarray) -> float:
    if len(pcm) == 0:
        return -120.0
    rms = math.sqrt(float(np.mean(pcm.astype(np.float64) ** 2)))
    return 20 * math.log10(max(rms, 1e-3) / 32768.0)


class Lc3Encoder:
    def __init__(self, sample_rate: int, channels: int, duration: int, bitrate: int) -> None:
        if _lc3 is None:
            raise RuntimeError("LC3 needs the 'lc3py' package: pip install lc3py")
        self.frame_us = duration * 10
        self.channels = channels
        self.samples = sample_rate * self.frame_us // 1_000_000
        self.nbytes = lc3_frame_bytes(bitrate, duration)
        self._enc = _lc3.Encoder(self.frame_us, sample_rate, channels)
        self._buf = np.zeros(0, dtype=np.int16)

    def encode(self, pcm: np.ndarray) -> bytes:
        """Feed interleaved int16 samples; returns whole LC3 frames produced."""
        self._buf = np.concatenate([self._buf, pcm])
        per_frame = self.samples * self.channels
        out = bytearray()
        while len(self._buf) >= per_frame:
            chunk = self._buf[:per_frame]
            self._buf = self._buf[per_frame:]
            out += self._enc.encode(chunk.tobytes(), self.nbytes * self.channels, bit_depth=16)
        return bytes(out)


class Lc3Decoder:
    def __init__(self, sample_rate: int, channels: int, duration: int, bitrate: int) -> None:
        if _lc3 is None:
            raise RuntimeError("LC3 needs the 'lc3py' package: pip install lc3py")
        self.nbytes = lc3_frame_bytes(bitrate, duration) * channels
        self._dec = _lc3.Decoder(duration * 10, sample_rate, channels)
        self._buf = bytearray()

    def decode(self, data: bytes) -> bytes:
        self._buf += data
        out = bytearray()
        while len(self._buf) >= self.nbytes:
            frame = bytes(self._buf[: self.nbytes])
            del self._buf[: self.nbytes]
            out += self._dec.decode(frame, bit_depth=16)
        return bytes(out)
