"""frame.camera.* for the simulator, backed by the phone camera.

Behaviour follows Halo firmware 0.8.19 ``lua_camera.c``:

* the camera starts in power save; ``capture()``/``image_ready()`` raise
  ``"camera is asleep"`` until ``frame.camera.power_save(false)``;
* only ``resolution = 640`` is accepted (640x480 output, like the PAG7982J1);
* quality ``VERY_HIGH``/``HIGH``/``MEDIUM``/``LOW``; ``VERY_LOW`` == ``LOW``;
* ``read(n)`` returns ``nil`` until ready and once the image is consumed;
* ``read_raw(n)`` streams the 640x480 8-bit SBGGR Bayer frame;
* ``frame.camera.mpix`` pipelines are approximated with Pillow
  (crop, resize_subsample, convert GREY, 3x3/5x5 kernels, denoise,
  jpeg_encode, qoi_encode). Bayer/colour-correction ops are accepted as
  no-ops because the phone image is already demosaiced and balanced.

A Frame-only API (``frame.camera.auto``, ``set_shutter`` ...) is deliberately
absent, as on real Halo hardware.
"""
from __future__ import annotations

import io
import threading
import time
from datetime import datetime
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from halo_sim.hooks import DeviceHooks

WIDTH, HEIGHT = 640, 480
PHOTO_TIMEOUT_S = 2.5

# Firmware quality index -> Pillow JPEG quality. Tuned so a typical scene
# lands near the documented 80/47/25/16 KB sizes.
_QUALITY_NAMES = {"VERY_HIGH": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "VERY_LOW": 3}
_PIL_QUALITY = {0: 92, 1: 80, 2: 60, 3: 40}

MPIX_OPS = (
    "debayer_2x2", "debayer_1x1", "debayer_3x3", "debayer_ir_5x3",
    "correct_black_level", "correct_white_balance", "correct_gamma",
    "correct_color_matrix", "correct_fused", "convert", "crop",
    "resize_subsample", "kernel_convolve_3x3", "kernel_convolve_5x5",
    "kernel_denoise_3x3", "kernel_denoise_5x5", "jpeg_encode", "qoi_encode",
    "palette_encode", "palette_decode",
)
MPIX_CID = ["BLACK_LEVEL", "GAMMA_LEVEL", "RED_BALANCE", "BLUE_BALANCE", "JPEG_QUALITY"] + [
    f"COLOR_MATRIX_{i}" for i in range(9)
]
MPIX_FMT = [
    "RGB332", "RGB565", "RGB24", "XRGB32", "YUV24", "YUYV", "GREY", "JPEG", "QOI",
    "PALETTE1", "PALETTE2", "PALETTE3", "PALETTE4", "PALETTE5", "PALETTE6",
    "PALETTE7", "PALETTE8", "SBGGR8", "SGBRG8", "SGRBG8", "SRGGB8",
]
MPIX_KERNEL = ["identity", "edge_detect", "gaussian_blur", "sharpen"]

DEFAULT_PIPELINE = [("debayer_2x2",), ("correct_black_level",), ("correct_white_balance",), ("jpeg_encode",)]


# --------------------------------------------------------------------------- helpers

def fit_4_3(img: Image.Image) -> Image.Image:
    """Centre-crop any phone frame to 4:3 and scale to 640x480 RGB."""
    return ImageOps.fit(img.convert("RGB"), (WIDTH, HEIGHT), method=Image.Resampling.BILINEAR)


def test_pattern(note: str = "no phone camera") -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    bars = [(192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
            (192, 0, 192), (192, 0, 0), (0, 0, 192)]
    w = WIDTH / len(bars)
    for i, c in enumerate(bars):
        d.rectangle([i * w, 0, (i + 1) * w, HEIGHT * 0.7], fill=c)
    d.rectangle([0, HEIGHT * 0.7, WIDTH, HEIGHT], fill=(16, 16, 16))
    d.text((20, HEIGHT * 0.75), f"HALO SIM  {note}", fill=(255, 255, 255))
    d.text((20, HEIGHT * 0.85), datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], fill=(255, 255, 255))
    return img


