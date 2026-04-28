# Deployment Checklist — Placeholders & Stubs

Items to update once real mission data becomes available. Ordered by when they become actionable.

---

## Pre-Launch (Once NORAD ID Is Assigned)

### 1. Satellite NORAD Catalog Number — `visualizer.py`

| | |
|---|---|
| **File** | `visualizer.py` lines 235, 300 |
| **Current** | `cat_nr: int = 25544` (ISS as stand-in) |
| **Replace with** | Your CubeSAT-I NORAD catalog number |

Both `get_orbital_state()` and `get_elements()` default to ISS. `web_server.py:71` calls `get_orbital_state()` with no argument, so this default propagates to all orbit calculations.

### 2. TinyGS NORAD ID Filter — `tinygs_mqtt.py`

| | |
|---|---|
| **File** | `tinygs_mqtt.py` lines 70-78 |
| **Current** | `SAT_NORAD_IDS = set()` (accepts all satellites) |
| **Set via** | `TINYGS_SAT_IDS` environment variable, comma-separated integers |

Without this, the MQTT listener ingests packets from every satellite on TinyGS.

### 3. TinyGS MQTT Credentials — `tinygs_mqtt.py`

| | |
|---|---|
| **File** | `tinygs_mqtt.py` lines 61-68 |
| **Current** | Empty strings (will warn at startup) |
| **Set via** | `TINYGS_MQTT_USER` and `TINYGS_MQTT_PASS` environment variables |

Sign up at https://tinygs.com, find credentials under Personal > MQTT.

---

## At Deployment (Satellite Separation)

### 4. Mission Epoch — `web_server.py`

| | |
|---|---|
| **File** | `web_server.py` line 40 |
| **Current** | `MISSION_EPOCH_UTC = None` |
| **Replace with** | ISO-8601 datetime string, e.g. `"2026-06-15T14:32:00+00:00"` |

Controls MET clock and orbit counter on the dashboard. While `None`, MET displays `--:--:--` and orbits show `---`.

---

## Optional / When Ready

### 5. Ground Station Altitude — `web_server.py`

| | |
|---|---|
| **File** | `web_server.py` line 117 |
| **Current** | `HARVARD_GS_ALT_M = (7 * 10 + 15) * 0.3048` (85 ft / ~25.9 m) |
| **Verify** | Confirm this matches the actual SEC roof antenna elevation |

Used in topocentric az/el calculations for approach predictions. A few meters of error is negligible for pass predictions, but worth confirming.

### 6. Slack Webhook — `db_backup.py`

| | |
|---|---|
| **File** | `db_backup.py` line 74 |
| **Current** | Empty string (alerts disabled) |
| **Set via** | `CUBESAT_SLACK_WEBHOOK` environment variable |

Sends a Slack message when a scheduled backup fails. No-op if unset — purely optional.

---

## UI Placeholders (Auto-Resolve When Data Flows)

These require no manual changes. They display "AWAITING DATA" until the backend APIs return real telemetry.

| Element | File | Clears when... |
|---|---|---|
| `awaiting-0` through `awaiting-4` | `index.html` lines 35-67 | `/api/panels` returns panel data |
| `awaiting-best-dir` | `index.html` line 73 | `/api/best_dir` returns counts |
| `awaiting-feed` | `index.html` line 100 | `/api/next_approaches` returns pass data |
| `awaiting-placeholder` | `index.html` line 105 | `/api/harvard_approach` returns polar data |
| `awaiting-fsm` | `index.html` line 125 | `/api/fsm` returns state history |
| FSM bar text (`fsm-state`, `fsm-depl`, `fsm-uptime`) | `dashboard.js` line 22 | `/api/status` returns FSM fields |
| MET clock | `dashboard.js` | `MISSION_EPOCH_UTC` is set (item 4) |
| Orbit counter | `dashboard.js` | `MISSION_EPOCH_UTC` is set (item 4) |

---

## TLE Cache Note

The file `.tle_cache/25544.json` contains cached ISS orbital elements. Once you switch to the real NORAD ID, a new cache file will be created automatically and the old one can be deleted.
