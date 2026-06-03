# NOTE: This is a copy of the root beacon_decoder.py for AWS Lambda deployment.
# Lambda functions cannot import from the project root. Keep in sync with the
# root copy — any bug fixes must be applied to both files.

"""
Beacon Decoder — Decodes binary beacon packets from the CubeSAT-I OBC_v5d firmware.

The satellite's OBC uses a BinaryEncoder (key-hash TLV format) to pack telemetry
into compact bytes.  TinyGS ground stations receive these bytes over LoRa and
deliver them as base64-encoded blobs in the MQTT ``data`` field.

This module:
1. Strips the PacketManager 6-byte radio frame header (if present).
2. Decodes the BinaryEncoder TLV stream into a Python dict.
3. Maps 4-byte key hashes back to human-readable field names using a
   pre-built key map generated from the satellite's firmware.

Usage::

    raw = base64.b64decode(tinygs_data_field)
    decoded = decode_beacon(raw)
    # decoded = {"name": "CubeSATI", "FSM_state": "nominal", "FSM_batt_v": 3.87, ...}

Key-map generation
------------------
The OBC firmware hashes field names with CircuitPython's ``hash(key) & 0xFFFFFFFF``
which uses the **djb2** algorithm (deterministic, 16-bit).  ``KEY_MAP`` below is
pre-populated with the correct djb2 hashes for all current beacon fields.

If the OBC firmware adds or renames fields, use ``_circuitpython_hash(new_key)``
to compute the hash and update ``KEY_MAP``.  For local testing with CPython-
generated packets, pass ``build_cpython_key_map()`` as the ``key_map`` argument.
"""

import struct
from typing import Optional

# ──────────────────────────────────────────────
# PacketManager header (from OBC_v5d packet_manager.py)
# ──────────────────────────────────────────────
# | Offset | Size | Description                               |
# |--------|------|-------------------------------------------|
# | 0      | 1 B  | packet_identifier (message counter)       |
# | 1-2    | 2 B  | sequence_number  (big-endian, 0-based)    |
# | 3-4    | 2 B  | total_packets    (big-endian)             |
# | 5      | 1 B  | abs(RSSI) of the radio at send time       |
PACKET_HEADER_SIZE = 6


# ──────────────────────────────────────────────
# BinaryEncoder type IDs (from OBC_v5d binary_encoder.py)
# ──────────────────────────────────────────────
_TYPE_STRING  = 0
_TYPE_INT8    = 1
_TYPE_INT16   = 2
_TYPE_INT32   = 3
_TYPE_INT64   = 4
_TYPE_FLOAT32 = 5
_TYPE_FLOAT64 = 6
_TYPE_UINT8   = 11
_TYPE_UINT16  = 12
_TYPE_UINT32  = 13
_TYPE_UINT64  = 14

_TYPE_FORMATS: dict[int, tuple[str, int]] = {
    _TYPE_INT8:    (">b", 1),
    _TYPE_INT16:   (">h", 2),
    _TYPE_INT32:   (">i", 4),
    _TYPE_INT64:   (">q", 8),
    _TYPE_FLOAT32: (">f", 4),
    _TYPE_FLOAT64: (">d", 8),
    _TYPE_UINT8:   (">B", 1),
    _TYPE_UINT16:  (">H", 2),
    _TYPE_UINT32:  (">I", 4),
    _TYPE_UINT64:  (">Q", 8),
}


# ──────────────────────────────────────────────
# Known beacon field names (order matches _build_state in OBC_v5d beacon.py)
# ──────────────────────────────────────────────
# These are all the keys the BinaryEncoder will hash.  The flight firmware's
# _build_state (with FSM active, _add_sensor_data commented out) produces:
KNOWN_BEACON_KEYS: list[str] = [
    "name",
    # — FSM dict, expanded by _encode_sensor_dict as "FSM_<subkey>" —
    "FSM_state",
    "FSM_depl",
    "FSM_pay_set",
    "FSM_pan_light",
    "FSM_payl_light",
    "FSM_best_dir",
    # dp_obj data items (renamed in _add_system_info)
    "FSM_magn_v_0",   # magnetometer x
    "FSM_magn_v_1",   # magnetometer y
    "FSM_magn_v_2",   # magnetometer z
    "FSM_av_0",       # angular velocity x
    "FSM_av_1",       # angular velocity y
    "FSM_av_2",       # angular velocity z
    "FSM_acc_0",      # acceleration x
    "FSM_acc_1",      # acceleration y
    "FSM_acc_2",      # acceleration z
    "FSM_batt_v",     # battery voltage
    # System info (after FSM dict)
    "time",
    "uptime",
]


