"""SIG Mesh protocol codec — packet encoding/decoding.

Pure encoding and decoding of SIG Mesh packets, config model messages,
access layer opcodes, Tuya vendor frames, composition data, and proxy PDUs.

No cryptographic operations — those live in ``sig_mesh_protocol.py``.

SECURITY: Key material is NEVER logged, printed, or included in
exception messages. Only lengths and opcodes are safe to log.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from tuya_ble_mesh.exceptions import MalformedPacketError, ProtocolError

_LOGGER = logging.getLogger(__name__)

# --- Proxy PDU constants ---
PROXY_SAR_COMPLETE = 0x00
PROXY_TYPE_NETWORK = 0x00

# --- Transport constants ---
MAX_UNSEG_ACCESS_PAYLOAD = 11  # 15 byte upper transport - 4 byte TransMIC
SEG_DATA_SIZE = 12  # max bytes per segment chunk

# --- Network PDU field masks and lengths ---
MESH_NID_MASK = 0x7F
MESH_IVI_SHIFT = 7
MESH_TTL_MASK = 0x7F
MESH_CTL_SHIFT = 7
MIC_LEN_ACCESS = 4
MIC_LEN_CONTROL = 8

# --- Lower transport header masks ---
MESH_SEG_BIT = 0x80
MESH_AKF_SHIFT = 6
MESH_AID_MASK = 0x3F

# --- Segmented transport header bit positions (24-bit info field) ---
MESH_SZMIC_SHIFT = 23
MESH_SEQ_ZERO_SHIFT = 10
MESH_SEQ_ZERO_MASK = 0x1FFF
MESH_SEG_O_SHIFT = 5
MESH_SEG_MASK = 0x1F

# --- Proxy PDU header masks ---
PROXY_SAR_MASK = 0x03
PROXY_TYPE_MASK = 0x3F

# --- Access layer opcode format bits (Mesh Profile 3.7.3) ---
MESH_OPCODE_1BYTE_MASK = 0x80
MESH_OPCODE_2BYTE_MASK = 0xC0
MESH_OPCODE_2BYTE_VALUE = 0x80

# --- Config model opcodes (SIG Mesh Profile 4.3) ---
OP_CONFIG_COMPOSITION_GET = 0x8008
OP_CONFIG_COMPOSITION_STATUS = 0x02
OP_CONFIG_APPKEY_ADD = 0x0000
OP_CONFIG_APPKEY_STATUS = 0x8003
OP_CONFIG_MODEL_APP_BIND = 0x803D
OP_CONFIG_MODEL_APP_STATUS = 0x803E

# --- Generic OnOff model opcodes (Mesh Model 3.2) ---
OP_GENERIC_ONOFF_GET = 0x8201
OP_GENERIC_ONOFF_SET = 0x8202
OP_GENERIC_ONOFF_STATUS = 0x8204

# --- Light model opcodes (Mesh Model 6.3) ---
OP_LIGHT_LIGHTNESS_GET = 0x824B
OP_LIGHT_LIGHTNESS_SET = 0x824C
OP_LIGHT_LIGHTNESS_STATUS = 0x824E
OP_LIGHT_CTL_GET = 0x825D
OP_LIGHT_CTL_SET = 0x825E
OP_LIGHT_CTL_STATUS = 0x8260
OP_LIGHT_CTL_TEMP_RANGE_GET = 0x8262
OP_LIGHT_CTL_TEMP_RANGE_STATUS = 0x8263
OP_LIGHT_CTL_TEMPERATURE_SET = 0x8264
OP_LIGHT_CTL_TEMPERATURE_STATUS = 0x8266
OP_LIGHT_HSL_GET = 0x826D
OP_LIGHT_HSL_SET = 0x8276
OP_LIGHT_HSL_STATUS = 0x8278

# --- SIG model IDs (Mesh Model 7.3) ---
MODEL_CONFIG_SERVER = 0x0000
MODEL_CONFIG_CLIENT = 0x0001
MODEL_HEALTH_SERVER = 0x0002
MODEL_HEALTH_CLIENT = 0x0003
MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_CTL_TEMPERATURE_SERVER = 0x1306
MODEL_LIGHT_HSL_SERVER = 0x1307

# Foundation models use the device key and must never be bound to an AppKey
FOUNDATION_MODELS = frozenset(
    {MODEL_CONFIG_SERVER, MODEL_CONFIG_CLIENT, MODEL_HEALTH_SERVER, MODEL_HEALTH_CLIENT}
)

# Light CTL Temperature range allowed by the spec (kelvin)
LIGHT_CTL_TEMP_MIN = 800
LIGHT_CTL_TEMP_MAX = 20000

# --- Tuya Vendor Model (CID 0x07D0) ---
TUYA_VENDOR_OPCODE = 0xCDD007
TUYA_VENDOR_WRITE_ACK = 0xC9D007
TUYA_VENDOR_WRITE_UNACK = 0xCAD007
TUYA_VENDOR_READ = 0xCCD007
TUYA_VENDOR_CID = 0x07D0
TUYA_CMD_DP_DATA = 0x01
TUYA_CMD_TIMESTAMP_SYNC = 0x02
DP_ID_SWITCH = 1
DP_ID_ENERGY_KWH = 17
DP_ID_POWER_W = 18
DP_ID_CURRENT_MA = 19
DP_ID_VOLTAGE_V = 20

# Internal alias used by segments module
_OPCODE_COMPOSITION_STATUS = OP_CONFIG_COMPOSITION_STATUS


# ============================================================
# Segment Header Parsing (Mesh Profile 3.5.2.2)
# ============================================================


@dataclass(frozen=True)
class SegmentHeader:
    """Parsed segmented access message header fields."""

    akf: int
    aid: int
    szmic: int
    seq_zero: int
    seg_o: int
    seg_n: int
    segment_data: bytes


def parse_segment_header(transport_pdu: bytes) -> SegmentHeader:
    """Parse a segmented access message lower transport PDU header."""
    if len(transport_pdu) < 4:
        msg = f"Segmented transport PDU too short: {len(transport_pdu)} bytes"
        raise MalformedPacketError(msg)

    hdr = transport_pdu[0]
    if not (hdr & MESH_SEG_BIT):
        msg = "Not a segmented PDU (SEG bit not set)"
        raise MalformedPacketError(msg)

    akf = (hdr >> MESH_AKF_SHIFT) & 1
    aid = hdr & MESH_AID_MASK
    info = (transport_pdu[1] << 16) | (transport_pdu[2] << 8) | transport_pdu[3]

    return SegmentHeader(
        akf=akf,
        aid=aid,
        szmic=(info >> MESH_SZMIC_SHIFT) & 1,
        seq_zero=(info >> MESH_SEQ_ZERO_SHIFT) & MESH_SEQ_ZERO_MASK,
        seg_o=(info >> MESH_SEG_O_SHIFT) & MESH_SEG_MASK,
        seg_n=info & MESH_SEG_MASK,
        segment_data=transport_pdu[4:],
    )


# ============================================================
# Proxy PDU (Mesh Profile 6.3)
# ============================================================


def make_proxy_pdu(network_pdu: bytes) -> bytes:
    """Wrap a network PDU in a Mesh Proxy PDU (SAR=complete, type=network)."""
    return bytes([(PROXY_SAR_COMPLETE << 6) | PROXY_TYPE_NETWORK]) + network_pdu


@dataclass(frozen=True)
class ProxyPDU:
    """Parsed Mesh Proxy PDU."""

    sar: int
    pdu_type: int
    payload: bytes


def parse_proxy_pdu(data: bytes) -> ProxyPDU:
    """Parse a Mesh Proxy PDU from GATT characteristic bytes."""
    if not data:
        msg = "Empty proxy PDU"
        raise MalformedPacketError(msg)
    return ProxyPDU(
        sar=(data[0] >> 6) & PROXY_SAR_MASK,
        pdu_type=data[0] & PROXY_TYPE_MASK,
        payload=data[1:],
    )


# ============================================================
# Config Model Messages (Mesh Profile 4.3)
# ============================================================


def config_composition_get(page: int = 0) -> bytes:
    """Config Composition Data Get (opcode 0x8008)."""
    if not 0 <= page <= 0xFF:
        msg = f"Page must be 0..255, got {page}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_CONFIG_COMPOSITION_GET) + bytes([page])


def config_appkey_add(net_idx: int, app_idx: int, app_key: bytes) -> bytes:
    """Config AppKey Add (opcode 0x00). 20-byte payload — requires segmented transport."""
    if not 0 <= net_idx <= 0xFFF:
        msg = f"net_idx must be 0..0xFFF, got {net_idx}"
        raise ProtocolError(msg)
    if not 0 <= app_idx <= 0xFFF:
        msg = f"app_idx must be 0..0xFFF, got {app_idx}"
        raise ProtocolError(msg)
    if len(app_key) != 16:
        msg = f"app_key must be 16 bytes, got {len(app_key)}"
        raise ProtocolError(msg)
    idx = (net_idx & 0xFFF) | ((app_idx & 0xFFF) << 12)
    return bytes([OP_CONFIG_APPKEY_ADD]) + struct.pack("<I", idx)[:3] + app_key


def config_model_app_bind(
    element_addr: int, app_idx: int, model_id: int, company_id: int | None = None
) -> bytes:
    """Config Model App Bind (opcode 0x803D).

    SIG models use a 16-bit model ID. Vendor models pass ``company_id`` and
    are encoded as company ID followed by model ID (both little-endian).
    """
    if not 0 <= element_addr <= 0xFFFF:
        msg = f"element_addr must be 0..0xFFFF, got {element_addr}"
        raise ProtocolError(msg)
    if not 0 <= app_idx <= 0xFFF:
        msg = f"app_idx must be 0..0xFFF, got {app_idx}"
        raise ProtocolError(msg)
    if not 0 <= model_id <= 0xFFFF:
        msg = f"model_id must be 0..0xFFFF, got {model_id}"
        raise ProtocolError(msg)
    header = struct.pack(">H", OP_CONFIG_MODEL_APP_BIND)
    if company_id is None:
        return header + struct.pack("<HHH", element_addr, app_idx, model_id)
    if not 0 <= company_id <= 0xFFFF:
        msg = f"company_id must be 0..0xFFFF, got {company_id}"
        raise ProtocolError(msg)
    return header + struct.pack("<HHHH", element_addr, app_idx, company_id, model_id)


# ============================================================
# Generic OnOff Model Messages (Mesh Model 3.2)
# ============================================================


def generic_onoff_set(on: bool, tid: int = 0) -> bytes:
    """Generic OnOff Set (opcode 0x8202)."""
    return struct.pack(">H", OP_GENERIC_ONOFF_SET) + bytes([0x01 if on else 0x00, tid & 0xFF])


def generic_onoff_get() -> bytes:
    """Generic OnOff Get (opcode 0x8201)."""
    return struct.pack(">H", OP_GENERIC_ONOFF_GET)


# ============================================================
# Light Model Messages (Mesh Model 6.3)
# ============================================================


def _check_u16(name: str, value: int) -> None:
    if not 0 <= value <= 0xFFFF:
        msg = f"{name} must be 0..0xFFFF, got {value}"
        raise ProtocolError(msg)


def light_lightness_set(lightness: int, tid: int = 0) -> bytes:
    """Light Lightness Set (opcode 0x824C). Lightness is 0..0xFFFF."""
    _check_u16("lightness", lightness)
    return struct.pack(">H", OP_LIGHT_LIGHTNESS_SET) + struct.pack("<HB", lightness, tid & 0xFF)


def light_ctl_set(lightness: int, temperature: int, delta_uv: int = 0, tid: int = 0) -> bytes:
    """Light CTL Set (opcode 0x825E). Temperature is kelvin, 800..20000."""
    _check_u16("lightness", lightness)
    if not LIGHT_CTL_TEMP_MIN <= temperature <= LIGHT_CTL_TEMP_MAX:
        msg = f"temperature must be {LIGHT_CTL_TEMP_MIN}..{LIGHT_CTL_TEMP_MAX}, got {temperature}"
        raise ProtocolError(msg)
    if not -0x8000 <= delta_uv <= 0x7FFF:
        msg = f"delta_uv must be a signed 16-bit value, got {delta_uv}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_CTL_SET) + struct.pack(
        "<HHhB", lightness, temperature, delta_uv, tid & 0xFF
    )


def light_ctl_temperature_set(temperature: int, delta_uv: int = 0, tid: int = 0) -> bytes:
    """Light CTL Temperature Set (opcode 0x8264). Temperature is kelvin, 800..20000."""
    if not LIGHT_CTL_TEMP_MIN <= temperature <= LIGHT_CTL_TEMP_MAX:
        msg = f"temperature must be {LIGHT_CTL_TEMP_MIN}..{LIGHT_CTL_TEMP_MAX}, got {temperature}"
        raise ProtocolError(msg)
    if not -0x8000 <= delta_uv <= 0x7FFF:
        msg = f"delta_uv must be a signed 16-bit value, got {delta_uv}"
        raise ProtocolError(msg)
    return struct.pack(">H", OP_LIGHT_CTL_TEMPERATURE_SET) + struct.pack(
        "<HhB", temperature, delta_uv, tid & 0xFF
    )


def light_hsl_set(lightness: int, hue: int, saturation: int, tid: int = 0) -> bytes:
    """Light HSL Set (opcode 0x8276). All three values are 0..0xFFFF."""
    _check_u16("lightness", lightness)
    _check_u16("hue", hue)
    _check_u16("saturation", saturation)
    return struct.pack(">H", OP_LIGHT_HSL_SET) + struct.pack(
        "<HHHB", lightness, hue, saturation, tid & 0xFF
    )


def light_state_gets() -> list[bytes]:
    """Get messages for the node's lightness, CTL, CTL temperature range and HSL state."""
    return [
        struct.pack(">H", op)
        for op in (
            OP_LIGHT_LIGHTNESS_GET,
            OP_LIGHT_CTL_GET,
            OP_LIGHT_CTL_TEMP_RANGE_GET,
            OP_LIGHT_HSL_GET,
        )
    ]


