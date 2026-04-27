"""
Panel: Magnetometer
Displays satellite magnetometer (X-axis) telemetry over time.
Data source: packet_store telemetry table, key "FSM_magn_v_0" (populated by tinygs_mqtt).
"""

import numpy as np
import packet_store

TITLE          = "MAGNETOMETER"
Y_LABEL        = "\u00b5T"
X_LABEL        = "Time (min)"
COLOR          = "#ffb000"
SOURCE         = "telemetry"
TELEMETRY_KEY  = "FSM_magn_v_0"


def compute():
    """Return (time_minutes, magnetometer_uT) arrays from stored telemetry.

    Queries the telemetry table for FSM_magn_v_0.  Returns empty arrays
    when no readings exist yet.
    """
    rows = packet_store.telemetry_series(TELEMETRY_KEY)
    if not rows:
        return np.array([]), np.array([])

    values = np.array([r["value"] for r in rows], dtype=float)

    from datetime import datetime
    try:
        times = [datetime.fromisoformat(r["timestamp"]) for r in rows]
        t0 = times[0]
        t_min = np.array([(t - t0).total_seconds() / 60.0 for t in times])
    except Exception:
        t_min = np.arange(len(values), dtype=float)

    return t_min, values
