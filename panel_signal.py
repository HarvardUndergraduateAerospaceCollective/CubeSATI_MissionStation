"""
Panel: Signal Strength
Displays ground-station RSSI from received TinyGS packets over time.
Data source: packet_store (packets.rssi column, populated by tinygs_mqtt).
"""

import numpy as np
import packet_store

TITLE   = "SIGNAL"
Y_LABEL = "RSSI (dBm)"
X_LABEL = "Time (min)"
COLOR   = "#33ff00"
SOURCE  = "telemetry"


def compute():
    """Return (time_minutes, rssi_dbm) arrays from stored packets.

    Reads RSSI values from the packets table.  Returns empty arrays
    when no packets have been received yet.
    """
    rows = packet_store.recent_packets(n=500)
    if not rows:
        return np.array([]), np.array([])

    # Build arrays — oldest first
    rows = list(reversed(rows))
    values = np.array([r["rssi"] for r in rows if r.get("rssi") is not None],
                      dtype=float)
    if len(values) == 0:
        return np.array([]), np.array([])

    # X-axis: sequential sample index (minutes unavailable without timestamps parse)
    from datetime import datetime
    try:
        times = [datetime.fromisoformat(r["received_at"]) for r in rows
                 if r.get("rssi") is not None]
        t0 = times[0]
        t_min = np.array([(t - t0).total_seconds() / 60.0 for t in times])
    except Exception:
        t_min = np.arange(len(values), dtype=float)

    return t_min, values