def describe_light_status(opcode: int, params: bytes) -> str | None:
    """Return a readable summary of a Light status message, or None if not one."""
    if opcode == OP_LIGHT_LIGHTNESS_STATUS and len(params) >= 2:
        (lightness,) = struct.unpack_from("<H", params)
        return f"Light Lightness Status: lightness={lightness}"
    if opcode == OP_LIGHT_CTL_STATUS and len(params) >= 4:
        lightness, temperature = struct.unpack_from("<HH", params)
        return f"Light CTL Status: lightness={lightness} temperature={temperature}K"
    if opcode == OP_LIGHT_CTL_TEMP_RANGE_STATUS and len(params) >= 5:
        status, range_min, range_max = struct.unpack_from("<BHH", params)
        return (
            f"Light CTL Temperature Range Status: status={status} min={range_min}K max={range_max}K"
        )
    if opcode == OP_LIGHT_CTL_TEMPERATURE_STATUS and len(params) >= 2:
        (temperature,) = struct.unpack_from("<H", params)
        return f"Light CTL Temperature Status: temperature={temperature}K"
    if opcode == OP_LIGHT_HSL_STATUS and len(params) >= 6:
        lightness, hue, saturation = struct.unpack_from("<HHH", params)
        return f"Light HSL Status: lightness={lightness} hue={hue} saturation={saturation}"
    return None


