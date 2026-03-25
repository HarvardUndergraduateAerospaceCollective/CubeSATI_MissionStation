# Stress Tests

Three scripts to benchmark the Mission Control dashboard on a Pi 3B+ (or any target hardware).

## Prerequisites

The dashboard server must be running first (except for `stress_bench.py`):

```bash
source ~/missionenv/bin/activate
python web_server.py --live --host 0.0.0.0
```

---

## 1. `stress_bench.py` — CPU Benchmark (run ON the Pi)

Measures raw computation cost of `ground_track()`, panel computes, and JSON serialization. No network overhead. **Does NOT need the web server running.**

```bash
python stress_bench.py                # defaults (10 rounds, 550 pts, 1.1 orbits)
python stress_bench.py --rounds 20    # more iterations for stable averages
python stress_bench.py --points 6000  # stress with high-res track
```

## 2. `stress_test.py` — Concurrent User Simulation (run from laptop or Pi)

Simulates N browsers polling the API at real dashboard intervals (track/5s, panels/10s, status/1s). **Requires the web server running.**

```bash
python stress_test.py -n 1 --host <PI_IP>                # 1 user baseline
python stress_test.py -n 3 --host <PI_IP> --duration 60  # 3 users, 1 min
python stress_test.py -n 5 --host <PI_IP> --burst        # max throughput
```

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
| 1 | `stress_bench.py` | On Pi | Raw CPU cost per request |
| 2 | `stress_test.py -n 1` | Laptop → Pi | Single-user latency |
| 3 | `stress_test.py -n 3` | Laptop → Pi | Multi-user scalability |
| 4 | `stress_test.py -n 5 --burst` | Laptop → Pi | Breaking point |
| 5 | `stress_render.py` | Pi browser | Render FPS (display required) |

## Reading the Results

- **`ground_track` p95 > 500ms** → server will feel sluggish
- **`/api/track` p95 > 2s** → page updates visibly lag
- **Error rate > 5%** → server overloaded, upgrade needed
- **Render FPS < 15** → browser struggling, GPU/CPU too weak
