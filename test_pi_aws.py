"""
Pi + AWS integration test — verifies the full sync path from the live
Lambda endpoint down to the local SQLite database.

Requires:
    export AWS_SYNC_URL="https://1avcugfjtg.execute-api.us-east-1.amazonaws.com/prod"

Run on the Pi:
    python test_pi_aws.py
"""

import base64
import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

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
# Config
# ──────────────────────────────────────────────

AWS_SYNC_URL = os.environ.get("AWS_SYNC_URL", "")
AWS_SYNC_API_KEY = os.environ.get("AWS_SYNC_API_KEY", "")

if not AWS_SYNC_URL:
    print("ERROR: Set AWS_SYNC_URL environment variable.")
    print("  export AWS_SYNC_URL=\"https://1avcugfjtg.execute-api.us-east-1.amazonaws.com/prod\"")
    sys.exit(1)


# ──────────────────────────────────────────────
# Redirect packet_store to a temp DB
# ──────────────────────────────────────────────

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()

import packet_store
packet_store._DB_PATH = Path(tmp.name)
if hasattr(packet_store._local, "conn"):
    del packet_store._local.conn

import aws_sync
import beacon_decoder


def _fetch_json(path, method="GET", body=None):
    url = f"{AWS_SYNC_URL}{path}"
    headers = {"Accept": "application/json"}
    if AWS_SYNC_API_KEY:
        headers["x-api-key"] = AWS_SYNC_API_KEY
    if body:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    else:
        data = None
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    print("\n=== Pi + AWS Integration Tests ===\n")
    print(f"  AWS endpoint: {AWS_SYNC_URL}")

    # ── 1. Fetch existing test packet from AWS ────
    print("\n[fetch from AWS]")
    try:
        resp = _fetch_json("/packets?since=2020-01-01T00:00:00Z&limit=5")
    except (URLError, OSError) as e:
        print(f"  FAIL  Could not reach AWS endpoint: {e}")
        sys.exit(1)

    packets = resp.get("packets", [])
    test("AWS returns packets", len(packets) > 0, f"got {len(packets)}")

    if not packets:
        print("\n  No packets in AWS to test with. POST a test packet first (see SETUP_CHECKLIST.md step 2).")
        sys.exit(1)

    pkt = packets[0]
    test("packet has received_at", "received_at" in pkt)
    test("packet has satellite_id", "satellite_id" in pkt)

    # ── 2. Check raw_data field exists ────────────
    print("\n[field name check]")
    raw_data = pkt.get("raw_data", "")
    data_field = pkt.get("data", "")
    has_raw = bool(raw_data) or bool(data_field)
    test("raw bytes field present", has_raw,
         f"raw_data={'yes' if raw_data else 'no'}, data={'yes' if data_field else 'no'}")

    actual_b64 = raw_data or data_field
    if actual_b64:
        try:
            raw_bytes = base64.b64decode(actual_b64)
            test("base64 decodes successfully", True)
            test("decoded bytes non-empty", len(raw_bytes) > 0, f"len={len(raw_bytes)}")
        except Exception as e:
            test("base64 decodes successfully", False, str(e))

    # ── 3. Process packet through aws_sync pipeline ─
    print("\n[aws_sync processing]")
    try:
        was_new = aws_sync._process_and_store(pkt)
        test("_process_and_store succeeded", True)
        test("packet was inserted", was_new)
    except Exception as e:
        test("_process_and_store succeeded", False, str(e))

    count = packet_store.packet_count()
    test("local DB has 1 packet", count == 1, f"got {count}")

    # ── 4. Verify byte integrity ──────────────────
    print("\n[byte integrity]")
    rows = packet_store.recent_packets(n=1)
    if rows:
        row = rows[0]
        if row.get("raw_frame") and actual_b64:
            original = base64.b64decode(actual_b64)
            test("raw bytes match after sync", row["raw_frame"] == original,
                 f"stored={len(row['raw_frame'])}B vs original={len(original)}B")
            test("frame_hash present", row.get("frame_hash") is not None)
        else:
            test("raw_frame stored", row.get("raw_frame") is not None, "raw_frame is None")
    else:
        test("packet retrievable", False, "no rows returned")

    # ── 5. Dedup: process same packet again ───────
    print("\n[dedup across sync]")
    try:
        was_new2 = aws_sync._process_and_store(pkt)
        test("duplicate rejected", not was_new2)
    except Exception as e:
        test("duplicate rejected", False, str(e))

    count2 = packet_store.packet_count()
    test("still 1 packet after dup", count2 == 1, f"got {count2}")

    # ── 6. Sync cursor logic ─────────────────────
    print("\n[sync cursor]")
    try:
        inserted = aws_sync._sync_once()
        test("_sync_once runs without error", True)
    except Exception as e:
        test("_sync_once runs without error", False, str(e))

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
