"""
Mission Control — Main Dashboard
Displays satellite ground track (center) flanked by telemetry panels.
All plotting lives here; data comes from visualizer.py and panel_*.py.
"""

import os
import time as _time

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as mticker
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap
from PIL import Image

import visualizer
import panel_altitude
import panel_signal
import panel_temperature
import panel_power
import panel_magnetometer
import packet_store
import tinygs_mqtt

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_EARTH_IMG_PATH = os.path.join(_SCRIPT_DIR, "Equirectangular_projection_SW.jpg")

# Side panels: (module, grid_row, grid_col)
_PANEL_LAYOUT = [
    (panel_altitude,     0, 0),   # top-left
    (panel_signal,       2, 0),   # mid-left
    (panel_magnetometer, 4, 0),   # bottom-left
    (panel_temperature,  0, 2),   # top-right      (gyroscope)
    (panel_power,        2, 2),   # mid-right
]

# Neon-glow color map for the ground track
_TRACK_CMAP = LinearSegmentedColormap.from_list(
    "orbit", ["#00ccff", "#00ffcc", "#ffcc00", "#ff4400"]
)
_TRACK_WINDOW_ORBITS = 1.5


# ──────────────────────────────────────────────
# Earth map rendering
# ──────────────────────────────────────────────

def _draw_earth(ax):
    """Render equirectangular Earth image with grid overlay."""
    ax.set_facecolor("#0a1628")
    img = Image.open(_EARTH_IMG_PATH)
    ax.imshow(img, extent=[-180, 180, -90, 90], aspect="auto", zorder=0)

    for lat in range(-90, 91, 30):
        ax.axhline(lat, color="white", linewidth=0.25, alpha=0.2, zorder=1)
    for lon in range(-180, 181, 30):
        ax.axvline(lon, color="white", linewidth=0.25, alpha=0.2, zorder=1)

    ax.axhline(0, color="#ffcc00", linewidth=0.5, linestyle="--", alpha=0.35, zorder=1)
    ax.axvline(0, color="#ffcc00", linewidth=0.5, linestyle="--", alpha=0.35, zorder=1)


# ──────────────────────────────────────────────
# Ground-track segment helpers
# ──────────────────────────────────────────────

def _build_segments(lon, lat):
    """Split ground track at ±180° wrap-arounds."""
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


def _add_track_segments(ax, track_artists, lon, lat, t_array=None):
    """Build LineCollections from lon/lat, add to *ax*, and append to *track_artists*."""
    if t_array is None:
        t_array = np.linspace(0.0, 1.0, len(lon))
    segments, seg_t = _build_segments(lon, lat)
    for (sx, sy), st in zip(segments, seg_t):
        if len(sx) < 2:
            continue
        pts = np.column_stack([sx, sy]).reshape(-1, 1, 2)
        segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
        lc = LineCollection(segs, cmap=_TRACK_CMAP, norm=plt.Normalize(0, 1),
                            linewidths=1.8, alpha=0.95, zorder=5)
        lc.set_array(st[:-1])
        ax.add_collection(lc)
        track_artists.append(lc)


def _clear_track(track_artists):
    """Remove all tracked LineCollection artists."""
    for a in track_artists:
        a.remove()
    track_artists.clear()


# ──────────────────────────────────────────────
# Panel styling helper
# ──────────────────────────────────────────────

def _style_panel_ax(ax, title, ylabel, color):
    """Apply mission-control dark styling to a side-panel axes."""
    ax.set_facecolor("#0a1628")
    ax.set_title(title, color=color, fontsize=8, fontfamily="monospace",
                 fontweight="bold", pad=4)
    ax.set_ylabel(ylabel, color="#8899aa", fontsize=7, fontfamily="monospace")
    ax.set_xlabel("Time (min)", color="#667788", fontsize=6, fontfamily="monospace")
    ax.tick_params(colors="#667788", labelsize=6)
    for spine in ax.spines.values():
        spine.set_color("#1a3a50")
    ax.grid(True, color="white", alpha=0.08, linewidth=0.3)


# ──────────────────────────────────────────────
# Main dashboard
# ──────────────────────────────────────────────

