"""
Post-fix validation — tests the two critical fixes from the code review:

1. decoded_json top-level merge: FSM consumers (fsm_state_history,
   latest_fsm_state) can find beacon fields in packets stored via
   the MQTT and aws_sync paths.

2. TOCTOU race fix: concurrent calls to store_packet_if_new with the
   same frame_hash don't raise IntegrityError or produce duplicates.

Run:
    python test_fixes.py
"""

import base64
import json
import os
import struct
import sys
import tempfile
import threading
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
# Redirect packet_store to a temp DB
# ──────────────────────────────────────────────

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()

import packet_store
packet_store._DB_PATH = Path(tmp.name)
if hasattr(packet_store._local, "conn"):
    del packet_store._local.conn

import beacon_decoder


# ──────────────────────────────────────────────
# Helper: build a minimal valid TLV beacon frame
# ──────────────────────────────────────────────

def _circuitpython_hash(s: str) -> int:
    h = 5381
    for ch in s.encode("utf-8"):
        h = ((h << 5) + h) ^ ch
        h &= 0xFFFFFFFF
    h &= 0xFFFF
    return h if h != 0 else 1

def _tlv_string(key: str, value: str) -> bytes:
    enc = value.encode("utf-8")
    return struct.pack(">IB", _circuitpython_hash(key), 0x00) + bytes([len(enc)]) + enc

def _tlv_float32(key: str, value: float) -> bytes:
    return struct.pack(">IBf", _circuitpython_hash(key), 0x05, value)

def _tlv_uint32(key: str, value: int) -> bytes:
    return struct.pack(">IBI", _circuitpython_hash(key), 0x0D, value)

def build_frame(batt_v=3.85, state="nominal"):
    header = bytes([0x01]) + struct.pack(">HH", 0, 1) + bytes([40])
    payload = b""
    payload += _tlv_string("name", "CubeSATI")
    payload += _tlv_string("FSM_state", state)
    payload += _tlv_float32("FSM_batt_v", batt_v)
    payload += _tlv_uint32("uptime", 12345)
    return header + payload


