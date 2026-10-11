"""A real brilliant-sdk host app talking to the simulator over /ws/ble."""
import asyncio
import io
import socket

import pytest
from aiohttp import web
from PIL import Image

from halo_sim import patch
from halo_sim.server import SimServer

CAMERA_APP = r"""
local data = require('data.min')
local camera = require('camera.min')
frame.camera.power_save(false)
print('ready')
while true do
  local ok, err = pcall(function()
    for _, item in ipairs(data.process_raw_items()) do
      if item[1] == 0x0d then camera.capture_and_send(camera.parse_capture_settings(item[2])) end
    end
    frame.sleep(0.02)
  end)
  if not ok then print(err) break end
end
"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
async def server(tmp_path):
    port = free_port()
    srv = SimServer(tmp_path / "sandbox", http_port=port, https_port=0)
    srv.loop = asyncio.get_running_loop()
    srv.device.start()
    runner = web.AppRunner(srv.make_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    patch.install(f"ws://127.0.0.1:{port}/ws/ble")
    yield srv
    await runner.cleanup()
    srv.device.shutdown()


async def test_camera_example_flow(server, tmp_path):
    from brilliant_msg import BrilliantMsg, RxPhoto, TxCaptureSettings

    app = tmp_path / "cam_app.lua"
    app.write_text(CAMERA_APP)
    frame = BrilliantMsg()
    name = await frame.connect()
    assert name == "Halo SIM"
    assert await frame.ble.send_lua("print(frame.HARDWARE_VERSION)", await_print=True) == "halo"
    await frame.upload_stdlua_libs(["data", "camera"])
    await frame.upload_frame_app(str(app), "cam_app.lua")
    await frame.start_frame_app("cam_app")
    q = await RxPhoto(upright=False).attach(frame)
    await frame.send_message(0x0d, TxCaptureSettings(resolution=640, quality_index=2).pack())
    jpeg = await asyncio.wait_for(q.get(), timeout=10)
    assert Image.open(io.BytesIO(jpeg)).size == (640, 480)
    await frame.stop_frame_app()
    await frame.disconnect()
