"""SimBrilliantBle: the SDK's BrilliantBle with the radio swapped for a WebSocket.

Only the transport is replaced (connect / disconnect / _transmit / send_audio).
Everything above it is the SDK's own code: send_lua, send_data, send_message
chunking + ACK flow control, upload_file_from_string, break/reset signals,
print/data notification routing. So an app that works here exercises the same
host-side path it will use with real glasses.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
from brilliant_ble.brilliant_ble import BrilliantBle as _RealBrilliantBle
from brilliant_ble.brilliant_ble import BrilliantDeviceType

DEFAULT_URL = "ws://127.0.0.1:8080/ws/ble"


def sim_url() -> str:
    return os.environ.get("HALO_SIM_URL", DEFAULT_URL)


class _SimLink:
    """Stands in for bleak's BleakClient (is_connected + mtu_size)."""

    def __init__(self, mtu: int) -> None:
        self.mtu_size = mtu
        self.is_connected = True


class SimBrilliantBle(_RealBrilliantBle):
    def __init__(self) -> None:
        super().__init__()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None

    async def connect(self, name=None, timeout=10, print_response_handler=lambda _: None,
                      data_response_handler=lambda _: None, disconnect_handler=lambda: None,
                      connect_timeout=5):
        self._user_disconnect_handler = disconnect_handler
        self._user_print_response_handler = print_response_handler
        self._user_data_response_handler = data_response_handler
        self._awaiting_print_response = False
        self._awaiting_data_response = False
        self._print_response = asyncio.Queue()
        self._data_response = asyncio.Queue()

        app = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "host app"
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                sim_url(), params={"app": app}, timeout=aiohttp.ClientWSTimeout(ws_close=timeout),
                max_msg_size=8 * 1024 * 1024)
            hello = json.loads((await self._ws.receive(timeout=timeout)).data)
        except Exception as exc:
            await self._session.close()
            raise Exception(f"No matching device found (is `halo-sim serve` running at {sim_url()}?): {exc}")
        if name is not None and name != hello["name"]:
            await self._ws.close()
            await self._session.close()
            raise Exception("No matching device found")

        self._client = _SimLink(int(hello.get("mtu", 247)))
        self._type = BrilliantDeviceType.HALO
        self._name = hello["name"]
        self._reader = asyncio.ensure_future(self._read_loop(self._client))
        return self._name

    async def _read_loop(self, link: _SimLink) -> None:
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.BINARY and msg.data[:1] == b"N":
                    await self._notification_handler(None, bytearray(msg.data[1:]))
        except asyncio.CancelledError:
            pass
        finally:
            link.is_connected = False
            self._disconnect_handler(link)

    async def disconnect(self):
        link = self._client
        if link is not None:
            link.is_connected = False
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = self._session = self._reader = None
        self._disconnect_handler(link)

    async def _transmit(self, data, show_me=False, await_bt_response=True):
        if show_me:
            print(data)
        link = self._connected_client()
        if len(data) > link.mtu_size - 3:
            raise Exception("payload length is too large")
        await self._ws.send_bytes(b"T" + bytes(data))

    async def send_audio(self, data, await_bt_response=False):
        link = self._connected_client()
        if len(data) > link.mtu_size - 3:
            return  # silently dropped, as on hardware
        await self._ws.send_bytes(b"A" + bytes(data))

    async def ota_flash_firmware(self, *args, **kwargs):
        raise NotImplementedError("Firmware updates are not simulated by halo-sim")