def to_bayer_bggr(img: Image.Image) -> bytes:
    """RGB 640x480 -> 8-bit SBGGR8 mosaic (the sensor's raw format)."""
    a = np.asarray(img.convert("RGB"))
    out = np.empty(a.shape[:2], dtype=np.uint8)
    out[0::2, 0::2] = a[0::2, 0::2, 2]  # B
    out[0::2, 1::2] = a[0::2, 1::2, 1]  # G
    out[1::2, 0::2] = a[1::2, 0::2, 1]  # G
    out[1::2, 1::2] = a[1::2, 1::2, 0]  # R
    return out.tobytes()


def qoi_encode(img: Image.Image) -> bytes:
    """Minimal QOI encoder (RGB, 3 channels)."""
    a = np.asarray(img.convert("RGB"), dtype=np.uint8)
    h, w = a.shape[:2]
    px = a.reshape(-1, 3).tolist()
    out = bytearray(b"qoif" + w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([3, 0]))
    index = [(0, 0, 0, 0)] * 64
    prev = (0, 0, 0, 255)
    run = 0
    n = len(px)
    for i, (r, g, b) in enumerate(px):
        cur = (r, g, b, 255)
        if cur == prev:
            run += 1
            if run == 62 or i == n - 1:
                out.append(0xC0 | (run - 1))
                run = 0
            continue
        if run:
            out.append(0xC0 | (run - 1))
            run = 0
        h_ = (r * 3 + g * 5 + b * 7 + 255 * 11) % 64
        if index[h_] == cur:
            out.append(h_)
        else:
            index[h_] = cur
            dr = (r - prev[0] + 128) % 256 - 128
            dg = (g - prev[1] + 128) % 256 - 128
            db = (b - prev[2] + 128) % 256 - 128
            if -2 <= dr <= 1 and -2 <= dg <= 1 and -2 <= db <= 1:
                out.append(0x40 | (dr + 2) << 4 | (dg + 2) << 2 | (db + 2))
            else:
                dr_dg, db_dg = dr - dg, db - dg
                if -32 <= dg <= 31 and -8 <= dr_dg <= 7 and -8 <= db_dg <= 7:
                    out += bytes([0x80 | (dg + 32), (dr_dg + 8) << 4 | (db_dg + 8)])
                else:
                    out += bytes([0xFE, r, g, b])
        prev = cur
    out += b"\x00" * 7 + b"\x01"
    return bytes(out)


_KERNEL_FILTERS = {
    "identity": None,
    "edge_detect": ImageFilter.FIND_EDGES,
    "gaussian_blur": ImageFilter.GaussianBlur(1),
    "sharpen": ImageFilter.SHARPEN,
}


# --------------------------------------------------------------------------- stub