# ============================================================
# Access Layer Opcode Parsing (Mesh Profile 3.7.3)
# ============================================================


def parse_access_opcode(data: bytes) -> tuple[int, bytes]:
    """Parse a SIG Mesh access layer opcode (1, 2, or 3 bytes)."""
    if not data:
        msg = "Empty access payload"
        raise MalformedPacketError(msg)

    if data[0] & MESH_OPCODE_1BYTE_MASK == 0:
        return data[0], data[1:]
    elif data[0] & MESH_OPCODE_2BYTE_MASK == MESH_OPCODE_2BYTE_VALUE:
        if len(data) < 2:
            msg = "2-byte opcode truncated"
            raise MalformedPacketError(msg)
        return (data[0] << 8) | data[1], data[2:]
    else:
        if len(data) < 3:
            msg = "3-byte vendor opcode truncated"
            raise MalformedPacketError(msg)
        return (data[0] << 16) | (data[1] << 8) | data[2], data[3:]


# ============================================================
# Tuya Vendor Model (CID 0x07D0)
# ============================================================


@dataclass(frozen=True)
class TuyaVendorDP:
    """A single Tuya vendor Data Point from a vendor message."""

    dp_id: int
    dp_type: int
    value: bytes


@dataclass(frozen=True)
class TuyaVendorFrame:
    """Parsed Tuya vendor message frame.

    Attributes:
        command: Frame command byte (0x01=DP data, 0x02=timestamp sync, 0=unknown/raw).
        data: Raw data bytes after the frame header.
        dps: Parsed data points (populated only when command is TUYA_CMD_DP_DATA).
    """

    command: int
    data: bytes
    dps: list[TuyaVendorDP]