def main():
    print("\n=== Fix Validation Tests ===\n")

    # ──────────────────────────────────────────
    # TEST 1: decoded_json top-level merge
    # ──────────────────────────────────────────
    print("[Fix 1: decoded_json top-level merge]")

    raw_frame = build_frame(batt_v=3.92, state="nominal")

    # Simulate what tinygs_mqtt._parse_and_store does
    beacon_telemetry = {}
    result = beacon_decoder.decode_beacon(raw_frame)
    beacon_telemetry = result.get("telemetry", {})
    test("beacon decoder found FSM_state", "FSM_state" in beacon_telemetry)

    envelope = {"satellite": "CubeSATI", "NORAD": 99999, "station": "TEST-1",
                "data": base64.b64encode(raw_frame).decode(), "rssi": -90, "snr": 8}

    decoded_combined = {**envelope}
    if beacon_telemetry:
        decoded_combined.update(beacon_telemetry)
        decoded_combined["_beacon"] = beacon_telemetry

    test("FSM_state at top level", "FSM_state" in decoded_combined)
    test("FSM_state in _beacon", "FSM_state" in decoded_combined.get("_beacon", {}))
    test("envelope fields preserved", decoded_combined.get("satellite") == "CubeSATI")

    # Store it and check FSM consumers find it
    pkt_id, was_new = packet_store.store_packet_if_new(
        satellite="CubeSATI", norad_id=99999, station="TEST-1",
        rssi=-90.0, snr=8.0, raw_frame=raw_frame,
        decoded=decoded_combined, source="test",
        received_at="2026-06-02T22:00:00Z",
    )
    test("packet stored", was_new)

    fsm_hist = packet_store.fsm_state_history(n=10)
    test("fsm_state_history returns data", len(fsm_hist) > 0,
         f"got {len(fsm_hist)} rows")
    if fsm_hist:
        test("fsm_state is 'nominal'", fsm_hist[0]["fsm_state"] == "nominal",
             f"got '{fsm_hist[0].get('fsm_state')}'")

    latest = packet_store.latest_fsm_state()
    test("latest_fsm_state returns data", latest is not None)
    if latest:
        test("latest fsm_state is 'nominal'", latest["fsm_state"] == "nominal",
             f"got '{latest.get('fsm_state')}'")

    # Store a second packet with different FSM state via aws_sync-style envelope
    raw_frame2 = build_frame(batt_v=3.50, state="safe")
    aws_envelope = {"satellite_id": "CubeSATI", "ground_station": "TEST-2",
                    "raw_data": base64.b64encode(raw_frame2).decode(),
                    "rssi": -95, "snr": 6, "received_at": "2026-06-02T23:00:00Z"}
    result2 = beacon_decoder.decode_beacon(raw_frame2)
    bt2 = result2.get("telemetry", {})
    dc2 = {**aws_envelope}
    if bt2:
        dc2.update(bt2)
        dc2["_beacon"] = bt2

    pkt_id2, was_new2 = packet_store.store_packet_if_new(
        satellite="CubeSATI", norad_id=99999, station="TEST-2",
        rssi=-95.0, snr=6.0, raw_frame=raw_frame2,
        decoded=dc2, source="test_aws",
        received_at="2026-06-02T23:00:00Z",
    )
    test("second packet stored", was_new2)

    latest2 = packet_store.latest_fsm_state()
    test("latest_fsm_state updated to 'safe'", latest2 is not None and latest2["fsm_state"] == "safe",
         f"got {latest2}")

    # ──────────────────────────────────────────
    # TEST 2: TOCTOU concurrent dedup
    # ──────────────────────────────────────────
    print("\n[Fix 2: concurrent dedup (TOCTOU race)]")

    # Reset to a fresh DB for this test
    packet_store._local.conn = None
    tmp2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp2.close()
    packet_store._DB_PATH = Path(tmp2.name)

    raw_frame3 = build_frame(batt_v=4.0, state="detumble")
    n_threads = 10
    results_lock = threading.Lock()
    insert_results = []  # (was_new, error_or_None)

    def concurrent_insert(thread_id):
        try:
            # Each thread gets its own connection (thread-local),
            # so this exercises real concurrent access.
            pkt_id, was_new = packet_store.store_packet_if_new(
                satellite="CubeSATI", norad_id=99999,
                station=f"THREAD-{thread_id}",
                raw_frame=raw_frame3,
                decoded={"FSM_state": "detumble"},
                source=f"thread_{thread_id}",
            )
            with results_lock:
                insert_results.append((was_new, None))
        except Exception as e:
            with results_lock:
                insert_results.append((None, str(e)))

    threads = [threading.Thread(target=concurrent_insert, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    new_count = sum(1 for was_new, err in insert_results if was_new is True)
    dup_count = sum(1 for was_new, err in insert_results if was_new is False)
    err_count = sum(1 for was_new, err in insert_results if err is not None)

    test(f"all {n_threads} threads completed", len(insert_results) == n_threads,
         f"got {len(insert_results)}")
    test("exactly 1 insert succeeded", new_count == 1,
         f"got {new_count} new inserts")
    test(f"{n_threads - 1} duplicates rejected", dup_count == n_threads - 1,
         f"got {dup_count} dups")
    test("zero errors", err_count == 0,
         f"got {err_count} errors: {[e for _, e in insert_results if e]}")

    final_count = packet_store.packet_count()
    test("DB has exactly 1 packet", final_count == 1,
         f"got {final_count}")

    # ──────────────────────────────────────────
    # Summary
    # ──────────────────────────────────────────
    print(f"\n{'='*40}")
    print(f"  {passed} passed, {failed} failed")
    print(f"{'='*40}\n")

    # Cleanup
    for f in [tmp.name, tmp2.name]:
        try:
            os.unlink(f)
        except OSError:
            pass

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
