"""Reconnect behaviour for devices that stop advertising (e.g. solar lights by day)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _ROOT)
sys.path.insert(0, str(Path(_ROOT) / "custom_components" / "tuya_ble_mesh" / "lib"))

from custom_components.tuya_ble_mesh import connection_manager as cm  # noqa: E402
from custom_components.tuya_ble_mesh.connection_manager import ConnectionManager  # noqa: E402

_real_sleep = asyncio.sleep


async def _fast_sleep(_delay: float) -> None:
    await _real_sleep(0)


def _make_manager(present: list[bool]) -> tuple[ConnectionManager, MagicMock]:
    device = MagicMock()
    device.address = "DC:23:52:ED:52:9A"
    device.connect = AsyncMock()
    device.rssi = None
    type(device).__name__ = "SIGMeshDevice"
    mgr = ConnectionManager(device, hass=MagicMock(), entry_id="entry", on_connected=MagicMock())
    mgr.running = True
    mgr._device_advertising = lambda: present[0]  # type: ignore[method-assign]
    mgr._maybe_create_repair_issue = MagicMock()  # type: ignore[method-assign]
    mgr._clear_repair_issues_on_recovery = MagicMock()  # type: ignore[method-assign]
    return mgr, device


async def _settle() -> None:
    for _ in range(20):
        await _real_sleep(0)


@pytest.mark.asyncio
async def test_absent_device_waits_without_connecting() -> None:
    present = [False]
    mgr, device = _make_manager(present)
    with patch.object(cm.asyncio, "sleep", _fast_sleep):
        mgr.schedule_reconnect()
        await _settle()

        assert mgr.waiting_for_advertisement is True
        device.connect.assert_not_called()
        assert mgr.consecutive_failures == 0
        mgr._maybe_create_repair_issue.assert_not_called()

        # Device wakes up and advertises
        present[0] = True
        mgr.notify_advertisement()
        await _settle()

        device.connect.assert_awaited_once()
        assert mgr.waiting_for_advertisement is False
        assert mgr._reconnect_task is not None
        await mgr._reconnect_task


@pytest.mark.asyncio
async def test_advertisement_starts_reconnect_when_idle() -> None:
    mgr, _device = _make_manager([True])
    with patch.object(mgr, "schedule_reconnect") as schedule:
        mgr.notify_advertisement()
    schedule.assert_called_once()


@pytest.mark.asyncio
async def test_advertisement_does_not_restart_backoff() -> None:
    mgr, _device = _make_manager([True])
    mgr._reconnect_task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
    with patch.object(mgr, "schedule_reconnect") as schedule:
        mgr.notify_advertisement()
    schedule.assert_not_called()
    mgr._reconnect_task.cancel()  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_repair_only_after_repeated_failures() -> None:
    mgr, device = _make_manager([True])
    device.connect = AsyncMock(side_effect=TimeoutError("no response"))
    with patch.object(cm.asyncio, "sleep", _fast_sleep):
        mgr.schedule_reconnect()
        while mgr.consecutive_failures < cm.REPAIR_AFTER_FAILURES - 1:
            await _real_sleep(0)
        mgr._maybe_create_repair_issue.assert_not_called()
        while mgr.consecutive_failures < cm.REPAIR_AFTER_FAILURES:
            await _real_sleep(0)
        await _settle()
        mgr._maybe_create_repair_issue.assert_called()
        mgr.running = False
        assert mgr._reconnect_task is not None
        mgr._reconnect_task.cancel()


@pytest.mark.asyncio
async def test_coordinator_start_waiting_skips_connect() -> None:
    from custom_components.tuya_ble_mesh.coordinator import TuyaBLEMeshCoordinator

    device = MagicMock()
    device.address = "DC:23:52:ED:52:9A"
    device.connect = AsyncMock()
    coord = TuyaBLEMeshCoordinator(device)
    with (
        patch.object(coord, "_load_seq", AsyncMock()),
        patch.object(coord._conn_mgr, "schedule_reconnect") as schedule,
    ):
        await coord.async_start_waiting()

    device.connect.assert_not_called()
    schedule.assert_called_once()
    assert coord.state.available is False
    assert coord._conn_mgr.running is True


async def _run_absent_checks(mgr: ConnectionManager, checks: int) -> None:
    with (
        patch.object(cm.asyncio, "sleep", _fast_sleep),
        patch.object(cm, "ABSENT_RECHECK_SECONDS", 0.0),
    ):
        mgr.schedule_reconnect()
        for _ in range(checks * 10):
            await _real_sleep(0)
        mgr.running = False
        assert mgr._reconnect_task is not None
        mgr._reconnect_task.cancel()


@pytest.mark.asyncio
async def test_absent_by_day_never_alerts() -> None:
    mgr, _device = _make_manager([False])
    mgr.expected_offline = lambda: True
    await _run_absent_checks(mgr, 5)
    mgr._maybe_create_repair_issue.assert_not_called()


@pytest.mark.asyncio
async def test_absent_after_dark_alerts_on_recheck() -> None:
    mgr, _device = _make_manager([False])
    mgr.expected_offline = lambda: False
    await _run_absent_checks(mgr, 5)
    mgr._maybe_create_repair_issue.assert_called_with(cm.ErrorClass.DEVICE_OFFLINE)


@pytest.mark.asyncio
async def test_failures_by_day_never_alert() -> None:
    mgr, device = _make_manager([True])
    mgr.expected_offline = lambda: True
    device.connect = AsyncMock(side_effect=TimeoutError("asleep"))
    with patch.object(cm.asyncio, "sleep", _fast_sleep):
        mgr.schedule_reconnect()
        while mgr.consecutive_failures < cm.REPAIR_AFTER_FAILURES + 2:
            await _real_sleep(0)
        mgr.running = False
        assert mgr._reconnect_task is not None
        mgr._reconnect_task.cancel()
    mgr._maybe_create_repair_issue.assert_not_called()


def test_daylight_uses_sun_elevation() -> None:
    from custom_components.tuya_ble_mesh.coordinator import _is_daylight

    hass = MagicMock()
    hass.states.get.return_value.attributes = {"elevation": -3.0}
    assert _is_daylight(hass) is True
    hass.states.get.return_value.attributes = {"elevation": -10.0}
    assert _is_daylight(hass) is False


def test_coordinator_expected_offline_only_when_solar() -> None:
    from custom_components.tuya_ble_mesh.coordinator import TuyaBLEMeshCoordinator

    device = MagicMock()
    device.address = "DC:23:52:ED:52:9A"
    entry = MagicMock()
    entry.options = {"solar_powered": True}
    hass = MagicMock()
    hass.states.get.return_value.attributes = {"elevation": 30.0}
    coord = TuyaBLEMeshCoordinator(device, hass=hass, entry=entry)
    assert coord.solar_powered is True
    assert coord._conn_mgr._offline_expected() is True
    coord.solar_powered = False
    assert coord._conn_mgr._offline_expected() is False


@pytest.mark.asyncio
async def test_solar_toggle_does_not_reload_entry() -> None:
    from custom_components.tuya_ble_mesh import _async_update_listener

    hass = MagicMock()
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.options = {"solar_powered": True}
    entry.runtime_data.reload_options = {}
    await _async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_not_called()

    entry.options = {"solar_powered": True, "iv_index": 2}
    await _async_update_listener(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once()


@pytest.mark.asyncio
async def test_turning_solar_on_clears_repairs_while_offline() -> None:
    mgr, _device = _make_manager([False])
    expected = [False]
    mgr.expected_offline = lambda: expected[0]
    with (
        patch.object(cm.asyncio, "sleep", _fast_sleep),
        patch.object(cm, "ABSENT_RECHECK_SECONDS", 60.0),
    ):
        mgr.schedule_reconnect()
        await _settle()
        assert mgr.waiting_for_advertisement is True
        mgr._clear_repair_issues_on_recovery.reset_mock()

        expected[0] = True
        mgr.offline_expectation_changed()
        mgr._clear_repair_issues_on_recovery.assert_called()

        mgr.running = False
        assert mgr._reconnect_task is not None
        mgr._reconnect_task.cancel()


def test_expectation_change_ignored_when_connected() -> None:
    mgr, _device = _make_manager([True])
    mgr.expected_offline = lambda: True
    mgr.offline_expectation_changed()
    mgr._clear_repair_issues_on_recovery.assert_not_called()
