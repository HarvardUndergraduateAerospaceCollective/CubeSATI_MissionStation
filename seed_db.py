"""
Populate mission_data.db with fake CubeSAT-I telemetry for testing.

Scenario: 240 packets at 30-second intervals (~2 hours of a pass).
  Packets   0-79  : payload OFF  — low payl_light (~100 + noise)
  Packets  80-159 : payload ON   — high payl_light (~400 + noise, payload emitting)
  Packets 160-239 : payload OFF  — low payl_light (~100 + noise)
Panel light follows a slow sinusoid (solar illumination over the pass).
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import packet_store
from beacon_decoder import TELEMETRY_FIELDS

SEED = 0
N = 240
T0 = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)
INTERVAL = timedelta(seconds=30)


def make_telemetry(i: int, rng: np.random.Generator) -> tuple[dict, bool]:
    """Return (decoded_beacon_dict, pay_set_bool) for packet index i."""
    pay_set = 80 <= i < 160

    # Panel light: sinusoid over the pass, range ~150–600
    pan_light = 375 + 225 * np.sin(np.pi * i / (N - 1)) + rng.normal(0, 15)
    pan_light = float(np.clip(pan_light, 0, 1023))

    # Payload light: baseline ~100 when OFF, ~400 when ON
    if pay_set:
        payl_light = 400 + rng.normal(0, 30)
    else:
        payl_light = 100 + rng.normal(0, 20)
    payl_light = float(np.clip(payl_light, 0, 1023))

    # Magnetometer (µT) — slow sinusoid + noise
    phase = 2 * np.pi * i / N
    magn = [
        25 * np.sin(phase) + rng.normal(0, 1),
        25 * np.cos(phase) + rng.normal(0, 1),
        40 + rng.normal(0, 2),
    ]

    # Angular velocity (°/s) — small wobble
    av = [rng.normal(0, 0.5), rng.normal(0, 0.5), rng.normal(0, 0.2)]

    # Acceleration (m/s²) — near-zero in free-fall
    acc = [rng.normal(0, 0.01), rng.normal(0, 0.01), rng.normal(9.8, 0.05)]

    decoded = {
        "name": "CubeSATI",
        "FSM_state": "nominal",
        "FSM_depl": True,
        "FSM_pay_set": pay_set,
        "FSM_pan_light": pan_light,
        "FSM_payl_light": payl_light,
        "FSM_best_dir": int(rng.integers(0, 6)),
        "FSM_magn_v_0": float(magn[0]),
        "FSM_magn_v_1": float(magn[1]),
        "FSM_magn_v_2": float(magn[2]),
        "FSM_av_0": float(av[0]),
        "FSM_av_1": float(av[1]),
        "FSM_av_2": float(av[2]),
        "FSM_acc_0": float(acc[0]),
        "FSM_acc_1": float(acc[1]),
        "FSM_acc_2": float(acc[2]),
        "FSM_batt_v": float(3.7 + 0.2 * np.sin(phase) + rng.normal(0, 0.02)),
        "time": (T0 + i * INTERVAL).isoformat(),
        "uptime": i * 30,
    }
    return decoded, pay_set


def main():
    rng = np.random.default_rng(SEED)

    # Confirm DB is writable
    db_path = Path(__file__).parent / "mission_data.db"
    print(f"Writing to {db_path}")

    for i in range(N):
        ts = (T0 + i * INTERVAL).isoformat()
        decoded, pay_set = make_telemetry(i, rng)

        packet_id = packet_store.store_packet(
            satellite="CubeSATI",
            norad_id=99999,
            station="SEED",
            frequency_mhz=433.775,
            rssi=float(-80 + rng.normal(0, 5)),
            snr=float(8 + rng.normal(0, 2)),
            decoded=decoded,
            source="seed",
            received_at=ts,
        )

        # Store numeric telemetry fields (mirrors what tinygs_mqtt.py would do)
        readings = [
            ("FSM_pan_light",  decoded["FSM_pan_light"],  ""),
            ("FSM_payl_light", decoded["FSM_payl_light"], ""),
            ("FSM_best_dir",   decoded["FSM_best_dir"],   ""),
            ("FSM_magn_v_0",   decoded["FSM_magn_v_0"],   "µT"),
            ("FSM_magn_v_1",   decoded["FSM_magn_v_1"],   "µT"),
            ("FSM_magn_v_2",   decoded["FSM_magn_v_2"],   "µT"),
            ("FSM_av_0",       decoded["FSM_av_0"],        "°/s"),
            ("FSM_av_1",       decoded["FSM_av_1"],        "°/s"),
            ("FSM_av_2",       decoded["FSM_av_2"],        "°/s"),
            ("FSM_acc_0",      decoded["FSM_acc_0"],       "m/s²"),
            ("FSM_acc_1",      decoded["FSM_acc_1"],       "m/s²"),
            ("FSM_acc_2",      decoded["FSM_acc_2"],       "m/s²"),
            ("FSM_batt_v",     decoded["FSM_batt_v"],      "V"),
            ("uptime",         decoded["uptime"],          "s"),
        ]
        packet_store.store_telemetry_batch(packet_id, readings, timestamp=ts)

    summary = packet_store.summary()
    print(f"Done. {summary['total_packets']} packets, "
          f"{summary['total_telemetry_rows']} telemetry rows.")
    print(f"Time range: {summary['earliest_packet']}  to  {summary['latest_packet']}")


if __name__ == "__main__":
    main()
