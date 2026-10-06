"""Unit tests for SIG Mesh light support.

Covers:
- Light Lightness / CTL / HSL message encoding and vendor Model App Bind
- Composition Data element parsing
- SIGMeshDevice.get_composition_data and light send methods
- Provisioning: AppKey bound to every model, light vs plug detection
- TuyaBLEMeshSIGLight entity commands
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _ROOT)
sys.path.insert(0, str(Path(_ROOT) / "custom_components" / "tuya_ble_mesh" / "lib"))

from homeassistant.components.light import ColorMode  # noqa: E402
from tuya_ble_mesh.exceptions import (  # noqa: E402
    MalformedPacketError,
    ProtocolError,
    SIGMeshError,
)
from tuya_ble_mesh.sig_mesh_device import SIGMeshDevice  # noqa: E402
from tuya_ble_mesh.sig_mesh_protocol import (  # noqa: E402
    OP_CONFIG_COMPOSITION_STATUS,
    MeshKeys,
    config_model_app_bind,
    light_ctl_set,
    light_hsl_set,
    light_lightness_set,
    parse_composition_data,
    parse_composition_elements,
)

from custom_components.tuya_ble_mesh.config_flow_sig import (  # noqa: E402
    _bind_app_key,
    device_type_for_models,
)
from custom_components.tuya_ble_mesh.const import (  # noqa: E402
    DEVICE_TYPE_SIG_LIGHT,
    DEVICE_TYPE_SIG_PLUG,
)
from custom_components.tuya_ble_mesh.coordinator import (  # noqa: E402
    TuyaBLEMeshDeviceState,
)
from custom_components.tuya_ble_mesh.device_factory import _DEVICE_CREATORS  # noqa: E402
from custom_components.tuya_ble_mesh.light import (  # noqa: E402
    TuyaBLEMeshSIGLight,
    sig_color_modes,
    sig_ctl_temp_from_ha,
    sig_hsl_from_ha,
    sig_lightness_from_ha,
)


def _element(sig: list[int], vendor: list[tuple[int, int]]) -> bytes:
    data = struct.pack("<HBB", 0, len(sig), len(vendor))
    data += b"".join(struct.pack("<H", m) for m in sig)
    data += b"".join(struct.pack("<HH", c, m) for c, m in vendor)
    return data


# Page 0 header: page, CID, PID, VID, CRPL, features
_COMP_HEADER = bytes([0x00]) + struct.pack("<HHHHH", 0x07D0, 0x1234, 0x0001, 0x0020, 0x0003)
_LIGHT_ELEMENTS = _element(
    [0x0000, 0x0002, 0x1000, 0x1300, 0x1303, 0x1307], [(0x07D0, 0x0004)]
) + _element([0x1306], [])
_LIGHT_COMPOSITION = _COMP_HEADER + _LIGHT_ELEMENTS


def _make_device() -> SIGMeshDevice:
    dev = SIGMeshDevice("DC:23:4F:10:52:C4", 0x00B0, 0x0001, MagicMock())
    dev._keys = MeshKeys(
        "f7a2a44f8e8a8029064f173ddc1e2b00",  # pragma: allowlist secret
        "00112233445566778899aabbccddeeff",  # pragma: allowlist secret
        "3216d1509884b533248541792b877f98",  # pragma: allowlist secret
    )
    dev._client = MagicMock()
    dev._client.write_gatt_char = AsyncMock()
    return dev


# ---------------------------------------------------------------------------
# Codec
# ---------------------------------------------------------------------------


class TestLightMessages:
    def test_lightness_set(self) -> None:
        assert light_lightness_set(0xFFFF, 7) == bytes.fromhex("824cffff07")

    def test_ctl_set(self) -> None:
        payload = light_ctl_set(0x8000, 4000, 0, 1)
        assert payload == bytes.fromhex("825e") + struct.pack("<HHhB", 0x8000, 4000, 0, 1)

    def test_ctl_temperature_set(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import light_ctl_temperature_set

        payload = light_ctl_temperature_set(4000, 0, 2)
        assert payload == bytes.fromhex("8264") + struct.pack("<HhB", 4000, 0, 2)

    def test_ctl_rejects_out_of_range_temperature(self) -> None:
        with pytest.raises(ProtocolError):
            light_ctl_set(0x8000, 500)

    def test_hsl_set(self) -> None:
        payload = light_hsl_set(0x7FFF, 0x5555, 0xFFFF, 2)
        assert payload == bytes.fromhex("8276") + struct.pack("<HHHB", 0x7FFF, 0x5555, 0xFFFF, 2)

    def test_hsl_fits_unsegmented_access_payload(self) -> None:
        assert len(light_hsl_set(1, 2, 3)) <= 11

    def test_rejects_out_of_range_lightness(self) -> None:
        with pytest.raises(ProtocolError):
            light_lightness_set(0x10000)


class TestModelAppBind:
    def test_sig_model_unchanged(self) -> None:
        assert config_model_app_bind(0x00B0, 0, 0x1000) == bytes.fromhex("803db0000000" + "0010")

    def test_vendor_model(self) -> None:
        payload = config_model_app_bind(0x00B0, 0, 0x0004, company_id=0x07D0)
        assert payload == bytes.fromhex("803d") + struct.pack("<HHHH", 0x00B0, 0, 0x07D0, 0x0004)


class TestCompositionElements:
    def test_parses_elements(self) -> None:
        elements = parse_composition_elements(_LIGHT_ELEMENTS)
        assert len(elements) == 2
        assert elements[0].sig_models == (0x0000, 0x0002, 0x1000, 0x1300, 0x1303, 0x1307)
        assert elements[0].vendor_models == ((0x07D0, 0x0004),)
        assert elements[1].sig_models == (0x1306,)

    def test_composition_data_sig_models(self) -> None:
        comp = parse_composition_data(_LIGHT_COMPOSITION)
        assert comp.cid == 0x07D0
        assert 0x1307 in comp.sig_models
        assert 0x1306 in comp.sig_models

    def test_truncated_element_raises(self) -> None:
        with pytest.raises(MalformedPacketError):
            parse_composition_elements(_LIGHT_ELEMENTS[:-1])

    def test_empty_element_list(self) -> None:
        assert parse_composition_elements(b"") == ()


# ---------------------------------------------------------------------------
# SIGMeshDevice
# ---------------------------------------------------------------------------


class TestDeviceLightCommands:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "args"),
        [
            ("request_onoff_state", ()),
            ("send_light_lightness", (0x8000,)),
            ("send_light_ctl", (0x8000, 3000)),
            ("send_light_ctl_temperature", (3000,)),
            ("send_light_hsl", (0x4000, 0x1000, 0xFFFF)),
        ],
    )
    async def test_writes_proxy_pdu(self, method: str, args: tuple[int, ...]) -> None:
        dev = _make_device()
        await getattr(dev, method)(*args)
        dev._client.write_gatt_char.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_light_state_sends_four_gets(self) -> None:
        dev = _make_device()
        await dev.request_light_state()
        assert dev._client.write_gatt_char.await_count == 4

    @pytest.mark.asyncio
    async def test_tid_increments(self) -> None:
        dev = _make_device()
        start = dev._tid
        await dev.send_light_lightness(1)
        await dev.send_light_lightness(2)
        assert dev._tid == (start + 2) & 0xFF

    @pytest.mark.asyncio
    async def test_not_connected(self) -> None:
        dev = SIGMeshDevice("DC:23:4F:10:52:C4", 0x00B0, 0x0001, MagicMock())
        with pytest.raises(SIGMeshError, match="Not connected"):
            await dev.send_light_lightness(1)


class TestGetCompositionData:
    @pytest.mark.asyncio
    async def test_returns_parsed_composition(self) -> None:
        dev = _make_device()

        async def _respond() -> None:
            for key, future in dev._pending_responses.items():
                if key[0] == OP_CONFIG_COMPOSITION_STATUS:
                    future.set_result(_LIGHT_COMPOSITION)

        dev.request_composition_data = AsyncMock(side_effect=_respond)  # type: ignore[method-assign]
        comp = await dev.get_composition_data(response_timeout=1.0)

        assert 0x1300 in comp.sig_models
        assert dev.firmware_version == "CID:07D0 PID:1234 VID:0001"
        assert dev._pending_responses == {}

    @pytest.mark.asyncio
    async def test_timeout_raises(self) -> None:
        dev = _make_device()
        dev.request_composition_data = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(SIGMeshError, match="Timeout"):
            await dev.get_composition_data(response_timeout=0.01)
        assert dev._pending_responses == {}

    @pytest.mark.asyncio
    async def test_malformed_raises(self) -> None:
        dev = _make_device()

        async def _respond() -> None:
            for future in dev._pending_responses.values():
                future.set_result(_COMP_HEADER + b"\x00\x00\x05")

        dev.request_composition_data = AsyncMock(side_effect=_respond)  # type: ignore[method-assign]
        with pytest.raises(SIGMeshError, match="Malformed"):
            await dev.get_composition_data(response_timeout=1.0)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


class TestBindAppKey:
    @pytest.mark.asyncio
    async def test_binds_every_non_foundation_model(self) -> None:
        device = MagicMock()
        device.get_composition_data = AsyncMock(
            return_value=parse_composition_data(_LIGHT_COMPOSITION)
        )
        device.send_config_model_app_bind = AsyncMock(return_value=True)

        models = await _bind_app_key(device, "AA:BB:CC:DD:EE:FF")

        calls = [(c.args, c.kwargs) for c in device.send_config_model_app_bind.await_args_list]
        assert calls == [
            ((0x00B0, 0, 0x1000), {"company_id": None}),
            ((0x00B0, 0, 0x1300), {"company_id": None}),
            ((0x00B0, 0, 0x1303), {"company_id": None}),
            ((0x00B0, 0, 0x1307), {"company_id": None}),
            ((0x00B0, 0, 0x0004), {"company_id": 0x07D0}),
            ((0x00B1, 0, 0x1306), {"company_id": None}),
        ]
        assert 0x1300 in models

    @pytest.mark.asyncio
    async def test_failed_bind_continues(self) -> None:
        device = MagicMock()
        device.get_composition_data = AsyncMock(
            return_value=parse_composition_data(_LIGHT_COMPOSITION)
        )
        device.send_config_model_app_bind = AsyncMock(return_value=False)
        await _bind_app_key(device, "AA:BB:CC:DD:EE:FF")
        assert device.send_config_model_app_bind.await_count == 6

    @pytest.mark.asyncio
    async def test_bind_timeout_continues_and_keeps_models(self) -> None:
        device = MagicMock()
        device.get_composition_data = AsyncMock(
            return_value=parse_composition_data(_LIGHT_COMPOSITION)
        )
        device.send_config_model_app_bind = AsyncMock(side_effect=SIGMeshError("Timeout"))
        models = await _bind_app_key(device, "AA:BB:CC:DD:EE:FF")
        assert device.send_config_model_app_bind.await_count == 6
        assert 0x1300 in models

    @pytest.mark.asyncio
    async def test_composition_failure_falls_back_to_onoff(self) -> None:
        device = MagicMock()
        device.get_composition_data = AsyncMock(side_effect=SIGMeshError("Timeout"))
        device.send_config_model_app_bind = AsyncMock(return_value=True)

        models = await _bind_app_key(device, "AA:BB:CC:DD:EE:FF")

        assert models == frozenset()
        device.send_config_model_app_bind.assert_awaited_once_with(0x00B0, 0, 0x1000)


class TestDeviceTypeDetection:
    def test_light_lightness_means_light(self) -> None:
        assert device_type_for_models(frozenset({0x1000, 0x1300})) == DEVICE_TYPE_SIG_LIGHT

    def test_onoff_only_means_plug(self) -> None:
        assert device_type_for_models(frozenset({0x1000})) == DEVICE_TYPE_SIG_PLUG

    def test_unknown_means_plug(self) -> None:
        assert device_type_for_models(frozenset()) == DEVICE_TYPE_SIG_PLUG

    def test_factory_handles_sig_light(self) -> None:
        assert DEVICE_TYPE_SIG_LIGHT in _DEVICE_CREATORS


# ---------------------------------------------------------------------------
# Light entity
# ---------------------------------------------------------------------------


class TestConversions:
    def test_lightness_full_scale(self) -> None:
        assert sig_lightness_from_ha(255) == 0xFFFF
        assert sig_lightness_from_ha(0) == 0

    def test_ctl_temp_spans_full_node_range(self) -> None:
        assert sig_ctl_temp_from_ha(2700) == 800
        assert sig_ctl_temp_from_ha(6500) == 20000
        assert sig_ctl_temp_from_ha(4600) == 10400
        assert sig_ctl_temp_from_ha(1000) == 800
        assert sig_ctl_temp_from_ha(9000) == 20000

    def test_hsl_red_full_brightness(self) -> None:
        lightness, hue, saturation = sig_hsl_from_ha((0.0, 100.0), 255)
        assert (lightness, hue, saturation) == (0x7FFF, 0, 0xFFFF)

    def test_hsl_hue_wraps(self) -> None:
        assert sig_hsl_from_ha((360.0, 50.0), 255)[1] == 0

    def test_color_modes(self) -> None:
        assert sig_color_modes(frozenset({0x1300, 0x1303, 0x1307})) == {
            ColorMode.HS,
            ColorMode.COLOR_TEMP,
        }
        assert sig_color_modes(frozenset({0x1300})) == {ColorMode.BRIGHTNESS}
        assert sig_color_modes(frozenset()) == {ColorMode.ONOFF}


def _make_light(models: frozenset[int]) -> tuple[TuyaBLEMeshSIGLight, MagicMock]:
    coord = MagicMock()
    coord.state = TuyaBLEMeshDeviceState(is_on=False, available=True)
    coord.device = MagicMock()
    coord.device.address = "AA:BB:CC:DD:EE:FF"
    for name in (
        "send_power",
        "send_light_lightness",
        "send_light_ctl",
        "send_light_ctl_temperature",
        "send_light_hsl",
    ):
        setattr(coord.device, name, AsyncMock())

    async def _run(coro_func: object, **_kwargs: object) -> None:
        await coro_func()  # type: ignore[operator]

    coord.send_command_with_retry = AsyncMock(side_effect=_run)
    light = TuyaBLEMeshSIGLight(coord, "entry", None, models)
    return light, coord


_ALL_LIGHT_MODELS = frozenset({0x1000, 0x1300, 0x1303, 0x1307})


class TestSIGLightEntity:
    def test_unique_id_and_default_mode(self) -> None:
        light, _ = _make_light(_ALL_LIGHT_MODELS)
        assert light.unique_id == "AA:BB:CC:DD:EE:FF_light"
        assert light.color_mode == ColorMode.COLOR_TEMP

    @pytest.mark.asyncio
    async def test_plain_turn_on_sends_onoff(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_on()
        coord.device.send_power.assert_awaited_once_with(True)
        coord.assume_state.assert_called_once_with({"is_on": True}, {"is_on": True})

    @pytest.mark.asyncio
    async def test_hs_color_sends_hsl(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_on(hs_color=(120.0, 100.0), brightness=255)
        coord.device.send_light_hsl.assert_awaited_once_with(0x7FFF, 0x5555, 0xFFFF)
        assert light.color_mode == ColorMode.HS
        assert light.hs_color == (120.0, 100.0)

    @pytest.mark.asyncio
    async def test_color_temp_sends_ctl(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_on(color_temp_kelvin=3000, brightness=128)
        coord.device.send_light_lightness.assert_awaited_once_with(sig_lightness_from_ha(128))
        coord.device.send_light_ctl_temperature.assert_awaited_once_with(sig_ctl_temp_from_ha(3000))
        coord.device.send_light_ctl.assert_not_awaited()
        assert light.color_temp_kelvin == 3000
        assert light.brightness == 128

    @pytest.mark.asyncio
    async def test_brightness_in_hs_mode_resends_hsl(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_on(hs_color=(240.0, 100.0))
        coord.device.send_light_hsl.reset_mock()
        await light.async_turn_on(brightness=128)
        coord.device.send_light_hsl.assert_awaited_once()
        coord.device.send_light_lightness.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_brightness_in_ct_mode_sends_lightness(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_on(brightness=64)
        coord.device.send_light_lightness.assert_awaited_once_with(sig_lightness_from_ha(64))

    @pytest.mark.asyncio
    async def test_onoff_only_ignores_brightness(self) -> None:
        light, coord = _make_light(frozenset({0x1000}))
        await light.async_turn_on(brightness=64)
        coord.device.send_power.assert_awaited_once_with(True)
        coord.device.send_light_lightness.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_off(self) -> None:
        light, coord = _make_light(_ALL_LIGHT_MODELS)
        await light.async_turn_off()
        coord.device.send_power.assert_awaited_once_with(False)
        coord.assume_state.assert_called_once_with({"is_on": False}, {"is_on": False})

    @pytest.mark.asyncio
    async def test_send_failure_raises_ha_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        light, coord = _make_light(_ALL_LIGHT_MODELS)
        coord.send_command_with_retry = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(HomeAssistantError):
            await light.async_turn_off()
        coord.assume_state.assert_not_called()


class TestCreateSIGDevice:
    """Regression: the factory passed ble_connect_callback, which SIGMeshDevice rejects."""

    @pytest.mark.parametrize("device_type", [DEVICE_TYPE_SIG_LIGHT, DEVICE_TYPE_SIG_PLUG])
    def test_creates_real_sig_device(self, device_type: str) -> None:
        from custom_components.tuya_ble_mesh.device_factory import create_device

        data = {
            "net_key": "00" * 16,
            "dev_key": "11" * 16,
            "app_key": "22" * 16,
            "unicast_target": "00B0",
            "unicast_our": "0001",
            "iv_index": 0,
        }
        device = create_device(
            device_type,
            "AA:BB:CC:DD:EE:FF",
            data,
            ble_device_callback=MagicMock(),
            ble_connect_callback=MagicMock(),
        )
        assert isinstance(device, SIGMeshDevice)
        assert device.address == "AA:BB:CC:DD:EE:FF"


class TestDescribeLightStatus:
    def test_ctl_status(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import describe_light_status

        params = struct.pack("<HH", 0x8000, 6500)
        assert describe_light_status(0x8260, params) == (
            "Light CTL Status: lightness=32768 temperature=6500K"
        )

    def test_ctl_temp_range_status(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import describe_light_status

        params = struct.pack("<BHH", 0, 800, 20000)
        assert describe_light_status(0x8263, params) == (
            "Light CTL Temperature Range Status: status=0 min=800K max=20000K"
        )

    def test_hsl_and_lightness_status(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import describe_light_status

        assert describe_light_status(0x8278, struct.pack("<HHH", 1, 2, 3)) == (
            "Light HSL Status: lightness=1 hue=2 saturation=3"
        )
        assert describe_light_status(0x824E, struct.pack("<H", 7)) == (
            "Light Lightness Status: lightness=7"
        )

    def test_other_or_short_returns_none(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import describe_light_status

        assert describe_light_status(0x8204, b"\x01") is None
        assert describe_light_status(0x8260, b"\x00") is None

    def test_state_gets_opcodes(self) -> None:
        from tuya_ble_mesh.sig_mesh_protocol import light_state_gets

        assert light_state_gets() == [b"\x82\x4b", b"\x82\x5d", b"\x82\x62", b"\x82\x6d"]


class TestCtlTemperatureElement:
    def test_defaults_to_next_element(self) -> None:
        dev = _make_device()
        dev._composition = None
        assert dev._ctl_temperature_element() == 0x00B1

    def test_uses_element_from_composition(self) -> None:
        dev = _make_device()
        dev._composition = MagicMock()
        dev._composition.elements = (
            MagicMock(sig_models=(0x1000, 0x1303)),
            MagicMock(sig_models=(0x1002,)),
            MagicMock(sig_models=(0x1306,)),
        )
        assert dev._ctl_temperature_element() == 0x00B2

    @pytest.mark.asyncio
    async def test_send_targets_ctl_temperature_element(self) -> None:
        dev = _make_device()
        dev._composition = None
        with patch(
            "tuya_ble_mesh.sig_mesh_device_commands.encrypt_network_pdu",
            return_value=b"\x00" * 20,
        ) as enc:
            await dev.send_light_ctl_temperature(3000)
        assert enc.call_args.kwargs["dst"] == 0x00B1
