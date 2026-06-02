"""
Packet simulator — generates realistic fake CubeSAT-I beacon packets and
injects them via the MQTT pipeline or the AWS ingest endpoint.

Usage:
    # Inject locally via MQTT pipeline (no network needed):
    python simulate_packets.py --mqtt --count 20 --interval 2

    # POST to live AWS Lambda endpoint:
    python simulate_packets.py --aws --count 20 --interval 2

    # Both at once (tests dedup — same frame via both paths):
    python simulate_packets.py --mqtt --aws --count 20 --interval 2

Options:
    --count N       Number of packets to generate (default: 30)
    --interval S    Seconds between packets (default: 5)
    --burst         Send all packets immediately (no delay)
    --seed N        RNG seed for reproducibility (default: 42)
"""

import argparse
import base64
import json
import math
import os
import random
import struct
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.error import URLError
from urllib.request import Request, urlopen


# ──────────────────────────────────────────────
# TLV beacon frame builder
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


def _tlv_uint8(key: str, value: int) -> bytes:
    return struct.pack(">IBB", _circuitpython_hash(key), 0x0B, value)


def _tlv_bool_as_string(key: str, value: bool) -> bytes:
    return _tlv_string(key, str(value))


def build_beacon_frame(i: int, n: int, rng: random.Random, t0: datetime) -> tuple[bytes, dict]:
    """Build a valid TLV beacon frame with realistic time-varying telemetry.

    Returns (raw_frame_bytes, telemetry_dict).
    """
    phase = 2 * math.pi * i / max(n, 1)
    pay_set = n // 3 <= i < 2 * n // 3

    pan_light = 375 + 225 * math.sin(math.pi * i / max(n - 1, 1)) + rng.gauss(0, 15)
    pan_light = max(0, min(1023, pan_light))

    payl_light = (400 + rng.gauss(0, 30)) if pay_set else (100 + rng.gauss(0, 20))
    payl_light = max(0, min(1023, payl_light))

    magn = [
        25 * math.sin(phase) + rng.gauss(0, 1),
        25 * math.cos(phase) + rng.gauss(0, 1),
        40 + rng.gauss(0, 2),
    ]
    av = [rng.gauss(0, 0.5), rng.gauss(0, 0.5), rng.gauss(0, 0.2)]
    acc = [rng.gauss(0, 0.01), rng.gauss(0, 0.01), rng.gauss(9.8, 0.05)]
    batt_v = 3.7 + 0.2 * math.sin(phase) + rng.gauss(0, 0.02)
    best_dir = rng.randint(0, 5)
    uptime_s = i * 30
    ts = (t0 + timedelta(seconds=i * 30)).isoformat()

    telem = {
        "name": "CubeSATI",
        "FSM_state": "nominal",
        "FSM_depl": True,
        "FSM_pay_set": pay_set,
        "FSM_pan_light": pan_light,
        "FSM_payl_light": payl_light,
        "FSM_best_dir": best_dir,
        "FSM_magn_v_0": magn[0],
        "FSM_magn_v_1": magn[1],
        "FSM_magn_v_2": magn[2],
        "FSM_av_0": av[0],
        "FSM_av_1": av[1],
        "FSM_av_2": av[2],
        "FSM_acc_0": acc[0],
        "FSM_acc_1": acc[1],
        "FSM_acc_2": acc[2],
        "FSM_batt_v": batt_v,
        "time": ts,
        "uptime": uptime_s,
    }

    # PacketManager header
    header = bytes([i & 0xFF]) + struct.pack(">HH", 0, 1) + bytes([80])

    payload = b""
    payload += _tlv_string("name", "CubeSATI")
    payload += _tlv_string("FSM_state", "nominal")
    payload += _tlv_bool_as_string("FSM_depl", True)
    payload += _tlv_bool_as_string("FSM_pay_set", pay_set)
    payload += _tlv_float32("FSM_pan_light", pan_light)
    payload += _tlv_float32("FSM_payl_light", payl_light)
    payload += _tlv_uint8("FSM_best_dir", best_dir)
    payload += _tlv_float32("FSM_magn_v_0", magn[0])
    payload += _tlv_float32("FSM_magn_v_1", magn[1])
    payload += _tlv_float32("FSM_magn_v_2", magn[2])
    payload += _tlv_float32("FSM_av_0", av[0])
    payload += _tlv_float32("FSM_av_1", av[1])
    payload += _tlv_float32("FSM_av_2", av[2])
    payload += _tlv_float32("FSM_acc_0", acc[0])
    payload += _tlv_float32("FSM_acc_1", acc[1])
    payload += _tlv_float32("FSM_acc_2", acc[2])
    payload += _tlv_float32("FSM_batt_v", batt_v)
    payload += _tlv_string("time", ts)
    payload += _tlv_uint32("uptime", uptime_s)

    return header + payload, telem


