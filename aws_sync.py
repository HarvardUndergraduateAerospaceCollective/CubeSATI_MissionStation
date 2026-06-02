"""
AWS Sync — Polls an AWS REST endpoint for satellite packets and upserts them
into the local SQLite database.

Architecture
------------
TinyGS ground stations POST received packets to an AWS API Gateway endpoint
(primary collection path).  The Pi also runs a local TinyGS MQTT listener
(tinygs_mqtt.py) as a backup.  This module handles the third case: after an
MQTT disconnect, the Pi may have missed packets that AWS already collected.
It periodically polls AWS, pulling everything since the last sync, and upserts
each packet into the local DB to fill any gaps.

Dedup
-----
packet_store.store_packet_if_new() deduplicates by frame_hash (SHA-256 of
raw bytes).  Packets already in the DB — whether from MQTT or a prior sync
— are silently skipped.  ``was_new=False`` means it was a duplicate.

Configuration (environment variables)
--------------------------------------
AWS_SYNC_URL      Base URL of the AWS API endpoint (required).
                  e.g. https://xxx.execute-api.us-east-1.amazonaws.com/prod
AWS_SYNC_API_KEY  API key sent as x-api-key header (optional but recommended).
AWS_SYNC_INTERVAL Poll interval in seconds (default: 60).

Usage
-----
As a background thread::

    from aws_sync import start_sync
    start_sync()   # returns Thread or None if AWS_SYNC_URL not set

Standalone::

    python aws_sync.py

State
-----
Last-synced cursor is stored in .aws_sync_state.json (next to this script).
The file survives reboots so the sync never re-fetches the full history.

NOTE: gs_time is preserved in decoded_json (the full TinyGS payload) but
is not stored as a separate column.
"""

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import beacon_decoder
import packet_store

log = logging.getLogger("aws_sync")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

AWS_SYNC_URL      = os.environ.get("AWS_SYNC_URL", "")
AWS_SYNC_API_KEY  = os.environ.get("AWS_SYNC_API_KEY", "")
AWS_SYNC_INTERVAL = int(os.environ.get("AWS_SYNC_INTERVAL", "60"))

_STATE_FILE = Path(__file__).parent / ".aws_sync_state.json"
_EPOCH      = "1970-01-01T00:00:00Z"
_TIMEOUT    = 15   # seconds for HTTP requests
_PAGE_LIMIT = 200  # packets per poll


# ──────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────

def _load_last_synced() -> str:
    """Return the ISO-8601 cursor from state file, or epoch if missing."""
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data.get("last_synced", _EPOCH)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _EPOCH


