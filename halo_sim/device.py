"""The virtual Halo: Lua VM thread, REPL and firmware control signals.

``SimHalo`` extends the official ``halo_emulator.HaloEmulator`` with the
simulator stubs (camera, live mic, speaker, sfx) and firmware-faithful break
handling. ``VirtualHalo`` wraps it in a REPL thread that behaves like the
device's Lua service: it executes Lua strings from the host one at a time,
dispatches callbacks while idle, and handles break (0x03), restart (0x04)
and remove-main-and-restart (0x05).
"""
from __future__ import annotations

import concurrent.futures
import queue
import threading
from pathlib import Path
from typing import Any, Callable

import lupa.lua54 as lupa

from halo_emulator import HaloEmulator
from halo_emulator.event_queue import Event
from halo_emulator.stubs.system import EmulatorStopException

from halo_sim.camera import DEFAULT_PIPELINE, CameraStub, install_camera
from halo_sim.hooks import DeviceHooks
from halo_sim.stubs import (
    BreakState, SimBluetooth, SimMicrophone, SimSound, SimSpeaker, SimSystem,
)

FIRMWARE_VERSION = "0.8.19-sim"

_SHIMS = r"""
-- Python-raised errors reach Lua as userdata; firmware raises strings
-- (luaL_error). Wrap every native frame.* function so pcall sees a string.
local function wrap(f)
  return function(...)
    local r = table.pack(pcall(f, ...))
    if not r[1] then error(tostring(r[2]), 2) end
    return table.unpack(r, 2, r.n)
  end
end
local function wrap_all(t, depth)
  for k, v in pairs(t) do
    if type(v) == 'userdata' then t[k] = wrap(v)
    elseif type(v) == 'table' and depth < 3 then wrap_all(v, depth + 1) end
  end
end
wrap_all(frame, 0)
local _reboot, _ship = frame.reboot, frame.ship_mode
local _sleep, _yield, _standby, _light = frame.sleep, frame['yield'], frame.standby, frame.light_sleep
local _brk = _sim_check_break
_sim_check_break = nil
local function chk(r)
  if r then error(r, 3) end
end
frame.sleep = function(s) chk(_sleep(s)) end
frame['yield'] = function() chk(_yield()) end
frame.standby = function(s) chk(_standby(s)) end
frame.light_sleep = function(s) chk(_light(s)) end
frame.reboot = function() chk(_reboot()) end
frame.ship_mode = function() chk(_ship()) end
local hook
hook = function()
  local r = _brk()
  if r == 'restart' then
    debug.sethook(hook, '', 1)   -- restart cannot be caught: raise on every instruction
    error('restart', 2)
  elseif r then
    error(r, 2)
  end
end
debug.sethook(hook, '', 1000)
"""


class SimHalo(HaloEmulator):
    def __init__(self, sandbox_dir: str | Path, hooks: DeviceHooks, *, mtu: int = 247,
                 print_handler: Callable[[str], None] | None = print) -> None:
        super().__init__(sandbox_dir, print_handler=print_handler)
        self.hooks = hooks
        self.brk = BreakState()
        self._bluetooth = SimBluetooth(mtu)
        self._microphone = SimMicrophone(hooks, self._event_queue.put)
        self._speaker = SimSpeaker(hooks)
        self._sound = SimSound(self._stop_event, hooks)
        self._system = SimSystem(
            event_queue=self._event_queue, dispatch_fn=self._dispatch_event,
            stop_event=self._stop_event, brk=self.brk,
            on_callback_error=lambda m: self._print_handler and self._print_handler(m),
        )
        self._camera = CameraStub(hooks)

    @property
    def camera(self) -> CameraStub:
        return self._camera

    @property
    def microphone(self) -> SimMicrophone:
        return self._microphone

    @property
    def speaker(self) -> SimSpeaker:
        return self._speaker

    @property
    def display(self):
        return self._display

    def _build_runtime(self) -> None:
        super()._build_runtime()
        rt = self._lua
        g = rt.globals()
        frame = g.frame
        frame.HARDWARE_VERSION = "halo"
        frame.FIRMWARE_VERSION = FIRMWARE_VERSION
        frame.GIT_TAG = "halosim"
        install_camera(rt, frame, self._camera)
        g._sim_check_break = self._system.check_break
        rt.execute(_SHIMS)

    def _dispatch_event(self, event: Event) -> None:
        if event.type == "aad":
            self._microphone.fire_aad()
        else:
            super()._dispatch_event(event)

    def stop_peripherals(self) -> None:
        """What a firmware break does whether or not the script catches it."""
        if self._microphone._streaming:
            self._microphone.stop()
        self._speaker.stop()
        self._camera.release()


