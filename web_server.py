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
from datetime import datetime, timezone
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

# ──────────────────────────────────────────────
# Mission epoch — set this to the deployment timestamp once known.
# Format: ISO-8601 with timezone, e.g. "2026-06-15T14:32:00+00:00"
# Leave as None until the satellite separates; MET will show --:--:-- until then.
# ──────────────────────────────────────────────
MISSION_EPOCH_UTC = None

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
    "tle_epoch_unix": 0.0,
    "mean_anomaly_deg": 0.0,
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
    tle_state = visualizer.get_orbital_state()
    inc = tle_state["inclination"]
    raan = tle_state["raan"]
    ecc = tle_state["eccentricity"]
    argp = tle_state["arg_periapsis"]
    sma = tle_state["semi_major_axis"]
    mean_anomaly_deg = tle_state["mean_anomaly_deg"]
    tle_epoch_unix = tle_state["epoch_unix"]

    period = visualizer.orbital_period(sma)
    alt_km = (sma - visualizer.R_EARTH) / 1000
    with _state_lock:
        _state.update(
            t0=time.time(),
            tle_epoch_unix=tle_epoch_unix,
            mean_anomaly_deg=mean_anomaly_deg,
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

HARVARD_LAT = 42.3736
HARVARD_LON = -71.1097
HARVARD_GS_ALT_M = (7 * 10 + 15) * 0.3048
HARVARD_LOOKAHEAD_ORBITS = 2.0
HARVARD_POINTS_PER_ORBIT = 1200


def _current_n_orbits():
    """Return restart-relative orbit count (kept for panel/history pacing)."""
    with _state_lock:
        t0 = _state["t0"]
        period = _state["period"]
        max_orbits = _state["n_orbits"]
    elapsed = time.time() - t0
    return min(HEAD_START_ORBITS + (elapsed * SPEED_FACTOR) / period if period > 0 else HEAD_START_ORBITS, max_orbits)


def _current_phase_orbit():
    """Return absolute orbit phase anchored to TLE epoch + mean anomaly."""
    with _state_lock:
        period = _state["period"]
        tle_epoch_unix = _state["tle_epoch_unix"]
        mean_anomaly_deg = _state["mean_anomaly_deg"]

    if period <= 0:
        return 0.0

    phase_orbits = ((time.time() - tle_epoch_unix) / period) + (mean_anomaly_deg / 360.0)
    return max(float(phase_orbits), 0.0)


def _central_angle_deg(lat_deg: np.ndarray, lon_deg: np.ndarray,
                       ref_lat_deg: float, ref_lon_deg: float) -> np.ndarray:
    """Great-circle angular separation in degrees to a reference point."""
    lat = np.radians(lat_deg)
    lon = np.radians(lon_deg)
    ref_lat = np.radians(ref_lat_deg)
    ref_lon = np.radians(ref_lon_deg)
    cos_c = (
        np.sin(ref_lat) * np.sin(lat)
        + np.cos(ref_lat) * np.cos(lat) * np.cos(lon - ref_lon)
    )
    cos_c = np.clip(cos_c, -1.0, 1.0)
    return np.degrees(np.arccos(cos_c))


def _observer_altaz(obs_lat_deg: float, obs_lon_deg: float,
                    sat_lat_deg: float, sat_lon_deg: float,
                    sat_alt_km: float, obs_alt_m: float = 0.0):
    """Convert satellite geodetic position to observer azimuth/elevation/range."""
    obs_lat = np.radians(obs_lat_deg)
    obs_lon = np.radians(obs_lon_deg)
    sat_lat = np.radians(sat_lat_deg)
    sat_lon = np.radians(sat_lon_deg)

    obs_r = visualizer.R_EARTH + obs_alt_m
    sat_r = visualizer.R_EARTH + sat_alt_km * 1000.0

    obs_x = obs_r * np.cos(obs_lat) * np.cos(obs_lon)
    obs_y = obs_r * np.cos(obs_lat) * np.sin(obs_lon)
    obs_z = obs_r * np.sin(obs_lat)

    sat_x = sat_r * np.cos(sat_lat) * np.cos(sat_lon)
    sat_y = sat_r * np.cos(sat_lat) * np.sin(sat_lon)
    sat_z = sat_r * np.sin(sat_lat)

    dx = sat_x - obs_x
    dy = sat_y - obs_y
    dz = sat_z - obs_z

    east = -np.sin(obs_lon) * dx + np.cos(obs_lon) * dy
    north = (
        -np.sin(obs_lat) * np.cos(obs_lon) * dx
        - np.sin(obs_lat) * np.sin(obs_lon) * dy
        + np.cos(obs_lat) * dz
    )
    up = (
        np.cos(obs_lat) * np.cos(obs_lon) * dx
        + np.cos(obs_lat) * np.sin(obs_lon) * dy
        + np.sin(obs_lat) * dz
    )

    horizontal = np.hypot(east, north)
    az_deg = (np.degrees(np.arctan2(east, north)) + 360.0) % 360.0
    el_deg = np.degrees(np.arctan2(up, horizontal))
    slant_range_km = np.sqrt(dx * dx + dy * dy + dz * dz) / 1000.0

    return float(az_deg), float(el_deg), float(slant_range_km)


def _observer_altaz_many(obs_lat_deg: float, obs_lon_deg: float,
                         sat_lat_deg: np.ndarray, sat_lon_deg: np.ndarray,
                         sat_alt_km: np.ndarray, obs_alt_m: float = 0.0):
    """Vectorized observer azimuth/elevation/slant-range for many satellite points."""
    obs_lat = np.radians(obs_lat_deg)
    obs_lon = np.radians(obs_lon_deg)
    sat_lat = np.radians(sat_lat_deg)
    sat_lon = np.radians(sat_lon_deg)

    obs_r = visualizer.R_EARTH + obs_alt_m
    sat_r = visualizer.R_EARTH + sat_alt_km * 1000.0

    obs_x = obs_r * np.cos(obs_lat) * np.cos(obs_lon)
    obs_y = obs_r * np.cos(obs_lat) * np.sin(obs_lon)
    obs_z = obs_r * np.sin(obs_lat)

    sat_x = sat_r * np.cos(sat_lat) * np.cos(sat_lon)
    sat_y = sat_r * np.cos(sat_lat) * np.sin(sat_lon)
    sat_z = sat_r * np.sin(sat_lat)

    dx = sat_x - obs_x
    dy = sat_y - obs_y
    dz = sat_z - obs_z

    east = -np.sin(obs_lon) * dx + np.cos(obs_lon) * dy
    north = (
        -np.sin(obs_lat) * np.cos(obs_lon) * dx
        - np.sin(obs_lat) * np.sin(obs_lon) * dy
        + np.cos(obs_lat) * dz
    )
    up = (
        np.cos(obs_lat) * np.cos(obs_lon) * dx
        + np.cos(obs_lat) * np.sin(obs_lon) * dy
        + np.sin(obs_lat) * dz
    )

    horizontal = np.hypot(east, north)
    az_deg = (np.degrees(np.arctan2(east, north)) + 360.0) % 360.0
    el_deg = np.degrees(np.arctan2(up, horizontal))
    slant_range_km = np.sqrt(dx * dx + dy * dy + dz * dz) / 1000.0

    return az_deg, el_deg, slant_range_km


# ──────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", live=_state["live"],
                           cache_bust=int(time.time()))


