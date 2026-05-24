# Lambda handler for POST /ingest/{token}
#
# TinyGS calls this endpoint each time one of its ground stations receives a
# signal from our satellite.  The {token} path parameter is compared to the
# INGEST_SECRET environment variable; mismatches are rejected with 403.
#
# Valid packets are stored verbatim in DynamoDB with a generated UUID,
# an ISO-8601 received_at timestamp, and a 90-day TTL so old rows expire
# automatically.
#
# This Lambda does NOT decode the satellite beacon binary — that is done
# Pi-side in beacon_decoder.py, which polls the /packets endpoint.

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


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    # --- Auth ---
    path_params = event.get("pathParameters") or {}
    token = path_params.get("token", "")
    if token != INGEST_SECRET:
        logger.warning("Rejected request: token mismatch")
        return _resp(403, {"error": "Forbidden"})

    # --- Parse body ---
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Invalid JSON body"})

    # --- Validate ---
    raw_data = body.get("data", "")
    if not raw_data:
        return _resp(400, {"error": "Missing required field: data"})

    if len(raw_data) > 4000:
        return _resp(400, {"error": "Payload too large: data field exceeds 4000 chars (2 KB base64 limit)"})

    # Check NORAD ID if present (TinyGS may send either key name)
    norad = body.get("NORAD") or body.get("norad")
    expected_norad = os.environ.get("NORAD_ID")
    if norad and expected_norad and str(norad) != str(expected_norad):
        return _resp(400, {"error": f"NORAD mismatch: expected {expected_norad}, got {norad}"})

    # --- Build item ---
    now = datetime.now(timezone.utc)
    ttl = int((now + timedelta(days=90)).timestamp())

    def _dec(value):
        """Convert a numeric value to Decimal for DynamoDB, or return None."""
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    item = {
        "satellite_id":    SATELLITE_ID,
        "received_at":     now.isoformat(),
        "packet_id":       str(uuid4()),
        "gs_time":         body.get("unix_GS_time"),   # integer; critical for Pi-side dedup
        "ground_station":  body.get("station") or body.get("stationName"),
        "snr":             _dec(body.get("snr")),
        "rssi":            _dec(body.get("rssi")),
        "frequency":       _dec(body.get("frequency")),
        "raw_data":        raw_data,
        "tinygs_payload":  json.dumps(body),
        "crc_error":       body.get("crc_error", False),
        "ttl":             ttl,
    }

    # DynamoDB rejects explicit None values
    item = {k: v for k, v in item.items() if v is not None}

    table.put_item(Item=item)

    logger.info("Stored packet %s from station %s", item["packet_id"], item.get("ground_station"))

    return _resp(200, {
        "packet_id":   item["packet_id"],
        "received_at": item["received_at"],
    })
