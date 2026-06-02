"""
Pi-local tests — verifies packet_store, beacon_decoder, and dedup
without any network or AWS dependencies.

Run on the Pi:
    python test_pi_local.py
"""

import base64
import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}  {detail}")
        failed += 1


# ──────────────────────────────────────────────
# Beacon frame builder (same as E2E test)
# ──────────────────────────────────────────────

def _circuitpython_hash(s: str) -> int:
    h = 5381
    for ch in s.encode("utf-8"):
        h = ((h << 5) + h) ^ ch
        h &= 0xFFFFFFFF
    h &= 0xFFFF
    return h if h != 0 else 1

def tlv_string(key, value):
    enc = value.encode("utf-8")
    return struct.pack(">IB", _circuitpython_hash(key), 0x00) + bytes([len(enc)]) + enc

def tlv_float32(key, value):
    return struct.pack(">IBf", _circuitpython_hash(key), 0x05, value)

def tlv_uint32(key, value):
    return struct.pack(">IBI", _circuitpython_hash(key), 0x0D, value)

header = bytes([0x01]) + struct.pack(">HH", 0, 1) + bytes([80])
payload = b""
payload += tlv_string("name", "CubeSATI")
payload += tlv_string("FSM_state", "nominal")
payload += tlv_float32("FSM_magn_v_0", 23.5)
payload += tlv_float32("FSM_batt_v", 3.87)
payload += tlv_uint32("uptime", 7200)
BEACON_FRAME = header + payload
BEACON_B64 = base64.b64encode(BEACON_FRAME).decode("ascii")


# ──────────────────────────────────────────────
# Redirect packet_store to a temp DB
# ──────────────────────────────────────────────

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()

import packet_store
packet_store._DB_PATH = Path(tmp.name)
if hasattr(packet_store._local, "conn"):
    del packet_store._local.conn

import beacon_decoder


def main():
    print("\n=== Pi Local Tests ===\n")

    # ── 1. Beacon decoder ──────────────────────────
    print("[beacon_decoder]")
    result = beacon_decoder.decode_beacon(BEACON_FRAME)
    telem = result.get("telemetry", {})

    test("decode returns telemetry dict", isinstance(telem, dict))
    test("name decoded", telem.get("name") == "CubeSATI")
    test("FSM_state decoded", telem.get("FSM_state") == "nominal")
    test("FSM_batt_v decoded", abs(telem.get("FSM_batt_v", 0) - 3.87) < 0.01)
    test("FSM_magn_v_0 decoded", abs(telem.get("FSM_magn_v_0", 0) - 23.5) < 0.01)
    test("uptime decoded", telem.get("uptime") == 7200)

    hdr = result.get("header", {})
    test("header parsed", hdr.get("packet_id") == 1)
    test("header tx_rssi", hdr.get("tx_rssi") == 80)

    # ── 2. extract_telemetry_readings ──────────────
    print("\n[extract_telemetry_readings]")
    readings = beacon_decoder.extract_telemetry_readings(telem)
    test("returns list", isinstance(readings, list))
    test("readings non-empty", len(readings) > 0)
    keys = [r[0] for r in readings]
    test("FSM_batt_v in readings", "FSM_batt_v" in keys)
    test("FSM_magn_v_0 in readings", "FSM_magn_v_0" in keys)

    # ── 3. packet_store: insert ────────────────────
    print("\n[packet_store]")
    pkt_id = packet_store.store_packet(
        satellite="CUBESAT-1",
        station="test-station",
        rssi=-118.0,
        snr=-6.75,
        raw_frame=BEACON_FRAME,
        decoded=telem,
        source="test",
    )
    test("store_packet returns id", pkt_id is not None and pkt_id > 0, f"got {pkt_id}")

    count = packet_store.packet_count()
    test("packet_count is 1", count == 1, f"got {count}")

    # ── 4. packet_store: read back ─────────────────
    print("\n[packet_store read-back]")
    rows = packet_store.recent_packets(n=1)
    test("recent_packets returns 1 row", len(rows) == 1)

    row = rows[0]
    test("satellite matches", row["satellite"] == "CUBESAT-1")
    test("station matches", row["station"] == "test-station")
    test("raw_frame is bytes", isinstance(row["raw_frame"], bytes))
    test("raw_frame matches original", row["raw_frame"] == BEACON_FRAME,
         f"len {len(row.get('raw_frame', b''))} vs {len(BEACON_FRAME)}")

    expected_hash = hashlib.sha256(BEACON_FRAME).hexdigest()
    test("frame_hash correct", row["frame_hash"] == expected_hash)

    decoded_back = json.loads(row["decoded_json"])
    test("decoded_json round-trips", decoded_back.get("name") == "CubeSATI")

    # ── 5. packet_store: dedup ─────────────────────
    print("\n[deduplication]")
    pkt_id2, was_new = packet_store.store_packet_if_new(
        satellite="CUBESAT-1",
        station="test-station",
        raw_frame=BEACON_FRAME,
        source="test-dup",
    )
    test("duplicate detected", not was_new)
    test("returns original id", pkt_id2 == pkt_id, f"got {pkt_id2} vs {pkt_id}")

    count2 = packet_store.packet_count()
    test("still 1 packet after dup", count2 == 1, f"got {count2}")

    # ── 6. different frame is not a dup ────────────
    print("\n[different frame accepted]")
    different_frame = BEACON_FRAME + b"\x00"
    pkt_id3, was_new3 = packet_store.store_packet_if_new(
        satellite="CUBESAT-1",
        station="test-station",
        raw_frame=different_frame,
        source="test-new",
    )
    test("new frame accepted", was_new3)
    test("different id", pkt_id3 != pkt_id)

    count3 = packet_store.packet_count()
    test("now 2 packets", count3 == 2, f"got {count3}")

    # ── 7. telemetry storage ──────────────────────
    print("\n[telemetry storage]")
    packet_store.store_telemetry_batch(pkt_id, readings)
    series = packet_store.telemetry_series("FSM_batt_v")
    test("telemetry series returned", len(series) > 0)
    test("telemetry value correct", abs(series[0]["value"] - 3.87) < 0.01)

    # ── Summary ───────────────────────────────────
    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}\n")

    # Cleanup
    try:
        os.unlink(tmp.name)
    except OSError:
        pass

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
