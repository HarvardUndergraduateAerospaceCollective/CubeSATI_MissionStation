"""
Satellite Orbit Data Module
Handles TLE fetching from CelesTrak and Keplerian orbital mechanics.
All computation, no plotting — plotting lives in missioncontrol.py.
"""

import json
import time
from datetime import datetime, timedelta, timezone
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


def _parse_tle_epoch(epoch_token: str) -> float:
    """Parse TLE epoch token (YYDDD.DDD...) into UTC Unix seconds."""
    token = epoch_token.strip()
    if len(token) < 5:
        raise ValueError(f"Invalid TLE epoch token: {epoch_token!r}")

    year_2d = int(token[:2])
    doy = float(token[2:])
    year = year_2d + (2000 if year_2d < 57 else 1900)

    epoch_dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1.0)
    return epoch_dt.timestamp()


def _parse_tle_lines(line1: str, line2: str):
    """Parse TLE lines into orbital elements and epoch/phase metadata."""
    inclination = float(line2[8:16].strip())
    raan = float(line2[17:25].strip())
    eccentricity = float("0." + line2[26:33].strip())
    arg_periapsis = float(line2[34:42].strip())
    mean_anomaly = float(line2[43:51].strip())
    mean_motion = float(line2[52:63].strip())
    sma = (MU_EARTH / (mean_motion * 2 * np.pi / 86400) ** 2) ** (1 / 3)

    epoch_token = line1[18:32]
    epoch_unix = _parse_tle_epoch(epoch_token)

    return {
        "inclination": inclination,
        "raan": raan,
        "eccentricity": eccentricity,
        "arg_periapsis": arg_periapsis,
        "semi_major_axis": sma,
        "mean_anomaly_deg": mean_anomaly,
        "epoch_unix": epoch_unix,
    }


def _fetch_tle_lines(cat_nr: int):
    """Fetch raw TLE lines from CelesTrak for the given NORAD catalog number."""
    import requests

    url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={cat_nr}&FORMAT=TLE"
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch TLE data: {response.status_code}")

    tle_data = response.text.strip().splitlines()
    if len(tle_data) < 3:
        raise Exception("Unexpected TLE response format")

    return tle_data[1].rstrip(), tle_data[2].rstrip()


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
    start_orbit: float = 0.0,
):
    """
    Compute sub-satellite ground track.
    Returns (longitude, latitude, time_seconds) arrays.
    """
    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = orbital_period(semi_major_axis)
    start_t = max(start_orbit, 0.0) * period
    t = np.linspace(start_t, start_t + n_orbits * period, n_points)
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


def ground_track_with_alt(
    semi_major_axis: float,
    eccentricity: float,
    inclination: float,
    raan: float,
    arg_periapsis: float,
    n_orbits: float = 3,
    n_points: int = 3000,
    start_orbit: float = 0.0,
):
    """
    Compute sub-satellite ground track with altitude.
    Returns (longitude, latitude, altitude_km, time_seconds) arrays.
    """
    inc = RAD(inclination)
    Omega = RAD(raan)
    omega = RAD(arg_periapsis)

    period = orbital_period(semi_major_axis)
    start_t = max(start_orbit, 0.0) * period
    t = np.linspace(start_t, start_t + n_orbits * period, n_points)
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
    lon = (DEG(lon) + 180) % 360 - 180
    lat = DEG(lat)

    r = semi_major_axis * (1 - eccentricity * np.cos(E))
    alt_km = (r - R_EARTH) / 1000.0

    return lon, lat, alt_km, t


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


def _load_cache_payload(cat_nr: int):
    path = _cache_path(cat_nr)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if time.time() - data["timestamp"] < CACHE_MAX_AGE:
            return data
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _load_cache(cat_nr: int):
    payload = _load_cache_payload(cat_nr)
    if payload is None:
        return None
    elements = payload.get("elements")
    if isinstance(elements, list) and len(elements) >= 5:
        return tuple(elements[:5])
    return None


def _save_cache(cat_nr: int, elements: tuple, extras=None):
    CACHE_DIR.mkdir(exist_ok=True)
    payload = {"timestamp": time.time(), "elements": list(elements)}
    if extras:
        payload.update(extras)
    _cache_path(cat_nr).write_text(json.dumps(payload))


def get_orbital_state(cat_nr: int = 25544):
    """Fetch TLE and return epoch-anchored orbital state.

    Returns keys:
      inclination, raan, eccentricity, arg_periapsis,
      semi_major_axis, mean_anomaly_deg, epoch_unix
    """
    cached_payload = _load_cache_payload(cat_nr)
    if cached_payload is not None:
        cached_elements = cached_payload.get("elements")
        if (
            isinstance(cached_elements, list)
            and len(cached_elements) >= 5
            and "mean_anomaly_deg" in cached_payload
            and "epoch_unix" in cached_payload
        ):
            return {
                "inclination": float(cached_elements[0]),
                "raan": float(cached_elements[1]),
                "eccentricity": float(cached_elements[2]),
                "arg_periapsis": float(cached_elements[3]),
                "semi_major_axis": float(cached_elements[4]),
                "mean_anomaly_deg": float(cached_payload["mean_anomaly_deg"]),
                "epoch_unix": float(cached_payload["epoch_unix"]),
            }

    try:
        line1, line2 = _fetch_tle_lines(cat_nr)
        state = _parse_tle_lines(line1, line2)
    except Exception:
        # Fallback for legacy cache entries that predate epoch/mean-anomaly fields.
        if cached_payload is not None:
            cached_elements = cached_payload.get("elements")
            if isinstance(cached_elements, list) and len(cached_elements) >= 5:
                return {
                    "inclination": float(cached_elements[0]),
                    "raan": float(cached_elements[1]),
                    "eccentricity": float(cached_elements[2]),
                    "arg_periapsis": float(cached_elements[3]),
                    "semi_major_axis": float(cached_elements[4]),
                    "mean_anomaly_deg": float(cached_payload.get("mean_anomaly_deg", 0.0)),
                    "epoch_unix": float(cached_payload.get("epoch_unix", time.time())),
                }
        raise

    elements = (
        state["inclination"],
        state["raan"],
        state["eccentricity"],
        state["arg_periapsis"],
        state["semi_major_axis"],
    )
    _save_cache(
        cat_nr,
        elements,
        extras={
            "mean_anomaly_deg": state["mean_anomaly_deg"],
            "epoch_unix": state["epoch_unix"],
            "tle_line1": line1,
            "tle_line2": line2,
        },
    )
    return state


def get_elements(cat_nr: int = 25544):
    """
    Fetch TLE from CelesTrak for the given NORAD catalog number.
    Results are cached for 2 hours to avoid rate-limiting.
    Returns (inclination, raan, eccentricity, arg_periapsis, semi_major_axis).
    """
    state = get_orbital_state(cat_nr)
    return (
        state["inclination"],
        state["raan"],
        state["eccentricity"],
        state["arg_periapsis"],
        state["semi_major_axis"],
    )
