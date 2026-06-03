"""
compare.py — Reconciliation script to merge packets from the AWS cloud server.

Pulls packets from the AWS ingest server that the Pi's local database
doesn't have yet, and inserts them via packet_store.  Deduplication relies
on the SHA-256 hash of ``raw_frame`` (the ``frame_hash`` column).

Two independent capture paths feed the AWS database:
  1. TinyGS webhook (packets forwarded by TinyGS to our AWS endpoint)
  2. Roof ground-station uploader

The Pi's own MQTT listener captures packets directly from TinyGS and stores
them locally.  This script closes the gap by pulling anything the Pi missed.

Usage:
  python compare.py                    # Run once
  python compare.py --daemon           # Run every 15 minutes
  python compare.py --interval 5       # Custom interval (minutes)
  python compare.py --full-sync        # Ignore last_sync, pull everything
  python compare.py --dry-run          # Show what would be merged
  python compare.py --status           # Show sync stats

Environment variables:
  CUBESAT_AWS_URL      Base URL of the AWS ingest server
  CUBESAT_INGEST_KEY   API key sent in the X-API-Key header
  CUBESAT_SLACK_WEBHOOK  (optional) Slack webhook for notifications
"""

import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import beacon_decoder
import packet_store

# ── Configuration ──────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / "backups"
STATE_FILE = STATE_DIR / "last_sync.txt"
LOG_FILE = STATE_DIR / "compare.log"

AWS_URL = os.environ.get("CUBESAT_AWS_URL", "")
API_KEY = os.environ.get("CUBESAT_INGEST_KEY", "")
SLACK_WEBHOOK_URL = os.environ.get("CUBESAT_SLACK_WEBHOOK", "")

DEFAULT_INTERVAL_MIN = 15
PAGE_LIMIT = 1000
HTTP_TIMEOUT = 30


# ── Logging ────────────────────────────────────

def setup_logging() -> logging.Logger:
    STATE_DIR.mkdir(exist_ok=True)

    logger = logging.getLogger("compare")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))
    logger.addHandler(ch)

    return logger


# ── Slack notifications ───────────────────────

def notify_slack(message: str, level: str = "warning"):
    """Post to Slack webhook. No-op when CUBESAT_SLACK_WEBHOOK is unset."""
    if not SLACK_WEBHOOK_URL:
        return
    emoji = {
        "info": ":white_check_mark:",
        "warning": ":warning:",
        "error": ":rotating_light:",
    }.get(level, ":bell:")
    payload = json.dumps({
        "text": f"{emoji} *CubeSAT Sync* — {message}",
        "username": "MissionStation Sync",
    })
    try:
        req = Request(
            SLACK_WEBHOOK_URL,
            data=payload.encode(),
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=10)
    except Exception as exc:
        logging.getLogger("compare").debug(f"Slack notify failed: {exc}")


# ── State file helpers ─────────────────────────

def read_last_sync() -> str:
    """Read last sync timestamp from the state file. Returns '' if none."""
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_last_sync(timestamp: str):
    """Persist the most recent received_at timestamp for the next run."""
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(timestamp + "\n", encoding="utf-8")


# ── AWS API client ─────────────────────────────

def fetch_packets(since: str, limit: int = PAGE_LIMIT) -> list[dict]:
    """GET /api/packets from the AWS server. Returns a list of packet dicts.

    Raises on HTTP or network errors so the caller can handle retries.
    """
    log = logging.getLogger("compare")

    params = f"limit={limit}"
    if since:
        params += f"&since={since}"
    url = f"{AWS_URL}/api/packets?{params}"

    req = Request(url)
    req.add_header("X-API-Key", API_KEY)
    req.add_header("Accept", "application/json")

    log.debug(f"GET {url}")
    resp = urlopen(req, timeout=HTTP_TIMEOUT)
    body = json.loads(resp.read().decode("utf-8"))
    packets = body.get("packets", [])
    log.debug(f"Received {len(packets)} packet(s) from AWS")
    return packets


# ── Packet processing ─────────────────────────