class VirtualHalo:
    """A running virtual Halo. Thread-safe entry points for host and phone."""

    def __init__(self, sandbox_dir: str | Path, hooks: DeviceHooks | None = None, *,
                 mtu: int = 247) -> None:
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.hooks = hooks or DeviceHooks()
        self.mtu = mtu
        self.emu = SimHalo(self.sandbox_dir, self.hooks, mtu=mtu, print_handler=self._on_print)
        self.emu._bluetooth.add_send_listener(self._on_send)
        self._notify: Callable[[str, bytes | str], None] | None = None
        self._cmds: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self.asleep = False
        self.state = "booting"  # booting | idle | running | asleep

    # ------------------------------------------------------------ wiring

    def set_notify(self, fn: Callable[[str, bytes | str], None] | None) -> None:
        """fn(kind, payload): kind 'print' (str) or 'data' (bytes). Called from the Lua thread."""
        self._notify = fn

    def _on_print(self, text: str) -> None:
        if self._notify:
            self._notify("print", text)
        self.hooks.log(text)

    def _on_send(self, data: bytes) -> None:
        if self._notify:
            self._notify("data", data)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="halo-sim-lua", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.emu.brk.request_restart()
        self.emu._event_queue.put(Event(type="stop"))
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._boot()
        while not self._shutdown.is_set():
            try:
                cmd = self._cmds.get(timeout=0.005)
            except queue.Empty:
                self._idle()
                continue
            kind = cmd[0]
            if kind == "lua":
                if not self.asleep:
                    self._exec(cmd[1])
            elif kind == "restart":
                self._boot(remove_main=cmd[1])
            elif kind == "file":
                _, name, content, fut = cmd
                try:
                    path = (self.sandbox_dir / name.lstrip("/")).resolve()
                    if self.sandbox_dir not in path.parents and path != self.sandbox_dir:
                        raise ValueError("path escapes the device filesystem")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                    fut.set_result(True)
                except Exception as exc:
                    fut.set_exception(exc)
            elif kind == "call":
                _, fn, fut = cmd
                try:
                    fut.set_result(fn())
                except Exception as exc:
                    fut.set_exception(exc)

    def _idle(self) -> None:
        if self.asleep or self.emu._lua is None:
            return
        try:
            self.emu._system._drain()
        except EmulatorStopException:
            pass

    def _boot(self, remove_main: bool = False) -> None:
        if remove_main:
            (self.sandbox_dir / "main.lua").unlink(missing_ok=True)
        self.emu.stop_peripherals()
        self.emu._event_queue.drain()
        self.emu.brk.clear()
        self.emu._stop_event.clear()
        cam = self.emu._camera
        cam.active, cam._pipeline = False, list(DEFAULT_PIPELINE)
        self.emu._build_runtime()
        self.asleep = False
        self.state = "idle"
        self.hooks.log("[halo-sim] Lua VM started")
        if (self.sandbox_dir / "main.lua").exists():
            self._exec("require('main')")

    def _exec(self, code: str) -> None:
        brk = self.emu.brk
        system = self.emu._system
        brk.busy = True
        self.state = "running"
        try:
            self.emu._lua.execute(code)
        except lupa.LuaSyntaxError as exc:
            self._on_print(str(exc))
        except lupa.LuaError as exc:
            msg = str(exc).split("\nstack traceback")[0]
            if not (brk.restart_pending() and msg.endswith("restart")):
                self._on_print(msg)
        except EmulatorStopException:
            pass
        except Exception as exc:  # Python-side error escaping a stub
            self._on_print(f"{type(exc).__name__}: {exc}")
        finally:
            brk.busy = False
            self.state = "idle"
        if system.power_off_requested:
            system.power_off_requested = False
            brk.clear()
            self._enter_deep_sleep()
        elif brk.restart_pending():
            self._boot()
        else:
            brk.clear()

    def _enter_deep_sleep(self) -> None:
        self.emu.stop_peripherals()
        self.asleep = True
        self.state = "asleep"
        self.hooks.log("[halo-sim] deep sleep: press the button to wake (reboots)")

    # ------------------------------------------------------------ host side (BLE)

    def lua(self, code: str) -> None:
        self._cmds.put(("lua", code))

    def data(self, payload: bytes) -> None:
        self.emu.inject_bluetooth_data(payload)

    def audio(self, payload: bytes) -> None:
        self.emu.speaker.feed(payload)

    def send_break(self) -> None:
        self.emu.brk.request_break()
        self.emu.stop_peripherals()

    def send_restart(self, remove_main: bool = False) -> None:
        if self.emu.brk.busy:
            if remove_main:
                (self.sandbox_dir / "main.lua").unlink(missing_ok=True)
            self.emu.brk.request_restart()
        else:
            self._cmds.put(("restart", remove_main))

    def upload(self, name: str, content: bytes) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._cmds.put(("file", name, content, fut))
        return fut

    def call_in_vm(self, fn: Callable[[], Any]) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._cmds.put(("call", fn, fut))
        return fut

    def set_host_connected(self, connected: bool) -> None:
        self.emu._bluetooth._connected = connected

    # ------------------------------------------------------------ phone side

    def button(self, kind: str) -> None:
        if self.asleep:
            self._cmds.put(("restart", False))
            return
        {"single": self.emu.inject_button_single, "double": self.emu.inject_button_double,
         "long": self.emu.inject_button_long}[kind]()

    def tap(self, kind: str) -> None:
        if not self.asleep:
            self.emu.inject_imu_tap(kind)

    def imu(self, pitch: float, roll: float, accel: tuple[float, float, float],
            compass: tuple[float, float, float]) -> None:
        self.emu.set_imu_direction(pitch, roll, 0.0)
        self.emu.set_imu_raw(compass=compass, accel=accel)

    def mic_pcm(self, pcm16: bytes) -> None:
        self.emu.microphone.feed_phone_pcm(pcm16)

    def set_battery(self, level: int | None = None, charging: bool | None = None) -> None:
        if level is not None:
            self.emu.set_battery_level(max(0, min(100, int(level))))
        if charging is not None:
            self.emu.set_battery_charging(bool(charging))