def _save_last_synced(ts: str) -> None:
    """Persist the ISO-8601 cursor to state file."""
    try:
        _STATE_FILE.write_text(
            json.dumps({"last_synced": ts}), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("Could not save sync state: %s", exc)


# ──────────────────────────────────────────────
# HTTP fetch
# ──────────────────────────────────────────────

def _fetch_packets(since: str) -> dict:
    """GET /packets?since=<since>&limit=<limit> and return parsed JSON.

    Raises URLError / ValueError on network or parse failures — callers
    should catch and log rather than crash.
    """
    url = f"{AWS_SYNC_URL}/packets?since={since}&limit={_PAGE_LIMIT}"
    headers = {"Accept": "application/json"}
    if AWS_SYNC_API_KEY:
        headers["x-api-key"] = AWS_SYNC_API_KEY

    req = Request(url, headers=headers)
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()

    return json.loads(raw)


# ──────────────────────────────────────────────
# Packet processing
# ──────────────────────────────────────────────

def _process_and_upsert(pkt: dict) -> bool:
    """Decode one AWS packet dict, upsert into local DB.

    Returns True if the packet was newly inserted, False if it was a duplicate.
    """
    # ── Extract envelope fields ──────────────────────────────────
    satellite  = pkt.get("satellite", "")
    norad_id   = pkt.get("NORAD", pkt.get("norad"))
    station    = pkt.get("station", pkt.get("stationName", ""))
    freq       = pkt.get("frequency")
    rssi       = pkt.get("rssi")
    snr        = pkt.get("snr")
    crc_error  = bool(pkt.get("crc_error", False))
    gs_time    = pkt.get("gs_time") or pkt.get("unix_GS_time")
    received_at = pkt.get("received_at") or datetime.now(timezone.utc).isoformat()

    # ── Decode raw satellite bytes ───────────────────────────────
    raw_b64 = pkt.get("raw_data", "") or pkt.get("data", "")
    try:
        raw_bytes = base64.b64decode(raw_b64) if raw_b64 else None
    except Exception:
        raw_bytes = None
        log.warning("aws_sync: failed to base64-decode 'data' field for station=%s gs_time=%s",
                    station, gs_time)

    # ── Run beacon decoder ───────────────────────────────────────
    beacon_telemetry: dict = {}
    if raw_bytes and not crc_error:
        try:
            result = beacon_decoder.decode_beacon(raw_bytes)
            beacon_telemetry = result.get("telemetry", {})
        except Exception:
            log.debug("aws_sync: beacon decode failed", exc_info=True)

    decoded_combined = {**pkt}
    if beacon_telemetry:
        decoded_combined["_beacon"] = beacon_telemetry

    # ── Insert into local DB (dedup by frame_hash) ────────────────
    pkt_id, was_new = packet_store.store_packet_if_new(
        satellite=str(satellite),
        norad_id=int(norad_id) if norad_id is not None else None,
        station=str(station),
        frequency_mhz=float(freq) if freq is not None else None,
        rssi=float(rssi) if rssi is not None else None,
        snr=float(snr) if snr is not None else None,
        crc_error=crc_error,
        raw_frame=raw_bytes,
        decoded=decoded_combined,
        source="aws_sync",
        received_at=received_at,
    )

    if not was_new:
        log.debug("aws_sync: duplicate skipped  station=%s  gs_time=%s", station, gs_time)
        return False

    # ── Store telemetry time-series ──────────────────────────────
    readings: list[tuple[str, float, str]] = []
    if rssi is not None:
        readings.append(("rssi", float(rssi), "dBm"))
    if snr is not None:
        readings.append(("snr", float(snr), "dB"))
    if beacon_telemetry:
        readings.extend(beacon_decoder.extract_telemetry_readings(beacon_telemetry))

    if readings:
        packet_store.store_telemetry_batch(pkt_id, readings, timestamp=received_at)

    log.info(
        "aws_sync: stored packet #%d  sat=%s  station=%s  rssi=%s  snr=%s  beacon_fields=%d",
        pkt_id, satellite, station, rssi, snr, len(beacon_telemetry),
    )
    return True


# ──────────────────────────────────────────────
# Sync loop
# ──────────────────────────────────────────────

def _sync_once() -> int:
    """Fetch one batch from AWS, upsert all packets, advance cursor.

    Returns the number of newly inserted packets.
    """
    since = _load_last_synced()
    log.debug("aws_sync: fetching packets since %s", since)

    response = _fetch_packets(since)

    packets = response.get("packets", response if isinstance(response, list) else [])
    if not packets:
        log.debug("aws_sync: no new packets since %s", since)
        return 0

    inserted = 0
    latest_ts = since

    for pkt in packets:
        try:
            was_new = _process_and_upsert(pkt)
            if was_new:
                inserted += 1
        except Exception:
            log.exception("aws_sync: error processing packet: %s", pkt)

        # Advance cursor to the latest timestamp seen, regardless of insert result.
        # This prevents re-fetching duplicates on every poll.
        pkt_ts = pkt.get("received_at") or ""
        if pkt_ts and pkt_ts > latest_ts:
            latest_ts = pkt_ts

    if latest_ts != since:
        _save_last_synced(latest_ts)
        log.debug("aws_sync: cursor advanced to %s", latest_ts)

    log.info("aws_sync: sync complete — %d new / %d total", inserted, len(packets))
    return inserted


def _sync_loop() -> None:
    """Poll AWS indefinitely, sleeping AWS_SYNC_INTERVAL between each cycle."""
    log.info("aws_sync: starting sync loop (interval=%ds, url=%s)", AWS_SYNC_INTERVAL, AWS_SYNC_URL)
    while True:
        try:
            _sync_once()
        except (URLError, OSError) as exc:
            log.warning("aws_sync: network error, will retry next interval: %s", exc)
        except Exception:
            log.exception("aws_sync: unexpected error in sync loop, will retry")
        time.sleep(AWS_SYNC_INTERVAL)


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def start_sync() -> threading.Thread | None:
    """Start the AWS sync background thread.

    Returns the Thread object, or None if AWS_SYNC_URL is not configured.
    """
    if not AWS_SYNC_URL:
        log.warning(
            "aws_sync: AWS_SYNC_URL not set — sync disabled. "
            "Set the AWS_SYNC_URL environment variable to enable."
        )
        return None

    t = threading.Thread(target=_sync_loop, name="aws-sync", daemon=True)
    t.start()
    log.info("aws_sync: background sync thread started")
    return t


# ──────────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    if not AWS_SYNC_URL:
        print(
            "ERROR: AWS_SYNC_URL environment variable not set.\n"
            "Example: export AWS_SYNC_URL=https://xxx.execute-api.us-east-1.amazonaws.com/prod"
        )
        raise SystemExit(1)

    print(f"Starting AWS sync (interval={AWS_SYNC_INTERVAL}s, url={AWS_SYNC_URL}) - Ctrl+C to stop")
    try:
        _sync_loop()
    except KeyboardInterrupt:
        print("\nStopped.")
