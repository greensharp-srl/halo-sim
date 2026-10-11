"""Make unmodified brilliant-sdk apps talk to halo-sim instead of Bluetooth.

    import halo_sim.patch; halo_sim.patch.install()   # before using BrilliantMsg

``halo-sim run app.py`` does this for you.
"""
from __future__ import annotations

_installed = False


def install(url: str | None = None) -> None:
    global _installed
    import os

    if url:
        os.environ["HALO_SIM_URL"] = url
    if _installed:
        return
    import brilliant_ble
    import brilliant_ble.brilliant_ble as ble_mod
    import brilliant_msg.brilliant_msg as msg_mod

    from halo_sim.client import SimBrilliantBle

    brilliant_ble.BrilliantBle = SimBrilliantBle
    ble_mod.BrilliantBle = SimBrilliantBle
    msg_mod.BrilliantBle = SimBrilliantBle
    _installed = True
