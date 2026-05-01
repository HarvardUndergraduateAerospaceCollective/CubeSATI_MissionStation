"""
Panel: Power System
Displays battery voltage from decoded satellite beacon telemetry.
Data source: packet_store telemetry table, key "FSM_batt_v" (populated by tinygs_mqtt).
"""

import numpy as np
import packet_store

TITLE          = "POWER"
Y_LABEL        = "Volts (V)"
X_LABEL        = "Time (min)"
COLOR          = "#33ff00"
SOURCE         = "telemetry"
TELEMETRY_KEY  = "FSM_batt_v"


def compute():
    """Return (time_minutes, battery_voltage) arrays from stored telemetry.

    Queries the telemetry table for FSM_batt_v.  Returns empty arrays
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
