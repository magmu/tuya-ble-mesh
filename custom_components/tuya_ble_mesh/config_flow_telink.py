"""Telink-specific config flow handlers.

Handles:
- Telink BLE mesh pairing (PAIR_REQUEST → PAIR_SUCCESS)
- Telink bridge configuration
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol

if TYPE_CHECKING:
    from bleak import BleakClient
    from homeassistant.data_entry_flow import FlowResult

from custom_components.tuya_ble_mesh.config_flow_validators import (
    _test_bridge_with_session,
    _validate_bridge_host,
)
from custom_components.tuya_ble_mesh.const import (
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    DEFAULT_BRIDGE_PORT,
    DEVICE_TYPE_TELINK_BRIDGE_LIGHT,
)

_LOGGER = logging.getLogger(__name__)


def mac_to_bytes(mac: str) -> bytes:
    """Convert MAC address string to bytes.

    Args:
        mac: MAC address (e.g. "AA:BB:CC:DD:EE:FF").

    Returns:
        6-byte MAC address.
    """
    return bytes(int(x, 16) for x in mac.split(":"))


async def perform_telink_pairing(
    client: BleakClient,
    mac: str,
    mesh_name: str,
    mesh_password: str,
    detected_type: str,
) -> dict[str, Any]:
    """Perform Telink mesh pairing.

    Args:
        client: Connected BLE client.
        mac: Device MAC address.
        mesh_name: Telink mesh network name.
        mesh_password: Telink mesh password.
        detected_type: Device type (DEVICE_TYPE_LIGHT or DEVICE_TYPE_PLUG).

    Returns:
        Extra data dict (empty for Telink devices, keys added later).

    Raises:
        ValueError: If pairing fails.
    """
    # Verify Telink GATT service
    services = client.services
    service_uuids = [str(s.uuid).lower() for s in services]
    has_telink = any(uuid.startswith("00010203-0405-0607-0809-0a0b0c0d") for uuid in service_uuids)
    if not has_telink:
        _LOGGER.warning("%s claims to be Telink but lacks Telink GATT service", mac)
        raise ValueError("device_type_mismatch")

    # Import provisioner for Telink pairing
    from tuya_ble_mesh.provisioner import pair

    _LOGGER.info("Starting Telink mesh pairing for %s", mac)
    try:
        session_key, _ = await pair(
            client, mesh_name.encode("utf-8"), mesh_password.encode("utf-8")
        )
        _LOGGER.info(
            "Telink pairing succeeded for %s (session_key=%d bytes)", mac, len(session_key)
        )
    except Exception as exc:
        _LOGGER.warning("Telink pairing failed for %s: %s", mac, exc, exc_info=True)
        # Map provisioning errors to user-friendly keys
        from tuya_ble_mesh.exceptions import ProvisioningError

        if isinstance(exc, ProvisioningError):
            raise ValueError("pairing_failed") from None
        raise ValueError("pairing_failed") from exc

    # No separate verify step: a successful pair handshake already proves the
    # mesh name and password. start_notify on char 1911 blocks until the BlueZ
    # timeout on Telink devices (the runtime path handles that in
    # _start_notify_safe), so a notify-based check here hung the flow.
    return {}


async def async_step_telink_bridge(
    flow: Any, user_input: dict[str, Any] | None = None
) -> FlowResult:
    """Handle Telink Bridge light configuration.

    Args:
        flow: ConfigFlow instance.
        user_input: User-provided bridge parameters.

    Returns:
        Flow result dict.
    """
    errors: dict[str, str] = {}
    if user_input is not None:
        host = user_input.get(CONF_BRIDGE_HOST, "")
        port = user_input.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
        host_error = _validate_bridge_host(host)
        if host_error:
            errors[CONF_BRIDGE_HOST] = host_error
        elif not await _test_bridge_with_session(flow.hass, host, port):
            errors["base"] = "cannot_connect"
        else:
            mac = flow._discovery_info["address"]
            await flow.async_set_unique_id(mac)
            flow._abort_if_unique_id_configured()
            return flow._finalize_entry(
                mac=mac,
                device_type=DEVICE_TYPE_TELINK_BRIDGE_LIGHT,
                bridge_host=host,
                bridge_port=port,
            )

    return flow.async_show_form(
        step_id="telink_bridge",
        data_schema=vol.Schema(
            {
                vol.Required(CONF_BRIDGE_HOST): str,
                vol.Optional(CONF_BRIDGE_PORT, default=DEFAULT_BRIDGE_PORT): int,
            }
        ),
        description_placeholders={
            "name": (flow._discovery_info.get("name", "") if flow._discovery_info else ""),
        },
        errors=errors,
    )