def _split_at_dateline(lon, lat):
    """Split lon/lat arrays into [lat, lon] segments at ±180° wrap-arounds."""
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
    return segments


FUTURE_TRACK_ORBITS = 1.0


@app.route("/api/track")
def api_track():
    """Return ground-track polyline as JSON arrays of [lat, lon] pairs."""
    n_total = _current_phase_orbit()
    n_draw = max(TRACK_WINDOW_ORBITS, 0.1)
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

    segments = _split_at_dateline(lon, lat)

    current = [round(float(lat[-1]), 4), round(float(lon[-1]), 4)]
    start = [round(float(lat[0]), 4), round(float(lon[0]), 4)]

    future_lon, future_lat, _ = visualizer.ground_track(
        sma, ecc, inc, raan, argp,
        n_orbits=FUTURE_TRACK_ORBITS,
        n_points=max(int(FUTURE_TRACK_ORBITS * 500), 300),
        start_orbit=n_total,
    )
    future_segments = _split_at_dateline(future_lon, future_lat)

    return jsonify(segments=segments, current=current, start=start,
                   future_segments=future_segments)


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
        alt_km = _state["alt_km"]
        inc = _state["inc"]
        ecc = _state["ecc"]
        period = _state["period"]

    # Mission Elapsed Time — anchored to MISSION_EPOCH_UTC, null until deployed.
    met_elapsed = None
    orbits_since_deploy = None
    if MISSION_EPOCH_UTC is not None:
        epoch = datetime.fromisoformat(MISSION_EPOCH_UTC)
        met_secs = (datetime.now(timezone.utc) - epoch).total_seconds()
        if met_secs >= 0:
            met_elapsed = round(met_secs)
            if period > 0:
                orbits_since_deploy = round(met_secs / period, 3)

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
        met_elapsed=met_elapsed,
        orbits_since_deploy=orbits_since_deploy,
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


