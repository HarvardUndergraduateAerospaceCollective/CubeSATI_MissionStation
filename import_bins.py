"""
import_bins.py — import raw .bin beacon frames (downloaded from TinyGS packet
pages) into the local packet store.

These early-mission frames exist only as local .bin files: the TinyGS API's
recent window no longer covers them, so they're the only surviving copy of the
first checkout day (early detumble / battery data).

TIMESTAMP CAVEAT: a raw .bin frame carries no reception time (the beacon RTC
reads year 2000). We use each file's modification time (~when you downloaded it,
which for the early checkout packets is close to when they were received). This
is APPROXIMATE — good enough to place them early on the timeline, flagged via
source="bin_backfill". Dedup is by frame_hash, so re-running is safe and these
won't collide with live poller packets.

USAGE
-----
    python import_bins.py <dir-or-glob>        # e.g. ~/hucsat_bins  or  "downloads/packet*.bin"
    python import_bins.py <dir> --dry-run
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone

import beacon_decoder
import packet_store

SOURCE = "bin_backfill"


def _iter_bins(path: str):
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        yield from sorted(glob.glob(os.path.join(path, "*.bin")))
    else:
        yield from sorted(glob.glob(path))  # treat as a glob pattern


def _looks_like_hucsat(raw: bytes, telemetry: dict) -> bool:
    """Only import HUCSat frames: real telemetry, or the \\xff\\xff\\x00 sync header."""
    if telemetry:
        return True
    sync = getattr(beacon_decoder, "HUCSAT_SYNC", b"\xff\xff\x00")
    return raw.startswith(sync)


def import_bins(path: str, dry_run: bool = False) -> dict:
    counts = {"new": 0, "dup": 0, "nodata": 0, "skipped": 0, "error": 0, "files": 0}
    for fp in _iter_bins(path):
        counts["files"] += 1
        try:
            raw = open(fp, "rb").read()
            received_at = datetime.fromtimestamp(
                os.path.getmtime(fp), tz=timezone.utc).isoformat()

            telemetry = {}
            try:
                telemetry = beacon_decoder.decode_beacon(raw).get("telemetry", {}) or {}
            except Exception:
                telemetry = {}

            if not _looks_like_hucsat(raw, telemetry):
                counts["skipped"] += 1
                continue

            decoded = dict(telemetry)
            if telemetry:
                decoded["_beacon"] = telemetry

            if dry_run:
                counts["new" if telemetry else "nodata"] += 1
                continue

            pkt_id, was_new = packet_store.store_packet_if_new(
                satellite="HUCSat", norad_id=99999, raw_frame=raw,
                decoded=decoded or None, source=SOURCE, received_at=received_at,
            )
            if not was_new:
                counts["dup"] += 1
                continue

            if telemetry:
                readings = beacon_decoder.extract_telemetry_readings(telemetry)
                if readings:
                    packet_store.store_telemetry_batch(pkt_id, readings, timestamp=received_at)
                counts["new"] += 1
            else:
                counts["nodata"] += 1
        except Exception as exc:
            counts["error"] += 1
            print(f"  error on {os.path.basename(fp)}: {exc}", file=sys.stderr)
    return counts


def main():
    ap = argparse.ArgumentParser(description="Import raw .bin beacon frames into the DB.")
    ap.add_argument("path", help="Directory containing *.bin files, or a glob pattern.")
    ap.add_argument("--dry-run", action="store_true", help="Decode/count but write nothing.")
    args = ap.parse_args()

    counts = import_bins(args.path, dry_run=args.dry_run)
    print(f"\n.bin import {'(dry run) ' if args.dry_run else ''}from {counts['files']} files:")
    print(f"  new (stored):        {counts['new']}")
    print(f"  duplicates skipped:  {counts['dup']}")
    print(f"  stored w/o telemetry:{counts['nodata']}")
    print(f"  skipped (not HUCSat):{counts['skipped']}")
    print(f"  errors:              {counts['error']}")
    if counts["files"] == 0:
        print("  (no .bin files matched — check the path)")


if __name__ == "__main__":
    main()
