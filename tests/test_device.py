"""Firmware-faithfulness tests for the virtual Halo (no phone, no host)."""
import io
import time

import numpy as np
from PIL import Image


def test_identity_and_mtu(dev):
    dev.lua("print(frame.HARDWARE_VERSION, frame.bluetooth.max_length())")
    assert dev.prints[-1] == "halo\t243"


def test_camera_starts_asleep(dev):
    dev.lua("print(select(2, pcall(frame.camera.capture, {})))")
    assert dev.prints[-1].endswith("camera is asleep")


def test_camera_only_640(dev):
    dev.lua("frame.camera.power_save(false) print(select(2, pcall(frame.camera.capture, {resolution=512})))")
    assert dev.prints[-1].endswith("camera resolution not supported")


def test_capture_from_phone_jpeg(dev):
    buf = io.BytesIO()
    Image.new("RGB", (1280, 720), (200, 30, 30)).save(buf, "JPEG")
    dev.hooks.photo_jpeg = buf.getvalue()
    dev.lua("""
frame.camera.power_save(false)
frame.camera.capture{quality='HIGH'}
while not frame.camera.image_ready() do frame.sleep(0.01) end
local parts = {}
while true do local c = frame.camera.read(240) if c == nil then break end parts[#parts+1] = c end
frame.bluetooth.send('\\x99') _G.jpeg = table.concat(parts) print(#_G.jpeg)
""")
    jpeg = dev.d.call_in_vm(lambda: dev.d.emu._lua.globals().jpeg.encode("latin-1")).result(2)
    img = Image.open(io.BytesIO(jpeg))
    assert img.size == (640, 480)  # centre-cropped to 4:3 like the sensor
    r, g, b = img.getpixel((320, 240))
    assert r > 150 and g < 80


def test_mpix_crop_and_qoi(dev):
    dev.lua("""
local p = {}
frame.camera.mpix.op.debayer_2x2(p)
frame.camera.mpix.op.crop(p, 0, 0, 64, 32)
frame.camera.mpix.op.qoi_encode(p)
frame.camera.mpix.set_pipeline(p)
frame.camera.power_save(false)
frame.camera.capture{}
while not frame.camera.image_ready() do frame.sleep(0.01) end
local d = frame.camera.read(14)
print(string.sub(d, 1, 4), (string.unpack('>I4', d, 5)), (string.unpack('>I4', d, 9)))
""")
    assert dev.prints[-1] == "qoif\t64\t32"


def test_break_raises_interrupted_once(dev):
    dev.lua("""
while true do
  local ok, err = pcall(frame.sleep, 1)
  if not ok then print('caught ' .. err) if string.find(err, 'interrupted$') then error(err, 0) end end
end""", wait=False)
    time.sleep(0.3)
    dev.d.send_break()
    dev.wait_idle()
    assert "caught interrupted" in dev.prints
    dev.lua("print('alive')")
    assert dev.prints[-1] == "alive"


def test_break_tight_loop(dev):
    dev.lua("local x = 0 while true do x = x + 1 end", wait=False)
    time.sleep(0.2)
    dev.d.send_break()
    dev.wait_idle()
    assert dev.prints[-1].endswith("interrupted")


def test_restart_runs_main_lua(dev, tmp_path):
    (tmp_path / "main.lua").write_text("print('main ran')")
    dev.d.send_restart()
    dev.wait_idle()
    assert dev.prints[-1] == "main ran"
    dev.d.send_restart(remove_main=True)
    dev.wait_idle()
    assert not (tmp_path / "main.lua").exists()


def test_deep_sleep_and_button_wake(dev):
    dev.lua("frame.sleep()")
    assert dev.d.asleep
    dev.lua("print('ignored while asleep')")
    assert "ignored while asleep" not in dev.prints
    dev.d.button("single")
    dev.wait_idle()
    assert not dev.d.asleep


def test_send_longer_than_mtu_fails(dev):
    dev.lua("print(pcall(frame.bluetooth.send, string.rep('x', 244)))")
    assert dev.prints[-1].startswith("false")


def test_button_and_tap_callbacks(dev):
    dev.lua("frame.button.long(function() print('long') end) frame.imu.tap_callback(function(k) print('tap ' .. k) end)")
    dev.d.button("long")
    dev.d.tap("triple")
    time.sleep(0.1)
    assert "long" in dev.prints and "tap triple" in dev.prints


def _tone(n=1600, f=440.0):
    t = np.arange(n) / 16000
    return (np.sin(2 * np.pi * f * t) * 12000).astype("<i2").tobytes()


def test_mic_pcm_8k_16bit(dev):
    dev.lua("frame.microphone.start{sample_rate=8000, bit_depth=16}")
    dev.d.mic_pcm(_tone(1600))  # 100 ms at 16 kHz
    dev.lua("local d = frame.microphone.read(4096) print(#d)")
    assert dev.prints[-1] == "1600"  # 800 samples * 2 bytes
    assert ("mic", True) in dev.hooks.events


def test_mic_8bit_stereo(dev):
    dev.lua("frame.microphone.start{sample_rate=16000, bit_depth=8, channels=2}")
    dev.d.mic_pcm(_tone(1600))
    dev.lua("print(#frame.microphone.read(4096))")
    assert dev.prints[-1] == "3200"  # 1600 samples * 2 ch * 1 byte


def test_mic_lc3(dev):
    dev.lua("frame.microphone.start{encoder='lc3', sample_rate=16000, bitrate=32000, duration=1000}")
    dev.d.mic_pcm(_tone(1600))  # 100 ms -> 10 frames of 40 bytes
    dev.lua("print(#frame.microphone.read(4096))")
    assert dev.prints[-1] == "400"


def test_speaker_lc3_to_phone_pcm(dev):
    import lc3
    enc = lc3.Encoder(10000, 16000, 1)
    frames = b"".join(enc.encode(_tone(160 * (i + 1))[-320:], 40, bit_depth=16) for i in range(5))
    dev.lua("frame.speaker.start{encoder='lc3', sample_rate=16000, bitrate=32000, duration=1000}")
    dev.d.audio(frames)
    pcm = [e for e in dev.hooks.events if e[0] == "pcm"]
    assert pcm and pcm[0][1] == 5 * 320 and pcm[0][2] == 16000


def test_sfx_reaches_phone(dev):
    dev.lua("frame.sound.play_async('laser', {volume=40, seed=7})")
    sfx = [e for e in dev.hooks.events if e[0] == "sfx"]
    assert sfx and sfx[0][1] == "laser" and sfx[0][2]["seed"] == 7