NEXT_APPROACHES_LOOKAHEAD_ORBITS = 10.0


def _find_visible_passes(n_now, lookahead_orbits, n_points=None):
    """Find all visible passes in a lookahead window.

    Returns list of dicts, each with aos/cpa/los timing, min range,
    max elevation, and (for the first pass) the az/el path array.
    """
    if n_points is None:
        n_points = int(lookahead_orbits * HARVARD_POINTS_PER_ORBIT)

    with _state_lock:
        sma, ecc, inc = _state["sma"], _state["ecc"], _state["inc"]
        raan, argp = _state["raan"], _state["argp"]

    lon, lat, alt_km, t_sec = visualizer.ground_track_with_alt(
        sma, ecc, inc, raan, argp,
        n_orbits=lookahead_orbits,
        n_points=n_points,
        start_orbit=n_now,
    )

    separation_deg = _central_angle_deg(lat, lon, HARVARD_LAT, HARVARD_LON)
    az_arr, el_arr, slant_range_arr = _observer_altaz_many(
        HARVARD_LAT, HARVARD_LON, lat, lon, alt_km,
        obs_alt_m=HARVARD_GS_ALT_M,
    )

    vis_idx = np.where(el_arr > 0.0)[0]
    passes = []

    if len(vis_idx) == 0:
        return passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec

    cuts = np.where(np.diff(vis_idx) > 1)[0]
    seg_starts = np.concatenate(([vis_idx[0]], vis_idx[cuts + 1]))
    seg_ends = np.concatenate((vis_idx[cuts], [vis_idx[-1]]))

    for s, e in zip(seg_starts, seg_ends):
        s_i, e_i = int(s), int(e)
        seg = np.arange(s_i, e_i + 1)
        cpa_local = int(np.argmin(slant_range_arr[seg]))
        cpa_idx = int(seg[cpa_local])
        max_el_local = int(np.argmax(el_arr[seg]))
        max_el_idx = int(seg[max_el_local])

        passes.append({
            "aos_eta_sec": round(float(t_sec[s_i] - t_sec[0]), 1),
            "cpa_eta_sec": round(float(t_sec[cpa_idx] - t_sec[0]), 1),
            "los_eta_sec": round(float(t_sec[e_i] - t_sec[0]), 1),
            "min_range_km": round(float(slant_range_arr[cpa_idx]), 1),
            "max_el_deg": round(float(el_arr[max_el_idx]), 1),
            "cpa_idx": cpa_idx,
            "seg_start": s_i,
            "seg_end": e_i,
        })

    return passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec


