"""
TinyGS historical packet backfill.

The live MQTT listener (``tinygs_mqtt.py``) only receives packets as they
arrive. This tool pulls *historical* packets that TinyGS already logged for
our satellite (e.g. the ones heard before our listener was running) and
imports them into the local packet store.

It reuses the exact same decode + store path as the live listener:
    - decodes the raw frame with ``beacon_decoder``
    - stores via ``packet_store.store_packet_if_new`` (dedup by frame_hash)
    - preserves each packet's ORIGINAL receive time (not import time)
    - tags rows ``source="tinygs_backfill"`` so they're distinguishable

Because dedup keys on the SHA-256 of the raw frame, this is safe to re-run
and safe to run alongside the live MQTT listener - duplicates are skipped.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
1. Open your satellite's page on https://tinygs.com, press F12 -> Network,
   reload, and find the XHR request to ``api.tinygs.com`` that returns the
   packet list. Copy its full URL.

2. Dry run first - this fetches, prints the raw JSON keys of the first
   packet and how each field mapped, and writes NOTHING:

     python tinygs_backfill.py --url "<paste API URL>" --dry-run

   Check the parsed output. If a field mapped to None that shouldn't have,
   the raw-keys dump tells us the real key name - tweak FIELD_CANDIDATES
   below (or send it to me) and re-run.

3. Real import:

     python tinygs_backfill.py --url "<paste API URL>"

   Add --token "<bearer>" if you have a TinyGS API token (from the Telegram
   bot) and are hitting an authenticated/rate-limited endpoint.
"""

import argparse
import base64
import binascii
import json
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import beacon_decoder
import packet_store

SOURCE = "tinygs_backfill"

# Candidate JSON keys for each field, tried in order. TinyGS's field names
# vary a little between API versions; adjust here if --dry-run shows a miss.
FIELD_CANDIDATES = {
    "satellite":  ["satellite", "satName", "sat"],
    "norad":      ["NORAD", "norad", "norad_id", "noradID"],
    "station":    ["station", "stationName", "ground_station"],
    "frequency":  ["frequency", "freq"],
    "rssi":       ["rssi", "RSSI"],
    "snr":        ["snr", "SNR"],
    "crc_error":  ["crc_error", "crc", "crcError"],
    "raw":        ["data", "raw", "frame", "hex", "payload"],
    "timestamp":  ["serverTime", "unix", "unix_GS_time", "t", "time", "timestamp", "date"],
}


def _pick(pkt: dict, field: str):
    """Return the first present candidate value for a logical field."""
    for key in FIELD_CANDIDATES[field]:
        if key in pkt and pkt[key] not in (None, ""):
            return pkt[key]
    return None


