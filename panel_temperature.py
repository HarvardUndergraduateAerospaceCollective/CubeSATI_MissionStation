"""
Panel: Thermal Telemetry
Displays satellite bus temperature over time.
[SKELETON — real data source TBD, currently generates simulated data]
"""

import numpy as np

TITLE   = "TEMPERATURE"
Y_LABEL = "°C"
X_LABEL = "Time (min)"
COLOR   = "#ffcc00"


def compute(orbital_elements: dict, n_orbits: float, n_points: int = 500):
    """Return (time_minutes, temperature_celsius) arrays.

    Skeleton implementation — replace internals with real telemetry source.
    """
    from visualizer import orbital_period

    period_min = orbital_period(orbital_elements["sma"]) / 60.0
    t_min = np.linspace(0, n_orbits * period_min, n_points)

    # Simulated: thermal cycling between sunlit (~+20 °C) and eclipse (~-10 °C)
    rng = np.random.default_rng(7)
    temp = (
        5
        + 15 * np.sin(2 * np.pi * t_min / period_min - np.pi / 4)
        + 2 * rng.normal(size=n_points)
    )
    return t_min, temp
