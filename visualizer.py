"""
Satellite Orbit Visualizer — 2D Equirectangular Projection
Displays a ground-track of a satellite orbit over a styled Earth map,
inspired by mission-control displays.
"""

import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.ticker as mticker
import matplotlib.animation as animation
from PIL import Image

# ──────────────────────────────────────────────
# Live-data flag (toggle this to enable/disable
# the blinking "LIVE" indicator in the top-right)
# ──────────────────────────────────────────────
LIVE_MODE = True

# ──────────────────────────────────────────────
# Orbital mechanics helpers (simplified Keplerian)
# ──────────────────────────────────────────────

MU_EARTH = 3.986004418e14          # m^3 s^-2
R_EARTH = 6_371_000                # m
EARTH_ROT_RATE = 7.2921159e-5      # rad/s
DEG = np.degrees
RAD = np.radians


def kepler_equation(M: np.ndarray, e: float, tol: float = 1e-10) -> np.ndarray:
    """Solve Kepler's equation  M = E - e·sin(E)  via Newton-Raphson."""
    E = M.copy()
    for _ in range(50):
        dE = (E - e * np.sin(E) - M) / (1.0 - e * np.cos(E))
        E -= dE
        if np.max(np.abs(dE)) < tol:
            break
    return E


def ground_track(
    semi_major_axis: float,   # metres
    eccentricity: float,
    inclination: float,       # degrees
    raan: float,              # degrees – Right Ascension of Ascending Node
    arg_periapsis: float,     # degrees
    n_orbits: float = 3,
    n_points: int = 3000,
):
    """
    Return (longitude, latitude) arrays of the satellite's sub-satellite
    point for *n_orbits* complete orbits, sampled at *n_points* points.
    """
    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = 2 * np.pi * np.sqrt(semi_major_axis ** 3 / MU_EARTH)
    t = np.linspace(0, n_orbits * period, n_points)
    M = 2 * np.pi / period * t          # mean anomaly
    E = kepler_equation(M, eccentricity)  # eccentric anomaly

    # True anomaly
    nu = 2 * np.arctan2(
        np.sqrt(1 + eccentricity) * np.sin(E / 2),
        np.sqrt(1 - eccentricity) * np.cos(E / 2),
    )

    # Argument of latitude
    u = omega + nu

    # Position in the orbital plane → ECI-like lat/lon
    lat = np.arcsin(np.sin(inc) * np.sin(u))
    lon = np.arctan2(
        np.sin(u) * np.cos(inc),
        np.cos(u),
    ) + Omega

    # Subtract Earth rotation to get ground-track longitude
    lon -= EARTH_ROT_RATE * t

    # Wrap to [-180, 180]
    lon = DEG(lon)
    lat = DEG(lat)
    lon = (lon + 180) % 360 - 180

    return lon, lat, t


# ──────────────────────────────────────────────
# Earth map drawing (equirectangular photo)
# ──────────────────────────────────────────────

# Path to the equirectangular projection image (next to this script)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_EARTH_IMG_PATH = os.path.join(_SCRIPT_DIR, "Equirectangular_projection_SW.jpg")


def draw_earth(ax):
    """Display an equirectangular Earth photo on *ax*."""
    ax.set_facecolor("#0a1628")

    # Load and display the satellite photo
    img = Image.open(_EARTH_IMG_PATH)
    ax.imshow(img, extent=[-180, 180, -90, 90], aspect="auto", zorder=0)

    # Subtle grid lines on top of the image
    for lat in range(-90, 91, 30):
        ax.axhline(lat, color="white", linewidth=0.25, alpha=0.2, zorder=1)
    for lon in range(-180, 181, 30):
        ax.axvline(lon, color="white", linewidth=0.25, alpha=0.2, zorder=1)

    # Equator / Prime Meridian highlights
    ax.axhline(0, color="#ffcc00", linewidth=0.5, linestyle="--", alpha=0.35, zorder=1)
    ax.axvline(0, color="#ffcc00", linewidth=0.5, linestyle="--", alpha=0.35, zorder=1)


# ──────────────────────────────────────────────
# Main visualisation
# ──────────────────────────────────────────────

