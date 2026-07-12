# Deployment Checklist — Placeholders & Stubs

Items to update once real mission data becomes available. Ordered by when they become actionable.

---

## Pre-Launch (Once NORAD ID Is Assigned)

### 1. Satellite NORAD Catalog Number / TLE — `visualizer.py` ✅ DONE (2026-07-12)

| | |
|---|---|
| **File** | `visualizer.py` — `DEFAULT_CAT_NR` |
| **Status** | **DONE:** `DEFAULT_CAT_NR = 69794` — HUCSat is cataloged ("ISS OBJECT YJ", 1998-067YJ), live CelesTrak TLEs |

HUCSat was cataloged 2026-07 as **NORAD 69794**. The pre-launch predicted-TLE override (`TLE_OVERRIDE` / `TEMP_CAT_NR` 98001) has been removed; `_fetch_tle_lines` now always pulls from CelesTrak. TLEs are cached 2h (`CACHE_MAX_AGE`) and `web_server.py` re-fetches on that cadence while running. On a transient CelesTrak failure `get_orbital_state()` falls back to the last cached TLE, so orbit viz doesn't go dark. Delete the stale `.tle_cache/98001.json` on the Pi if present (harmless leftover, no longer queried).

### 2. TinyGS NORAD ID Filter — `tinygs_mqtt.py`

| | |
|---|---|
| **File** | `tinygs_mqtt.py` lines 70-78 |
| **Current** | **TEMP:** `TINYGS_SAT_IDS='98001'` in `mission.env` (TinyGS placeholder catalog number) |
| **Set via** | `TINYGS_SAT_IDS` environment variable, comma-separated integers |

Set to `98001` (the TinyGS placeholder for CubeSAT-I) in `mission.env`. Update to the real NORAD ID once assigned. Blank = accept all satellites.

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
| **File** | `web_server.py` line 43 |
| **Current** | ✅ `MISSION_EPOCH_UTC = "2026-07-02T09:00:00+00:00"` (Thu Jul 2 2026, 05:00 EDT) |
| **Replace with** | — done |

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

### 6. Backup Cron Job — Raspberry Pi

Automated backups run every 3 hours via cron. To enable:

```bash
crontab -e
```

Add:
```
0 */3 * * * /usr/bin/python3 /home/huac/Desktop/HUCSat-GroundStationVisual/db_backup.py >> /dev/null 2>&1
```

Backs up to both `/mnt/cubesat-backup` (USB drive, exFAT, auto-mounts via systemd) and Google Drive via rclone remote `dbbackup`.

### 7. Slack Webhook — `db_backup.py`

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
