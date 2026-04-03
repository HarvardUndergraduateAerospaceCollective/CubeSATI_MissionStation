"""
Mission Control — Web Dashboard Server

Replaces the matplotlib-based missioncontrol.py with a Flask + SocketIO
server that feeds a Leaflet/Chart.js browser frontend.

Usage::

    python web_server.py                  # offline mode (orbital only)
    python web_server.py --live           # live MQTT + orbital
    python web_server.py --port 8080      # custom port

Then open http://localhost:5000 (or your Pi's IP) in a browser.
"""

import argparse
import logging
import time
from threading import Lock

import numpy as np
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

import visualizer
import panel_altitude
import panel_signal
import panel_temperature
import panel_power
import panel_magnetometer
import panel_best_dir
import packet_store

log = logging.getLogger(__name__)
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = "missioncontrol"
socketio = SocketIO(app, async_mode="threading")

# ──────────────────────────────────────────────
# Orbital state (computed once at startup, refreshed on demand)
# ──────────────────────────────────────────────

_state_lock = Lock()
_state = {
    "t0": 0.0,
    "live": False,
    "n_orbits": 3.0,
    "orbital": {},
    "period": 0.0,
    "alt_km": 0.0,
    "inc": 0.0,
    "raan": 0.0,
    "ecc": 0.0,
    "argp": 0.0,
    "sma": 0.0,
}


def _init_orbital():
    """Fetch TLE and populate global orbital state."""
    inc, raan, ecc, argp, sma = visualizer.get_elements()
    period = visualizer.orbital_period(sma)
    alt_km = (sma - visualizer.R_EARTH) / 1000
    with _state_lock:
        _state.update(
            t0=time.time(),
            inc=inc, raan=raan, ecc=ecc, argp=argp, sma=sma,
            period=period,
            alt_km=alt_km,
            orbital={
                "sma": sma,
                "eccentricity": ecc,
                "inclination": inc,
                "raan": raan,
                "arg_periapsis": argp,
            },
        )


# Head-start: show 1 orbit of history on first load, then grow in real time.
HEAD_START_ORBITS = 1.0
SPEED_FACTOR = 1.0       # 1.0 = real-time (1 orbital period → 1 new orbit drawn)
TRACK_WINDOW_ORBITS = 1.1

# Per-panel sliding window in minutes (None = show all data).
# Tune these once you know what looks right for each panel.
PANEL_WINDOWS = {
    "panel_altitude":      240,    # last 4 hours
    "panel_signal":        240,    # last 4 hours
    "panel_temperature":   2880,   # gyroscope — last 2 days
    "panel_power":         2880,   # last 2 days
    "panel_magnetometer":  2880,   # last 2 days
}


def _current_n_orbits():
    """Return n_orbits that grows with real elapsed time, matching missioncontrol.py."""
    with _state_lock:
        t0 = _state["t0"]
        period = _state["period"]
        max_orbits = _state["n_orbits"]
    elapsed = time.time() - t0
    return min(HEAD_START_ORBITS + (elapsed * SPEED_FACTOR) / period if period > 0 else HEAD_START_ORBITS, max_orbits)


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", live=_state["live"],
                           cache_bust=int(time.time()))


@app.route("/api/track")
def api_track():
    """Return ground-track polyline as JSON arrays of [lat, lon] pairs."""
    n_total = _current_n_orbits()
    n_draw = min(n_total, TRACK_WINDOW_ORBITS)
    start_orbit = max(n_total - n_draw, 0.0)
    n_points = int(request.args.get("n_points", max(int(n_draw * 500), 200)))

    with _state_lock:
        sma, ecc, inc = _state["sma"], _state["ecc"], _state["inc"]
        raan, argp = _state["raan"], _state["argp"]

    lon, lat, t_sec = visualizer.ground_track(
        sma, ecc, inc, raan, argp,
        n_orbits=n_draw,
        n_points=n_points,
        start_orbit=start_orbit,
    )

    # Split at ±180° wrap-arounds for Leaflet polyline segments
    segments = []
    cur = []
    for i in range(len(lon)):
        if i > 0 and abs(lon[i] - lon[i - 1]) > 180:
            if len(cur) >= 2:
                segments.append(cur)
            cur = []
        cur.append([round(float(lat[i]), 4), round(float(lon[i]), 4)])
    if len(cur) >= 2:
        segments.append(cur)

    # Current position = last point
    current = [round(float(lat[-1]), 4), round(float(lon[-1]), 4)]
    start = [round(float(lat[0]), 4), round(float(lon[0]), 4)]

    return jsonify(segments=segments, current=current, start=start)


