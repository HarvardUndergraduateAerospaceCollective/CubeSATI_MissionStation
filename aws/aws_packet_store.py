"""
AWS Packet Store — SQLite database for satellite telemetry on the ingest server.

Mirrors the schema from the Pi-side ``packet_store.py`` so that data can be
synced bi-directionally without schema translation.  The database path is
configurable via the ``CUBESAT_DB_PATH`` environment variable (defaults to
``./aws_mission_data.db``).

All timestamps are stored as ISO-8601 UTC strings.
"""

import base64
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

_DB_PATH = os.environ.get("CUBESAT_DB_PATH", "./aws_mission_data.db")
_local = threading.local()

# ──────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection (creates DB/tables on first call)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
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
            source        TEXT    NOT NULL DEFAULT 'unknown',
            frame_hash    TEXT                        -- SHA-256 hex digest of raw_frame
        );

        CREATE INDEX IF NOT EXISTS idx_packets_sat
            ON packets(satellite);
        CREATE INDEX IF NOT EXISTS idx_packets_time
            ON packets(received_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_packets_frame_hash
            ON packets(frame_hash) WHERE frame_hash IS NOT NULL;

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

    # Migration: add frame_hash column to existing databases that lack it.
    try:
        conn.execute("ALTER TABLE packets ADD COLUMN frame_hash TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_packets_frame_hash "
        "ON packets(frame_hash) WHERE frame_hash IS NOT NULL"
    )
    conn.commit()


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
    frame_hash = hashlib.sha256(raw_frame).hexdigest() if raw_frame is not None else None

    with _cursor() as cur:
        cur.execute(
            """INSERT INTO packets
               (received_at, satellite, norad_id, station,
                frequency_mhz, rssi, snr, crc_error,
                raw_frame, decoded_json, source, frame_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (received_at, satellite, norad_id, station,
             frequency_mhz, rssi, snr, int(crc_error),
             raw_frame, decoded_json, source, frame_hash),
        )
        return cur.lastrowid


def store_packet_if_new(
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
) -> tuple[int, bool]:
    """Insert a packet only if its frame has not been seen before.

    Returns ``(packet_id, was_new)`` — if a duplicate exists the
    existing row id is returned and *was_new* is ``False``.
    Packets without a *raw_frame* (hash is NULL) are always inserted.
    """
    if received_at is None:
        received_at = datetime.now(timezone.utc).isoformat()
    decoded_json = json.dumps(decoded) if decoded else None
    frame_hash = hashlib.sha256(raw_frame).hexdigest() if raw_frame is not None else None

    if frame_hash is None:
        # No raw frame — can't dedup, always insert
        packet_id = store_packet(
            satellite=satellite, norad_id=norad_id, station=station,
            frequency_mhz=frequency_mhz, rssi=rssi, snr=snr,
            crc_error=crc_error, raw_frame=raw_frame, decoded=decoded,
            source=source, received_at=received_at,
        )
        return (packet_id, True)

    # Single transaction: check + insert atomically
    with _cursor() as cur:
        cur.execute("SELECT id FROM packets WHERE frame_hash = ?", (frame_hash,))
        row = cur.fetchone()
        if row is not None:
            return (row[0], False)

        cur.execute(
            """INSERT INTO packets
               (received_at, satellite, norad_id, station,
                frequency_mhz, rssi, snr, crc_error,
                raw_frame, decoded_json, source, frame_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (received_at, satellite, norad_id, station,
             frequency_mhz, rssi, snr, int(crc_error),
             raw_frame, decoded_json, source, frame_hash),
        )
        return (cur.lastrowid, True)


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


def packets_since(since_iso: str, limit: int = 1000) -> list[dict]:
    """Return packets with ``received_at >= since_iso``, oldest first.

    The *raw_frame* bytes column is base64-encoded as a string so that
    the returned dicts are directly JSON-serializable.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM packets WHERE received_at >= ? "
            "ORDER BY received_at LIMIT ?",
            (since_iso, limit),
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("raw_frame") is not None:
                d["raw_frame"] = base64.b64encode(d["raw_frame"]).decode("ascii")
            rows.append(d)
        return rows


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
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("raw_frame") is not None:
                d["raw_frame"] = base64.b64encode(d["raw_frame"]).decode("ascii")
            rows.append(d)
        return rows