def _decode_raw(value) -> bytes | None:
    """Decode a raw-frame field that may be base64 or hex."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value).strip()
    # Try base64 first (TinyGS 'data' is base64), then hex.
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        pass
    try:
        return bytes.fromhex(s)
    except ValueError:
        return None


def _to_iso(value) -> str:
    """Normalise a packet timestamp (ms/s epoch or ISO string) to ISO-8601 UTC."""
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    # Numeric epoch - seconds or milliseconds.
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        num = float(value)
        if num > 1e12:      # milliseconds
            num /= 1000.0
        return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    # Assume ISO-8601 string; normalise trailing Z.
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def _extract_list(payload):
    """Find the packet list inside a variety of response container shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("packets", "data", "results", "items", "rows"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def load_file(path: str) -> list:
    """Load packets from a saved JSON response file (browser fallback)."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Failed to read/parse {path}: {exc}")
    packets = _extract_list(payload)
    if not packets:
        sys.exit(f"No packet list found in {path}. Top-level type was {type(payload).__name__}.")
    return packets


# A real browser User-Agent + tinygs.com Referer/Origin. TinyGS's edge stalls
# bare script requests (no response -> read timeout); browser-like headers
# usually get through. If it still times out, use --file (see module docstring).
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://tinygs.com/",
    "Origin": "https://tinygs.com",
}


def fetch(url: str, token: str | None,
          session_token: str | None = None, user_id: str | None = None) -> list:
    headers = dict(_BROWSER_HEADERS)
    # TinyGS v4 authenticates with custom sessiontoken + userid headers (grab
    # them from a logged-in browser's DevTools request), not a Bearer token.
    if session_token:
        headers["sessiontoken"] = session_token
    if user_id:
        headers["userid"] = user_id
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except HTTPError as exc:
        sys.exit(f"HTTP {exc.code} from TinyGS: {exc.reason}")
    except (URLError, json.JSONDecodeError, TimeoutError) as exc:
        sys.exit(f"Failed to fetch/parse TinyGS response: {exc}\n"
                 "If this is a timeout, the API is likely blocking scripts. Fall back to:\n"
                 "  1. open the --url in your browser, save the JSON to packets.json\n"
                 "  2. python tinygs_backfill.py --file packets.json [--dry-run]")
    packets = _extract_list(payload)
    if not packets:
        sys.exit("No packet list found in response. Top-level type was "
                 f"{type(payload).__name__}; keys={list(payload)[:10] if isinstance(payload, dict) else 'n/a'}")
    return packets


def parse_packet(pkt: dict) -> dict:
    """Map a TinyGS packet object onto our store fields."""
    raw = _decode_raw(_pick(pkt, "raw"))
    norad = _pick(pkt, "norad")
    freq = _pick(pkt, "frequency")
    rssi = _pick(pkt, "rssi")
    snr = _pick(pkt, "snr")
    return {
        "satellite": str(_pick(pkt, "satellite") or ""),
        "norad_id": int(norad) if norad is not None else None,
        "station": str(_pick(pkt, "station") or ""),
        "frequency_mhz": float(freq) if freq is not None else None,
        "rssi": float(rssi) if rssi is not None else None,
        "snr": float(snr) if snr is not None else None,
        "crc_error": bool(_pick(pkt, "crc_error")),
        "raw_frame": raw,
        "received_at": _to_iso(_pick(pkt, "timestamp")),
    }


def store_one(fields: dict, original: dict, source: str = SOURCE) -> str:
    """Decode + store one parsed packet. Returns 'new' | 'dup' | 'nodata'.

    ``source`` tags the DB row so different importers (backfill vs the live
    browser poller) stay distinguishable; defaults to this module's SOURCE.
    """
    raw = fields["raw_frame"]

    beacon_telemetry: dict = {}
    if raw and not fields["crc_error"]:
        try:
            beacon_telemetry = beacon_decoder.decode_beacon(raw).get("telemetry", {}) or {}
        except Exception:
            beacon_telemetry = {}

    # Mirror the live listener: keep the full TinyGS object, merge beacon
    # fields at the top level for FSM/light consumers, preserve under _beacon.
    decoded_combined = {**original}
    if beacon_telemetry:
        decoded_combined.update(beacon_telemetry)
        decoded_combined["_beacon"] = beacon_telemetry

    pkt_id, was_new = packet_store.store_packet_if_new(
        satellite=fields["satellite"],
        norad_id=fields["norad_id"],
        station=fields["station"],
        frequency_mhz=fields["frequency_mhz"],
        rssi=fields["rssi"],
        snr=fields["snr"],
        crc_error=fields["crc_error"],
        raw_frame=raw,
        decoded=decoded_combined if decoded_combined else None,
        source=source,
        received_at=fields["received_at"],
    )
    if not was_new:
        return "dup"

    readings: list[tuple[str, float, str]] = []
    if fields["rssi"] is not None:
        readings.append(("rssi", fields["rssi"], "dBm"))
    if fields["snr"] is not None:
        readings.append(("snr", fields["snr"], "dB"))
    if beacon_telemetry:
        readings.extend(beacon_decoder.extract_telemetry_readings(beacon_telemetry))
    if readings:
        packet_store.store_telemetry_batch(pkt_id, readings, timestamp=fields["received_at"])

    return "new" if raw else "nodata"


def main():
    ap = argparse.ArgumentParser(description="Backfill historical TinyGS packets into the local DB.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Full TinyGS API URL returning the packet list (from DevTools).")
    src.add_argument("--file", nargs="+", metavar="FILE",
                     help="One or more local JSON files of packets (saved from your browser). "
                          "Duplicates across files are skipped, so overlapping pages are fine.")
    ap.add_argument("--token", help="Optional TinyGS API bearer token.")
    ap.add_argument("--session-token", help="TinyGS 'sessiontoken' header (from a logged-in browser).")
    ap.add_argument("--user-id", help="TinyGS 'userid' header (same as your MQTT username).")
    ap.add_argument("--norad", type=int, help="If set, only import packets matching this NORAD id.")
    ap.add_argument("--dry-run", action="store_true", help="Fetch and parse, but write nothing.")
    args = ap.parse_args()

    if args.file:
        packets = []
        for fp in args.file:
            packets.extend(load_file(fp))
        print(f"Loaded {len(packets)} packets from {len(args.file)} file(s).\n")
    else:
        packets = fetch(args.url, args.token, args.session_token, args.user_id)
        print(f"Loaded {len(packets)} packets from TinyGS.\n")

    if args.dry_run and packets:
        print("--- raw keys of first packet ---")
        print(sorted(packets[0].keys()))
        print("\n--- parsed interpretation of first packet ---")
        parsed = parse_packet(packets[0])
        preview = {k: (f"<{len(v)} bytes>" if isinstance(v, (bytes, bytearray)) else v)
                   for k, v in parsed.items()}
        print(json.dumps(preview, indent=2, default=str))
        print()

    counts = {"new": 0, "dup": 0, "nodata": 0, "skip": 0, "error": 0}
    for pkt in packets:
        if not isinstance(pkt, dict):
            counts["skip"] += 1
            continue
        fields = parse_packet(pkt)
        if args.norad is not None and fields["norad_id"] != args.norad:
            counts["skip"] += 1
            continue
        if args.dry_run:
            continue
        try:
            counts[store_one(fields, pkt)] += 1
        except Exception as exc:
            counts["error"] += 1
            print(f"  error on packet: {exc}", file=sys.stderr)

    print("--- summary ---")
    if args.dry_run:
        print("(dry run - nothing written)")
    print(f"  new (stored):        {counts['new']}")
    print(f"  duplicates skipped:  {counts['dup']}")
    print(f"  stored w/o raw data: {counts['nodata']}")
    print(f"  skipped (filter):    {counts['skip']}")
    print(f"  errors:              {counts['error']}")


if __name__ == "__main__":
    main()
