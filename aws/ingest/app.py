# Lambda handler for POST /ingest/{token}
#
# TinyGS calls this endpoint each time one of its ground stations receives a
# signal from our satellite.  The {token} path parameter is compared to the
# INGEST_SECRET environment variable; mismatches are rejected with 403.
#
# FIELD NAMES — why this used to 400 every TinyGS POST
# ----------------------------------------------------
# This endpoint was originally written to require the frame in a field named
# "data" (plus "frequency"/"station"/"unix_GS_time").  TinyGS does NOT use
# those names: its own API/webhook serializes the frame as "raw", frequency as
# "freq", the station as "stationNumber", and the time as "serverTime" (epoch
# MILLISECONDS).  So every real packet failed the `body["data"]` check and came
# back "400 Missing required field: data" — which the TinyGS admins saw and
# (reasonably) guessed was an auth problem.  It is not: a bad token returns 403.
#
# The fix: accept the frame under any known alias, tolerate API Gateway's
# base64-encoded bodies, log the actual payload shape (so we can confirm the
# real schema from CloudWatch rather than guessing), and NEVER bounce a POST
# with a 4xx just because a field name is unexpected — capture it, ack 200, and
# adapt.  A webhook that keeps getting errors is a webhook the sender turns off.
#
# Valid packets are stored verbatim in DynamoDB with a generated UUID, an
# ISO-8601 received_at timestamp, and a 90-day TTL so old rows expire.  This
# Lambda does NOT decode the beacon binary — that is done Pi-side.

import base64
import json
import os
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME    = os.environ["TABLE_NAME"]
SATELLITE_ID  = os.environ.get("SATELLITE_ID", "CUBESAT-1")
INGEST_SECRET = os.environ["INGEST_SECRET"]

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(TABLE_NAME)

# TinyGS field aliases (its API/webhook names on the left of each group are the
# ones actually observed; the rest are defensive).  If a future TinyGS payload
# uses a name not listed here, the "ingest payload keys=..." log line below
# tells us exactly what to add.
_FRAME_KEYS   = ("data", "raw", "raw_data", "raw_frame", "frame", "packet", "payload")
_STATION_KEYS = ("station", "stationName", "stationNumber", "ground_station", "gs")
_FREQ_KEYS    = ("frequency", "freq")
_NORAD_KEYS   = ("NORAD", "norad", "norad_id", "noradId")
_TIME_KEYS    = ("unix_GS_time", "serverTime", "gs_time", "time", "timestamp")

MAX_FRAME_CHARS = 20000   # a HUCSat frame is ~280 base64 chars; this is generous


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _first(body: dict, keys):
    """Return the first present, non-empty value among `keys`."""
    for k in keys:
        v = body.get(k)
        if v not in (None, ""):
            return v
    return None


def _dec(value):
    """Convert a numeric value to Decimal for DynamoDB, or return None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _read_body(event: dict) -> str:
    """Return the request body as text, transparently undoing API Gateway's
    base64 wrapping (HTTP API base64-encodes bodies for some content types;
    if TinyGS ever sends one, json.loads on the raw value would 400)."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8", "replace")
        except Exception:
            logger.warning("body flagged isBase64Encoded but failed to decode")
    return raw


def _gs_time_seconds(value):
    """Normalize a ground-station time to epoch SECONDS (int). TinyGS
    'serverTime' is milliseconds; 'unix_GS_time' is seconds."""
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    if n > 100_000_000_000:   # magnitude says milliseconds
        n //= 1000
    return n


def handler(event, context):
    # --- Auth: secret token in the URL path (/ingest/{token}) ---
    token = (event.get("pathParameters") or {}).get("token", "")
    if token != INGEST_SECRET:
        logger.warning("Rejected request: token mismatch")
        return _resp(403, {"error": "Forbidden"})

    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=90)).timestamp())

    # --- Parse body (tolerant of base64 + content-type quirks) ---
    raw_body = _read_body(event)
    try:
        body = json.loads(raw_body) if raw_body.strip() else {}
    except (json.JSONDecodeError, AttributeError):
        body = None

    if not isinstance(body, dict):
        # Could not parse JSON. Do NOT 400 — that just makes TinyGS give up.
        # Persist the raw bytes verbatim so nothing is lost and we can inspect
        # the true format in CloudWatch, then ack.
        logger.error("Unparseable body (%d bytes): %r", len(raw_body), raw_body[:1000])
        _put({
            "satellite_id": SATELLITE_ID,
            "received_at":  now.isoformat(),
            "packet_id":    str(uuid4()),
            "raw_payload":  raw_body[:MAX_FRAME_CHARS],
            "parse_error":  True,
            "ttl":          ttl,
        })
        return _resp(200, {"status": "stored_unparsed"})

    # Log the ACTUAL field names TinyGS sends — this is how we confirm the real
    # webhook schema going forward instead of guessing.
    logger.info("ingest payload keys=%s", sorted(body.keys()))

    raw_data = _first(body, _FRAME_KEYS)
    if isinstance(raw_data, str) and len(raw_data) > MAX_FRAME_CHARS:
        logger.warning("frame field oversized (%d chars)", len(raw_data))

    # Optional NORAD sanity check — only when NORAD_ID is explicitly set, and it
    # now WARNS rather than rejecting: a NORAD mismatch must never silently 400
    # and kill the whole feed (that was another latent 400 source).
    norad = _first(body, _NORAD_KEYS)
    expected_norad = os.environ.get("NORAD_ID")
    if norad and expected_norad and str(norad) != str(expected_norad):
        logger.warning("NORAD mismatch: expected %s, got %s (storing anyway)",
                       expected_norad, norad)

    item = {
        "satellite_id":    SATELLITE_ID,
        "received_at":     now.isoformat(),
        "packet_id":       str(uuid4()),
        "gs_time":         _gs_time_seconds(_first(body, _TIME_KEYS)),
        "ground_station":  _first(body, _STATION_KEYS),
        "snr":             _dec(body.get("snr")),
        "rssi":            _dec(body.get("rssi")),
        "frequency":       _dec(_first(body, _FREQ_KEYS)),
        "raw_data":        raw_data,
        "tinygs_payload":  json.dumps(body),
        "crc_error":       bool(body.get("crc_error", False)),
        "ttl":             ttl,
    }
    if raw_data is None:
        # JSON parsed but no recognized frame field. Still store (the full
        # payload is kept in tinygs_payload) and ack 200 so the feed keeps
        # flowing — then add the real key to _FRAME_KEYS once the log reveals it.
        logger.warning("no frame field found among %s; keys were %s",
                       _FRAME_KEYS, sorted(body.keys()))

    # DynamoDB rejects explicit None values.
    item = {k: v for k, v in item.items() if v is not None}
    _put(item)

    logger.info("Stored packet %s from station %s (frame=%s)",
                item["packet_id"], item.get("ground_station"),
                "yes" if raw_data else "MISSING")

    return _resp(200, {
        "packet_id":   item["packet_id"],
        "received_at": item["received_at"],
    })


def _put(item: dict):
    """PutItem with a safety net: if the item exceeds DynamoDB's 400 KB limit
    (an unexpectedly large payload), retry without the verbatim copy."""
    try:
        table.put_item(Item=item)
    except Exception as exc:
        logger.error("put_item failed (%s); retrying without tinygs_payload", exc)
        item.pop("tinygs_payload", None)
        table.put_item(Item=item)