class CameraStub:
    def __init__(self, hooks: DeviceHooks) -> None:
        self._hooks = hooks
        self._runtime: Any = None
        self._lock = threading.Lock()
        self.active = False  # firmware: starts in power save
        self._quality = 0
        self._pending = None
        self._pending_since = 0.0
        self._ready = False
        self._out: bytes | None = None
        self._out_pos = 0
        self._raw: bytes | None = None
        self._raw_pos = 0
        self._pipeline: list[tuple] = list(DEFAULT_PIPELINE)
        self._ctrls: dict[int, int] = {}
        self._stats: dict[str, Any] = {}
        self.last_image: Image.Image | None = None  # for tests / inspection

    # ---- Lua API ----------------------------------------------------------

    def capture(self, cfg: Any = None) -> None:
        if not self.active:
            raise RuntimeError("camera is asleep")
        if cfg is not None:
            res = _opt(cfg, "resolution")
            if res is not None and int(res) != 640:
                raise RuntimeError("camera resolution not supported")
            q = _opt(cfg, "quality")
            if q is not None:
                q = str(q)
                if q not in _QUALITY_NAMES:
                    raise RuntimeError("quality must be either VERY_HIGH, HIGH, MEDIUM, LOW or VERY_LOW")
                self._quality = _QUALITY_NAMES[q]
        with self._lock:
            self._ready = False  # previous frame stops being readable now
            self._out = None
            self._pending = self._hooks.request_photo()
            self._pending_since = time.monotonic()
        self._hooks.camera_flash()

    def image_ready(self) -> bool:
        if not self.active:
            raise RuntimeError("camera is asleep")
        with self._lock:
            if self._pending is not None:
                src = None
                if self._pending.done():
                    try:
                        src = self._pending.result()
                    except Exception:
                        src = None
                    self._process(src)
                elif time.monotonic() - self._pending_since > PHOTO_TIMEOUT_S:
                    self._process(None, note="phone camera timeout")
            return self._ready

    def read(self, nbytes: int) -> str | None:
        n = int(nbytes)
        if n <= 0:
            raise RuntimeError("bytes must be greater than 0")
        if not self._ready or self._out is None or self._out_pos >= len(self._out):
            return None
        chunk = self._out[self._out_pos:self._out_pos + n]
        self._out_pos += len(chunk)
        return chunk.decode("latin-1")

    def read_raw(self, nbytes: int) -> str | None:
        n = int(nbytes)
        if n <= 0:
            raise RuntimeError("bytes must be greater than 0")
        if self._raw is None:
            raise RuntimeError("raw buffer not set")
        if self._raw_pos >= len(self._raw):
            return None
        chunk = self._raw[self._raw_pos:self._raw_pos + n]
        self._raw_pos += len(chunk)
        return chunk.decode("latin-1")

    def power_save(self, enable: Any = None) -> None:
        if not isinstance(enable, bool):
            raise RuntimeError("value must be true or false")
        self.active = not enable

    # ---- mpix --------------------------------------------------------------

    def mpix_set_pipeline(self, p: Any) -> None:
        ops = []
        for i in range(1, 64):
            entry = p[i]
            if entry is None:
                break
            ops.append(tuple(entry[j] for j in range(1, 8) if entry[j] is not None))
        self._pipeline = ops

    def mpix_set_ctrl(self, cid: int, value: int) -> None:
        self._ctrls[int(cid)] = int(value)

    def mpix_get_stats(self) -> Any:
        t = self._runtime.table()
        for k, v in self._stats.items():
            t[k] = self._runtime.table_from(v) if isinstance(v, list) else v
        return t

    # ---- internals -----------------------------------------------------------

    def release(self) -> None:
        """Break/restart: firmware releases camera resources."""
        with self._lock:
            self._pending = None
            self._ready = False

    def _process(self, jpeg: bytes | None, note: str = "no phone camera") -> None:
        self._pending = None
        try:
            img = fit_4_3(Image.open(io.BytesIO(jpeg))) if jpeg else test_pattern(note)
        except Exception:
            img = test_pattern("bad phone frame")
        self.last_image = img
        self._raw, self._raw_pos = to_bayer_bggr(img), 0
        self._update_stats(img)
        out = img
        encoded: bytes | None = None
        for op in self._pipeline:
            name, args = op[0], op[1:]
            if name == "crop" and len(args) == 4:
                x, y, w, h = (int(a) for a in args)
                out = out.crop((x, y, x + w, y + h))
            elif name == "resize_subsample" and len(args) == 2:
                out = out.resize((int(args[0]), int(args[1])), Image.Resampling.NEAREST)
            elif name == "convert" and args and args[0] == MPIX_FMT.index("GREY"):
                out = out.convert("L")
            elif name in ("kernel_convolve_3x3", "kernel_convolve_5x5") and args:
                k = MPIX_KERNEL[int(args[0])] if 0 <= int(args[0]) < len(MPIX_KERNEL) else "identity"
                f = _KERNEL_FILTERS[k]
                if f is not None:
                    out = out.filter(f)
            elif name == "kernel_denoise_3x3":
                out = out.filter(ImageFilter.MedianFilter(3))
            elif name == "kernel_denoise_5x5":
                out = out.filter(ImageFilter.MedianFilter(5))
            elif name == "jpeg_encode":
                buf = io.BytesIO()
                out.save(buf, "JPEG", quality=_PIL_QUALITY[self._quality])
                encoded = buf.getvalue()
            elif name == "qoi_encode":
                encoded = qoi_encode(out)
            elif name == "palette_encode":
                raise RuntimeError("halo-sim: palette_encode is not simulated")
        if encoded is None:  # pipeline without an encoder: raw RGB bytes
            encoded = out.convert("RGB").tobytes()
        self._out, self._out_pos = encoded, 0
        self._ready = True

    def _update_stats(self, img: Image.Image) -> None:
        a = np.asarray(img)
        y = np.asarray(img.convert("L"))
        hist, edges = np.histogram(y, bins=64, range=(0, 256))
        self._stats = {
            "y_histogram": [int(v) for v in hist],
            "y_histogram_vals": [int(v) for v in edges[:-1]],
            "y_histogram_total": int(hist.sum()),
            "rgb_average_r": int(a[..., 0].mean()), "rgb_average_g": int(a[..., 1].mean()),
            "rgb_average_b": int(a[..., 2].mean()),
            "rgb_min_r": int(a[..., 0].min()), "rgb_min_g": int(a[..., 1].min()),
            "rgb_min_b": int(a[..., 2].min()),
            "rgb_max_r": int(a[..., 0].max()), "rgb_max_g": int(a[..., 1].max()),
            "rgb_max_b": int(a[..., 2].max()),
            "nvals": int(y.size),
        }


