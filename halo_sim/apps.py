"""Halo apps installed in the simulator: manifest, discovery, install/uninstall.

A Halo app is a folder with a host program (Python, brilliant-sdk) and its Lua
side, plus a ``halo-app.toml`` manifest:

    id = "my_app"                       # optional, defaults to the folder name
    name = "My App"                     # shown in the launcher menu
    host = "my_app.py"                  # host program, relative to the folder
    args = ["--lang", "it"]             # optional
    dependencies = ["opencv-python-headless>=4.8"]   # optional, pip requirements
    [env]                               # optional environment for the host
    MY_SERVICE_URL = "https://..."

Built-in apps live in this repo's ``apps/`` and run in the simulator's own
environment. Installed apps live in ``~/.halo-sim/apps/<id>`` (a copy, a git
clone, or a symlink with --link for development) and each gets its own virtual
environment in ``~/.halo-sim/venvs/<id>`` with halo-sim and its dependencies.
On real glasses the same app runs with ``python <host>``; nothing in it is
simulator-specific.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILTIN_DIR = REPO / "apps"
HOME = Path(os.environ.get("HALO_SIM_HOME", Path.home() / ".halo-sim"))
APPS_DIR = HOME / "apps"
VENVS_DIR = HOME / "venvs"
MANIFEST = "halo-app.toml"


@dataclass
class AppSpec:
    id: str
    name: str
    dir: Path
    host: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    builtin: bool = False

    @property
    def python(self) -> str:
        """Interpreter for the host: the simulator's own for built-ins, the app's venv otherwise."""
        if self.builtin:
            return sys.executable
        return str(VENVS_DIR / self.id / "bin" / "python")


def load_manifest(folder: Path, builtin: bool = False) -> AppSpec:
    data = tomllib.loads((folder / MANIFEST).read_text())
    app_id = str(data.get("id") or folder.name)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", app_id):
        raise ValueError(f"{folder / MANIFEST}: invalid id {app_id!r}")
    if "host" not in data:
        raise ValueError(f"{folder / MANIFEST}: 'host' (the host program) is required")
    if not (folder / data["host"]).is_file():
        raise ValueError(f"{folder / MANIFEST}: host program {data['host']} not found")
    return AppSpec(id=app_id, name=str(data.get("name") or app_id), dir=folder.resolve(), host=data["host"],
                   args=[str(a) for a in data.get("args", [])],
                   env={str(k): str(v) for k, v in data.get("env", {}).items()},
                   dependencies=[str(d) for d in data.get("dependencies", [])], builtin=builtin)


def discover() -> list[AppSpec]:
    """Built-in apps first, then installed ones, each sorted by name."""
    found: list[AppSpec] = []
    for base, builtin in ((BUILTIN_DIR, True), (APPS_DIR, False)):
        group = []
        for d in sorted(base.glob("*")) if base.is_dir() else []:
            if (d / MANIFEST).is_file():
                try:
                    group.append(load_manifest(d, builtin))
                except Exception as exc:
                    print(f"[halo-sim] skipping {d.name}: {exc}")
        found += sorted(group, key=lambda a: a.name.lower())
    return found


def find(app_id: str) -> AppSpec | None:
    return next((a for a in discover() if a.id == app_id), None)


# ---------------------------------------------------------------- install
def _uv() -> str:
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).exists():
        raise SystemExit("uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh")
    return uv


def _is_git(src: str) -> bool:
    return src.startswith(("https://", "http://", "git@", "ssh://")) or src.endswith(".git")


def install(src: str, link: bool = False) -> AppSpec:
    """Install an app from a folder or a git URL and build its environment."""
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    staging = APPS_DIR / ".staging"
    if staging.exists() or staging.is_symlink():
        _remove(staging)
    if _is_git(src):
        if link:
            raise SystemExit("--link only works with a local folder")
        subprocess.run(["git", "clone", "--depth", "1", src, str(staging)], check=True)
        folder = staging
    else:
        folder = Path(src).expanduser().resolve()
        if not (folder / MANIFEST).is_file():
            raise SystemExit(f"{folder} has no {MANIFEST}")
    spec = load_manifest(folder)
    dest = APPS_DIR / spec.id
    if dest.exists() or dest.is_symlink():
        _remove(dest)
    if folder == staging:
        staging.rename(dest)
    elif link:
        dest.symlink_to(folder, target_is_directory=True)
    else:
        shutil.copytree(folder, dest, ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", "*.pyc", "captures", "photos"))
    spec = load_manifest(dest)

    uv, venv = _uv(), VENVS_DIR / spec.id
    subprocess.run([uv, "venv", "-q", "--python", "3.12", "--allow-existing", str(venv)], check=True)
    # halo-sim itself (editable) supplies the brilliant-sdk patch used by `halo-sim run`
    subprocess.run([uv, "pip", "install", "-q", "--python", str(venv / "bin" / "python"),
                    "-e", str(REPO), *spec.dependencies], check=True)
    return spec


def uninstall(app_id: str) -> None:
    dest, venv = APPS_DIR / app_id, VENVS_DIR / app_id
    if not (dest.exists() or dest.is_symlink()):
        raise SystemExit(f"{app_id} is not installed (built-in apps can't be uninstalled)")
    _remove(dest)
    if venv.exists():
        shutil.rmtree(venv)


def _remove(p: Path) -> None:
    # a --link install is a symlink: remove the link, never the developer's folder
    if p.is_symlink():
        p.unlink()
    else:
        shutil.rmtree(p)