@app.route("/api/panels")
def api_panels():
    """Return data for all four side panels."""
    n = _current_n_orbits()

    with _state_lock:
        orbital = _state["orbital"]

    panels = []
    for mod in [panel_altitude, panel_signal, panel_temperature, panel_power, panel_magnetometer]:
        if getattr(mod, "SOURCE", "orbital") == "telemetry":
            x, y = mod.compute()
        else:
            x, y = mod.compute(orbital, n)

        # Sliding window: keep only the last N minutes of data
        window_min = PANEL_WINDOWS.get(mod.__name__)
        if window_min is not None and len(x) > 0:
            cutoff = x[-1] - window_min
            mask = x >= cutoff
            x, y = x[mask], y[mask]

        if len(x) == 0:
            panels.append({
                "title": mod.TITLE,
                "color": mod.COLOR,
                "ylabel": mod.Y_LABEL,
                "x": [],
                "y": [],
            })
        else:
            # Downsample for the browser if too many points
            step = max(1, len(x) // 300)
            panels.append({
                "title": mod.TITLE,
                "color": mod.COLOR,
                "ylabel": mod.Y_LABEL,
                "x": [round(float(v), 3) for v in x[::step]],
                "y": [round(float(v), 3) for v in y[::step]],
            })

    return jsonify(panels=panels)


@app.route("/api/status")
def api_status():
    """Return HUD info: orbital params, MET, packet count."""
    with _state_lock:
        t0 = _state["t0"]
        alt_km = _state["alt_km"]
        inc = _state["inc"]
        ecc = _state["ecc"]
        period = _state["period"]

    elapsed = time.time() - t0
    n_now = _current_n_orbits()

    try:
        n_pkts = packet_store.packet_count()
    except Exception:
        n_pkts = 0

    # Latest FSM state for HUD
    fsm = packet_store.latest_fsm_state()
    fsm_state = fsm["fsm_state"] if fsm else "—"
    fsm_depl = fsm["fsm_depl"] if fsm else "—"
    fsm_uptime = fsm["uptime"] if fsm else "—"

    return jsonify(
        alt_km=round(alt_km, 1),
        inc=round(inc, 2),
        ecc=round(ecc, 6),
        period_min=round(period / 60, 1),
        n_orbits=round(n_now, 3),
        elapsed=round(elapsed),
        n_pkts=n_pkts,
        live=_state["live"],
        fsm_state=fsm_state,
        fsm_depl=fsm_depl,
        fsm_uptime=fsm_uptime,
    )


# ──────────────────────────────────────────────
# FSM state timeline endpoint
# ──────────────────────────────────────────────

@app.route("/api/fsm")
def api_fsm():
    """Return FSM state history for the timeline panel."""
    history = packet_store.fsm_state_history(n=200)
    return jsonify(history=history)


@app.route("/api/best_dir")
def api_best_dir():
    """Return best-direction distribution for the doughnut chart."""
    return jsonify(panel_best_dir.compute())


# ──────────────────────────────────────────────
# Test-only endpoint: simulate a packet write (for stress testing)
# ──────────────────────────────────────────────

import random as _random

@app.route("/api/test/write", methods=["POST"])
def api_test_write():
    """Insert a fake packet + telemetry rows (stress test only)."""
    _fsm_states = ["nominal", "safe", "detumble", "deploy", "standby"]
    pkt_id = packet_store.store_packet(
        satellite="STRESS-TEST",
        norad_id=99999,
        station="stress-client",
        frequency_mhz=437.5,
        rssi=round(_random.uniform(-120, -80), 1),
        snr=round(_random.uniform(0, 15), 1),
        decoded={
            "FSM_state": _random.choice(_fsm_states),
            "FSM_depl": _random.choice([True, False]),
            "FSM_batt_v": round(_random.uniform(3.0, 4.2), 2),
            "uptime": _random.randint(100, 100000),
        },
        source="stress_test",
    )
    packet_store.store_telemetry_batch(pkt_id, [
        ("FSM_batt_v",   round(_random.uniform(3.0, 4.2), 2),   "V"),
        ("FSM_magn_v_0", round(_random.uniform(-50, 50), 2),     "µT"),
        ("FSM_av_0",     round(_random.uniform(-10, 10), 3),     "°/s"),
        ("FSM_best_dir", _random.choices([0, 1, 2, 3, -1], weights=[4, 2, 3, 2, 1])[0], ""),
    ])
    return jsonify(ok=True, packet_id=pkt_id)


@app.route("/api/test/cleanup", methods=["POST"])
def api_test_cleanup():
    """Remove all rows inserted by stress tests."""
    try:
        from packet_store import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM telemetry WHERE packet_id IN "
                     "(SELECT id FROM packets WHERE source IN ('stress_test', 'stress_bench'))")
        conn.execute("DELETE FROM packets WHERE source IN ('stress_test', 'stress_bench')")
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# ──────────────────────────────────────────────
# SocketIO: push new packets to browser in real time
# ──────────────────────────────────────────────

_last_pkt_id = [0]


def _check_new_packets():
    """Periodically check for new packets and push to connected clients."""
    while True:
        socketio.sleep(5)
        try:
            pkts = packet_store.recent_packets(n=5)
            if pkts and pkts[0]["id"] > _last_pkt_id[0]:
                _last_pkt_id[0] = pkts[0]["id"]
                socketio.emit("new_packets", {
                    "count": packet_store.packet_count(),
                    "latest": pkts[0]["received_at"],
                })
        except Exception:
            pass


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Mission Control Web Dashboard")
    parser.add_argument("--live", action="store_true",
                        help="Enable live MQTT listener")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--n-orbits", type=float, default=3.0,
                        help="Default number of orbits to display")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (0.0.0.0 for network access)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-20s  %(message)s")

    _state["live"] = args.live
    _state["n_orbits"] = args.n_orbits

    log.info("Fetching orbital elements from CelesTrak...")
    _init_orbital()
    log.info("Orbital data ready  (period=%.1f min, alt=%.0f km)",
             _state["period"] / 60, _state["alt_km"])

    if args.live:
        import tinygs_mqtt
        tinygs_mqtt.start_listener()
        socketio.start_background_task(_check_new_packets)
        log.info("MQTT listener started")

    log.info("Dashboard at  http://localhost:%d", args.port)
    socketio.run(app, host=args.host, port=args.port,
                 debug=False, use_reloader=False, log_output=False)


if __name__ == "__main__":
    main()
