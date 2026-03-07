"""
Panel: Signal Strength
Displays downlink signal strength / link margin over time.
[SKELETON — real data source TBD, currently generates simulated data]
"""

import numpy as np

TITLE   = "SIGNAL"
Y_LABEL = "dBm"
X_LABEL = "Time (min)"
COLOR   = "#00ffcc"


def compute(orbital_elements: dict, n_orbits: float, n_points: int = 500):
    """Return (time_minutes, signal_dbm) arrays.

    Skeleton implementation — replace internals with real telemetry source.
    """
    from visualizer import orbital_period

    period_min = orbital_period(orbital_elements["sma"]) / 60.0
    t_min = np.linspace(0, n_orbits * period_min, n_points)

    # Simulated: signal peaks near overhead passes, dips at horizon
    rng = np.random.default_rng(42)
    signal = (
        -80
        + 20 * np.sin(2 * np.pi * t_min / period_min)
        + 3 * rng.normal(size=n_points)
    )
    return t_min, signal