def parse_tuya_vendor_frame(params: bytes) -> TuyaVendorFrame:
    """Parse a Tuya vendor message with frame header ``[command 1B][data_length 1B][data NB]``."""
    if len(params) < 2:
        _LOGGER.debug("Vendor frame too short (%d bytes)", len(params))
        return TuyaVendorFrame(command=0, data=params, dps=[])

    command = params[0]
    data = params[2:]

    if command == TUYA_CMD_TIMESTAMP_SYNC:
        _LOGGER.debug("Tuya timestamp sync request (data=%s)", data.hex())
        return TuyaVendorFrame(command=command, data=data, dps=[])

    if command == TUYA_CMD_DP_DATA:
        return TuyaVendorFrame(command=command, data=data, dps=_parse_dp_bytes(data))

    _LOGGER.debug("Unknown vendor command 0x%02X, trying raw DP parse on full params", command)
    return TuyaVendorFrame(command=command, data=params, dps=_parse_dp_bytes(params))


def tuya_vendor_timestamp_response() -> bytes:
    """Build a Tuya vendor WRITE_UNACK payload with current UTC timestamp."""
    import time

    now = int(time.time())
    opcode_bytes = TUYA_VENDOR_WRITE_UNACK.to_bytes(3, "big")
    ts_bytes = now.to_bytes(4, "big")
    tz_offset = time.timezone // -3600 if not time.daylight else time.altzone // -3600
    tz_byte = tz_offset.to_bytes(1, "big", signed=True) if -12 <= tz_offset <= 14 else b"\x00"
    data = ts_bytes + tz_byte + b"\x00\x00\x00"
    frame = bytes([TUYA_CMD_TIMESTAMP_SYNC, len(data)]) + data
    return opcode_bytes + frame


