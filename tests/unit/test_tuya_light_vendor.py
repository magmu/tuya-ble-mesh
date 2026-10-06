"""Tests for Tuya white-label Telink lights (vendor 0x0102, e.g. Smart Life "WC Bulb").

Byte layouts and the working command set come from a user's report and local
patch for the lamp (product bXun1QKL), tested from Home Assistant.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak import BleakError

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "custom_components" / "tuya_ble_mesh" / "lib"))

from tuya_ble_mesh import connection as connection_mod  # noqa: E402
from tuya_ble_mesh.connection import BLEConnection, ConnectionState  # noqa: E402
from tuya_ble_mesh.const import (  # noqa: E402
    PAIR_OPCODE_SET_LTK,
    PAIR_OPCODE_SET_OK,
    TELINK_CHAR_PAIRING,
    TELINK_CMD_DP_WRITE,
    TELINK_CMD_POWER,
    TELINK_VENDOR_ID,
    TUYA_LIGHT_VENDOR_ID,
)
from tuya_ble_mesh.device import MeshDevice  # noqa: E402
from tuya_ble_mesh.device_dispatcher import _CommandDispatcher  # noqa: E402
from tuya_ble_mesh.exceptions import MeshConnectionError  # noqa: E402
from tuya_ble_mesh.protocol import (  # noqa: E402
    decode_status,
    encode_compact_dp,
    parse_pair_response,
)
from tuya_ble_mesh.provisioner import set_mesh_credentials  # noqa: E402

MAC = "BC:23:4C:00:00:01"
SESSION_KEY = bytes(range(16))

# Status for 100 % brightness, full cold, as captured: ... db 02 01 00 00 00 ff 00 64 00 1c 00 00
_STATUS_COLD_100 = bytes(7) + bytes.fromhex("db0201000000ff0064001c0000")


class TestStatusDecode:
    def test_tuya_light_status_layout(self) -> None:
        status = decode_status(_STATUS_COLD_100)
        assert status is not None
        assert status.white_brightness == 100
        assert status.white_temp == 127  # full cold on the integration's 0-127 scale
        assert status.power_known is False
        assert status.vendor_id == TUYA_LIGHT_VENDOR_ID

    def test_tuya_light_warm_status(self) -> None:
        data = bytearray(_STATUS_COLD_100)
        data[13], data[14], data[15] = 0x00, 0xFF, 0x01
        status = decode_status(bytes(data))
        assert status is not None
        assert status.white_temp == 0
        assert status.white_brightness == 1

    def test_tuya_light_non_status_packet_ignored(self) -> None:
        data = bytearray(_STATUS_COLD_100)
        data[7] = 0xDC
        assert decode_status(bytes(data)) is None

    def test_default_layout_unchanged(self) -> None:
        data = bytearray(20)
        data[8:10] = TELINK_VENDOR_ID
        data[12], data[13], data[14] = 0, 80, 40
        status = decode_status(bytes(data))
        assert status is not None
        assert status.white_brightness == 80
        assert status.white_temp == 40
        assert status.power_known is True


class TestLongTermKey:
    def test_pair_response_accepts_ltk_request(self) -> None:
        assert parse_pair_response(bytes([PAIR_OPCODE_SET_LTK])).opcode == PAIR_OPCODE_SET_LTK

    @pytest.mark.asyncio
    async def test_ltk_sent_when_device_asks(self) -> None:
        client = AsyncMock()
        client.read_gatt_char = AsyncMock(
            side_effect=[bytes([PAIR_OPCODE_SET_LTK]), bytes([PAIR_OPCODE_SET_OK])]
        )
        with patch("tuya_ble_mesh.provisioner.asyncio.sleep", new=AsyncMock()):
            await set_mesh_credentials(client, SESSION_KEY, b"out_of_mesh", b"123456")

        opcodes = [c.args[1][0] for c in client.write_gatt_char.call_args_list]
        assert opcodes == [0x04, 0x05, PAIR_OPCODE_SET_LTK]
        assert all(c.args[0] == TELINK_CHAR_PAIRING for c in client.write_gatt_char.call_args_list)
        assert len(client.write_gatt_char.call_args_list[2].args[1]) == 17

    @pytest.mark.asyncio
    async def test_no_ltk_when_device_answers_ok(self) -> None:
        client = AsyncMock()
        client.read_gatt_char = AsyncMock(return_value=bytes([PAIR_OPCODE_SET_OK]))
        await set_mesh_credentials(client, SESSION_KEY, b"out_of_mesh", b"123456")
        assert client.write_gatt_char.call_count == 2


class TestCommands:
    def _device(self, vendor: bytes) -> tuple[MeshDevice, AsyncMock]:
        device = MeshDevice(MAC, b"out_of_mesh", b"123456", vendor_id=vendor)
        send = AsyncMock()
        device.send_command = send  # type: ignore[method-assign]
        return device, send

    @pytest.mark.asyncio
    async def test_power_uses_d0(self) -> None:
        device, send = self._device(TUYA_LIGHT_VENDOR_ID)
        await device.send_power(True)
        await device.send_power(False)
        assert [c.args for c in send.call_args_list] == [
            (TELINK_CMD_POWER, b"\x01"),
            (TELINK_CMD_POWER, b"\x00"),
        ]

    @pytest.mark.asyncio
    async def test_brightness_and_temp_use_compact_dps(self) -> None:
        device, send = self._device(TUYA_LIGHT_VENDOR_ID)
        await device.send_brightness(50)
        await device.send_color_temp(127)
        await device.send_color_temp(0)
        assert send.call_args_list[0].args == (TELINK_CMD_DP_WRITE, encode_compact_dp(122, 2, 50))
        # DP 123 reads only the second-lowest byte; 0 is ignored, so full warm is 0x0100
        assert send.call_args_list[1].args == (
            TELINK_CMD_DP_WRITE,
            encode_compact_dp(123, 2, 0xFF00),
        )
        assert send.call_args_list[2].args == (
            TELINK_CMD_DP_WRITE,
            encode_compact_dp(123, 2, 0x0100),
        )

    def test_command_vendor_per_opcode(self) -> None:
        device = MeshDevice(MAC, b"out_of_mesh", b"123456")
        assert device._command_vendor(TELINK_CMD_POWER) == TELINK_VENDOR_ID
        device._tuya_light = True
        assert device._command_vendor(TELINK_CMD_POWER) == TUYA_LIGHT_VENDOR_ID
        assert device._command_vendor(TELINK_CMD_DP_WRITE) == TELINK_VENDOR_ID

    @pytest.mark.asyncio
    async def test_default_vendor_keeps_compact_dp(self) -> None:
        device, send = self._device(TELINK_VENDOR_ID)
        assert device.is_tuya_light is False
        await device.send_power(True)
        assert send.call_args.args[0] == TELINK_CMD_DP_WRITE

    def test_vendor_adopted_from_status(self) -> None:
        device = MeshDevice(MAC, b"out_of_mesh", b"123456")
        device._conn._session_key = bytearray(SESSION_KEY)
        received: list[object] = []
        device.register_status_callback(received.append)
        with patch("tuya_ble_mesh.device.decrypt_notification", return_value=_STATUS_COLD_100):
            device._handle_notification(MagicMock(), bytearray(20))
        assert device.is_tuya_light is True
        assert len(received) == 1

    def test_non_status_packet_not_dispatched(self) -> None:
        device = MeshDevice(MAC, b"out_of_mesh", b"123456")
        device._conn._session_key = bytearray(SESSION_KEY)
        callback = MagicMock()
        device.register_status_callback(callback)
        data = bytearray(_STATUS_COLD_100)
        data[7] = 0xDC
        with patch("tuya_ble_mesh.device.decrypt_notification", return_value=bytes(data)):
            device._handle_notification(MagicMock(), bytearray(20))
        callback.assert_not_called()


class TestDispatcherSpacing:
    @pytest.mark.asyncio
    async def test_back_to_back_commands_are_spaced(self) -> None:
        device = MagicMock()
        device.is_connected = True
        sent_at: list[float] = []

        async def _send_now(*_args: object) -> None:
            sent_at.append(time.monotonic())

        device._send_now = _send_now
        dispatcher = _CommandDispatcher(device)
        dispatcher.start()
        try:
            await dispatcher.enqueue(0xE2, b"\x01", 0)
            await dispatcher.enqueue(0xE2, b"\x02", 0)
            await asyncio.wait_for(dispatcher._queue.join(), timeout=3)
        finally:
            await dispatcher.stop()
        assert len(sent_at) == 2
        assert sent_at[1] - sent_at[0] >= 0.35


class TestConnectionRecovery:
    def _ready(self) -> tuple[BLEConnection, AsyncMock]:
        conn = BLEConnection(MAC, b"out_of_mesh", b"123456")
        client = AsyncMock()
        conn._client = client
        conn._session_key = bytearray(SESSION_KEY)
        conn._state = ConnectionState.READY
        return conn, client

    @pytest.mark.asyncio
    async def test_bleak_error_on_write_is_a_disconnect(self) -> None:
        conn, client = self._ready()
        client.write_gatt_char = AsyncMock(side_effect=BleakError("no services"))
        dropped = MagicMock()
        conn.register_disconnect_callback(dropped)
        with pytest.raises(MeshConnectionError):
            await conn.write_command(bytes(20))
        assert conn.state == ConnectionState.DISCONNECTED
        dropped.assert_called_once()

    @pytest.mark.asyncio
    async def test_link_drop_triggers_disconnect_once(self) -> None:
        conn, _ = self._ready()
        dropped = MagicMock()
        conn.register_disconnect_callback(dropped)
        conn._on_ble_disconnected(MagicMock())
        assert conn._disconnect_task is not None
        await conn._disconnect_task
        conn._on_ble_disconnected(MagicMock())  # already handled
        await conn._handle_disconnect()
        dropped.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_notify_skipped_after_failure(self) -> None:
        connection_mod._SKIP_START_NOTIFY.discard(MAC)
        conn, client = self._ready()
        conn.set_notification_handler(MagicMock())
        client.start_notify = AsyncMock(side_effect=BleakError("not supported"))
        assert await conn._start_notify_safe() is False
        # A new connection for the same address (entry reload) skips it too
        conn2, client2 = self._ready()
        conn2.set_notification_handler(MagicMock())
        assert await conn2._start_notify_safe() is False
        assert client.start_notify.call_count == 1
        client2.start_notify.assert_not_called()
        connection_mod._SKIP_START_NOTIFY.discard(MAC)


class TestDeadLinkAfterNotify:
    @pytest.mark.asyncio
    async def test_connect_fails_when_notify_setup_kills_link(self) -> None:
        conn = BLEConnection(MAC, b"out_of_mesh", b"123456")
        client = AsyncMock()
        client.is_connected = False
        client.read_gatt_char = AsyncMock(return_value=b"1.0")

        async def _connect(*_args: object) -> None:
            conn._client = client

        conn.set_notification_handler(MagicMock())
        with (
            patch.object(conn, "_connect_with_retry", side_effect=_connect),
            patch("tuya_ble_mesh.connection.provision", new=AsyncMock(return_value=SESSION_KEY)),
            pytest.raises(MeshConnectionError),
        ):
            await conn.connect()
        assert conn.state == ConnectionState.DISCONNECTED
        connection_mod._SKIP_START_NOTIFY.discard(MAC)
