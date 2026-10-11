"""The simulator's app launcher: a menu on the glasses and an app supervisor.

When no app is running the virtual glasses show the installed apps
(single tap = next, double tap = open). Opening one starts its host program
as a child process, exactly as a developer would with ``halo-sim run``; the
host connects, breaks the menu loop and deploys its Lua like it would on real
glasses. When the app exits, or HOME is pressed on the phone, the menu returns.

The menu runs as a REPL command, so it never writes to the glasses' filesystem.
It asks for an app by printing a marker line that the server intercepts.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import TYPE_CHECKING

from halo_sim import apps

if TYPE_CHECKING:
    from halo_sim.server import SimServer

MARKER = "@@halo-sim:launch:"


def _lua_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def menu_lua(entries: list[apps.AppSpec]) -> str:
    """Lua for the menu. Same rules as any Halo app: pcall loop, re-raise breaks."""
    names = ", ".join(_lua_str(a.name[:22]) for a in entries)
    ids = ", ".join(_lua_str(a.id) for a in entries)
    return f"""
local names, ids = {{{names}}}, {{{ids}}}
local W, VISIBLE, sel, top, pending, starting = 256, 6, 1, 1, {{}}, false
frame.imu.tap_callback(function(kind) pending[kind] = true end)
local function center(s, y, c) frame.display.text(s, math.max(1, math.floor((W - #s * 8) / 2) + 1), y, c) end
local function draw()
  frame.display.clear()
  frame.display.circle(128, 128, 126, 0x1E3A4C, false)
  center('HALO APPS', 34, 0x39D0FF)
  if #names == 0 then
    center('no apps installed', 110, 0xC0C0C0)
    center('halo-sim install <app>', 128, 0x808080)
    return
  end
  if sel < top then top = sel elseif sel >= top + VISIBLE then top = sel - VISIBLE + 1 end
  for i = top, math.min(#names, top + VISIBLE - 1) do
    local y = 58 + (i - top) * 22
    if i == sel then
      frame.display.rect(36, y - 5, 184, 18, 0x39D0FF, true)
      center(names[i], y, 0x000000)
    else
      center(names[i], y, 0xC0C0C0)
    end
  end
  if top > 1 then center('^', 50, 0x808080) end
  if top + VISIBLE <= #names then center('v', 58 + VISIBLE * 22 - 4, 0x808080) end
  center(starting and 'starting...' or 'tap: next  2 taps: open', 200, 0x808080)
end
frame.display.power_save(false)
draw()
while true do
  local ok, err = pcall(function()
    if pending.single and #names > 0 and not starting then sel = sel % #names + 1; draw() end
    if pending.double and #names > 0 and not starting then
      starting = true; draw(); print('{MARKER}' .. ids[sel])
    end
    pending = {{}}
    frame.sleep(0.05)
  end)
  if not ok then
    if type(err) == 'string' and string.find(err, 'interrupted$') then frame.display.clear(); break end
    print(err)
  end
end
"""


class Launcher:
    """Shows the menu while idle and runs one app process at a time."""

    def __init__(self, server: "SimServer") -> None:
        self.server = server
        self.proc: asyncio.subprocess.Process | None = None
        self.app: apps.AppSpec | None = None
        self.menu_shown = False
        self._watch: asyncio.Task | None = None

    # ---------------------------------------------------------------- menu
    async def show_menu(self, force: bool = False) -> None:
        if self.server.host_ws is not None and not force:
            return                               # a host app owns the glasses
        dev = self.server.device
        dev.send_break()                         # stop whatever Lua is still looping
        for _ in range(40):                      # wait for the VM to come back to the prompt
            if dev.state != "running":
                break
            await asyncio.sleep(0.05)
        entries = apps.discover()
        dev.lua(menu_lua(entries))
        self.menu_shown = True
        self.server.log(f"[halo-sim] launcher: {len(entries)} app(s)")

    def on_print(self, line: str) -> bool:
        """Called from the Lua thread for every print; True if it was a launcher request."""
        if not line.startswith(MARKER):
            return False
        app_id = line[len(MARKER):].strip()
        self.server._post(self.launch, app_id)
        return True

    # ---------------------------------------------------------------- apps
    async def launch(self, app_id: str) -> None:
        spec = apps.find(app_id)
        if spec is None:
            self.server.log(f"[halo-sim] unknown app {app_id}")
            await self.show_menu()
            return
        await self.stop_app(show_menu=False)
        url = f"ws://127.0.0.1:{self.server.http_port}/ws/ble"
        env = {**os.environ, **spec.env, "HALO_SIM_URL": url, "PYTHONUNBUFFERED": "1"}
        cmd = [spec.python, "-m", "halo_sim.cli", "run", spec.host, *spec.args]
        print(f"[halo-sim] starting {spec.name}: {' '.join(cmd[3:])}  (in {spec.dir})")
        self.menu_shown = False
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=spec.dir, env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except Exception as exc:
            self.server.log(f"[halo-sim] could not start {spec.name}: {exc}")
            await self.show_menu()
            return
        self.app = spec
        self._watch = asyncio.ensure_future(self._follow(self.proc, spec))

    async def _follow(self, proc: asyncio.subprocess.Process, spec: apps.AppSpec) -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            print(f"[{spec.id}] {line}")
            self.server.log(f"[{spec.id}] {line}")
        code = await proc.wait()
        print(f"[halo-sim] {spec.name} exited ({code})")
        if self.proc is proc:            # not replaced by HOME / another launch
            self.proc, self.app = None, None
            await asyncio.sleep(0.3)     # let its BLE link close first
            await self.show_menu()

    async def stop_app(self, show_menu: bool = True) -> None:
        proc, self.proc, self.app = self.proc, None, None
        if proc and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGINT)      # like Ctrl-C: the host stops its Lua app
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if show_menu:
            await self.show_menu()

    async def home(self) -> None:
        """HOME on the phone: leave the running app (or an external host) for the menu."""
        if self.proc:
            await self.stop_app(show_menu=False)
        if self.server.host_ws is not None:
            await self.server.host_ws.close(message=b"home")
        await self.show_menu(force=True)

    def kill(self) -> None:
        """Server shutdown: don't leave an orphaned host process behind."""
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    async def on_host_disconnected(self) -> None:
        # an external `halo-sim run` session ended: back to the menu
        if self.proc is None:
            await asyncio.sleep(0.3)
            if self.server.host_ws is None and self.proc is None:
                await self.show_menu()
