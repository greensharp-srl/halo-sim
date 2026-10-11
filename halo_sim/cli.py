"""halo-sim command line.

  halo-sim serve            start the virtual glasses (phone UI, launcher menu, host endpoint)
  halo-sim apps             list the apps the launcher shows
  halo-sim install SRC      install an app (folder with halo-app.toml, or git URL)
  halo-sim uninstall ID     remove an installed app
  halo-sim run app.py ...   run an unmodified brilliant-sdk host app against it (dev mode)
  halo-sim repl             interactive Lua REPL on the virtual glasses
"""
from __future__ import annotations

import argparse
import asyncio
import os
import runpy
import sys
from pathlib import Path

from halo_sim import DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT

DEFAULT_SANDBOX = Path.home() / ".halo-sim" / "sandbox"


def _serve(args: argparse.Namespace) -> None:
    from halo_sim.server import SimServer

    server = SimServer(Path(args.sandbox), http_port=args.port, https_port=args.https_port,
                       mtu=args.mtu, verbose=args.verbose, launcher=not args.no_launcher)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n[halo-sim] bye")


def _apps(args: argparse.Namespace) -> None:
    from halo_sim import apps

    found = apps.discover()
    if not found:
        print("no apps; install one with: halo-sim install <folder|git-url>")
    for a in found:
        where = "built-in" if a.builtin else ("linked " if a.dir != (apps.APPS_DIR / a.id) else "") + str(a.dir)
        print(f"  {a.id:22} {a.name:28} {where}")


def _install(args: argparse.Namespace) -> None:
    from halo_sim import apps

    spec = apps.install(args.src, link=args.link)
    print(f"installed {spec.name} ({spec.id}); open it from the launcher menu "
          f"(restart `halo-sim serve` or press HOME on the phone to refresh)")


def _uninstall(args: argparse.Namespace) -> None:
    from halo_sim import apps

    apps.uninstall(args.id)
    print(f"uninstalled {args.id}")


def _run(args: argparse.Namespace) -> None:
    from halo_sim import patch

    patch.install(args.url)
    script = Path(args.script).resolve()
    sys.argv = [str(script), *args.args]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")


def _repl(args: argparse.Namespace) -> None:
    from halo_sim import patch

    patch.install(args.url)
    from brilliant_msg import BrilliantMsg

    async def main() -> None:
        frame = BrilliantMsg()
        name = await frame.connect(initialize=not args.no_break)
        frame.attach_print_response_handler(lambda s: print(s))
        print(f"Connected to {name}. Type Lua; Ctrl-C sends a break, Ctrl-D quits.")
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, "lua> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                await frame.ble.send_break_signal()
                continue
            if line.strip():
                if len(line.encode()) > frame.ble.max_lua_payload():
                    print("line longer than the BLE MTU allows; upload a file instead")
                    continue
                await frame.ble.send_lua(line)
                await asyncio.sleep(0.15)
        await frame.disconnect()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    p = argparse.ArgumentParser(prog="halo-sim", description="Phone-backed Brilliant Labs Halo simulator")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the virtual Halo")
    s.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="HTTP port (Mac browser + host apps)")
    s.add_argument("--https-port", type=int, default=DEFAULT_HTTPS_PORT, help="HTTPS port (phones)")
    s.add_argument("--sandbox", default=str(DEFAULT_SANDBOX), help="the glasses' filesystem directory")
    s.add_argument("--mtu", type=int, default=247, help="simulated BLE MTU (max_length() = MTU-4)")
    s.add_argument("-v", "--verbose", action="store_true", help="echo Lua prints in this terminal")
    s.add_argument("--no-launcher", action="store_true", help="don't show the app menu (bare glasses)")
    s.set_defaults(fn=_serve)

    a = sub.add_parser("apps", help="list installed apps")
    a.set_defaults(fn=_apps)
    i = sub.add_parser("install", help="install an app from a folder or git URL")
    i.add_argument("src", help="folder containing halo-app.toml, or a git URL")
    i.add_argument("--link", action="store_true", help="symlink the folder instead of copying (development)")
    i.set_defaults(fn=_install)
    u = sub.add_parser("uninstall", help="remove an installed app")
    u.add_argument("id")
    u.set_defaults(fn=_uninstall)

    r = sub.add_parser("run", help="run a brilliant-sdk host app against the simulator")
    r.add_argument("--url", default=None, help=f"simulator endpoint (default {os.environ.get('HALO_SIM_URL', 'ws://127.0.0.1:8080/ws/ble')})")
    r.add_argument("script")
    r.add_argument("args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=_run)

    q = sub.add_parser("repl", help="interactive Lua REPL on the virtual Halo")
    q.add_argument("--url", default=None)
    q.add_argument("--no-break", action="store_true", help="don't break/reset a running app on connect")
    q.set_defaults(fn=_repl)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
