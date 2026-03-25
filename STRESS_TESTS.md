# Stress Tests

Three scripts to benchmark the Mission Control dashboard on a Pi 3B+ (or any target hardware).

## Prerequisites

The dashboard server must be running first (except for `stress_bench.py`):

```bash
source ~/missionenv/bin/activate
python web_server.py --live --host 0.0.0.0
```

---

## 1. `stress_bench.py` — CPU + DB Benchmark (run ON the Pi)

Measures raw computation cost of `ground_track()`, panel computes, JSON serialization, and SQLite operations. No network overhead. **Does NOT need the web server running.**

Benchmarks:
- Numpy orbital math (`ground_track`, `orbital_altitude`)
- All 4 panel computes + JSON serialization
- SQLite writes: single `store_packet()`, batch packet + 5 telemetry rows
- SQLite reads: `packet_count()`, `recent_packets(20)`, `recent_packets(500)`, `telemetry_series()`, `summary()`
- Concurrent R/W: 4 reader threads + 1 writer thread (simulates MQTT writes during dashboard reads)
- Peak memory usage

```bash
python stress_bench.py                # defaults (10 rounds, 550 pts, 1.1 orbits)
python stress_bench.py --rounds 20    # more iterations for stable averages
python stress_bench.py --points 6000  # stress with high-res track
```

Stress test DB rows are automatically cleaned up after each run.

## 2. `stress_test.py` — Concurrent User Simulation (run from laptop or Pi)

Simulates N browsers polling the API at real dashboard intervals (track/5s, panels/10s, status/1s). **Requires the web server running.**

With `--db-writes`, each simulated user also POSTs a fake packet + telemetry to `/api/test/write` every 2s, simulating MQTT traffic arriving while users browse.

```bash
python stress_test.py -n 1 --host <PI_IP>                # 1 user baseline
python stress_test.py -n 3 --host <PI_IP> --duration 60  # 3 users, 1 min
python stress_test.py -n 5 --host <PI_IP> --burst        # max throughput
python stress_test.py -n 3 --host <PI_IP> --db-writes    # 3 users + DB writes
```

DB write rows are automatically cleaned up after the test via `POST /api/test/cleanup`.

## 3. `stress_render.py` — Browser Rendering Test (requires a display)

Serves a test page measuring Leaflet + Chart.js render times and FPS. **Requires a real browser — not useful on a headless Pi.**

```bash
python stress_render.py --host 0.0.0.0 --server http://localhost:5000
# Then open http://<HOST_IP>:5001 in a browser
```

---

## Recommended Flow

| Step | Script | Where | What it tells you |
|------|--------|-------|-------------------|
| 1 | `stress_bench.py` | On Pi | Raw CPU + DB cost per operation |
| 2 | `stress_test.py -n 1` | Laptop → Pi | Single-user latency |
| 3 | `stress_test.py -n 3 --db-writes` | Laptop → Pi | Multi-user + DB write contention |
| 4 | `stress_test.py -n 5 --burst` | Laptop → Pi | Breaking point |
| 5 | `stress_render.py` | Pi browser | Render FPS (display required) |

## Reading the Results

- **`ground_track` p95 > 500ms** → server will feel sluggish
- **`/api/track` p95 > 2s** → page updates visibly lag
- **Error rate > 5%** → server overloaded, upgrade needed
- **Render FPS < 15** → browser struggling, GPU/CPU too weak
- **DB write p95 > 50ms** → SQLite may bottleneck under heavy MQTT traffic
- **Concurrent R/W errors > 0** → WAL mode issue, check SQLite config