# ──────────────────────────────────────────────
# KEY MAP  —  hash(key) & 0xFFFFFFFF  →  key name
# ──────────────────────────────────────────────
# These are the CircuitPython djb2 hashes (seed=5381, Q_HASH_MASK=0xFFFF)
# for every field in KNOWN_BEACON_KEYS.  CircuitPython's hash() differs from
# CPython's (SipHash) — these values are deterministic and were computed by
# replicating the djb2 algorithm from CircuitPython's qstr_compute_hash().
#
# *** If the OBC firmware (OBC_v5d) changes which fields are transmitted ***
# *** in beacon.py _build_state / _add_system_info, you MUST regenerate ***
# *** these hashes. Run _circuitpython_hash() below on every new key.   ***
KEY_MAP: dict[int, str] = {
    0x000075A2: "name",
    0x0000FC95: "FSM_state",
    0x0000473F: "FSM_depl",
    0x0000FE57: "FSM_pay_set",
    0x000058DC: "FSM_pan_light",
    0x000021A7: "FSM_payl_light",
    0x000090A2: "FSM_best_dir",
    0x00004761: "FSM_magn_v_0",
    0x00004760: "FSM_magn_v_1",
    0x00004763: "FSM_magn_v_2",
    0x0000E39A: "FSM_av_0",
    0x0000E39B: "FSM_av_1",
    0x0000E398: "FSM_av_2",
    0x000093AC: "FSM_acc_0",
    0x000093AD: "FSM_acc_1",
    0x000093AE: "FSM_acc_2",
    0x000097E8: "FSM_batt_v",
    0x0000C1F0: "time",
    0x00005BD5: "uptime",
}


def _circuitpython_hash(s: str) -> int:
    """Replicate CircuitPython's qstr_compute_hash (djb2, 16-bit).

    Use this to compute the hash for any new beacon field added to the
    OBC firmware, then add the result to KEY_MAP above.
    """
    h = 5381
    for ch in s.encode("utf-8"):
        h = ((h << 5) + h) ^ ch
        h &= 0xFFFFFFFF
    h &= 0xFFFF
    return h if h != 0 else 1


def build_cpython_key_map() -> dict[int, str]:
    """Generate a key map using CPython's ``hash()`` — useful for local testing.

    Returns a dict mapping ``hash(key) & 0xFFFFFFFF`` → key name for every
    entry in ``KNOWN_BEACON_KEYS``.

    .. warning::
        CPython hashes differ from CircuitPython hashes.  This map is only
        valid when decoding data produced by CPython (e.g. a ground-station
        test harness).  For real satellite data use the CircuitPython hashes
        in ``KEY_MAP`` above.
    """
    return {hash(k) & 0xFFFFFFFF: k for k in KNOWN_BEACON_KEYS}


# ──────────────────────────────────────────────
# Low-level binary decoder  (mirrors OBC_v5d BinaryDecoder)
# ──────────────────────────────────────────────

def _decode_tlv_stream(data: bytes, key_map: dict[int, str]) -> dict[str, object]:
    """Walk a BinaryEncoder byte stream and return decoded key-value pairs."""
    result: dict[str, object] = {}
    offset = 0
    field_index = 0

    while offset < len(data):
        if offset + 5 > len(data):
            break  # not enough room for key_hash(4) + type_id(1)

        key_hash, type_id = struct.unpack(">IB", data[offset:offset + 5])
        offset += 5

        key_name = key_map.get(key_hash, f"field_{key_hash:08x}")

        if type_id == _TYPE_STRING:
            if offset >= len(data):
                break
            str_len = data[offset]
            offset += 1
            if offset + str_len > len(data):
                break
            value = data[offset:offset + str_len].decode("utf-8", errors="replace")
            offset += str_len
        elif type_id in _TYPE_FORMATS:
            fmt, size = _TYPE_FORMATS[type_id]
            if offset + size > len(data):
                break
            value = struct.unpack(fmt, data[offset:offset + size])[0]
            offset += size
        else:
            # Unknown type — stop decoding (data may be corrupt)
            break

        result[key_name] = value
        field_index += 1

    return result


def strip_packet_header(raw: bytes) -> tuple[dict, bytes]:
    """Strip 6-byte PacketManager header and return (header_info, payload).

    Returns:
        (header_dict, payload_bytes) where header_dict contains:
            packet_id, sequence, total_packets, rssi
    """
    if len(raw) < PACKET_HEADER_SIZE:
        return {}, raw

    pkt_id = raw[0]
    seq = int.from_bytes(raw[1:3], "big")
    total = int.from_bytes(raw[3:5], "big")
    rssi = raw[5]

    header = {
        "packet_id": pkt_id,
        "sequence": seq,
        "total_packets": total,
        "tx_rssi": rssi,
    }
    return header, raw[PACKET_HEADER_SIZE:]


