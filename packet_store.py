"""
Packet Store — SQLite database for long-term telemetry storage.

Stores raw packets received from TinyGS (or any other source) and
parsed telemetry key-value pairs for post-mission analysis.

Database file lives next to this script as ``mission_data.db``.
All timestamps are stored as ISO-8601 UTC strings.
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH = Path(__file__).parent / "mission_data.db"
_local = threading.local()

# ──────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection (creates DB/tables on first call)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent readers
        conn.execute("PRAGMA foreign_keys=ON")
        _init_tables(conn)
        _local.conn = conn
    return conn


@contextmanager
def _cursor():
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _init_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS packets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at   TEXT    NOT NULL,          -- ISO-8601 UTC
            satellite     TEXT    NOT NULL DEFAULT '',
            norad_id      INTEGER,
            station       TEXT    NOT NULL DEFAULT '',
            frequency_mhz REAL,
            rssi          REAL,
            snr           REAL,
            crc_error     INTEGER DEFAULT 0,           -- 1 if CRC failed
            raw_frame     BLOB,
            decoded_json  TEXT,                       -- full decoded beacon telemetry
            source        TEXT    NOT NULL DEFAULT 'unknown'
        );

        CREATE INDEX IF NOT EXISTS idx_packets_sat
            ON packets(satellite);
        CREATE INDEX IF NOT EXISTS idx_packets_time
            ON packets(received_at);

        CREATE TABLE IF NOT EXISTS telemetry (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_id  INTEGER NOT NULL REFERENCES packets(id),
            timestamp  TEXT    NOT NULL,               -- ISO-8601 UTC
            key        TEXT    NOT NULL,
            value      REAL,
            unit       TEXT    NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_telem_key_time
            ON telemetry(key, timestamp);
    """)


# ──────────────────────────────────────────────
# Write API
# ──────────────────────────────────────────────

def store_packet(
    *,
    satellite: str = "",
    norad_id: Optional[int] = None,
    station: str = "",
    frequency_mhz: Optional[float] = None,
    rssi: Optional[float] = None,
    snr: Optional[float] = None,
    crc_error: bool = False,
    raw_frame: Optional[bytes] = None,
    decoded: Optional[dict] = None,
    source: str = "unknown",
    received_at: Optional[str] = None,
) -> int:
    """Insert a packet and return its row id."""
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
    decoded_json = json.dumps(decoded) if decoded else None

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO packets
               (received_at, satellite, norad_id, station,
                frequency_mhz, rssi, snr, crc_error,
                raw_frame, decoded_json, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (received_at, satellite, norad_id, station,
             frequency_mhz, rssi, snr, int(crc_error),
             raw_frame, decoded_json, source),
        )
        return cur.lastrowid


def store_telemetry(packet_id: int, key: str, value: float,
                    unit: str = "", timestamp: Optional[str] = None):
    """Insert a single parsed telemetry reading linked to a packet."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry (packet_id, timestamp, key, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (packet_id, timestamp, key, value, unit),
        )


def store_telemetry_batch(packet_id: int,
                          readings: list[tuple[str, float, str]],
                          timestamp: Optional[str] = None):
    """Insert many (key, value, unit) telemetry rows for one packet."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    with _cursor() as cur:
        cur.executemany(
            "INSERT INTO telemetry (packet_id, timestamp, key, value, unit) "
            "VALUES (?, ?, ?, ?, ?)",
            [(packet_id, timestamp, k, v, u) for k, v, u in readings],
        )


# ──────────────────────────────────────────────
# Read / query API
# ──────────────────────────────────────────────

def packet_count(satellite: Optional[str] = None) -> int:
    """Total number of stored packets (optionally filtered by satellite)."""
    with _cursor() as cur:
        if satellite:
            cur.execute("SELECT COUNT(*) FROM packets WHERE satellite = ?",
                        (satellite,))
        else:
            cur.execute("SELECT COUNT(*) FROM packets")
        return cur.fetchone()[0]


def recent_packets(n: int = 20, satellite: Optional[str] = None) -> list[dict]:
    """Return the *n* most recent packets as dicts."""
    with _cursor() as cur:
        if satellite:
            cur.execute(
                "SELECT * FROM packets WHERE satellite = ? "
                "ORDER BY received_at DESC LIMIT ?", (satellite, n))
        else:
            cur.execute(
                "SELECT * FROM packets ORDER BY received_at DESC LIMIT ?", (n,))
        return [dict(row) for row in cur.fetchall()]


