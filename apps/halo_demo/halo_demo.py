"""Halo demo host app: exercises camera, mic, speaker, IMU, button and taps.

Real glasses:      python halo_demo.py [--name "Halo 08"]
Simulator:         halo-sim run halo_demo.py

The same file works for both: halo-sim only swaps the Bluetooth transport.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from brilliant_msg import (BrilliantMsg, RxAudio, RxPhoto, TxCode, TxImageSpriteBlock,
                           TxPlainText, TxSprite)

HERE = Path(__file__).resolve().parent
TEXT_MSG, SPRITE_MSG, LEVEL_MSG, SPEAKER_MSG, EVENT_MSG = 0x0a, 0x20, 0x31, 0x32, 0x40
STD_LIBS = ["data", "camera", "code", "plain_text", "image_sprite_block", "audio"]


def chime_pcm(rate: int = 16000) -> bytes:
    """A short C-E-G arpeggio as 16-bit mono PCM."""
    out = []
    for f in (523.25, 659.25, 783.99):
        t = np.arange(int(rate * 0.22)) / rate
        env = np.minimum(1, t / 0.01) * np.exp(-t * 9)
        out.append(0.5 * env * np.sin(2 * math.pi * f * t))
    return (np.concatenate(out) * 32767).astype("<i2").tobytes()


async def status(frame: BrilliantMsg, text: str) -> None:
    await frame.send_message(TEXT_MSG, TxPlainText(text[:20]).pack())


async def photos_task(frame: BrilliantMsg, queue: asyncio.Queue, outdir: Path) -> None:
    while True:
        jpeg = await queue.get()
        path = outdir / f"halo_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(jpeg)
        print(f"photo: {len(jpeg)} bytes -> {path}")
        # 256x192 fits the round display; progressive 16-px lines keep device memory low
        sprite = TxSprite.from_image_bytes(jpeg, max_pixels=256 * 192)
        isb = TxImageSpriteBlock(sprite, sprite_line_height=16)
        await frame.send_message(SPRITE_MSG, isb.pack())
        for line in isb.sprite_lines:
            await frame.send_message(SPRITE_MSG, line.pack())


async def audio_task(frame: BrilliantMsg, queue: asyncio.Queue) -> None:
    last = 0.0
    while True:
        chunk = await queue.get()
        if chunk is None:
            continue
        x = np.frombuffer(chunk[: len(chunk) // 2 * 2], dtype="<i2").astype(np.float64)
        if not len(x):
            continue
        db = 20 * math.log10(max(math.sqrt((x ** 2).mean()), 1) / 32768)
        if time.monotonic() - last > 0.1:
            last = time.monotonic()
            level = int(max(0, min(100, (db + 60) * 100 / 60)))
            await frame.send_message(LEVEL_MSG, TxCode(value=level).pack())


async def events_task(frame: BrilliantMsg, queue: asyncio.Queue) -> None:
    pcm = chime_pcm()
    while True:
        ev = await queue.get()
        if ev == 1:
            print("mic: on")
        elif ev == 0:
            print("mic: off")
        elif ev == 3:
            print("chime -> speaker")
            await frame.send_message(SPEAKER_MSG, TxCode(value=1).pack())
            await asyncio.sleep(0.15)
            step = (frame.ble.max_lua_payload()) // 2 * 2
            t0 = time.monotonic()
            for i in range(0, len(pcm), step):
                await frame.ble.send_audio(pcm[i:i + step])
                # pace a little faster than real time (16 kHz * 2 bytes)
                ahead = (i + step) / 32000 - (time.monotonic() - t0)
                if ahead > 0.1:
                    await asyncio.sleep(ahead - 0.1)
            await asyncio.sleep(0.3)
            await frame.send_message(SPEAKER_MSG, TxCode(value=0).pack())


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--name", default=None, help='BLE name, e.g. "Halo 08" (default: first found)')
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl-C)")
    args = p.parse_args()
    outdir = HERE / "photos"
    outdir.mkdir(exist_ok=True)

    frame = BrilliantMsg()
    name = await frame.connect(name=args.name)
    print(f"connected to {name}")
    try:
        await frame.upload_stdlua_libs(STD_LIBS)
        await frame.upload_frame_app(str(HERE / "lua" / "halo_demo_app.lua"), "halo_demo_app.lua")
        frame.attach_print_response_handler(lambda s: print(f"[halo] {s}"))

        events: asyncio.Queue = asyncio.Queue()
        frame.register_data_response_handler(events, [EVENT_MSG], lambda d: events.put_nowait(d[1]))
        photo_q = await RxPhoto(upright=False).attach(frame)
        audio_q = await RxAudio(streaming=True).attach(frame)

        await frame.start_frame_app("halo_demo_app")
        await status(frame, "host connected")
        tasks = [asyncio.ensure_future(t) for t in (
            photos_task(frame, photo_q, outdir), audio_task(frame, audio_q), events_task(frame, events))]
        print("running: click = photo, hold = sound, 2 taps = mic, 3 taps = chime (Ctrl-C to stop)")
        try:
            if args.seconds:
                await asyncio.sleep(args.seconds)
            else:
                await asyncio.Event().wait()
        finally:
            for t in tasks:
                t.cancel()
        await frame.stop_frame_app()
    finally:
        await frame.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
