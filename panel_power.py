"""
Panel: Power System
Displays battery state-of-charge over time.
[SKELETON — real data source TBD, currently generates simulated data]
"""

import numpy as np

TITLE   = "POWER"
Y_LABEL = "SoC (%)"
X_LABEL = "Time (min)"
COLOR   = "#ff4400"


def compute(orbital_elements: dict, n_orbits: float, n_points: int = 500):
    """Return (time_minutes, state_of_charge_percent) arrays.

    Skeleton implementation — replace internals with real telemetry source.
    """
    from visualizer import orbital_period

    period_min = orbital_period(orbital_elements["sma"]) / 60.0
    t_min = np.linspace(0, n_orbits * period_min, n_points)

    # Simulated: charge in sunlight, discharge in eclipse
    rng = np.random.default_rng(13)
    phase = (t_min % period_min) / period_min
    soc = (
        60
        + 30 * (0.5 + 0.5 * np.sin(2 * np.pi * phase - np.pi / 3))
        + 1.5 * rng.normal(size=n_points)
    )
    soc = np.clip(soc, 0, 100)
    return t_min, soc
