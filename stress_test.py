#!/usr/bin/env python3
"""
Stress Test — Concurrent User Simulation

Simulates N browser clients each polling the web_server endpoints at the
same intervals as dashboard.js:

    /api/track   — every 5 s   (heaviest: numpy ground_track computation)
    /api/panels  — every 10 s  (medium: 4x panel compute + sliding window)
    /api/status  — every 1 s   (lightest: dict lookup + JSON)

Usage (run from a separate machine, or another terminal on the Pi):

    python stress_test.py                        # 1 user, http://localhost:5000
    python stress_test.py -n 5                   # 5 concurrent users
    python stress_test.py -n 10 --host 192.168.1.42 --duration 120
    python stress_test.py -n 5 --burst           # burst mode: all users hit simultaneously

Prints per-endpoint latency stats (min/avg/p95/max) and flags any failures.
Requires: pip install requests  (already needed by visualizer.py)
"""

import argparse
import statistics
import sys
import threading
import time
from collections import defaultdict

import requests

# ─────────────────────────────────────────────
# Simulated poll intervals matching dashboard.js
# ─────────────────────────────────────────────
ENDPOINTS = {
    "/api/track":  5.0,    # every 5 s
    "/api/panels": 10.0,   # every 10 s
    "/api/status": 1.0,    # every 1 s
}

# ─────────────────────────────────────────────
# Result collection (thread-safe)
# ─────────────────────────────────────────────
_lock = threading.Lock()
_results = defaultdict(list)   # endpoint -> list of (latency_ms, status_code)
_errors  = defaultdict(int)    # endpoint -> count of failures


def _poll(base_url, endpoint, interval, duration, user_id, burst):
    """Simulate one browser tab polling a single endpoint."""
    deadline = time.time() + duration
    while time.time() < deadline:
        t0 = time.perf_counter()
        try:
            resp = requests.get(f"{base_url}{endpoint}", timeout=30)
            latency = (time.perf_counter() - t0) * 1000  # ms
            with _lock:
                _results[endpoint].append((latency, resp.status_code))
            if resp.status_code != 200:
                with _lock:
                    _errors[endpoint] += 1
        except Exception as e:
            latency = (time.perf_counter() - t0) * 1000
            with _lock:
                _results[endpoint].append((latency, 0))
                _errors[endpoint] += 1

        if burst:
            # In burst mode, fire as fast as possible (no sleep)
            pass
        else:
            # Sleep for the remaining interval (minus time spent on request)
            elapsed = (time.perf_counter() - t0)
            sleep_for = max(0, interval - elapsed)
            time.sleep(sleep_for)


def _simulate_user(base_url, duration, user_id, burst):
    """Spawn one thread per endpoint to mimic a real browser tab."""
    threads = []
    for endpoint, interval in ENDPOINTS.items():
        t = threading.Thread(
            target=_poll,
            args=(base_url, endpoint, interval, duration, user_id, burst),
            daemon=True,
        )
        threads.append(t)
        t.start()
    return threads


