"""
Panel: Thermal Telemetry
Displays satellite sensor telemetry over time.
Data source: packet_store telemetry table (populated by tinygs_mqtt).

The telemetry key is configurable — set TELEMETRY_KEY to whichever beacon
field carries temperature data once the OBC firmware enables _add_sensor_data.
"""

import numpy as np
import packet_store

TITLE          = "TEMPERATURE"
Y_LABEL        = "°C"
X_LABEL        = "Time (min)"
COLOR          = "#ffcc00"
SOURCE         = "telemetry"
TELEMETRY_KEY  = "FSM_acc_0"   # placeholder until temp sensor field is added


def compute():
    """Return (time_minutes, values) arrays from stored telemetry.

    Queries the telemetry table for TELEMETRY_KEY.  Returns empty arrays
    when no matching readings exist yet.
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
