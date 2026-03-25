#!/usr/bin/env python3
"""
Stress Test — Server-Side Computation Benchmark

Directly times the CPU-heavy operations that web_server.py performs on
each API request, without any network overhead.  Run this ON the Pi to
measure raw compute cost.

Timings reported:
  1. ground_track()      — numpy orbital propagation   (scales with n_points)
  2. orbital_altitude()  — numpy altitude computation
  3. Panel compute       — all four panels
  4. JSON serialisation  — jsonify-equivalent for /api/track response
  5. SQLite operations   — packet_store writes, reads, queries, concurrent R/W

Usage:
    python stress_bench.py                # default settings
    python stress_bench.py --rounds 20    # more iterations for stable averages
    python stress_bench.py --points 6000  # simulate high-res track

Requires only the existing project dependencies (numpy, flask).
"""

import argparse
import gc
import json
import random
import statistics
import sys
import time
import threading
import tracemalloc

import numpy as np

# ── Add project root so we can import the modules directly ──
sys.path.insert(0, ".")

import visualizer
import panel_altitude
import panel_signal
import panel_temperature
import panel_power
import packet_store


def _bench(func, rounds=10, label=""):
    """Run `func()` `rounds` times, return list of elapsed-ms values."""
    # Warm-up
    func()
    gc.collect()

    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    return times


def _fmt(times):
    """Return a formatted stats string."""
    s = sorted(times)
    avg = statistics.mean(s)
    med = statistics.median(s)
    mn, mx = s[0], s[-1]
    p95 = s[int(len(s) * 0.95)] if len(s) >= 5 else mx
    return (f"min={mn:7.1f}ms  avg={avg:7.1f}ms  "
            f"med={med:7.1f}ms  p95={p95:7.1f}ms  max={mx:7.1f}ms")