def _percentile(data, p):
    """Return the p-th percentile of a sorted list."""
    if not data:
        return 0
    k = (len(data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def _print_report(n_users, duration, burst):
    """Print latency stats per endpoint."""
    print("\n" + "=" * 72)
    print(f"  STRESS TEST RESULTS — {n_users} user(s), {duration}s"
          f"{'  [BURST MODE]' if burst else ''}")
    print("=" * 72)

    total_requests = 0
    total_errors = 0

    for endpoint in ENDPOINTS:
        samples = _results.get(endpoint, [])
        errs = _errors.get(endpoint, 0)
        total_requests += len(samples)
        total_errors += errs

        if not samples:
            print(f"\n  {endpoint:20s}  — NO DATA (server down?)")
            continue

        latencies = sorted(s[0] for s in samples)
        ok_count = sum(1 for _, code in samples if code == 200)

        avg = statistics.mean(latencies)
        med = statistics.median(latencies)
        p95 = _percentile(latencies, 95)
        mn  = latencies[0]
        mx  = latencies[-1]

        print(f"\n  {endpoint}")
        print(f"    Requests : {len(samples):>6d}   (OK: {ok_count}, Errors: {errs})")
        print(f"    Latency  : min={mn:7.0f}ms  avg={avg:7.0f}ms  "
              f"med={med:7.0f}ms  p95={p95:7.0f}ms  max={mx:7.0f}ms")

        # Flag slow endpoints
        if p95 > 2000:
            print(f"    ⚠  p95 > 2s — this endpoint will feel laggy")
        elif p95 > 500:
            print(f"    ⚠  p95 > 500ms — borderline for smooth UX")

    print(f"\n  {'─' * 50}")
    print(f"  Total requests : {total_requests}")
    print(f"  Total errors   : {total_errors}")

    if total_errors > 0:
        pct = total_errors / total_requests * 100
        print(f"  Error rate     : {pct:.1f}%")
        if pct > 5:
            print(f"  ⚠  >5% error rate — server is overloaded")

    # Overall verdict
    track_samples = [s[0] for s in _results.get("/api/track", [])]
    if track_samples:
        track_p95 = _percentile(sorted(track_samples), 95)
        print(f"\n  VERDICT: ", end="")
        if track_p95 < 300 and total_errors == 0:
            print("✓ Pi handles this load comfortably")
        elif track_p95 < 1000 and total_errors / max(total_requests, 1) < 0.02:
            print("~ Acceptable, but not much headroom")
        else:
            print("✗ Server is struggling — consider upgrading")

    print("=" * 72 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Stress test the Mission Control web server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python stress_test.py                          # 1 user, localhost
  python stress_test.py -n 5 --duration 60       # 5 users, 1 minute
  python stress_test.py -n 10 --host 10.0.0.5    # 10 users, remote Pi
  python stress_test.py -n 3 --burst             # 3 users, max throughput
        """,
    )
    parser.add_argument("-n", "--users", type=int, default=1,
                        help="Number of simulated concurrent users (default: 1)")
    parser.add_argument("--host", default="localhost",
                        help="Server hostname or IP (default: localhost)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Server port (default: 5000)")
    parser.add_argument("--duration", type=int, default=30,
                        help="Test duration in seconds (default: 30)")
    parser.add_argument("--burst", action="store_true",
                        help="Burst mode: fire requests as fast as possible "
                             "(ignores poll intervals)")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"

    # Sanity check: can we reach the server?
    print(f"Connecting to {base_url} ...")
    try:
        r = requests.get(f"{base_url}/api/status", timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"ERROR: Cannot reach server at {base_url}: {e}")
        print("Make sure web_server.py is running first:")
        print(f"  python web_server.py --live --host 0.0.0.0 --port {args.port}")
        sys.exit(1)

    print(f"Server OK. Starting {args.users} simulated user(s) for {args.duration}s...\n")
    if args.burst:
        print("  ⚡ BURST MODE — no delays between requests\n")

    # Spawn all users
    all_threads = []
    for uid in range(args.users):
        threads = _simulate_user(base_url, args.duration, uid, args.burst)
        all_threads.extend(threads)
        # Stagger user starts slightly to avoid thundering herd on first request
        if not args.burst and uid < args.users - 1:
            time.sleep(0.2)

    # Progress indicator
    t_start = time.time()
    while time.time() - t_start < args.duration:
        elapsed = int(time.time() - t_start)
        total = sum(len(v) for v in _results.values())
        errs = sum(_errors.values())
        sys.stdout.write(
            f"\r  [{elapsed:>3d}/{args.duration}s]  "
            f"requests: {total}  errors: {errs}"
        )
        sys.stdout.flush()
        time.sleep(1)

    # Wait for stragglers
    print("\n\n  Waiting for in-flight requests to complete...")
    for t in all_threads:
        t.join(timeout=10)

    _print_report(args.users, args.duration, args.burst)


if __name__ == "__main__":
    main()
