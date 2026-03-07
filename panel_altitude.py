"""
Panel: Orbital Altitude
Displays satellite altitude above Earth's surface over time.
Data computed from Keplerian orbital mechanics via visualizer module.
"""

import numpy as np

TITLE   = "ALTITUDE"
Y_LABEL = "Alt (km)"
X_LABEL = "Time (min)"
COLOR   = "#00ccff"


def compute(orbital_elements: dict, n_orbits: float, n_points: int = 500):
    """Return (time_minutes, altitude_km) arrays.

    Parameters
    ----------
    orbital_elements : dict
        Must contain keys ``sma`` and ``eccentricity``.
    n_orbits : float
    n_points : int
    """
    from visualizer import orbital_altitude

    t_sec, alt_km = orbital_altitude(
        orbital_elements["sma"],
        orbital_elements["eccentricity"],
        n_orbits=n_orbits,
        n_points=n_points,
    )
    return t_sec / 60.0, alt_km
