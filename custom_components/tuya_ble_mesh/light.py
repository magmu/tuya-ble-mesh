"""Light entity platform for Tuya BLE Mesh.

Mappings:
- Brightness: device 1-100 <-> HA 1-255 (linear)
- Color temp: device 0(warm)-127(cool) <-> mireds 370(warm)-153(cool) (inverse)
- Color brightness: device 0-255 <-> HA 0-255 (same scale)
- Supported modes: COLOR_TEMP, RGB
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.tuya_ble_mesh.const import (
    CONF_DEVICE_TYPE,
    CONF_SIG_MODELS,
    DEVICE_BRIGHTNESS_MAX,
    DEVICE_BRIGHTNESS_MIN,
    DEVICE_COLOR_TEMP_MAX,
    DEVICE_COLOR_TEMP_MIN,
    DEVICE_TYPE_SIG_LIGHT,
    DOMAIN,
    HA_BRIGHTNESS_MAX,
    HA_BRIGHTNESS_MIN,
    HA_MIRED_MAX,
    HA_MIRED_MIN,
    MESH_SCENES,
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


def _log_task_exc(task: asyncio.Task[None]) -> None:
    """Log exception from a background light task so it is not silently swallowed."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        _LOGGER.warning("Light background task failed: %s", exc)


# BLE mesh serializes commands — limit to one concurrent update
PARALLEL_UPDATES = 1

# Reverse lookup: scene name → scene ID (inverse of MESH_SCENES)
_SCENES_BY_NAME: dict[str, int] = {v: k for k, v in MESH_SCENES.items()}

# Debounce window for coalescing rapid slider commands (e.g. brightness drag)
_COMMAND_DEBOUNCE_INTERVAL = 0.05  # 50 ms


def brightness_to_ha(device_value: int) -> int:
    """Convert device brightness (1-100) to HA brightness (1-255).

    Args:
        device_value: Device brightness value.

    Returns:
        HA brightness value.
    """
    clamped = max(DEVICE_BRIGHTNESS_MIN, min(device_value, DEVICE_BRIGHTNESS_MAX))
    return round(
        HA_BRIGHTNESS_MIN
        + (clamped - DEVICE_BRIGHTNESS_MIN)
        * (HA_BRIGHTNESS_MAX - HA_BRIGHTNESS_MIN)
        / (DEVICE_BRIGHTNESS_MAX - DEVICE_BRIGHTNESS_MIN)
    )


def brightness_to_device(ha_value: int) -> int:
    """Convert HA brightness (1-255) to device brightness (1-100).

    Args:
        ha_value: HA brightness value.

    Returns:
        Device brightness value.
    """
    clamped = max(HA_BRIGHTNESS_MIN, min(ha_value, HA_BRIGHTNESS_MAX))
    return round(
        DEVICE_BRIGHTNESS_MIN
        + (clamped - HA_BRIGHTNESS_MIN)
        * (DEVICE_BRIGHTNESS_MAX - DEVICE_BRIGHTNESS_MIN)
        / (HA_BRIGHTNESS_MAX - HA_BRIGHTNESS_MIN)
    )


def color_temp_to_ha(device_value: int) -> int:
    """Convert device color temp (0=warm, 127=cool) to mireds (370=warm, 153=cool).

    Inverse mapping: higher device value = cooler = lower mireds.

    Args:
        device_value: Device color temp value.

    Returns:
        HA color temp in mireds.
    """
    clamped = max(DEVICE_COLOR_TEMP_MIN, min(device_value, DEVICE_COLOR_TEMP_MAX))
    return round(
        HA_MIRED_MAX
        - (clamped - DEVICE_COLOR_TEMP_MIN)
        * (HA_MIRED_MAX - HA_MIRED_MIN)
        / (DEVICE_COLOR_TEMP_MAX - DEVICE_COLOR_TEMP_MIN)
    )


def color_temp_to_device(mired_value: int) -> int:
    """Convert mireds (370=warm, 153=cool) to device color temp (0=warm, 127=cool).

    Inverse mapping: lower mireds = cooler = higher device value.

    Args:
        mired_value: HA color temp in mireds.

    Returns:
        Device color temp value.
    """
    clamped = max(HA_MIRED_MIN, min(mired_value, HA_MIRED_MAX))
    return round(
        DEVICE_COLOR_TEMP_MAX
        - (clamped - HA_MIRED_MIN)
        * (DEVICE_COLOR_TEMP_MAX - DEVICE_COLOR_TEMP_MIN)
        / (HA_MIRED_MAX - HA_MIRED_MIN)
    )


