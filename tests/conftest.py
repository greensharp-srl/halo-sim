import time

import pytest

from halo_sim.device import VirtualHalo
from halo_sim.hooks import DeviceHooks


class RecordingHooks(DeviceHooks):
    def __init__(self):
        self.events = []
        self.photo_jpeg = None

    def request_photo(self):
        import concurrent.futures
        f = concurrent.futures.Future()
        f.set_result(self.photo_jpeg)
        return f

    def mic_state(self, on): self.events.append(("mic", on))
    def speaker_state(self, on, cfg=None): self.events.append(("speaker", on, cfg))
    def speaker_pcm(self, pcm16, rate, ch): self.events.append(("pcm", len(pcm16), rate, ch))
    def play_sfx(self, name, opts): self.events.append(("sfx", name, opts))


class Dev:
    """VirtualHalo plus helpers to run Lua and collect prints/sends."""

    def __init__(self, tmp_path):
        self.hooks = RecordingHooks()
        self.d = VirtualHalo(tmp_path, self.hooks)
        self.prints, self.sent = [], []
        self.d.set_notify(lambda k, p: (self.prints if k == "print" else self.sent).append(p))
        self.d.start()
        self.wait_idle()

    def wait_idle(self, timeout=5.0):
        t = time.monotonic() + timeout
        time.sleep(0.05)
        while time.monotonic() < t:
            if self.d.state in ("idle", "asleep") and self.d._cmds.empty():
                return
            time.sleep(0.01)
        raise TimeoutError(self.d.state)

    def lua(self, code, wait=True):
        self.d.lua(code)
        if wait:
            self.wait_idle()
        return self.prints


@pytest.fixture
def dev(tmp_path):
    d = Dev(tmp_path)
    yield d
    d.d.shutdown()