def process_packet(pkt: dict, *, dry_run: bool = False) -> str:
    """Merge a single AWS packet into the local database.

    Returns one of: "new", "skipped", "error".
    """
    log = logging.getLogger("compare")

    try:
        # Base64-decode the raw_frame back to bytes
        raw_b64 = pkt.get("raw_frame", "")
        raw_bytes = base64.b64decode(raw_b64) if raw_b64 else None

        if dry_run:
            # Check if we already have this frame by computing the hash
            import hashlib
            if raw_bytes is not None:
                frame_hash = hashlib.sha256(raw_bytes).hexdigest()
                # Use packet_store internals to check existence
                from packet_store import _cursor
                with _cursor() as cur:
                    cur.execute(
                        "SELECT id FROM packets WHERE frame_hash = ?",
                        (frame_hash,),
                    )
                    if cur.fetchone() is not None:
                        return "skipped"
            log.info(
                f"[DRY RUN] Would merge packet: sat={pkt.get('satellite', '?')} "
                f"station={pkt.get('station', '?')} "
                f"received_at={pkt.get('received_at', '?')}"
            )
            return "new"

        # Decode the beacon before storing so decoded_json is populated
        beacon_telemetry: dict = {}
        if raw_bytes and not pkt.get("crc_error", False):
            try:
                result = beacon_decoder.decode_beacon(raw_bytes)
                beacon_telemetry = result.get("telemetry", {})
            except Exception as exc:
                log.debug(f"Beacon decode failed (pre-store): {exc}")

        decoded_combined = {**pkt}
        if beacon_telemetry:
            decoded_combined.update(beacon_telemetry)   # top-level for FSM/light consumers
            decoded_combined["_beacon"] = beacon_telemetry

        # Store the packet (deduplication by frame_hash)
        pkt_id, was_new = packet_store.store_packet_if_new(
            satellite=pkt.get("satellite", ""),
            norad_id=pkt.get("norad_id"),
            station=pkt.get("station", ""),
            frequency_mhz=pkt.get("frequency_mhz"),
            rssi=pkt.get("rssi"),
            snr=pkt.get("snr"),
            crc_error=bool(pkt.get("crc_error", False)),
            raw_frame=raw_bytes,
            decoded=decoded_combined,
            source=pkt.get("source", "aws_sync"),
            received_at=pkt.get("received_at"),
        )

        if not was_new:
            return "skipped"

        # Store telemetry time-series for new packets
        if beacon_telemetry:
            try:
                readings = beacon_decoder.extract_telemetry_readings(beacon_telemetry)
                if readings:
                    packet_store.store_telemetry_batch(pkt_id, readings)
            except Exception as exc:
                log.debug(f"Telemetry store failed for packet #{pkt_id}: {exc}")

        log.info(
            f"Merged packet #{pkt_id}: sat={pkt.get('satellite', '?')} "
            f"station={pkt.get('station', '?')} "
            f"received_at={pkt.get('received_at', '?')}"
        )
        return "new"

    except Exception as exc:
        log.error(f"Error processing packet: {exc}")
        return "error"


# ── Core sync logic ───────────────────────────

