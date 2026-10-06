"""Switch entity platform for Tuya BLE Mesh smart plugs, plus a solar-powered setting."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.tuya_ble_mesh.const import (
    CONF_DEVICE_TYPE,
    CONF_SOLAR_POWERED,
    DEVICE_TYPE_LIGHT,
    DEVICE_TYPE_SIG_LIGHT,
    DOMAIN,
    PLUG_DEVICE_TYPES,
)
from custom_components.tuya_ble_mesh.entity import TuyaBLEMeshEntity

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from custom_components.tuya_ble_mesh import TuyaBLEMeshConfigEntry
    from custom_components.tuya_ble_mesh.coordinator import TuyaBLEMeshCoordinator

    AddEntitiesCallback = Callable[..., None]

_LOGGER = logging.getLogger(__name__)

# BLE mesh serializes commands — limit to one concurrent update
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaBLEMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya BLE Mesh switch entities from a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register new entities.
    """
    device_type = entry.data.get(CONF_DEVICE_TYPE)
    runtime_data = entry.runtime_data
    coordinator: TuyaBLEMeshCoordinator = runtime_data.coordinator
    device_info: DeviceInfo = runtime_data.device_info
    entities: list[SwitchEntity] = []
    if device_type in PLUG_DEVICE_TYPES:
        entities.append(TuyaBLEMeshSwitch(coordinator, entry.entry_id, device_info))
    if device_type in _SOLAR_CAPABLE_TYPES:
        entities.append(TuyaBLEMeshSolarSwitch(hass, entry, coordinator, device_info))
    if entities:
        async_add_entities(entities)


# Battery lights reached directly over BLE (not through a bridge)
_SOLAR_CAPABLE_TYPES = {DEVICE_TYPE_LIGHT, DEVICE_TYPE_SIG_LIGHT}


class TuyaBLEMeshSolarSwitch(SwitchEntity):
    """Setting: the device is solar powered and sleeps while it's light out.

    While on, being offline between civil dawn and dusk (from HA's sun) is
    treated as normal: no warnings or repair issues. After dark, a device that
    doesn't come back is reported as usual.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "solar_powered"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_icon = "mdi:solar-power-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TuyaBLEMeshConfigEntry,
        coordinator: TuyaBLEMeshCoordinator,
        device_info: DeviceInfo | None = None,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.device.address}_solar_powered"
        if device_info is not None:
            self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """A setting, so it can be changed while the device is asleep."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True when the device is marked solar powered."""
        return bool(self._coordinator.solar_powered)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mark the device as solar powered."""
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Mark the device as mains or battery powered (always expected online)."""
        self._set(False)

    def _set(self, value: bool) -> None:
        self._coordinator.set_solar_powered(value)
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, CONF_SOLAR_POWERED: value}
        )
        self.async_write_ha_state()


class TuyaBLEMeshSwitch(TuyaBLEMeshEntity, SwitchEntity):
    """Switch entity for a Tuya BLE Mesh smart plug."""

    _attr_should_poll = False
    _attr_device_class = SwitchDeviceClass.OUTLET
    _attr_name = None  # Use device name as entity name
    _attr_unique_id: str

    def __init__(
        self,
        coordinator: TuyaBLEMeshCoordinator,
        entry_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        super().__init__(coordinator, entry_id, device_info)
        self._attr_unique_id = f"{coordinator.device.address}_switch"

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        is_on: bool = self.coordinator.state.is_on
        return is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on.

        Args:
            **kwargs: Additional arguments (unused).
        """
        try:
            await self.coordinator.send_command_with_retry(
                lambda: self.coordinator.device.send_power(True),  # type: ignore[arg-type]
                description="send_power(True)",
            )
        except (OSError, ConnectionError, TimeoutError) as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="switch_on_failed",
            ) from exc

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off.

        Args:
            **kwargs: Additional arguments (unused).
        """
        try:
            await self.coordinator.send_command_with_retry(
                lambda: self.coordinator.device.send_power(False),  # type: ignore[arg-type]
                description="send_power(False)",
            )
        except (OSError, ConnectionError, TimeoutError) as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="switch_off_failed",
            ) from exc
