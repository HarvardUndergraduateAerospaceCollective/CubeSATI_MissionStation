"""
Panel: Best Direction
Displays which CubeSat face has the best sun exposure (orient_best_direction).
Data source: packet_store telemetry table, key "FSM_best_dir".

Unlike other panels, this returns *distribution* data (counts per face)
rather than a time series, since the value is a discrete direction index.

Direction mapping (from OBC state_orient.py):
    0 → +Y    1 → −X    2 → −Y    3 → +X    −1 → N/A
"""

import packet_store

TITLE         = "BEST DIRECTION"
COLOR         = "#ffb000"
SOURCE        = "telemetry"
TELEMETRY_KEY = "FSM_best_dir"

# Label for each face index (matches OBC orient_best_direction mapping)
FACE_LABELS = {
    0: "+Y",
    1: "\u2212X",   # −X  (proper minus sign)
    2: "\u2212Y",   # −Y
    3: "+X",
    -1: "N/A",
}

FACE_COLORS = {
    0: "#33ff00",   # +Y  — green
    1: "#ffb000",   # −X  — amber
    2: "#ff6600",   # −Y  — orange
    3: "#00ff88",   # +X  — teal
    -1: "#1a1a1a",  # N/A — dim
}


def compute():
    """Return distribution dict for the doughnut chart.

    Returns dict with keys:
        labels  – face direction labels  ["+Y", "−X", "−Y", "+X", "N/A"]
        counts  – number of readings per direction
        colors  – per-slice color
        latest  – most recent direction index (int)
        latest_label – human-readable label for latest
    """
    rows = packet_store.telemetry_series(TELEMETRY_KEY)

    # Count occurrences per direction
    counts = {k: 0 for k in FACE_LABELS}
    latest = -1
    for r in rows:
        val = int(round(r["value"]))
        if val in counts:
            counts[val] += 1
        else:
            counts[-1] += 1
        latest = val

    # Build ordered lists (0, 1, 2, 3, -1)
    order = [0, 1, 2, 3, -1]
    return {
        "labels":       [FACE_LABELS[k] for k in order],
        "counts":       [counts[k] for k in order],
        "colors":       [FACE_COLORS[k] for k in order],
        "latest":       latest,
        "latest_label": FACE_LABELS.get(latest, "?"),
    }