@dataclass(frozen=True)
class _TurnOnCommand:
    """Structured turn-on command built from HA service call parameters."""

    power_on: bool
    brightness: int | None
    color_temp: int | None
    rgb: tuple[int, int, int] | None
    use_color_brightness: bool


def _build_turn_on_command(
    brightness: int | None,
    color_temp: int | None,
    rgb_color: tuple[int, int, int] | None,
    has_target: bool,
    current_mode: int,
) -> _TurnOnCommand:
    """Build a structured turn-on command from HA service call arguments.

    Args:
        brightness: HA brightness (1-255), or None.
        color_temp: Color temperature in mireds, or None.
        rgb_color: RGB tuple (0-255 each), or None.
        has_target: True if any brightness/CT/RGB target was supplied.
        current_mode: Current device mode (0=CT/white, 1=RGB/color).

    Returns:
        _TurnOnCommand with resolved fields.
    """
    if not has_target:
        return _TurnOnCommand(
            power_on=True,
            brightness=None,
            color_temp=None,
            rgb=None,
            use_color_brightness=False,
        )

    # Determine color scale vs white scale
    # CT request → white scale (use_color_brightness=False)
    # RGB request → color scale (use_color_brightness=True)
    # Brightness only → follow current mode
    if color_temp is not None:
        use_color = False
    elif rgb_color is not None:
        use_color = True
    else:
        use_color = current_mode == 1

    dev_brightness: int | None = None
    if brightness is not None:
        dev_brightness = brightness if use_color else brightness_to_device(brightness)

    dev_color_temp: int | None = None
    if color_temp is not None:
        dev_color_temp = color_temp_to_device(color_temp)

    return _TurnOnCommand(
        power_on=False,
        brightness=dev_brightness,
        color_temp=dev_color_temp,
        rgb=rgb_color,
        use_color_brightness=use_color,
    )


def color_brightness_to_ha(device_value: int) -> int:
    """Convert device color brightness (0-255) to HA color brightness (0-255).

    Color mode uses the same 0-255 scale on both sides.

    Args:
        device_value: Device color brightness value.

    Returns:
        HA color brightness value.
    """
    return max(0, min(device_value, 255))


