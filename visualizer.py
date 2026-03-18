"""
Satellite Orbit Data Module
Handles TLE fetching from CelesTrak and Keplerian orbital mechanics.
All computation, no plotting — plotting lives in missioncontrol.py.
"""

import json
import time
from pathlib import Path

import numpy as np

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent / ".tle_cache"
CACHE_MAX_AGE = 2 * 60 * 60  # 2 hours in seconds

MU_EARTH = 3.986004418e14          # m^3 s^-2
R_EARTH = 6_371_000                # m
EARTH_ROT_RATE = 7.2921159e-5      # rad/s
DEG = np.degrees
RAD = np.radians


# ──────────────────────────────────────────────
# Orbital mechanics
# ──────────────────────────────────────────────

def kepler_equation(M: np.ndarray, e: float, tol: float = 1e-10) -> np.ndarray:
    """Solve Kepler's equation  M = E - e·sin(E)  via Newton-Raphson."""
    E = M.copy()
    for _ in range(50):
        dE = (E - e * np.sin(E) - M) / (1.0 - e * np.cos(E))
        E -= dE
        if np.max(np.abs(dE)) < tol:
            break
    return E


def orbital_period(semi_major_axis: float) -> float:
    """Return the orbital period in seconds."""
    return 2 * np.pi * np.sqrt(semi_major_axis ** 3 / MU_EARTH)


def ground_track(
    semi_major_axis: float,
    eccentricity: float,
    inclination: float,
    raan: float,
    arg_periapsis: float,
    n_orbits: float = 3,
    n_points: int = 3000,
):
    """
    Compute sub-satellite ground track.
    Returns (longitude, latitude, time_seconds) arrays.
    """
    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = orbital_period(semi_major_axis)
    t = np.linspace(0, n_orbits * period, n_points)
    M = 2 * np.pi / period * t
    E = kepler_equation(M, eccentricity)

    nu = 2 * np.arctan2(
        np.sqrt(1 + eccentricity) * np.sin(E / 2),
        np.sqrt(1 - eccentricity) * np.cos(E / 2),
    )

    u = omega + nu
    lat = np.arcsin(np.sin(inc) * np.sin(u))
    lon = np.arctan2(np.sin(u) * np.cos(inc), np.cos(u)) + Omega
    lon -= EARTH_ROT_RATE * t

    lon = DEG(lon)
    lat = DEG(lat)
    lon = (lon + 180) % 360 - 180

    return lon, lat, t


def orbital_altitude(
    semi_major_axis: float,
    eccentricity: float,
    n_orbits: float = 3,
    n_points: int = 3000,
):
    """
    Compute altitude above Earth's surface over time.
    Returns (time_seconds, altitude_km) arrays.
    """
    period = orbital_period(semi_major_axis)
    t = np.linspace(0, n_orbits * period, n_points)
    M = 2 * np.pi / period * t
    E = kepler_equation(M, eccentricity)
    r = semi_major_axis * (1 - eccentricity * np.cos(E))
    alt_km = (r - R_EARTH) / 1000.0
    return t, alt_km


# ──────────────────────────────────────────────
# TLE fetching
# ──────────────────────────────────────────────

def _cache_path(cat_nr: int) -> Path:
    return CACHE_DIR / f"{cat_nr}.json"


def _load_cache(cat_nr: int):
    path = _cache_path(cat_nr)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["timestamp"] < CACHE_MAX_AGE:
            return tuple(data["elements"])
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_cache(cat_nr: int, elements: tuple):
    CACHE_DIR.mkdir(exist_ok=True)
    payload = {"timestamp": time.time(), "elements": list(elements)}
    _cache_path(cat_nr).write_text(json.dumps(payload))


def get_elements(cat_nr: int = 25544):
    """
    Fetch TLE from CelesTrak for the given NORAD catalog number.
    Results are cached for 2 hours to avoid rate-limiting.
    Returns (inclination, raan, eccentricity, arg_periapsis, semi_major_axis).
    """
    cached = _load_cache(cat_nr)
    if cached is not None:
        return cached

    import requests
    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={cat_nr}&FORMAT=TLE"
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch TLE data: {response.status_code}")

    tle_data = response.text.strip().splitlines()
    line2 = tle_data[2]

    inclination   = float(line2[8:16].strip())
    raan          = float(line2[17:25].strip())
    eccentricity  = float("0." + line2[26:33].strip())
    arg_periapsis = float(line2[34:42].strip())
    mean_motion   = float(line2[52:63].strip())
    sma = (MU_EARTH / (mean_motion * 2 * np.pi / 86400) ** 2) ** (1 / 3)

    elements = (inclination, raan, eccentricity, arg_periapsis, sma)
    _save_cache(cat_nr, elements)
    return elements