def _opt(tbl: Any, name: str) -> Any:
    try:
        return tbl[name]
    except Exception:
        return None


def install_camera(rt: Any, frame: Any, cam: CameraStub) -> None:
    """Register frame.camera (incl. mpix) on a lupa runtime."""
    cam._runtime = rt
    cam_tbl = rt.table()
    cam_tbl.capture = cam.capture
    cam_tbl.image_ready = cam.image_ready
    cam_tbl.read = cam.read
    cam_tbl.read_raw = cam.read_raw
    cam_tbl.power_save = cam.power_save
    frame.camera = cam_tbl
    g = rt.globals()
    g._sim_mpix_set_pipeline = cam.mpix_set_pipeline
    g._sim_mpix_set_ctrl = cam.mpix_set_ctrl
    g._sim_mpix_get_stats = cam.mpix_get_stats
    lua_list = lambda xs: "{" + ",".join(f"'{x}'" for x in xs) + "}"
    rt.execute(f"""
local mpix = {{ op = {{}}, cid = {{}}, fmt = {{}}, kernel = {{}} }}
for i, n in ipairs({lua_list(MPIX_OPS)}) do
  mpix.op[n] = function(p, ...) p[#p + 1] = {{ n, ... }} return p end
end
for i, n in ipairs({lua_list(MPIX_CID)}) do mpix.cid[n] = i - 1 end
for i, n in ipairs({lua_list(MPIX_FMT)}) do mpix.fmt[n] = i - 1 end
for i, n in ipairs({lua_list(MPIX_KERNEL)}) do mpix.kernel[n] = i - 1 end
mpix.set_pipeline = _sim_mpix_set_pipeline
mpix.set_ctrl = _sim_mpix_set_ctrl
mpix.get_stats = _sim_mpix_get_stats
frame.camera.mpix = mpix
_sim_mpix_set_pipeline, _sim_mpix_set_ctrl, _sim_mpix_get_stats = nil, nil, nil
""")
