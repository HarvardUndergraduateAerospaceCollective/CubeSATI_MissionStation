# Lambda handler for GET /packets?since=<ISO-8601>&limit=<n>
#
# The Pi polls this endpoint roughly every 60 seconds to fetch any packets it
# may have missed (network blips, restarts, etc.).
#
# Packets are returned in chronological order (ascending received_at).
# The response includes a "next_since" cursor the Pi should use on its next
# poll — this is simply the received_at of the last returned packet, so the
# caller never re-requests the same rows.
#
# Deduplication is done Pi-side using frame_hash (SHA-256 of raw bytes).
# This Lambda returns raw DynamoDB rows; it does not decode beacon payloads.

import json
import os
import logging
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME   = os.environ["TABLE_NAME"]
SATELLITE_ID = os.environ.get("SATELLITE_ID", "CUBESAT-1")
SYNC_API_KEY = os.environ.get("SYNC_API_KEY", "")

if not SYNC_API_KEY:
    logger.warning("SYNC_API_KEY not set — GET /packets is UNAUTHENTICATED")

dynamodb = boto3.resource("dynamodb")
table    = dynamodb.Table(TABLE_NAME)

_DEFAULT_LIMIT = 200
_MAX_LIMIT     = 500


def _sanitize_decimals(obj):
    """Recursively convert Decimal values to int or float for JSON serialization."""
    if isinstance(obj, list):
        return [_sanitize_decimals(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_decimals(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    # --- Validate API key (HTTP API v2 lowercases header names) ---
    if SYNC_API_KEY:
        provided = (event.get("headers") or {}).get("x-api-key")
        if provided != SYNC_API_KEY:
            return _resp(401, {"error": "Invalid or missing x-api-key header"})

    params = event.get("queryStringParameters") or {}

    # --- Validate required param ---
    since = params.get("since")
    if not since:
        return _resp(400, {"error": "Missing required query parameter: since (ISO-8601 timestamp)"})

    # --- Parse optional limit ---
    try:
        limit = min(int(params.get("limit", _DEFAULT_LIMIT)), _MAX_LIMIT)
    except (ValueError, TypeError):
        return _resp(400, {"error": "Invalid limit parameter: must be an integer"})

    # --- Query DynamoDB ---
    response = table.query(
        KeyConditionExpression=(
            Key("satellite_id").eq(SATELLITE_ID) &
            Key("received_at").gt(since)
        ),
        ScanIndexForward=True,   # ascending chronological order
        Limit=limit,
    )

    packets = _sanitize_decimals(response.get("Items", []))

    # Cursor for next poll: last packet's received_at, or the caller's "since" if empty
    next_since = packets[-1]["received_at"] if packets else since

    logger.info("Returning %d packets since %s", len(packets), since)

    return _resp(200, {
        "packets":    packets,
        "count":      len(packets),
        "next_since": next_since,
    })