def main():
    parser = argparse.ArgumentParser(description="Benchmark server-side computations")
    parser.add_argument("--rounds", type=int, default=10,
                        help="Repetitions per benchmark (default: 10)")
    parser.add_argument("--points", type=int, default=550,
                        help="n_points for ground_track (default: 550, matching 1.1*500)")
    parser.add_argument("--n-orbits", type=float, default=1.1,
                        help="Orbit count to simulate (default: 1.1, matching server cap)")
    args = parser.parse_args()

    print("Fetching TLE data...")
    inc, raan, ecc, argp, sma = visualizer.get_elements()
    period = visualizer.orbital_period(sma)
    alt_km = (sma - visualizer.R_EARTH) / 1000
    orbital = {
        "sma": sma, "eccentricity": ecc, "inclination": inc,
        "raan": raan, "arg_periapsis": argp,
    }
    print(f"  SMA: {sma/1000:.1f} km  |  Period: {period:.0f}s  |  Alt: {alt_km:.1f} km\n")

    print(f"Running benchmarks ({args.rounds} rounds each, n_orbits={args.n_orbits}, "
          f"n_points={args.points})...\n")
    print("=" * 72)

    # ── 1. ground_track ──────────────────────────────
    def bench_track():
        return visualizer.ground_track(
            sma, ecc, inc, raan, argp,
            n_orbits=args.n_orbits, n_points=args.points,
        )

    t = _bench(bench_track, args.rounds, "ground_track")
    print(f"  ground_track({args.points} pts, {args.n_orbits} orbits)")
    print(f"    {_fmt(t)}")

    # Also measure the segment-splitting JSON prep (mirroring /api/track)
    def bench_track_json():
        lon, lat, t_sec = bench_track()
        segments, cur = [], []
        for i in range(len(lon)):
            if i > 0 and abs(lon[i] - lon[i - 1]) > 180:
                if len(cur) >= 2:
                    segments.append(cur)
                cur = []
            cur.append([round(float(lat[i]), 4), round(float(lon[i]), 4)])
        if len(cur) >= 2:
            segments.append(cur)
        payload = {
            "segments": segments,
            "current": [round(float(lat[-1]), 4), round(float(lon[-1]), 4)],
            "start":   [round(float(lat[0]), 4), round(float(lon[0]), 4)],
        }
        return json.dumps(payload)

    t = _bench(bench_track_json, args.rounds, "track+json")
    json_bytes = len(bench_track_json().encode())
    print(f"\n  ground_track + segment split + JSON serialisation")
    print(f"    {_fmt(t)}")
    print(f"    Payload size: {json_bytes / 1024:.1f} KB")

    # ── 2. orbital_altitude ──────────────────────────
    def bench_alt():
        return visualizer.orbital_altitude(sma, ecc, n_orbits=args.n_orbits, n_points=args.points)

    t = _bench(bench_alt, args.rounds, "orbital_altitude")
    print(f"\n  orbital_altitude({args.points} pts)")
    print(f"    {_fmt(t)}")

    # ── 3. Panel computations ────────────────────────
    panels = [
        ("panel_altitude",    panel_altitude),
        ("panel_signal",      panel_signal),
        ("panel_temperature", panel_temperature),
        ("panel_power",       panel_power),
    ]
    for name, mod in panels:
        if getattr(mod, "SOURCE", "orbital") == "telemetry":
            func = mod.compute
        else:
            func = lambda m=mod: m.compute(orbital, args.n_orbits)
        t = _bench(func, args.rounds, name)
        print(f"\n  {name}.compute()")
        print(f"    {_fmt(t)}")

    # ── 4. Full /api/panels equivalent ───────────────
    def bench_all_panels():
        result = []
        for name, mod in panels:
            if getattr(mod, "SOURCE", "orbital") == "telemetry":
                x, y = mod.compute()
            else:
                x, y = mod.compute(orbital, args.n_orbits)
            result.append({"x": list(x[:200]), "y": list(y[:200])})
        return json.dumps(result)

    t = _bench(bench_all_panels, args.rounds, "all_panels+json")
    print(f"\n  All 4 panels + JSON serialisation")
    print(f"    {_fmt(t)}")

    # ── 5. SQLite operations ─────────────────────────
    print(f"\n{'=' * 72}")
    print("  SQLITE BENCHMARKS (packet_store)")
    print("=" * 72)

    # Count existing rows for context
    existing = packet_store.packet_count()
    print(f"  Existing packets in DB: {existing}")

    # 5a. Single packet write
    def bench_write_single():
        pkt_id = packet_store.store_packet(
            satellite="STRESS-TEST",
            norad_id=99999,
            station="bench",
            frequency_mhz=437.5,
            rssi=round(random.uniform(-120, -80), 1),
            snr=round(random.uniform(0, 15), 1),
            decoded={"FSM_batt_v": round(random.uniform(3.0, 4.2), 2),
                     "FSM_acc_0": round(random.uniform(-1, 1), 3)},
            source="stress_bench",
        )
        return pkt_id

    t = _bench(bench_write_single, args.rounds, "store_packet (single)")
    print(f"\n  store_packet() — single insert")
    print(f"    {_fmt(t)}")

    # 5b. Packet + telemetry batch write (simulates a full beacon decode)
    def bench_write_batch():
        pkt_id = packet_store.store_packet(
            satellite="STRESS-TEST",
            norad_id=99999,
            station="bench",
            frequency_mhz=437.5,
            rssi=round(random.uniform(-120, -80), 1),
            snr=round(random.uniform(0, 15), 1),
            decoded={"FSM_batt_v": 3.8, "FSM_acc_0": 0.5},
            source="stress_bench",
        )
        packet_store.store_telemetry_batch(pkt_id, [
            ("FSM_batt_v",  round(random.uniform(3.0, 4.2), 2), "V"),
            ("FSM_acc_0",   round(random.uniform(-1, 1), 3),    "g"),
            ("FSM_state",   random.randint(0, 5),                ""),
            ("light_int_0", round(random.uniform(0, 1000), 1),   "lux"),
            ("light_int_1", round(random.uniform(0, 1000), 1),   "lux"),
        ])
        return pkt_id

    t = _bench(bench_write_batch, args.rounds, "store_packet + telemetry batch")
    print(f"\n  store_packet() + store_telemetry_batch(5 rows)")
    print(f"    {_fmt(t)}")

    # 5c. packet_count() — used by /api/status every 1s
    t = _bench(packet_store.packet_count, args.rounds, "packet_count")
    print(f"\n  packet_count()")
    print(f"    {_fmt(t)}")

    # 5d. recent_packets(20) — used by SocketIO _check_new_packets
    def bench_recent():
        return packet_store.recent_packets(n=20)

    t = _bench(bench_recent, args.rounds, "recent_packets(20)")
    print(f"\n  recent_packets(n=20)")
    print(f"    {_fmt(t)}")

    # 5e. recent_packets(500) — used by panel_signal.compute()
    def bench_recent_500():
        return packet_store.recent_packets(n=500)

    t = _bench(bench_recent_500, args.rounds, "recent_packets(500)")
    print(f"\n  recent_packets(n=500)")
    print(f"    {_fmt(t)}")

    # 5f. telemetry_series() — used by panel_temperature/power
    def bench_telem_series():
        return packet_store.telemetry_series("FSM_batt_v")

    t = _bench(bench_telem_series, args.rounds, "telemetry_series")
    print(f"\n  telemetry_series('FSM_batt_v')")
    print(f"    {_fmt(t)}")

    # 5g. summary() — full DB overview
    t = _bench(packet_store.summary, args.rounds, "summary")
    print(f"\n  summary()")
    print(f"    {_fmt(t)}")

    # 5h. Concurrent read/write (simulates MQTT writes during dashboard reads)
    print(f"\n  Concurrent R/W test (4 reader threads + 1 writer, {args.rounds} rounds)...")
    rw_errors = []
    rw_read_times = []
    rw_write_times = []
    barrier = threading.Barrier(5)  # 4 readers + 1 writer

    def _rw_reader():
        barrier.wait()
        for _ in range(args.rounds):
            t0 = time.perf_counter()
            try:
                packet_store.packet_count()
                packet_store.recent_packets(n=20)
            except Exception as e:
                rw_errors.append(str(e))
            rw_read_times.append((time.perf_counter() - t0) * 1000)

    def _rw_writer():
        barrier.wait()
        for _ in range(args.rounds):
            t0 = time.perf_counter()
            try:
                bench_write_batch()
            except Exception as e:
                rw_errors.append(str(e))
            rw_write_times.append((time.perf_counter() - t0) * 1000)

    threads = [threading.Thread(target=_rw_reader) for _ in range(4)]
    threads.append(threading.Thread(target=_rw_writer))
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    if rw_read_times:
        print(f"    Reads:  {_fmt(sorted(rw_read_times))}")
    if rw_write_times:
        print(f"    Writes: {_fmt(sorted(rw_write_times))}")
    if rw_errors:
        print(f"    ⚠  {len(rw_errors)} errors during concurrent R/W!")
    else:
        print(f"    ✓  No errors (WAL mode working correctly)")

    # Clean up stress test rows
    try:
        from packet_store import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM telemetry WHERE packet_id IN "
                     "(SELECT id FROM packets WHERE source = 'stress_bench')")
        conn.execute("DELETE FROM packets WHERE source = 'stress_bench'")
        conn.commit()
        print(f"\n  Cleaned up stress test rows from DB")
    except Exception as e:
        print(f"\n  ⚠  Could not clean up stress rows: {e}")

    # ── 6. Memory snapshot ───────────────────────────
    print(f"\n{'=' * 72}")
    print("  MEMORY USAGE")
    print("=" * 72)

    tracemalloc.start()
    # Simulate a burst of 10 "requests" worth of computation
    for _ in range(10):
        bench_track()
        bench_all_panels()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"  Peak memory for 10× (track + panels): {peak / 1024 / 1024:.1f} MB")

    # Estimate per-request overhead
    arr = np.empty(args.points * 3, dtype=np.float64)
    per_req_mb = arr.nbytes / 1024 / 1024
    print(f"  Estimated numpy array per request:     {per_req_mb:.2f} MB")

    print(f"\n{'=' * 72}")
    print("  PI 3B+ FEASIBILITY GUIDE")
    print("=" * 72)
    print("  Target latencies (for smooth 5s/10s polling):")
    print("    /api/track  → should be < 500ms (p95)")
    print("    /api/panels → should be < 1000ms (p95)")
    print("    /api/status → trivial (< 10ms)")
    print("  If ground_track p95 > 800ms, the Pi will struggle with multiple users.")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