TUYA_DP_TYPE_RAW = 0x00
TUYA_DP_TYPE_BOOL = 0x01
TUYA_DP_TYPE_VALUE = 0x02
TUYA_DP_TYPE_STRING = 0x03
TUYA_DP_TYPE_ENUM = 0x04
TUYA_DP_TYPE_BITMAP = 0x05


def decode_tuya_dp_value(dp_type: int, value: bytes) -> bool | int | str:
    """Decode a Tuya DP value by its type: bool, int (value/enum/bitmap), text, or hex."""
    if dp_type == TUYA_DP_TYPE_BOOL and len(value) == 1:
        return value[0] != 0
    if dp_type == TUYA_DP_TYPE_VALUE and value:
        return int.from_bytes(value, "big", signed=True)
    if dp_type in (TUYA_DP_TYPE_ENUM, TUYA_DP_TYPE_BITMAP) and value:
        return int.from_bytes(value, "big")
    if dp_type == TUYA_DP_TYPE_STRING:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value.hex()


def tuya_vendor_dp_query() -> bytes:
    """Build a Tuya vendor READ payload asking the device to report all its DPs.

    Frame: command 0x01 (DP data), length 1, DP id 0. Tuya's pass-through spec:
    "If the received DPID is 0, the device reports all DP status".
    """
    return TUYA_VENDOR_READ.to_bytes(3, "big") + bytes([TUYA_CMD_DP_DATA, 1, 0])


def parse_tuya_vendor_dps(params: bytes) -> list[TuyaVendorDP]:
    """Parse Tuya vendor DP values (raw TLV, no frame header)."""
    return _parse_dp_bytes(params)


def _parse_dp_bytes(data: bytes) -> list[TuyaVendorDP]:
    """Parse raw DP bytes: ``[dp_id 1B][dp_type 1B][dp_len 1B][value NB]...``"""
    dps: list[TuyaVendorDP] = []
    offset = 0
    while offset < len(data):
        if offset + 3 > len(data):
            _LOGGER.debug("Truncated DP header at offset %d", offset)
            break
        dp_id = data[offset]
        dp_type = data[offset + 1]
        dp_len = data[offset + 2]
        offset += 3
        if offset + dp_len > len(data):
            _LOGGER.debug(
                "Truncated DP value: dp_id=%d, need %d bytes, have %d",
                dp_id,
                dp_len,
                len(data) - offset,
            )
            break
        dps.append(TuyaVendorDP(dp_id=dp_id, dp_type=dp_type, value=data[offset : offset + dp_len]))
        offset += dp_len
    return dps


# ============================================================
# Composition Data (Mesh Profile 4.2.1)
# ============================================================


@dataclass(frozen=True)
class CompositionElement:
    """One element from Composition Data Page 0."""

    location: int
    sig_models: tuple[int, ...]
    vendor_models: tuple[tuple[int, int], ...]  # (company_id, model_id)