def telemetry_series(key: str, since: Optional[str] = None,
                     satellite: Optional[str] = None) -> list[dict]:
    """Return time-series for a telemetry key as [{timestamp, value, unit}, ...]."""
    clauses = ["t.key = ?"]
    params: list = [key]

    if since:
        clauses.append("t.timestamp >= ?")
        params.append(since)
    if satellite:
        clauses.append("p.satellite = ?")
        params.append(satellite)

    where = " AND ".join(clauses)
    with _cursor() as cur:
        cur.execute(
            f"SELECT t.timestamp, t.value, t.unit "
            f"FROM telemetry t JOIN packets p ON t.packet_id = p.id "
            f"WHERE {where} ORDER BY t.timestamp",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def fsm_state_history(n: int = 500) -> list[dict]:
    """Return FSM state timeline from decoded packet JSON.

    Returns a list of dicts with keys: received_at, fsm_state, fsm_depl,
    uptime.  Only packets with a valid decoded_json containing FSM_state
    are included.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT received_at, decoded_json FROM packets "
            "WHERE decoded_json IS NOT NULL "
            "ORDER BY received_at DESC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()

    results = []
    for row in reversed(rows):  # oldest first
        try:
            decoded = json.loads(row["decoded_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if "FSM_state" not in decoded:
            continue
        results.append({
            "received_at": row["received_at"],
            "fsm_state": decoded.get("FSM_state", ""),
            "fsm_depl": decoded.get("FSM_depl", ""),
            "fsm_pay_set": decoded.get("FSM_pay_set", ""),
            "uptime": decoded.get("uptime", ""),
        })
    return results


def latest_fsm_state() -> Optional[dict]:
    """Return the most recent FSM state info, or None if no data."""
    with _cursor() as cur:
        cur.execute(
            "SELECT decoded_json FROM packets "
            "WHERE decoded_json IS NOT NULL "
            "ORDER BY received_at DESC LIMIT 20"
        )
        for row in cur.fetchall():
            try:
                decoded = json.loads(row["decoded_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            if "FSM_state" in decoded:
                return {
                    "fsm_state": decoded.get("FSM_state", ""),
                    "fsm_depl": decoded.get("FSM_depl", ""),
                    "fsm_pay_set": decoded.get("FSM_pay_set", ""),
                    "fsm_pan_light": decoded.get("FSM_pan_light", ""),
                    "fsm_payl_light": decoded.get("FSM_payl_light", ""),
                    "fsm_best_dir": decoded.get("FSM_best_dir", ""),
                    "uptime": decoded.get("uptime", ""),
                }
    return None


def all_telemetry_keys() -> list[str]:
    """List distinct telemetry keys stored in the database."""
    with _cursor() as cur:
        cur.execute("SELECT DISTINCT key FROM telemetry ORDER BY key")
        return [row[0] for row in cur.fetchall()]


# ──────────────────────────────────────────────
# Export helpers (post-mission analysis)
# ──────────────────────────────────────────────

def export_packets_csv(path: str, satellite: Optional[str] = None):
    """Export all packets to a CSV file."""
    import csv
    rows = recent_packets(n=10_000_000, satellite=satellite)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def export_telemetry_csv(path: str, key: Optional[str] = None,
                         satellite: Optional[str] = None):
    """Export telemetry time-series to a CSV file."""
    import csv
    with _cursor() as cur:
        clauses, params = [], []
        if key:
            clauses.append("t.key = ?")
            params.append(key)
        if satellite:
            clauses.append("p.satellite = ?")
            params.append(satellite)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cur.execute(
            f"SELECT p.satellite, p.station, t.key, t.timestamp, t.value, t.unit "
            f"FROM telemetry t JOIN packets p ON t.packet_id = p.id "
            f"{where} ORDER BY t.timestamp",
            params,
        )
        rows = cur.fetchall()
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["satellite", "station", "key", "timestamp", "value", "unit"])
        writer.writerows(rows)


def summary() -> dict:
    """Quick overview of what's in the database."""
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM packets")
        pkt_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM telemetry")
        telem_count = cur.fetchone()[0]
        cur.execute("SELECT MIN(received_at), MAX(received_at) FROM packets")
        row = cur.fetchone()
        return {
            "total_packets": pkt_count,
            "total_telemetry_rows": telem_count,
            "earliest_packet": row[0],
            "latest_packet": row[1],
            "telemetry_keys": all_telemetry_keys(),
        }