def plot_orbit(
    semi_major_axis,   # ISS-like (~410 km altitude)
    eccentricity,
    inclination,
    raan,
    arg_periapsis,
    n_orbits,
    live,
    head_start_orbits
):
    """Create and display the ground-track visualisation.

    If *live* is True the track grows continuously in real time.
    *head_start_orbits* adds an initial offset so the map isn't empty at launch.
    """
    import time as _time
    from matplotlib.animation import FuncAnimation

    period = 2 * np.pi * np.sqrt(semi_major_axis ** 3 / MU_EARTH)
    alt_km = (semi_major_axis - R_EARTH) / 1000
    t0 = _time.time()

    # Custom neon-glow colour map
    cmap = LinearSegmentedColormap.from_list(
        "orbit", ["#00ccff", "#00ffcc", "#ffcc00", "#ff4400"]
    )

    # ── figure ──
    fig = plt.figure(figsize=(16, 8.5), facecolor="#060e1a")
    ax = fig.add_axes([0.06, 0.08, 0.88, 0.82])
    draw_earth(ax)

    # ── axis styling ──
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)
    ax.set_xlabel("Longitude (°)", color="#8899aa", fontsize=10, fontfamily="monospace")
    ax.set_ylabel("Latitude (°)", color="#8899aa", fontsize=10, fontfamily="monospace")
    ax.xaxis.set_major_locator(mticker.MultipleLocator(30))
    ax.yaxis.set_major_locator(mticker.MultipleLocator(30))
    ax.tick_params(colors="#667788", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#1a3a50")
    ax.set_aspect("equal")

    # ── title ──
    fig.text(
        0.50, 0.95,
        "SATELLITE  GROUND  TRACK",
        ha="center", va="center", fontsize=16, fontweight="bold",
        color="#00ddff", fontfamily="monospace",
    )

    # HUD info line (updated each frame in live mode)
    hud_text = fig.text(
        0.50, 0.92, "",
        ha="center", va="center", fontsize=9,
        color="#55aa88", fontfamily="monospace",
    )

    # Mission Elapsed Time display (top-left)
    met_text = fig.text(
        0.06, 0.95, "MET 00:00:00",
        ha="left", va="center", fontsize=11, fontweight="bold",
        color="#ff4444", fontfamily="monospace",
    )

    # Persistent artist handles we update each frame
    sat_marker, = ax.plot([], [], marker="o", markersize=8, color="#ff3333",
                          markeredgecolor="white", markeredgewidth=1.2, zorder=10)
    sat_label = ax.annotate(
        "HUCSAT", (0, 0),
        textcoords="offset points", xytext=(10, 8),
        fontsize=9, fontweight="bold", color="#ff6666",
        fontfamily="monospace", zorder=10,
    )
    start_marker, = ax.plot([], [], marker="D", markersize=6, color="#00ccff",
                            markeredgecolor="white", markeredgewidth=0.8, zorder=10)

    # Legend
    legend_elements = [
        mpatches.Patch(facecolor="#00ccff", edgecolor="white", label="Start"),
        mpatches.Patch(facecolor="#ff3333", edgecolor="white", label="Current Pos"),
    ]
    ax.legend(handles=legend_elements, loc="lower left", fontsize=8,
              facecolor="#0d1b2a", edgecolor="#1a3a50", labelcolor="#8899aa",
              framealpha=0.8)

    # ── LIVE / OFFLINE indicator (top-right corner) ──
    if live:
        fig.text(
            0.96, 0.95, "LIVE",
            ha="right", va="center", fontsize=11, fontweight="bold",
            color="#ff4444", fontfamily="monospace",
        )
        live_dot = fig.text(
            0.930, 0.9515, "\u25CF",
            ha="center", va="center", fontsize=13,
            color="#ff0000", fontfamily="monospace",
        )
    else:
        fig.text(
            0.96, 0.95, "OFFLINE",
            ha="right", va="center", fontsize=10, fontweight="bold",
            color="#556666", fontfamily="monospace",
        )
        fig.text(
            0.925, 0.95, "\u25CB",
            ha="center", va="center", fontsize=13,
            color="#556666", fontfamily="monospace",
        )
        live_dot = None

    # Container for LineCollection artists (cleared each redraw)
    track_artists = []

    # ── helpers ──
    def _build_segments(lon, lat):
        """Split the track at ±180° wrap-arounds."""
        segments, seg_t = [], []
        cur_lon, cur_lat, cur_t = [lon[0]], [lat[0]], [0.0]
        for i in range(1, len(lon)):
            if abs(lon[i] - lon[i - 1]) > 180:
                segments.append((np.array(cur_lon), np.array(cur_lat)))
                seg_t.append(np.array(cur_t))
                cur_lon, cur_lat, cur_t = [lon[i]], [lat[i]], [i / len(lon)]
            else:
                cur_lon.append(lon[i])
                cur_lat.append(lat[i])
                cur_t.append(i / len(lon))
        segments.append((np.array(cur_lon), np.array(cur_lat)))
        seg_t.append(np.array(cur_t))
        return segments, seg_t

    def _draw_track(n_now):
        """Recompute and redraw the ground track for *n_now* orbits."""
        # Remove previous line collections
        for a in track_artists:
            a.remove()
        track_artists.clear()

        lon, lat, _t = ground_track(
            semi_major_axis, eccentricity, inclination, raan, arg_periapsis,
            n_orbits=n_now, n_points=max(int(n_now * 1500), 500),
        )

        segments, seg_t = _build_segments(lon, lat)
        for (sx, sy), st in zip(segments, seg_t):
            if len(sx) < 2:
                continue
            pts = np.column_stack([sx, sy]).reshape(-1, 1, 2)
            segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
            lc = LineCollection(segs, cmap=cmap, norm=plt.Normalize(0, 1),
                                linewidths=1.8, alpha=0.95, zorder=5)
            lc.set_array(st[:-1])
            ax.add_collection(lc)
            track_artists.append(lc)

        # Update markers
        sat_marker.set_data([lon[-1]], [lat[-1]])
        sat_label.xy = (lon[-1], lat[-1])
        start_marker.set_data([lon[0]], [lat[0]])

        # Update HUD
        hud_text.set_text(
            f"ALT: {alt_km:.0f} km   "
            f"INC: {inclination:.1f}\u00b0   "
            f"ECC: {eccentricity:.4f}   "
            f"PERIOD: {period / 60:.1f} min   "
            f"ORBITS: {n_now:.2f}"
        )

    # ═══════════════════════════════════════════
    # Static mode — draw once and show
    # ═══════════════════════════════════════════
    if not live:
        _draw_track(n_orbits)
        plt.show()
        return

    # ═══════════════════════════════════════════
    # Live mode — incremental track drawing
    # ═══════════════════════════════════════════
    speed_factor = 1.0  # Real-time (set higher to speed up for testing)
    last_n = [head_start_orbits]  # track how far we've drawn so far

    # Draw the initial head-start portion
    if head_start_orbits > 0:
        lon_init, lat_init, _ = ground_track(
            semi_major_axis, eccentricity, inclination, raan, arg_periapsis,
            n_orbits=head_start_orbits,
            n_points=max(int(head_start_orbits * 1500), 500),
        )
        # Build initial segments
        segments, seg_t = _build_segments(lon_init, lat_init)
        for (sx, sy), st in zip(segments, seg_t):
            if len(sx) < 2:
                continue
            pts = np.column_stack([sx, sy]).reshape(-1, 1, 2)
            segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
            lc = LineCollection(segs, cmap=cmap, norm=plt.Normalize(0, 1),
                                linewidths=1.8, alpha=0.95, zorder=5)
            lc.set_array(st[:-1])
            ax.add_collection(lc)
            track_artists.append(lc)
        # Set initial markers
        sat_marker.set_data([lon_init[-1]], [lat_init[-1]])
        sat_label.xy = (lon_init[-1], lat_init[-1])
        start_marker.set_data([lon_init[0]], [lat_init[0]])
        hud_text.set_text(
            f"ALT: {alt_km:.0f} km   "
            f"INC: {inclination:.1f}\u00b0   "
            f"ECC: {eccentricity:.4f}   "
            f"PERIOD: {period / 60:.1f} min   "
            f"ORBITS: {head_start_orbits:.2f}"
        )

    def _update(frame):
        elapsed = _time.time() - t0
        n_now = head_start_orbits + (elapsed * speed_factor) / period
        n_prev = last_n[0]

        if n_now - n_prev < 1e-6:
            return

        # Compute the full track up to n_now (needed for correct positions)
        total_pts = max(int(n_now * 1500), 500)
        lon, lat, _t = ground_track(
            semi_major_axis, eccentricity, inclination, raan, arg_periapsis,
            n_orbits=n_now, n_points=total_pts,
        )

        # Only draw the NEW slice since last frame
        frac = n_prev / n_now if n_now > 0 else 0
        start_idx = max(int(frac * len(lon)) - 2, 0)  # small overlap

        new_lon = lon[start_idx:]
        new_lat = lat[start_idx:]

        if len(new_lon) >= 2:
            points = np.column_stack([new_lon, new_lat]).reshape(-1, 1, 2)
            segs = np.concatenate([points[:-1], points[1:]], axis=1)

            # Filter out wrap-around jumps at ±180°
            diffs = np.abs(segs[:, 1, 0] - segs[:, 0, 0])
            mask = diffs < 180
            segs = segs[mask]

            if len(segs) > 0:
                t_vals = np.linspace(n_prev / n_now, 1.0, len(segs))
                lc = LineCollection(segs, cmap=cmap, norm=plt.Normalize(0, 1),
                                    linewidths=1.8, alpha=0.95, zorder=5)
                lc.set_array(t_vals)
                ax.add_collection(lc)
                track_artists.append(lc)

        # Update satellite position marker
        sat_marker.set_data([lon[-1]], [lat[-1]])
        sat_label.xy = (lon[-1], lat[-1])

        # Update HUD
        hud_text.set_text(
            f"ALT: {alt_km:.0f} km   "
            f"INC: {inclination:.1f}\u00b0   "
            f"ECC: {eccentricity:.4f}   "
            f"PERIOD: {period / 60:.1f} min   "
            f"ORBITS: {n_now:.2f}"
        )

        # Blink LIVE dot is handled by a separate timer below

        last_n[0] = n_now

    fig._live_anim = FuncAnimation(
        fig, _update, interval=20_000, cache_frame_data=False,
    )

    # Separate timer for LIVE dot blink + MET clock — runs at ~1 Hz
    blink_state = [True]

    def _tick(frame):
        # Update MET
        elapsed = _time.time() - t0
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        s = int(elapsed % 60)
        met_text.set_text(f"MET {h:02d}:{m:02d}:{s:02d}")

        # Blink LIVE dot
        if live_dot is not None:
            blink_state[0] = not blink_state[0]
            live_dot.set_alpha(1.0 if blink_state[0] else 0.0)

    fig._tick_anim = FuncAnimation(
        fig, _tick, interval=600, cache_frame_data=False,
    )

    plt.show()

def get_elements():
    url = "https://celestrak.org/NORAD/elements/gp.php?CATNR=900&FORMAT=TLE"
    import requests
    response = requests.get(url)
    if response.status_code == 200:
        tle_data = response.text.strip().splitlines()
    else:
        raise Exception(f"Failed to fetch TLE data: {response.status_code}")
    

    line2 = tle_data[2]
    # Parse the TLE line to extract orbital elements
    inclination = float(line2[8:16].strip())
    raan = float(line2[17:25].strip())
    eccentricity = float("0." + line2[26:33].strip())
    arg_periapsis = float(line2[34:42].strip())
    mean_motion = float(line2[52:63].strip())
    # Calculate semi-major axis from mean motion
    sma = (MU_EARTH / (mean_motion * 2 * np.pi / 86400) ** 2) ** (1/3)

    alt_km = (sma - R_EARTH) / 1000
    #print(f"[TLE] {tle_data[0].strip()}")
    #print(f"  INC: {inclination:.2f}°  RAAN: {raan:.2f}°  ECC: {eccentricity:.7f}")
    #print(f"  ARG_P: {arg_periapsis:.2f}°  MEAN_MOTION: {mean_motion:.8f} rev/day")
    #print(f"  SMA: {sma:.0f} m  ALT: {alt_km:.1f} km")

    return inclination, raan, eccentricity, arg_periapsis, sma




# ──────────────────────────────────────────────
if __name__ == "__main__":


    inclination, raan, eccentricity, arg_periapsis, sma = get_elements() # get orbital elements from celestrak TLE
    #make sure to repeat only every two hours or so to avoid hitting rate limits


    plot_orbit(
        semi_major_axis=sma,   # ~410 km altitude (ISS-like)
        eccentricity=eccentricity,
        inclination=inclination,            # ISS inclination
        raan=raan,
        arg_periapsis=arg_periapsis,
        live=False,                   # set False for a static 3-orbit plot
        head_start_orbits=1, 
        n_orbits=1 # start with 1.5 orbits already drawn
    )