@dataclass(frozen=True)
class CompositionData:
    """Parsed Composition Data Page 0 header."""

    cid: int  # Company ID
    pid: int  # Product ID
    vid: int  # Version ID
    crpl: int  # Replay protection list size
    features: int  # Features bitmask
    raw_elements: bytes  # Unparsed element data

    @property
    def elements(self) -> tuple[CompositionElement, ...]:
        """Element list parsed from ``raw_elements``."""
        return parse_composition_elements(self.raw_elements)

    @property
    def sig_models(self) -> frozenset[int]:
        """All SIG model IDs across every element."""
        return frozenset(m for el in self.elements for m in el.sig_models)


def parse_composition_elements(raw: bytes) -> tuple[CompositionElement, ...]:
    """Parse the element list of Composition Data Page 0 (Mesh Profile 4.2.1.1).

    Each element is Loc(2) NumS(1) NumV(1), then NumS 16-bit SIG model IDs
    and NumV vendor model IDs (company ID + model ID, 16 bits each).

    Raises:
        MalformedPacketError: If an element runs past the end of the data.
    """
    elements: list[CompositionElement] = []
    offset = 0
    while offset < len(raw):
        if offset + 4 > len(raw):
            msg = f"Element header truncated at offset {offset}"
            raise MalformedPacketError(msg)
        location = struct.unpack_from("<H", raw, offset)[0]
        num_s = raw[offset + 2]
        num_v = raw[offset + 3]
        offset += 4
        end = offset + 2 * num_s + 4 * num_v
        if end > len(raw):
            msg = f"Element model list truncated at offset {offset}"
            raise MalformedPacketError(msg)
        sig = struct.unpack_from(f"<{num_s}H", raw, offset)
        offset += 2 * num_s
        vendor = tuple(struct.unpack_from("<HH", raw, offset + 4 * i) for i in range(num_v))
        offset = end
        elements.append(CompositionElement(location, tuple(sig), vendor))
    return tuple(elements)


def parse_composition_data(params: bytes) -> CompositionData:
    """Parse Composition Data Status page 0 parameters."""
    if len(params) < 11:
        msg = f"Composition Data too short: {len(params)} bytes (need >= 11)"
        raise MalformedPacketError(msg)

    data = params[1:]  # Skip page byte
    return CompositionData(
        cid=struct.unpack_from("<H", data, 0)[0],
        pid=struct.unpack_from("<H", data, 2)[0],
        vid=struct.unpack_from("<H", data, 4)[0],
        crpl=struct.unpack_from("<H", data, 6)[0],
        features=struct.unpack_from("<H", data, 8)[0],
        raw_elements=data[10:],
    )


# ============================================================
# Status Response Formatting
# ============================================================

_CONFIG_STATUS_NAMES: dict[int, str] = {
    0x00: "Success",
    0x01: "InvalidAddress",
    0x02: "InvalidModel",
    0x03: "InvalidAppKeyIndex",
    0x04: "InvalidNetKeyIndex",
    0x05: "InsufficientResources",
    0x06: "KeyIndexAlreadyStored",
}


def format_status_response(opcode: int, params: bytes) -> str:
    """Format a mesh status response for human-readable display."""
    if opcode == OP_CONFIG_APPKEY_STATUS:
        status = params[0] if params else 0xFF
        return f"AppKey Status: {_CONFIG_STATUS_NAMES.get(status, f'Unknown(0x{status:02X})')}"

    if opcode == OP_CONFIG_MODEL_APP_STATUS:
        status = params[0] if params else 0xFF
        bind_status: dict[int, str] = {
            0x00: "Success",
            0x02: "InvalidModel",
            0x03: "InvalidAppKeyIndex",
            0x04: "InvalidNetKeyIndex",
            0x06: "ModelAppAlreadyBound",
        }
        return f"Model App Status: {bind_status.get(status, f'Unknown(0x{status:02X})')}"

    if opcode == OP_CONFIG_COMPOSITION_STATUS:
        page = params[0] if params else 0xFF
        return f"Composition Data: page={page} ({len(params) - 1} bytes)"

    if opcode == OP_GENERIC_ONOFF_STATUS:
        state = params[0] if params else 0xFF
        msg = f"OnOff Status: {'ON' if state else 'OFF'}"
        if len(params) >= 3:
            msg += f" (target={'ON' if params[1] else 'OFF'}, remaining={params[2]})"
        return msg

    return f"Opcode 0x{opcode:04X}: {len(params)} bytes"