def sync_once(*, full_sync: bool = False, dry_run: bool = False) -> dict:
    """Run one sync cycle: pull packets from AWS and merge locally.

    Returns a stats dict: {"new": N, "skipped": N, "errors": N}.
    Can be imported and called by other modules (e.g. db_backup.py).
    """
    log = setup_logging()
    stats = {"new": 0, "skipped": 0, "errors": 0}

    if not AWS_URL:
        log.error("CUBESAT_AWS_URL is not set — cannot sync")
        return stats
    if not API_KEY:
        log.error("CUBESAT_INGEST_KEY is not set — cannot sync")
        return stats

    since = "" if full_sync else read_last_sync()
    if full_sync:
        log.info("Full sync requested — pulling all packets from AWS")
    elif since:
        log.info(f"Syncing packets since {since}")
    else:
        log.info("No previous sync timestamp — pulling all packets from AWS")

    latest_received_at = since

    while True:
        try:
            packets = fetch_packets(since, limit=PAGE_LIMIT)
        except HTTPError as exc:
            msg = f"AWS API error: HTTP {exc.code} — {exc.reason}"
            log.error(msg)
            stats["errors"] += 1
            notify_slack(msg, "error")
            break
        except URLError as exc:
            msg = f"AWS connection error: {exc.reason}"
            log.error(msg)
            stats["errors"] += 1
            notify_slack(msg, "error")
            break
        except Exception as exc:
            msg = f"Unexpected error fetching packets: {exc}"
            log.error(msg)
            stats["errors"] += 1
            notify_slack(msg, "error")
            break

        if not packets:
            log.debug("No more packets to sync")
            break

        for pkt in packets:
            result = process_packet(pkt, dry_run=dry_run)
            stats[result] = stats.get(result, 0) + 1

            # Track the latest received_at for the state file
            pkt_time = pkt.get("received_at", "")
            if pkt_time and pkt_time > latest_received_at:
                latest_received_at = pkt_time

        # If we got a full page, there may be more — paginate
        if len(packets) >= PAGE_LIMIT:
            since = latest_received_at
            log.info(f"Page full ({PAGE_LIMIT} packets), fetching next page since {since}")
        else:
            break

    # Update state file with the latest timestamp (unless dry run)
    if not dry_run and latest_received_at and latest_received_at != read_last_sync():
        write_last_sync(latest_received_at)
        log.debug(f"Updated last_sync to {latest_received_at}")

    # Summary
    log.info(
        f"Sync complete: {stats['new']} new, "
        f"{stats['skipped']} duplicates skipped, "
        f"{stats['errors']} errors"
    )

    # Slack notification for new packets
    if stats["new"] > 0 and not dry_run:
        notify_slack(
            f"Merged {stats['new']} new packet(s) from AWS "
            f"({stats['skipped']} duplicates skipped)",
            "info",
        )
    if stats["errors"] > 0:
        notify_slack(
            f"Sync completed with {stats['errors']} error(s)",
            "warning",
        )

    return stats


# ── Status display ─────────────────────────────

def show_status():
    """Print sync status and database summary."""
    print(f"AWS URL:       {AWS_URL or '(not set)'}")
    print(f"API key:       {'configured' if API_KEY else '(not set)'}")
    print(f"Slack:         {'configured' if SLACK_WEBHOOK_URL else 'not configured'}")

    last_sync = read_last_sync()
    print(f"\nLast sync:     {last_sync or '(never)'}")
    if last_sync:
        try:
            sync_dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - sync_dt
            hours = age.total_seconds() / 3600
            print(f"  age:         {hours:.1f} hours ago")
        except ValueError:
            pass

    print(f"\nState file:    {STATE_FILE}")
    print(f"Log file:      {LOG_FILE}")

    total = packet_store.packet_count()
    print(f"\nLocal DB:      {total} total packets")

    s = packet_store.summary()
    if s.get("earliest_packet"):
        print(f"  earliest:    {s['earliest_packet']}")
        print(f"  latest:      {s['latest_packet']}")
        print(f"  telemetry:   {s['total_telemetry_rows']} rows")


# ── Daemon mode ───────────────────────────────

def run_daemon(interval_min: int):
    """Run sync_once() in a loop with the given interval."""
    log = setup_logging()
    log.info(f"Starting daemon mode (interval={interval_min}m)")
    print(f"Running sync every {interval_min} minute(s) — Ctrl+C to stop")

    while True:
        try:
            sync_once()
        except Exception as exc:
            log.error(f"Sync cycle failed: {exc}")
            notify_slack(f"Sync daemon error: {exc}", "error")

        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            log.info("Daemon stopped by user")
            print("\nStopped.")
            break


# ── Main ───────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CubeSAT AWS ↔ Pi packet reconciliation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run continuously at a fixed interval",
    )
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL_MIN,
        help=f"Sync interval in minutes for daemon mode (default: {DEFAULT_INTERVAL_MIN})",
    )
    parser.add_argument(
        "--full-sync", action="store_true",
        help="Ignore last sync timestamp and pull all packets",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be merged without writing to the database",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show sync status and database summary",
    )
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.daemon:
        run_daemon(args.interval)
    else:
        stats = sync_once(full_sync=args.full_sync, dry_run=args.dry_run)
        if stats["errors"] > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