def launch(live: bool = False, n_orbits: float = 3, head_start_orbits: float = 1.0):
    """Create and display the full mission-control dashboard.

    Parameters
    ----------
    live : bool
        If True the ground track grows in real time.
    n_orbits : float
        Number of orbits to display (static mode) or max range (live mode).
    head_start_orbits : float
        Pre-drawn orbits so the map isn't empty at launch (live mode only).
    """

    # ── Start MQTT packet collector (daemon thread) ──
    if live:
        tinygs_mqtt.start_listener()

    # ── Fetch orbital data from CelesTrak ──
    inc, raan, ecc, argp, sma = visualizer.get_elements()
    orbital = {
        "sma": sma,
        "eccentricity": ecc,
        "inclination": inc,
        "raan": raan,
        "arg_periapsis": argp,
    }
    period = visualizer.orbital_period(sma)
    alt_km = (sma - visualizer.R_EARTH) / 1000
    t0 = _time.time()

    # ── Figure + GridSpec layout ──
    #
    #  ┌──────────┬────────────────────────┬──────────┐
    #  │ altitude │                        │ gyro     │
    #  ├──────────┤                        ├──────────┤
    #  │ signal   │    Ground Track Map    │ power    │
    #  ├──────────┤                        ├──────────┤
    #  │ magnet.  │                        │          │
    #  └──────────┴────────────────────────┴──────────┘

    fig = plt.figure(figsize=(20, 12), facecolor="#060e1a")
    gs = fig.add_gridspec(
        5, 3,
        width_ratios=[1, 4, 1],
        height_ratios=[1, 0.10, 1, 0.10, 1],
        left=0.04, right=0.96, bottom=0.06, top=0.88,
        wspace=0.22, hspace=0.40,
    )

    # ── Center: ground-track map (spans all rows) ──
    ax_map = fig.add_subplot(gs[:, 1])
    _draw_earth(ax_map)

    # Pin the map's top edge to align with the top of the upper side panels
    pos = ax_map.get_position()
    ax_map.set_position([pos.x0, pos.y0, pos.width, 0.88 - pos.y0])

    # Highlight border to make the map stand out
    for spine in ax_map.spines.values():
        spine.set_edgecolor("#00ddff")
        spine.set_linewidth(1.5)

    # Harvard University marker (Cambridge, MA)
    ax_map.plot(-71.1097, 42.3736, marker="o", markersize=7, color="crimson",
                markeredgecolor="white", markeredgewidth=0.8, zorder=8)
    ax_map.annotate(
        "Harvard", (-71.1172, 42.3744),
        textcoords="offset points", xytext=(8, -10),
        fontsize=7, fontweight="bold", color="crimson",
        fontfamily="monospace", zorder=8,
    )

    ax_map.set_xlim(-180, 180)
    ax_map.set_ylim(-90, 90)
    ax_map.set_xlabel("Longitude (°)", color="#8899aa", fontsize=9, fontfamily="monospace")
    ax_map.set_ylabel("Latitude (°)", color="#8899aa", fontsize=9, fontfamily="monospace")
    ax_map.xaxis.set_major_locator(mticker.MultipleLocator(30))
    ax_map.yaxis.set_major_locator(mticker.MultipleLocator(30))
    ax_map.tick_params(colors="#667788", labelsize=7)
    ax_map.set_aspect("equal", adjustable="box", anchor="N")

    # Satellite markers
    sat_marker, = ax_map.plot([], [], marker="o", markersize=8, color="#ff3333",
                              markeredgecolor="white", markeredgewidth=1.2, zorder=10)
    sat_label = ax_map.annotate(
        "HUCSAT", (0, 0),
        textcoords="offset points", xytext=(10, 8),
        fontsize=8, fontweight="bold", color="#ff6666",
        fontfamily="monospace", zorder=10,
    )
    start_marker, = ax_map.plot([], [], marker="D", markersize=5, color="#00ccff",
                                markeredgecolor="white", markeredgewidth=0.8, zorder=10)

    # Legend
    legend_elems = [
        mpatches.Patch(facecolor="#00ccff", edgecolor="white", label="Start"),
        mpatches.Patch(facecolor="#ff3333", edgecolor="white", label="Current Pos"),
    ]
    ax_map.legend(handles=legend_elems, loc="lower left", fontsize=7,
                  facecolor="#0d1b2a", edgecolor="#1a3a50", labelcolor="#8899aa",
                  framealpha=0.8)

    track_artists = []

    # ── Map helper closures ──

    def _update_markers_and_hud(lon, lat, n_now):
        sat_marker.set_data([lon[-1]], [lat[-1]])
        sat_label.xy = (lon[-1], lat[-1])
        start_marker.set_data([lon[0]], [lat[0]])
        hud_text.set_text(
            f"ALT: {alt_km:.0f} km   "
            f"INC: {inc:.1f}\u00b0   "
            f"ECC: {ecc:.4f}   "
            f"PERIOD: {period / 60:.1f} min   "
            f"ORBITS: {n_now:.2f}"
        )

    def _draw_track(n_now):
        _clear_track(track_artists)

        # Keep only a rolling tail to avoid map clutter during long live runs.
        draw_orbits = min(n_now, _TRACK_WINDOW_ORBITS)
        start_orbit = max(n_now - draw_orbits, 0.0)
        lon, lat, _ = visualizer.ground_track(
            sma, ecc, inc, raan, argp,
            n_orbits=draw_orbits,
            n_points=max(int(draw_orbits * 1500), 500),
            start_orbit=start_orbit,
        )
        _add_track_segments(ax_map, track_artists, lon, lat)
        _update_markers_and_hud(lon, lat, n_now)

    # ── Side panels ──
    panel_lines = []
    _NO_DATA_LABELS = []
    for mod, row, col in _PANEL_LAYOUT:
        ax_p = fig.add_subplot(gs[row, col])
        ax_p.set_box_aspect(1)  # force square shape
        _style_panel_ax(ax_p, mod.TITLE, mod.Y_LABEL, mod.COLOR)

        # Dispatch based on data source
        if getattr(mod, "SOURCE", "orbital") == "telemetry":
            x, y = mod.compute()
        else:
            x, y = mod.compute(orbital, n_orbits)

        if len(x) == 0:
            # No telemetry data yet — show placeholder
            line, = ax_p.plot([], [], color=mod.COLOR, linewidth=1.0, alpha=0.9)
            lbl = ax_p.text(0.5, 0.5, "AWAITING DATA", transform=ax_p.transAxes,
                            ha="center", va="center", fontsize=7,
                            color="#445566", fontfamily="monospace")
            _NO_DATA_LABELS.append((ax_p, lbl))
        else:
            line, = ax_p.plot(x, y, color=mod.COLOR, linewidth=1.0, alpha=0.9)
            margin = (y.max() - y.min()) * 0.08 or 1.0
            ax_p.set_xlim(x[0], x[-1])
            ax_p.set_ylim(y.min() - margin, y.max() + margin)

        panel_lines.append((ax_p, mod, line))

    # ── HUD text overlays ──
    fig.text(
        0.50, 0.95, "SATELLITE  GROUND  TRACK",
        ha="center", va="center", fontsize=15, fontweight="bold",
        color="#00ddff", fontfamily="monospace",
    )
    hud_text = fig.text(
        0.50, 0.92, "",
        ha="center", va="center", fontsize=8,
        color="#55aa88", fontfamily="monospace",
    )
    met_text = fig.text(
        0.04, 0.95, "MET 00:00:00",
        ha="left", va="center", fontsize=10, fontweight="bold",
        color="#ff4444", fontfamily="monospace",
    )
    db_text = fig.text(
        0.04, 0.02, "DB: 0 pkts",
        ha="left", va="center", fontsize=7,
        color="#446688", fontfamily="monospace",
    )
    fsm_text = fig.text(
        0.50, 0.02, "FSM: —  DEPL: —  UPTIME: —",
        ha="center", va="center", fontsize=8,
        color="#00ddff", fontfamily="monospace",
    )

    # ── LIVE / OFFLINE indicator (top-right) ──
    if live:
        fig.text(
            0.96, 0.95, "LIVE",
            ha="right", va="center", fontsize=10, fontweight="bold",
            color="#ff4444", fontfamily="monospace",
        )
        live_dot = fig.text(
            0.935, 0.9515, "\u25CF",
            ha="center", va="center", fontsize=12,
            color="#ff0000", fontfamily="monospace",
        )
    else:
        fig.text(
            0.96, 0.95, "OFFLINE",
            ha="right", va="center", fontsize=9, fontweight="bold",
            color="#556666", fontfamily="monospace",
        )
        live_dot = None

    # ═══════════════════════════════════════════
    # Static mode — draw once and show
    # ═══════════════════════════════════════════
    if not live:
        _draw_track(n_orbits)
        plt.show()
        return

    # ═══════════════════════════════════════════
    # Live mode — manual blitting for performance
    # ═══════════════════════════════════════════

    speed_factor = 1.0
    last_n = [head_start_orbits]

    if head_start_orbits > 0:
        _draw_track(head_start_orbits)

    # -- Mark overlay artists as animated (excluded from static background) --
    _overlay_artists = [met_text, db_text, fsm_text, hud_text,
                        sat_marker, start_marker, sat_label]
    if live_dot is not None:
        _overlay_artists.append(live_dot)
    for a in _overlay_artists:
        a.set_animated(True)

    # -- Initial full draw, then capture the static background --
    fig.canvas.draw()
    _bg = [fig.canvas.copy_from_bbox(fig.bbox)]

    def _blit_overlays():
        """Restore cached background and re-draw only the overlay artists."""
        fig.canvas.restore_region(_bg[0])
        for a in _overlay_artists:
            if a.axes is not None:
                a.axes.draw_artist(a)
            else:
                fig.draw_artist(a)
        fig.canvas.blit(fig.bbox)
        fig.canvas.flush_events()

    # -- Combined timer state --
    _frame = [0]
    blink_state = [True]
    HEAVY_EVERY = 20          # full track/panel refresh every N ticks

    def _on_timer():
        _frame[0] += 1
        elapsed = _time.time() - t0

        # ── Always: update MET clock, blink dot, DB count ──
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        met_text.set_text(f"MET {h:02d}:{m:02d}:{s:02d}")

        if live_dot is not None:
            blink_state[0] = not blink_state[0]
            live_dot.set_alpha(1.0 if blink_state[0] else 0.0)

        try:
            db_text.set_text(f"DB: {packet_store.packet_count()} pkts")
        except Exception:
            pass

        # ── Update FSM state HUD ──
        try:
            fsm = packet_store.latest_fsm_state()
            if fsm:
                uptime_str = ""
                ut = fsm.get("uptime", "")
                if ut not in ("", None):
                    try:
                        secs = int(float(ut))
                        uh, urem = divmod(secs, 3600)
                        um, us = divmod(urem, 60)
                        uptime_str = f"{uh:02d}:{um:02d}:{us:02d}"
                    except (ValueError, TypeError):
                        uptime_str = str(ut)
                _fs = fsm.get("fsm_state", "\u2014")
                _fd = fsm.get("fsm_depl", "\u2014")
                _fu = uptime_str or "\u2014"
                fsm_text.set_text(
                    f"FSM: {_fs}  DEPL: {_fd}  UPTIME: {_fu}"
                )
        except Exception:
            pass

        # ── Every HEAVY_EVERY ticks: track + panels (full redraw) ──
        if _frame[0] % HEAVY_EVERY == 0:
            n_now = head_start_orbits + (elapsed * speed_factor) / period
            if n_now - last_n[0] > 1e-6:
                _draw_track(n_now)

                for ax_p, mod, line in panel_lines:
                    if getattr(mod, "SOURCE", "orbital") == "telemetry":
                        x, y = mod.compute()
                    else:
                        x, y = mod.compute(orbital, n_now)
                    if len(x) == 0:
                        continue
                    line.set_data(x, y)
                    ax_p.set_xlim(x[0], x[-1])
                    margin = (y.max() - y.min()) * 0.08 or 1.0
                    ax_p.set_ylim(y.min() - margin, y.max() + margin)
                    for stored_ax, lbl in _NO_DATA_LABELS:
                        if stored_ax is ax_p:
                            lbl.set_visible(False)

                last_n[0] = n_now

            # Full redraw → recapture background → blit overlays on top
            fig.canvas.draw()
            _bg[0] = fig.canvas.copy_from_bbox(fig.bbox)
            _blit_overlays()
            return

        # ── Fast path: blit only overlays (no full redraw) ──
        _blit_overlays()

    _timer = fig.canvas.new_timer(interval=1000)
    _timer.add_callback(_on_timer)
    _timer.start()
    fig._live_timer = _timer      # prevent garbage collection

    fig1 = plt.gcf()
    figManager = plt.get_current_fig_manager()
    figManager.full_screen_toggle()
    fig1.canvas.window().statusBar().setVisible(False)
    plt.show()


# ──────────────────────────────────────────────
if __name__ == "__main__":
    launch(
        live=False,
        n_orbits=1,
        head_start_orbits=1.0,
    )
