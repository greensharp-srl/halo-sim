"""halo-sim server: the virtual glasses plus the phone and host bridges.

Endpoints
  GET  /            phone UI (camera/mic/IMU/speaker/display/button/taps)
  GET  /ca.crt      local CA certificate to install on the phone (HTTP only)
  WS   /ws/phone    phone <-> glasses hardware bridge
  WS   /ws/ble      host app <-> glasses, mirroring the Halo BLE GATT channels

While no host app is connected the glasses show the launcher menu of installed
apps (see launcher.py); the phone's HOME button returns to it.

/ws/ble framing (binary). Host -> sim: b'T' + bytes written to LUA RX
(Lua text, 0x01+data, or a single control byte 0x02..0x07), b'A' + AUDIO RX
bytes, b'U' + u16 name length + name + file content (fast upload).
Sim -> host: b'N' + LUA TX notification (0x01+data, or print text),
b'u' + status byte for an upload. The first text frame is a JSON hello.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import logging
import ssl
import struct
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from PIL import Image

from halo_sim import DEVICE_NAME
from halo_sim.certs import ensure_certs, lan_ips, mdns_name
from halo_sim.device import VirtualHalo
from halo_sim.hooks import DeviceHooks
from halo_sim.launcher import Launcher

log = logging.getLogger("halo_sim")
WEB_DIR = Path(__file__).parent / "web"
DISPLAY_FPS = 25


class Phone:
    def __init__(self, ws: web.WebSocketResponse, peer: str) -> None:
        self.ws = ws
        self.peer = peer
        self.has_camera = False
        self.has_mic = False
        self.has_audio = False


class SimServer(DeviceHooks):
    def __init__(self, sandbox_dir: Path, *, http_port: int, https_port: int, mtu: int = 247,
                 verbose: bool = False, launcher: bool = True) -> None:
        self.http_port, self.https_port = http_port, https_port
        self.verbose = verbose
        self.loop: asyncio.AbstractEventLoop | None = None
        self.phones: list[Phone] = []
        self.host_ws: web.WebSocketResponse | None = None
        self.host_name = ""
        self._photo_id = 0
        self._photo_waiters: dict[int, concurrent.futures.Future] = {}
        self._mic_on = False
        self._speaker: dict[str, Any] = {"on": False}
        self._last_frame: bytes | None = None
        self._last_png: bytes | None = None
        self._last_disp_state: dict[str, Any] = {}
        self._log_tail: list[str] = []
        self.device = VirtualHalo(sandbox_dir, hooks=self, mtu=mtu)
        self.launcher = Launcher(self) if launcher else None

    # ================================================================ hooks (Lua thread)

    def _post(self, coro_fn, *args) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro_fn(*args)))

    def request_photo(self) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        cam = next((p for p in reversed(self.phones) if p.has_camera), None)
        if cam is None or self.loop is None:
            fut.set_result(None)
            return fut
        self._photo_id += 1
        pid = self._photo_id
        self._photo_waiters[pid] = fut
        self._post(self._send_json, cam, {"t": "photo", "id": pid})
        return fut

    def mic_state(self, on: bool) -> None:
        self._mic_on = on
        self._post(self._broadcast_json, {"t": "mic", "on": on})

    def speaker_state(self, on: bool, cfg: dict[str, Any] | None = None) -> None:
        self._speaker = {"on": on, **(cfg or {})}
        self._post(self._broadcast_json, {"t": "speaker", **self._speaker})

    def speaker_pcm(self, pcm16: bytes, sample_rate: int, channels: int) -> None:
        msg = b"S" + struct.pack("<HB", sample_rate, channels) + pcm16
        self._post(self._broadcast_bytes, msg)

    def speaker_volume(self, volume: int) -> None:
        self._speaker["volume"] = volume
        self._post(self._broadcast_json, {"t": "speaker", **self._speaker})

    def play_sfx(self, name: str, opts: dict[str, Any]) -> None:
        self._post(self._broadcast_json, {"t": "sfx", "name": name, **opts})

    def camera_flash(self) -> None:
        self._post(self._broadcast_json, {"t": "flash"})

    def log(self, line: str) -> None:
        if self.launcher and self.launcher.on_print(line):
            return
        self._log_tail = (self._log_tail + [line])[-200:]
        if self.verbose:
            print(f"[lua] {line}")
        self._post(self._broadcast_json, {"t": "log", "line": line})

    # ================================================================ phone side

    async def _send_json(self, phone: Phone, obj: dict) -> None:
        try:
            await phone.ws.send_str(json.dumps(obj))
        except Exception:
            pass

    async def _broadcast_json(self, obj: dict) -> None:
        for p in list(self.phones):
            await self._send_json(p, obj)

    async def _broadcast_bytes(self, data: bytes) -> None:
        for p in list(self.phones):
            try:
                await p.ws.send_bytes(data)
            except Exception:
                pass

    def _status(self) -> dict[str, Any]:
        emu = self.device.emu
        return {
            "t": "status", "device": DEVICE_NAME, "state": self.device.state,
            "host": self.host_name if self.host_ws is not None else None,
            "app": self.launcher.app.name if self.launcher and self.launcher.app else None,
            "phones": len(self.phones), "mic": self._mic_on,
            "mic_db": round(emu.microphone.last_level_db, 1),
            "camera_awake": emu.camera.active, "battery": emu._system._battery_level,
        }

    async def phone_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
        await ws.prepare(request)
        phone = Phone(ws, request.remote or "?")
        self.phones.append(phone)
        print(f"[halo-sim] phone connected from {phone.peer} ({len(self.phones)} total)")
        await self._send_json(phone, {"t": "hello", "device": DEVICE_NAME, "mtu": self.device.mtu})
        await self._send_json(phone, {"t": "mic", "on": self._mic_on})
        await self._send_json(phone, {"t": "speaker", **self._speaker})
        await self._send_json(phone, {"t": "display", **self._last_disp_state})
        for line in self._log_tail[-50:]:
            await self._send_json(phone, {"t": "log", "line": line})
        if self._last_png:
            await ws.send_bytes(b"F" + self._last_png)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    self._phone_binary(phone, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    self._phone_json(phone, json.loads(msg.data))
        finally:
            self.phones.remove(phone)
            print(f"[halo-sim] phone disconnected ({len(self.phones)} left)")
        return ws

    def _phone_binary(self, phone: Phone, data: bytes) -> None:
        kind = data[:1]
        if kind == b"M":
            self.device.mic_pcm(data[1:])
        elif kind == b"C":
            pid = struct.unpack("<I", data[1:5])[0]
            fut = self._photo_waiters.pop(pid, None)
            if fut and not fut.done():
                fut.set_result(data[5:])

    def _phone_json(self, phone: Phone, m: dict) -> None:
        t = m.get("t")
        dev = self.device
        if t == "caps":
            phone.has_camera = bool(m.get("camera"))
            phone.has_mic = bool(m.get("mic"))
            phone.has_audio = bool(m.get("audio"))
        elif t == "button" and m.get("kind") in ("single", "double", "long"):
            dev.button(m["kind"])
        elif t == "tap" and m.get("kind") in ("single", "double", "triple"):
            dev.tap(m["kind"])
        elif t == "imu":
            dev.imu(float(m.get("pitch", 0)), float(m.get("roll", 0)),
                    tuple(m.get("accel", (0, 0, 1000))), tuple(m.get("compass", (0, 0, 0))))
        elif t == "battery":
            dev.set_battery(m.get("level"), m.get("charging"))
        elif t == "photo_fail":
            fut = self._photo_waiters.pop(int(m.get("id", -1)), None)
            if fut and not fut.done():
                fut.set_result(None)
        elif t == "lua" and isinstance(m.get("code"), str):
            if self.host_ws is None:  # phone console REPL only when no host app owns the link
                dev.lua(m["code"])
        elif t == "control" and m.get("kind") in ("break", "restart"):
            dev.send_break() if m["kind"] == "break" else dev.send_restart()
        elif t == "home" and self.launcher:
            asyncio.ensure_future(self.launcher.home())

    # ================================================================ display pump

    async def display_pump(self) -> None:
        period = 1.0 / DISPLAY_FPS
        status_at = 0.0
        while True:
            await asyncio.sleep(period)
            disp = self.device.emu.display
            img: Image.Image = disp.get_image()
            raw = img.tobytes()
            if raw != self._last_frame:
                self._last_frame = raw
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG", compress_level=1)
                self._last_png = buf.getvalue()
                if self.phones:
                    await self._broadcast_bytes(b"F" + self._last_png)
            st = {"brightness": disp._brightness_percent, "pan": [disp._pan_x, disp._pan_y],
                  "power_save": disp._suspended, "asleep": self.device.asleep}
            if st != self._last_disp_state:
                self._last_disp_state = st
                await self._broadcast_json({"t": "display", **st})
            now = time.monotonic()
            if now - status_at > 0.5 and self.phones:
                status_at = now
                await self._broadcast_json(self._status())

    # ================================================================ host (BLE) side

    async def ble_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        if self.host_ws is not None:  # Halo talks to one host at a time
            await self.host_ws.close(message=b"another host connected")
        self.host_ws = ws
        self.host_name = request.query.get("app", "host app")
        dev = self.device
        loop = asyncio.get_running_loop()
        outq: asyncio.Queue = asyncio.Queue()

        def notify(kind: str, payload: Any) -> None:  # Lua thread
            frame = b"N" + (b"\x01" + payload if kind == "data" else str(payload).encode("utf-8"))
            loop.call_soon_threadsafe(outq.put_nowait, frame)

        async def pump() -> None:
            while True:
                frame = await outq.get()
                await ws.send_bytes(frame)

        dev.set_notify(notify)
        dev.set_host_connected(True)
        print(f"[halo-sim] host connected: {self.host_name}")
        self.log(f"[halo-sim] host connected: {self.host_name}")
        await ws.send_str(json.dumps({"name": DEVICE_NAME, "mtu": dev.mtu, "type": "Halo"}))
        sender = asyncio.ensure_future(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.BINARY or not msg.data:
                    continue
                kind, body = msg.data[:1], msg.data[1:]
                if kind == b"T":
                    self._lua_rx(body)
                elif kind == b"A":
                    dev.audio(body)
                elif kind == b"U":
                    (n,) = struct.unpack("<H", body[:2])
                    name = body[2:2 + n].decode("utf-8")
                    fut = dev.upload(name, body[2 + n:])
                    ok = True
                    try:
                        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=5)
                    except Exception:
                        ok = False
                    await ws.send_bytes(b"u" + (b"\x01" if ok else b"\x00"))
        finally:
            sender.cancel()
            if self.host_ws is ws:
                self.host_ws = None
                dev.set_notify(None)
                dev.set_host_connected(False)
                print(f"[halo-sim] host disconnected: {self.host_name}")
                self.log(f"[halo-sim] host disconnected: {self.host_name}")
                if self.launcher:
                    asyncio.ensure_future(self.launcher.on_host_disconnected())
        return ws

    def _lua_rx(self, body: bytes) -> None:
        dev = self.device
        if body[:1] == b"\x01":
            dev.data(body[1:])
        elif len(body) == 1 and 2 <= body[0] <= 7:
            code = body[0]
            if code == 0x03:
                dev.send_break()
            elif code in (0x02, 0x04):
                dev.send_restart()
            elif code == 0x05:
                dev.send_restart(remove_main=True)
            elif code == 0x07:
                for f in dev.sandbox_dir.iterdir():
                    if f.is_file():
                        f.unlink()
        else:
            dev.lua(body.decode("utf-8", errors="replace"))

    # ================================================================ http

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def ca_crt(self, request: web.Request) -> web.StreamResponse:
        _, _, ca = ensure_certs()
        return web.Response(body=ca.read_bytes(), content_type="application/x-x509-ca-cert",
                            headers={"Content-Disposition": 'attachment; filename="halo-sim-ca.crt"'})

    async def info(self, request: web.Request) -> web.Response:
        return web.json_response({**self._status(), "https_port": self.https_port,
                                  "http_port": self.http_port})

    async def display_png(self, request: web.Request) -> web.Response:
        buf = io.BytesIO()
        self.device.emu.display.get_image().convert("RGB").save(buf, "PNG")
        return web.Response(body=buf.getvalue(), content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/ca.crt", self.ca_crt)
        app.router.add_get("/api/info", self.info)
        app.router.add_get("/api/display.png", self.display_png)
        app.router.add_get("/ws/phone", self.phone_ws)
        app.router.add_get("/ws/ble", self.ble_ws)
        app.router.add_static("/static/", WEB_DIR, append_version=True)
        return app

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.device.start()
        app = self.make_app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.http_port).start()
        https_ok = False
        try:
            crt, key, _ = ensure_certs()
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(crt, key)
            await web.TCPSite(runner, "0.0.0.0", self.https_port, ssl_context=ctx).start()
            https_ok = True
        except Exception as exc:
            print(f"[halo-sim] HTTPS disabled ({exc})")
        self._print_banner(https_ok)
        asyncio.ensure_future(self.display_pump())
        if self.launcher:
            await asyncio.sleep(0.5)             # let the VM finish booting
            await self.launcher.show_menu()
        try:
            await asyncio.Event().wait()
        finally:
            if self.launcher:
                self.launcher.kill()

    def _print_banner(self, https_ok: bool) -> None:
        ips = lan_ips()
        host = mdns_name()
        print("\n  Halo SIM is running  (virtual Halo glasses, firmware-faithful Lua VM)\n")
        print(f"  This Mac (browser):  http://localhost:{self.http_port}/")
        if https_ok:
            primary = ips[0] if ips else "MAC-IP"
            print(f"  Phone (same Wi-Fi):  https://{primary}:{self.https_port}/")
            if host:
                print(f"                  or:  https://{host}:{self.https_port}/")
            if len(ips) > 1:
                print(f"  (other addresses: {', '.join(ips[1:])})")
            print(f"  First time on a phone: install http://{primary}:{self.http_port}/ca.crt (see README)")
        print(f"  Apps:                launcher menu on the glasses (tap: next, 2 taps: open, HOME: back)")
        print(f"                       halo-sim apps | install <folder|git-url> | uninstall <id>")
        print(f"  Dev mode:            halo-sim run your_app.py   (sandbox: {self.device.sandbox_dir})\n")