def build_tinygs_envelope(raw_frame: bytes, telem: dict, rng: random.Random) -> dict:
    return {
        "satellite": "CubeSATI",
        "NORAD": 99999,
        "station": f"SIM-{rng.randint(1, 5)}",
        "frequency": 433.775,
        "rssi": -80 + rng.gauss(0, 5),
        "snr": 8 + rng.gauss(0, 2),
        "data": base64.b64encode(raw_frame).decode("ascii"),
        "crc_error": False,
        "unix_GS_time": int(time.time()),
    }


# ──────────────────────────────────────────────
# Injection targets
# ──────────────────────────────────────────────

def inject_mqtt(payload_bytes: bytes):
    from tinygs_mqtt import _parse_and_store
    _parse_and_store(payload_bytes, topic="tinygs/tele/rx")


def inject_aws(envelope: dict, aws_url: str, ingest_secret: str):
    url = f"{aws_url}/ingest/{ingest_secret}"
    data = json.dumps(envelope).encode()
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    parser = argparse.ArgumentParser(description="Simulate CubeSAT-I beacon packets")
    parser.add_argument("--mqtt", action="store_true", help="Inject via MQTT pipeline (local)")
    parser.add_argument("--aws", action="store_true", help="POST to AWS Lambda ingest endpoint")
    parser.add_argument("--count", type=int, default=30, help="Number of packets (default: 30)")
    parser.add_argument("--interval", type=float, default=5, help="Seconds between packets (default: 5)")
    parser.add_argument("--burst", action="store_true", help="Send all packets immediately")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (default: 42)")
    args = parser.parse_args()

    if not args.mqtt and not args.aws:
        parser.error("Specify at least one of --mqtt or --aws")

    aws_url = os.environ.get("AWS_SYNC_URL", "")
    ingest_secret = os.environ.get("AWS_INGEST_SECRET", "")

    if args.aws and (not aws_url or not ingest_secret):
        print("ERROR: --aws requires AWS_SYNC_URL and AWS_INGEST_SECRET env vars.")
        print('  export AWS_SYNC_URL="https://xxx.execute-api.us-east-1.amazonaws.com/prod"')
        print('  export AWS_INGEST_SECRET="your-ingest-secret"')
        sys.exit(1)

    rng = random.Random(args.seed)
    t0 = datetime.now(timezone.utc)
    n = args.count

    targets = []
    if args.mqtt:
        targets.append("MQTT")
    if args.aws:
        targets.append("AWS")

    print(f"\nSimulating {n} packets via {' + '.join(targets)}")
    print(f"  interval: {'burst' if args.burst else f'{args.interval}s'}  seed: {args.seed}")
    print()

    mqtt_ok = 0
    aws_ok = 0
    errors = 0

    for i in range(n):
        raw_frame, telem = build_beacon_frame(i, n, rng, t0)
        envelope = build_tinygs_envelope(raw_frame, telem, rng)
        payload_bytes = json.dumps(envelope).encode()

        status = f"[{i+1:3d}/{n}]  batt={telem['FSM_batt_v']:.2f}V  magn_x={telem['FSM_magn_v_0']:+.1f}µT  rssi={envelope['rssi']:.0f}dBm"

        if args.mqtt:
            try:
                inject_mqtt(payload_bytes)
                mqtt_ok += 1
                status += "  mqtt=ok"
            except Exception as e:
                errors += 1
                status += f"  mqtt=FAIL({e})"

        if args.aws:
            try:
                resp = inject_aws(envelope, aws_url, ingest_secret)
                aws_ok += 1
                status += f"  aws=ok(id={resp.get('packet_id', '?')[:8]})"
            except Exception as e:
                errors += 1
                status += f"  aws=FAIL({e})"

        print(status)

        if not args.burst and i < n - 1:
            time.sleep(args.interval)

    print(f"\n{'='*50}")
    print(f"  Done. {n} packets generated.")
    if args.mqtt:
        print(f"  MQTT:  {mqtt_ok} stored")
    if args.aws:
        print(f"  AWS:   {aws_ok} ingested")
    if errors:
        print(f"  Errors: {errors}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