def color_brightness_to_device(ha_value: int) -> int:
    """Convert HA color brightness (0-255) to device color brightness (0-255).

    Color mode uses the same 0-255 scale on both sides.

    Args:
        ha_value: HA color brightness value.

    Returns:
        Device color brightness value.
    """
    return max(0, min(ha_value, 255))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaBLEMeshConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya BLE Mesh light entities from a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry being set up.
        async_add_entities: Callback to register new entities.
    """
    if entry.data.get(CONF_DEVICE_TYPE) in PLUG_DEVICE_TYPES:
        return
    runtime_data = entry.runtime_data
    coordinator: TuyaBLEMeshCoordinator = runtime_data.coordinator
    device_info: DeviceInfo = runtime_data.device_info
    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_SIG_LIGHT:
        sig_models = frozenset(entry.data.get(CONF_SIG_MODELS, []))
        async_add_entities(
            [TuyaBLEMeshSIGLight(coordinator, entry.entry_id, device_info, sig_models)]
        )
        return
    async_add_entities([TuyaBLEMeshLight(coordinator, entry.entry_id, device_info)])


class TuyaBLEMeshLight(TuyaBLEMeshEntity, LightEntity):
    """Light entity for a Tuya BLE Mesh device."""

    _attr_should_poll = False
    _attr_supported_features = LightEntityFeature.TRANSITION | LightEntityFeature.EFFECT
    _attr_name = None  # Use device name as entity name
    _attr_unique_id: str

    def __init__(
        self,
        coordinator: TuyaBLEMeshCoordinator,
        entry_id: str,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the light entity.

        Args:
            coordinator: Coordinator managing the BLE mesh device state.
            entry_id: Config entry ID used to scope the unique entity ID.
            device_info: Device registry info for grouping entities under a device.
        """
        super().__init__(coordinator, entry_id, device_info)
        self._attr_unique_id = f"{coordinator.device.address}_light"
        self._transition_task: asyncio.Task[None] | None = None
        self._pending_command_task: asyncio.Task[None] | None = None
        # PLAT-756: Semaphore to serialize light transitions and prevent race conditions
        self._transition_lock = asyncio.Lock()

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        return self.coordinator.state.is_on

    @property
    def brightness(self) -> int | None:
        """Return the current brightness (HA 1-255)."""
        if not self.coordinator.state.is_on:
            return None
        if self.coordinator.state.mode == 1:
            return self.coordinator.state.color_brightness
        return brightness_to_ha(self.coordinator.state.brightness)

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the current color temperature in kelvin."""
        if not self.coordinator.state.is_on:
            return None
        mired = color_temp_to_ha(self.coordinator.state.color_temp)
        return round(1_000_000 / mired)

    _attr_min_color_temp_kelvin = 2703  # warmest (370 mireds)
    _attr_max_color_temp_kelvin = 6535  # coolest (153 mireds)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the current RGB color."""
        if not self.coordinator.state.is_on:
            return None
        if self.coordinator.state.mode != 1:
            return None
        state = self.coordinator.state
        return (state.red, state.green, state.blue)

    @property
    def color_mode(self) -> ColorMode:
        """Return the current color mode."""
        if self.coordinator.state.mode == 1:
            return ColorMode.RGB
        return ColorMode.COLOR_TEMP

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Return supported color modes."""
        return {ColorMode.COLOR_TEMP, ColorMode.RGB}

    @property
    def effect(self) -> str | None:
        """Return the active scene/effect name, or None if no scene is active."""
        scene_id = self.coordinator.state.scene_id
        if scene_id == 0:
            return None
        return MESH_SCENES.get(scene_id)

    @property
    def supported_effects(self) -> list[str]:
        """Return list of supported scene/effect names."""
        return list(MESH_SCENES.values())

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return extra state attributes for the light entity.

        Returns:
            Dict with brightness_mode ('rgb' or 'white') and device_brightness
            (raw device brightness value: 0-255 for RGB, 1-100 for white mode).
        """
        state = self._coordinator.state
        if state.mode == 1:
            return {
                "brightness_mode": "rgb",
                "device_brightness": state.color_brightness,
            }
        return {
            "brightness_mode": "white",
            "device_brightness": state.brightness,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light.

        Immediate (non-transition) commands are debounced over a short window
        to coalesce rapid slider moves (e.g. brightness drag) into a single
        BLE command, reducing mesh traffic.

        Args:
            **kwargs: Optional brightness, color_temp, rgb_color, and transition.
        """
        self._cancel_transition()
        self._cancel_pending_command()

        # Handle scene/effect activation immediately (no debounce needed)
        effect: str | None = kwargs.get("effect")
        if effect is not None:
            scene_id = _SCENES_BY_NAME.get(effect)
            if scene_id is not None:
                await self.coordinator.device.send_scene(scene_id)
                self.coordinator.set_scene_id(scene_id)
            return

        transition: float | None = kwargs.get(ATTR_TRANSITION)
        brightness = kwargs.get("brightness")
        color_temp_kelvin: int | None = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        color_temp = round(1_000_000 / color_temp_kelvin) if color_temp_kelvin else None
        rgb_color: tuple[int, int, int] | None = kwargs.get(ATTR_RGB_COLOR)
        has_target = brightness is not None or color_temp is not None or rgb_color is not None

        if transition is not None and transition > 0 and has_target:
            target_bright = brightness_to_device(brightness) if brightness is not None else None
            target_temp = color_temp_to_device(color_temp) if color_temp is not None else None
            self._transition_task = asyncio.create_task(
                self._run_transition(target_bright, target_temp, transition, target_rgb=rgb_color)
            )
            self._transition_task.add_done_callback(_log_task_exc)
            return

        # Debounce: schedule command after short window so rapid slider
        # moves cancel the previous pending command and only the latest fires.
        self._pending_command_task = asyncio.create_task(
            self._debounced_send_turn_on(brightness, color_temp, rgb_color, has_target)
        )
        self._pending_command_task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )

    async def _debounced_send_turn_on(
        self,
        brightness: int | None,
        color_temp: int | None,
        rgb_color: tuple[int, int, int] | None,
        has_target: bool,
    ) -> None:
        """Send turn-on command after debounce interval.

        PLAT-756: Uses transition_lock to serialize commands and prevent race conditions.

        Called from a task; cancelled if a newer command arrives within
        _COMMAND_DEBOUNCE_INTERVAL.

        Args:
            brightness: HA brightness value (1-255), or None.
            color_temp: Color temp in mireds, or None.
            rgb_color: RGB tuple, or None.
            has_target: True if any parameter was specified.
        """
        await asyncio.sleep(_COMMAND_DEBOUNCE_INTERVAL)
        self._pending_command_task = None

        async with self._transition_lock:
            device = self.coordinator.device

            if rgb_color is not None:
                await device.send_color(rgb_color[0], rgb_color[1], rgb_color[2])
                await device.send_light_mode(1)
                _LOGGER.debug("Set RGB color: (%d,%d,%d)", *rgb_color)
                if brightness is not None:
                    await device.send_color_brightness(brightness)
                    _LOGGER.debug("Set color brightness: %d", brightness)
                return

            if color_temp is not None:
                if self.coordinator.state.mode == 1:
                    await device.send_light_mode(0)
                device_temp = color_temp_to_device(color_temp)
                await device.send_color_temp(device_temp)
                _LOGGER.debug("Set color temp: HA %d mireds -> device %d", color_temp, device_temp)

            if brightness is not None:
                if self.coordinator.state.mode == 1:
                    await device.send_color_brightness(brightness)
                    _LOGGER.debug("Set color brightness: %d", brightness)
                else:
                    device_brightness = brightness_to_device(brightness)
                    await device.send_brightness(device_brightness)
                    _LOGGER.debug(
                        "Set brightness: HA %d -> device %d", brightness, device_brightness
                    )

            if not has_target:
                await device.send_power(True)

    def _cancel_pending_command(self) -> None:
        """Cancel any pending debounced command task."""
        if self._pending_command_task is not None and not self._pending_command_task.done():
            self._pending_command_task.cancel()
        self._pending_command_task = None

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light.

        Args:
            **kwargs: Optional transition.
        """
        self._cancel_transition()
        self._cancel_pending_command()
        transition: float | None = kwargs.get(ATTR_TRANSITION)

        if transition is not None and transition > 0:
            self._transition_task = asyncio.create_task(
                self._run_transition(
                    target_brightness=DEVICE_BRIGHTNESS_MIN,
                    target_color_temp=None,
                    duration=transition,
                    power_off_after=True,
                )
            )
            self._transition_task.add_done_callback(_log_task_exc)
            return

        await self.coordinator.device.send_power(False)

    def _cancel_transition(self) -> None:
        """Cancel any in-progress transition task."""
        if self._transition_task is not None and not self._transition_task.done():
            self._transition_task.cancel()
        self._transition_task = None

    async def _apply_transition_step(
        self,
        fraction: float,
        target_brightness: int | None,
        start_bright: int | None,
        target_color_temp: int | None,
        start_temp: int | None,
        target_rgb: tuple[int, int, int] | None,
        start_rgb: tuple[int, int, int] | None,
    ) -> None:
        """Apply a single transition step with interpolated values.

        Args:
            fraction: Progress fraction (0.0 to 1.0).
            target_brightness: Target device brightness, or None.
            start_bright: Starting brightness, or None.
            target_color_temp: Target color temperature, or None.
            start_temp: Starting color temperature, or None.
            target_rgb: Target RGB tuple, or None.
            start_rgb: Starting RGB tuple, or None.
        """
        device = self.coordinator.device

        if target_brightness is not None and start_bright is not None:
            val = round(start_bright + (target_brightness - start_bright) * fraction)
            val = max(DEVICE_BRIGHTNESS_MIN, min(val, DEVICE_BRIGHTNESS_MAX))
            await device.send_brightness(val)

        if target_color_temp is not None and start_temp is not None:
            val = round(start_temp + (target_color_temp - start_temp) * fraction)
            val = max(DEVICE_COLOR_TEMP_MIN, min(val, DEVICE_COLOR_TEMP_MAX))
            await device.send_color_temp(val)

        if target_rgb is not None and start_rgb is not None:
            r = round(start_rgb[0] + (target_rgb[0] - start_rgb[0]) * fraction)
            g = round(start_rgb[1] + (target_rgb[1] - start_rgb[1]) * fraction)
            b = round(start_rgb[2] + (target_rgb[2] - start_rgb[2]) * fraction)
            await device.send_color(
                max(0, min(r, 255)),
                max(0, min(g, 255)),
                max(0, min(b, 255)),
            )

    async def _run_transition(
        self,
        target_brightness: int | None,
        target_color_temp: int | None,
        duration: float,
        *,
        power_off_after: bool = False,
        target_rgb: tuple[int, int, int] | None = None,
    ) -> None:
        """Run a gradual transition by sending incremental commands.

        PLAT-756: Uses transition_lock to serialize transitions and prevent race conditions
        when multiple transition commands are issued in quick succession.

        Args:
            target_brightness: Target device brightness (1-100), or None.
            target_color_temp: Target device color temp (0-127), or None.
            duration: Transition duration in seconds.
            power_off_after: Send power off after transition completes.
            target_rgb: Target RGB color tuple, or None.
        """
        async with self._transition_lock:
            device = self.coordinator.device
            state = self.coordinator.state

            steps = min(int(duration * 10), 50)
            if steps < 2:
                steps = 2
            interval = duration / steps

            start_bright = state.brightness if target_brightness is not None else None
            start_temp = state.color_temp if target_color_temp is not None else None
            start_rgb: tuple[int, int, int] | None = None
            if target_rgb is not None:
                start_rgb = (state.red, state.green, state.blue)

            for i in range(1, steps + 1):
                fraction = i / steps
                await self._apply_transition_step(
                    fraction,
                    target_brightness,
                    start_bright,
                    target_color_temp,
                    start_temp,
                    target_rgb,
                    start_rgb,
                )

                if i < steps:
                    await asyncio.sleep(interval)

            if power_off_after:
                await device.send_power(False)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel in-progress transitions and pending commands when removed from HA."""
        self._cancel_transition()
        self._cancel_pending_command()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Called automatically by CoordinatorEntity when coordinator dispatches updates.
        """
        self.async_write_ha_state()


# --- SIG Mesh lights (standard Light Lightness / CTL / HSL models) ---

_SIG_MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
_SIG_MODEL_LIGHT_CTL_SERVER = 0x1303
_SIG_MODEL_LIGHT_HSL_SERVER = 0x1307
_SIG_U16_MAX = 0xFFFF
_SIG_CTL_KELVIN_MIN = 800
_SIG_CTL_KELVIN_MAX = 20000
_SIG_HA_KELVIN_MIN = 2700
_SIG_HA_KELVIN_MAX = 6500
_SIG_DEFAULT_KELVIN = 4000


def sig_lightness_from_ha(brightness: int) -> int:
    """Convert HA brightness (0-255) to SIG Light Lightness (0-65535)."""
    clamped = max(0, min(brightness, HA_BRIGHTNESS_MAX))
    return round(clamped * _SIG_U16_MAX / HA_BRIGHTNESS_MAX)


def sig_ctl_temp_from_ha(kelvin: int) -> int:
    """Map HA's kelvin slider onto the node's full CTL temperature range.

    Telink SIG firmware spreads warm-to-cool white across the whole 800-20000 K
    CTL range, so sending HA's 2700-6500 K directly only reaches the warm end.
    """
    span = _SIG_HA_KELVIN_MAX - _SIG_HA_KELVIN_MIN
    ratio = (max(_SIG_HA_KELVIN_MIN, min(kelvin, _SIG_HA_KELVIN_MAX)) - _SIG_HA_KELVIN_MIN) / span
    return round(_SIG_CTL_KELVIN_MIN + ratio * (_SIG_CTL_KELVIN_MAX - _SIG_CTL_KELVIN_MIN))


def sig_hsl_from_ha(hs_color: tuple[float, float], brightness: int) -> tuple[int, int, int]:
    """Convert HA hue/saturation and brightness to SIG HSL (lightness, hue, saturation).

    HSL lightness of a fully saturated colour is 50 %, so HA brightness maps to
    0-50 % lightness. Going higher would wash the colour out towards white.
    """
    hue = round((hs_color[0] % 360) * _SIG_U16_MAX / 360)
    saturation = round(max(0.0, min(hs_color[1], 100.0)) * _SIG_U16_MAX / 100)
    lightness = sig_lightness_from_ha(brightness) // 2
    return lightness, hue, saturation


def sig_color_modes(sig_models: frozenset[int]) -> set[ColorMode]:
    """Pick HA colour modes from the SIG models the node reported."""
    modes: set[ColorMode] = set()
    if _SIG_MODEL_LIGHT_HSL_SERVER in sig_models:
        modes.add(ColorMode.HS)
    if _SIG_MODEL_LIGHT_CTL_SERVER in sig_models:
        modes.add(ColorMode.COLOR_TEMP)
    if not modes:
        if _SIG_MODEL_LIGHT_LIGHTNESS_SERVER in sig_models:
            modes.add(ColorMode.BRIGHTNESS)
        else:
            modes.add(ColorMode.ONOFF)
    return modes


class TuyaBLEMeshSIGLight(TuyaBLEMeshEntity, LightEntity):
    """Light entity for a SIG Mesh node provisioned directly over BLE.

    The node only reports on/off back to us, so brightness and colour are
    kept as the last values Home Assistant sent.
    """

    _attr_should_poll = False
    _attr_name = None  # Use device name as entity name
    _attr_unique_id: str
    _attr_min_color_temp_kelvin = _SIG_HA_KELVIN_MIN
    _attr_max_color_temp_kelvin = _SIG_HA_KELVIN_MAX

    def __init__(
        self,
        coordinator: TuyaBLEMeshCoordinator,
        entry_id: str,
        device_info: DeviceInfo | None,
        sig_models: frozenset[int],
    ) -> None:
        """Initialize the SIG Mesh light entity.

        Args:
            coordinator: Coordinator managing the BLE mesh device state.
            entry_id: Config entry ID used to scope the unique entity ID.
            device_info: Device registry info for grouping entities under a device.
            sig_models: SIG model IDs reported by the node at provisioning.
        """
        super().__init__(coordinator, entry_id, device_info)
        self._attr_unique_id = f"{coordinator.device.address}_light"
        self._attr_supported_color_modes = sig_color_modes(sig_models)
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        else:
            self._attr_color_mode = next(iter(self._attr_supported_color_modes))
        self._attr_brightness = HA_BRIGHTNESS_MAX
        self._attr_color_temp_kelvin = _SIG_DEFAULT_KELVIN
        self._attr_hs_color = (0.0, 0.0)
        self._command_lock = asyncio.Lock()

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        return bool(self.coordinator.state.is_on)

    async def _send(self, coro_func: Callable[[], Any], description: str) -> None:
        try:
            await self.coordinator.send_command_with_retry(coro_func, description=description)
        except (OSError, ConnectionError, TimeoutError) as exc:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="light_command_failed",
            ) from exc

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light, applying brightness, colour temperature or colour."""
        device: Any = self.coordinator.device
        modes = self._attr_supported_color_modes or set()
        brightness: int = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or HA_BRIGHTNESS_MAX)

        async with self._command_lock:
            if ATTR_HS_COLOR in kwargs and ColorMode.HS in modes:
                hs_color: tuple[float, float] = kwargs[ATTR_HS_COLOR]
                lightness, hue, saturation = sig_hsl_from_ha(hs_color, brightness)
                await self._send(
                    lambda: device.send_light_hsl(lightness, hue, saturation), "send_light_hsl"
                )
                self._attr_hs_color = hs_color
                self._attr_color_mode = ColorMode.HS
            elif ATTR_COLOR_TEMP_KELVIN in kwargs and ColorMode.COLOR_TEMP in modes:
                kelvin = max(
                    _SIG_HA_KELVIN_MIN, min(kwargs[ATTR_COLOR_TEMP_KELVIN], _SIG_HA_KELVIN_MAX)
                )
                lightness = sig_lightness_from_ha(brightness)
                ctl_temp = sig_ctl_temp_from_ha(kelvin)
                await self._send(
                    lambda: device.send_light_ctl(lightness, ctl_temp), "send_light_ctl"
                )
                self._attr_color_temp_kelvin = kelvin
                self._attr_color_mode = ColorMode.COLOR_TEMP
            elif ATTR_BRIGHTNESS in kwargs and ColorMode.ONOFF not in modes:
                if self._attr_color_mode == ColorMode.HS:
                    lightness, hue, saturation = sig_hsl_from_ha(
                        self._attr_hs_color or (0.0, 0.0), brightness
                    )
                    await self._send(
                        lambda: device.send_light_hsl(lightness, hue, saturation),
                        "send_light_hsl",
                    )
                else:
                    lightness = sig_lightness_from_ha(brightness)
                    await self._send(
                        lambda: device.send_light_lightness(lightness), "send_light_lightness"
                    )
            else:
                await self._send(lambda: device.send_power(True), "send_power(True)")

        self._attr_brightness = brightness
        self.coordinator.assume_state({"is_on": True}, {"is_on": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        device: Any = self.coordinator.device
        async with self._command_lock:
            await self._send(lambda: device.send_power(False), "send_power(False)")
        self.coordinator.assume_state({"is_on": False}, {"is_on": False})

    def _handle_coordinator_update(self) -> None:
        """Write state when the coordinator reports a change."""
        self.async_write_ha_state()
