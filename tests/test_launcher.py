"""Launcher: menu on the glasses, app registry, and the launch -> exit -> menu cycle."""
import asyncio
import socket
import time

import pytest
from aiohttp import web

from halo_sim import apps, launcher
from halo_sim.launcher import MARKER, menu_lua
from halo_sim.server import SimServer


def spec(app_id, name, tmp_path):
    d = tmp_path / app_id
    d.mkdir(exist_ok=True)
    (d / "host.py").write_text("")
    return apps.AppSpec(id=app_id, name=name, dir=d, host="host.py")


def wait_for(cond, timeout=5.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_menu_scrolls_and_opens(dev, tmp_path):
    entries = [spec("a", "Alpha", tmp_path), spec("b", "Bravo", tmp_path), spec("c", "Charlie", tmp_path)]
    dev.lua(menu_lua(entries), wait=False)
    assert wait_for(lambda: dev.d.state == "running")
    dev.d.tap("double")
    assert wait_for(lambda: any(p == MARKER + "a" for p in dev.prints))
    # 'starting...' ignores further taps until a host takes over; a break ends the menu cleanly
    dev.d.send_break()
    dev.wait_idle()
    dev.prints.clear()
    dev.lua(menu_lua(entries), wait=False)
    assert wait_for(lambda: dev.d.state == "running")
    for _ in range(4):                     # a -> b -> c -> a -> b (wraps around)
        dev.d.tap("single")
        time.sleep(0.15)
    dev.d.tap("double")
    assert wait_for(lambda: any(p == MARKER + "b" for p in dev.prints))
    dev.d.send_break()
    dev.wait_idle()
    assert not [p for p in dev.prints if "error" in p.lower()]


def test_empty_menu_runs(dev):
    dev.lua(menu_lua([]), wait=False)
    assert wait_for(lambda: dev.d.state == "running")
    dev.d.tap("double")
    time.sleep(0.2)
    assert not [p for p in dev.prints if p.startswith(MARKER)]
    dev.d.send_break()
    dev.wait_idle()


def test_manifest_and_discovery(tmp_path, monkeypatch):
    builtin, installed = tmp_path / "builtin", tmp_path / "home" / "apps"
    for base, app_id, name in ((builtin, "zeta", "Zeta"), (builtin, "alpha", "Alpha"), (installed, "mine", "My App")):
        d = base / app_id
        d.mkdir(parents=True)
        (d / "main.py").write_text("")
        (d / apps.MANIFEST).write_text(f'name = "{name}"\nhost = "main.py"\nargs = ["--x", 1]\n[env]\nK = "v"\n')
    bad = builtin / "broken"
    bad.mkdir()
    (bad / apps.MANIFEST).write_text('name = "Broken"\nhost = "missing.py"\n')
    monkeypatch.setattr(apps, "BUILTIN_DIR", builtin)
    monkeypatch.setattr(apps, "APPS_DIR", installed)
    found = apps.discover()
    assert [a.id for a in found] == ["alpha", "zeta", "mine"]          # built-ins first, by name
    assert found[0].builtin and not found[2].builtin
    assert found[0].args == ["--x", "1"] and found[0].env == {"K": "v"}


def test_install_link_and_uninstall_keep_source(tmp_path, monkeypatch):
    src = tmp_path / "src_app"
    src.mkdir()
    (src / "host.py").write_text("")
    (src / apps.MANIFEST).write_text('id = "dev_app"\nname = "Dev App"\nhost = "host.py"\n')
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path / "home" / "apps")
    monkeypatch.setattr(apps, "VENVS_DIR", tmp_path / "home" / "venvs")
    monkeypatch.setattr(apps, "BUILTIN_DIR", tmp_path / "none")
    calls = []
    monkeypatch.setattr(apps.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    s = apps.install(str(src), link=True)
    assert s.id == "dev_app" and (apps.APPS_DIR / "dev_app").is_symlink()
    assert any("venv" in c for c in calls) and any("-e" in c for c in calls)
    assert [a.id for a in apps.discover()] == ["dev_app"]
    apps.uninstall("dev_app")
    assert src.is_dir() and (src / "host.py").exists()                  # the developer's folder survives
    assert apps.discover() == []


HOST_APP = '''
import asyncio
from brilliant_msg import BrilliantMsg

async def main():
    frame = BrilliantMsg()
    await frame.connect()
    await frame.ble.send_lua("print('hello from app')", await_print=True)
    print("host done", flush=True)
    await frame.disconnect()

asyncio.run(main())
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def test_launch_runs_app_then_returns_to_menu(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin" / "tiny"
    builtin.mkdir(parents=True)
    (builtin / "tiny.py").write_text(HOST_APP)
    (builtin / apps.MANIFEST).write_text('name = "Tiny"\nhost = "tiny.py"\n')
    monkeypatch.setattr(apps, "BUILTIN_DIR", tmp_path / "builtin")
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path / "none")

    port = free_port()
    srv = SimServer(tmp_path / "sandbox", http_port=port, https_port=0)
    srv.loop = asyncio.get_running_loop()
    srv.device.start()
    runner = web.AppRunner(srv.make_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        await asyncio.sleep(0.3)
        await srv.launcher.show_menu()
        await asyncio.sleep(0.3)
        assert srv.launcher.menu_shown and srv.device.state == "running"
        srv.device.tap("double")                                     # open "Tiny"
        for _ in range(200):
            if srv.launcher.proc is not None:
                break
            await asyncio.sleep(0.05)
        assert srv.launcher.app and srv.launcher.app.id == "tiny"
        for _ in range(400):                                         # app runs, exits, menu returns
            if srv.launcher.proc is None and srv.launcher.menu_shown:
                break
            await asyncio.sleep(0.05)
        assert srv.launcher.menu_shown and srv.launcher.proc is None
        assert any("hello from app" in line for line in srv._log_tail)
        assert any("[tiny] host done" in line for line in srv._log_tail)
    finally:
        srv.launcher.kill()
        await runner.cleanup()
        srv.device.shutdown()


async def test_home_stops_a_running_app(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin" / "sleepy"
    builtin.mkdir(parents=True)
    (builtin / "sleepy.py").write_text("import time\nprint('up', flush=True)\ntime.sleep(60)\n")
    (builtin / apps.MANIFEST).write_text('name = "Sleepy"\nhost = "sleepy.py"\n')
    monkeypatch.setattr(apps, "BUILTIN_DIR", tmp_path / "builtin")
    monkeypatch.setattr(apps, "APPS_DIR", tmp_path / "none")
    srv = SimServer(tmp_path / "sandbox", http_port=free_port(), https_port=0)
    srv.loop = asyncio.get_running_loop()
    srv.device.start()
    try:
        await asyncio.sleep(0.3)
        await srv.launcher.launch("sleepy")
        proc = srv.launcher.proc
        assert proc is not None
        await asyncio.sleep(0.5)
        await srv.launcher.home()
        assert proc.returncode is not None and srv.launcher.proc is None and srv.launcher.menu_shown
    finally:
        srv.launcher.kill()
        srv.device.shutdown()