def _looks_like_packet_header(raw: bytes) -> bool:
    """Heuristic: does the first 6 bytes look like a PacketManager header?

    A valid single-fragment beacon would have sequence=0 and total_packets=1.
    Multi-fragment beacons have sequence < total_packets.
    """
    if len(raw) < PACKET_HEADER_SIZE:
        return False
    seq = int.from_bytes(raw[1:3], "big")
    total = int.from_bytes(raw[3:5], "big")
    # Reasonable: total_packets >= 1 and sequence < total_packets
    return 1 <= total <= 20 and seq < total


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def decode_beacon(
    raw: bytes,
    key_map: Optional[dict[int, str]] = None,
    strip_header: Optional[bool] = None,
) -> dict:
    """Decode a raw beacon frame into a telemetry dict.

    Parameters
    ----------
    raw : bytes
        Raw bytes from the satellite (e.g. base64-decoded TinyGS ``data`` field).
    key_map : dict, optional
        Mapping of ``hash & 0xFFFFFFFF`` → field name.  Falls back to the
        module-level ``KEY_MAP``; if that is also empty, uses CPython hashes.
    strip_header : bool, optional
        Whether to strip the 6-byte PacketManager header.
        ``None`` = auto-detect using heuristic.

    Returns
    -------
    dict
        ``{"header": {...}, "telemetry": {...}}`` where *header* is the
        PacketManager metadata (if stripped) and *telemetry* is the decoded
        beacon fields.
    """
    if key_map is None:
        key_map = KEY_MAP

    header_info: dict = {}
    payload = raw

    if strip_header is True or (strip_header is None and _looks_like_packet_header(raw)):
        header_info, payload = strip_packet_header(raw)

    telemetry = _decode_tlv_stream(payload, key_map)

    return {"header": header_info, "telemetry": telemetry}


# ──────────────────────────────────────────────
# Telemetry field metadata  (units / display names)
# ──────────────────────────────────────────────
# Used by downstream modules for labelling and CSV export.

FIELD_META: dict[str, dict[str, str]] = {
    "name":            {"unit": "",    "label": "Satellite Name"},
    "FSM_state":       {"unit": "",    "label": "FSM State"},
    "FSM_depl":        {"unit": "",    "label": "Deployed"},
    "FSM_pay_set":     {"unit": "",    "label": "Payload Setting"},
    "FSM_pan_light":   {"unit": "",    "label": "Panel Light Intensity"},
    "FSM_payl_light":  {"unit": "",    "label": "Payload Light Intensity"},
    "FSM_best_dir":    {"unit": "",    "label": "Best Direction"},
    "FSM_magn_v_0":    {"unit": "µT",  "label": "Magnetometer X"},
    "FSM_magn_v_1":    {"unit": "µT",  "label": "Magnetometer Y"},
    "FSM_magn_v_2":    {"unit": "µT",  "label": "Magnetometer Z"},
    "FSM_av_0":        {"unit": "°/s", "label": "Angular Velocity X"},
    "FSM_av_1":        {"unit": "°/s", "label": "Angular Velocity Y"},
    "FSM_av_2":        {"unit": "°/s", "label": "Angular Velocity Z"},
    "FSM_acc_0":       {"unit": "m/s²","label": "Acceleration X"},
    "FSM_acc_1":       {"unit": "m/s²","label": "Acceleration Y"},
    "FSM_acc_2":       {"unit": "m/s²","label": "Acceleration Z"},
    "FSM_batt_v":      {"unit": "V",   "label": "Battery Voltage"},
    "time":            {"unit": "",    "label": "OBC Time"},
    "uptime":          {"unit": "s",   "label": "Uptime"},
}

# Subset of fields that are numeric telemetry (suitable for time-series storage)
TELEMETRY_FIELDS: dict[str, str] = {
    "FSM_pan_light":   "",
    "FSM_payl_light":  "",
    "FSM_best_dir":    "",
    "FSM_magn_v_0":    "µT",
    "FSM_magn_v_1":    "µT",
    "FSM_magn_v_2":    "µT",
    "FSM_av_0":        "°/s",
    "FSM_av_1":        "°/s",
    "FSM_av_2":        "°/s",
    "FSM_acc_0":       "m/s²",
    "FSM_acc_1":       "m/s²",
    "FSM_acc_2":       "m/s²",
    "FSM_batt_v":      "V",
    "uptime":          "s",
}


def extract_telemetry_readings(
    decoded: dict[str, object],
) -> list[tuple[str, float, str]]:
    """Pull numeric telemetry from a decoded beacon for database storage.

    Returns a list of ``(key, value, unit)`` tuples suitable for
    ``packet_store.store_telemetry_batch()``.
    """
    readings: list[tuple[str, float, str]] = []
    for key, unit in TELEMETRY_FIELDS.items():
        val = decoded.get(key)
        if val is not None:
            try:
                readings.append((key, float(val), unit))
            except (TypeError, ValueError):
                pass
    return readings