@app.route("/api/harvard_approach")
def api_harvard_approach():
    """Return alt/az pass-arc around the next closest slant-range approach."""
    n_now = _current_phase_orbit()
    lookahead_orbits = max(float(request.args.get("n_orbits", HARVARD_LOOKAHEAD_ORBITS)), 0.25)
    n_points = int(request.args.get(
        "n_points",
        max(int(lookahead_orbits * HARVARD_POINTS_PER_ORBIT), 600),
    ))

    passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec = \
        _find_visible_passes(n_now, lookahead_orbits, n_points)

    path = []
    pass_start_eta_sec = None
    pass_end_eta_sec = None

    if passes:
        best_pass = min(passes, key=lambda p: p["min_range_km"])
        idx = best_pass["cpa_idx"]
        best_seg_start = best_pass["seg_start"]
        best_seg_end = best_pass["seg_end"]

        seg = np.arange(best_seg_start, best_seg_end + 1)
        step = max(1, len(seg) // 260)
        seg_ds = seg[::step]
        if seg_ds[-1] != seg[-1]:
            seg_ds = np.append(seg_ds, seg[-1])

        path = [
            {
                "az": round(float(az_arr[i]), 2),
                "el": round(float(el_arr[i]), 2),
                "eta_sec": round(float(t_sec[i] - t_sec[0]), 1),
            }
            for i in seg_ds
        ]

        pass_start_eta_sec = max(float(t_sec[best_seg_start] - t_sec[0]), 0.0)
        pass_end_eta_sec = max(float(t_sec[best_seg_end] - t_sec[0]), 0.0)
        visible = True
    else:
        idx = int(np.argmin(slant_range_arr))
        visible = False

    az_deg = float(az_arr[idx])
    el_deg = float(el_arr[idx])
    slant_range_km = float(slant_range_arr[idx])

    eta_sec = max(float(t_sec[idx] - t_sec[0]), 0.0)

    return jsonify(
        observer={"lat": HARVARD_LAT, "lon": HARVARD_LON},
        observer_alt_m=round(HARVARD_GS_ALT_M, 2),
        az_deg=round(az_deg, 2),
        el_deg=round(el_deg, 2),
        eta_sec=round(eta_sec, 1),
        slant_range_km=round(slant_range_km, 1),
        separation_deg=round(float(separation_deg[idx]), 2),
        approach_lat=round(float(lat[idx]), 4),
        approach_lon=round(float(lon[idx]), 4),
        approach_alt_km=round(float(alt_km[idx]), 1),
        visible=visible,
        pass_start_eta_sec=round(pass_start_eta_sec, 1) if pass_start_eta_sec is not None else None,
        pass_end_eta_sec=round(pass_end_eta_sec, 1) if pass_end_eta_sec is not None else None,
        path=path,
    )


@app.route("/api/next_approaches")
def api_next_approaches():
    """Return the current and next 3 visible passes over Harvard."""
    n_now = _current_phase_orbit()
    lookahead = float(request.args.get("n_orbits", NEXT_APPROACHES_LOOKAHEAD_ORBITS))

    passes, az_arr, el_arr, slant_range_arr, separation_deg, lat, lon, alt_km, t_sec = \
        _find_visible_passes(n_now, lookahead)

    result_passes = []
    for p in passes[:4]:
        result_passes.append({
            "aos_eta_sec": p["aos_eta_sec"],
            "cpa_eta_sec": p["cpa_eta_sec"],
            "los_eta_sec": p["los_eta_sec"],
            "min_range_km": p["min_range_km"],
            "max_el_deg": p["max_el_deg"],
        })

    current = result_passes[0] if result_passes else None
    upcoming = result_passes[1:4] if len(result_passes) > 1 else []

    return jsonify(current_pass=current, upcoming=upcoming)


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
