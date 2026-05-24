"""
AWS Ingest Server — FastAPI application for receiving satellite packets.

Accepts telemetry from multiple sources (TinyGS webhook, roof ground station)
and stores them in a local SQLite database via ``aws_packet_store``.

Configuration (environment variables):
    CUBESAT_INGEST_KEY  — API key required in the ``X-API-Key`` header.
    CUBESAT_DB_PATH     — SQLite database path (default ``./aws_mission_data.db``).

Run locally::

    uvicorn aws_server:app --host 0.0.0.0 --port 8000
"""

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import aws_packet_store as store
import beacon_decoder

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

INGEST_KEY = os.environ.get("CUBESAT_INGEST_KEY", "")

# ──────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────

app = FastAPI(
    title="CubeSAT Ingest Server",
    description="Receives and stores satellite beacon packets from TinyGS and ground stations.",
    version="1.0.0",
)


# ──────────────────────────────────────────────
# Authentication dependency
# ──────────────────────────────────────────────

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Validate the X-API-Key header against CUBESAT_INGEST_KEY."""
    if not INGEST_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: CUBESAT_INGEST_KEY not set",
        )
    if x_api_key is None or x_api_key != INGEST_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ──────────────────────────────────────────────
# Ingest helpers
# ──────────────────────────────────────────────

def _ingest_packet(body: dict, default_source: str) -> dict:
    """Common ingest logic for both POST endpoints.

    Decodes the raw frame, stores the packet (deduplicating by frame hash),
    and persists any extracted telemetry readings.

    Returns a JSON-ready response dict.
    """
    # --- Extract fields from request body ---
    data_b64 = body.get("data")
    if not data_b64:
        raise HTTPException(status_code=400, detail="Missing 'data' field (base64-encoded frame)")

    try:
        raw_frame = base64.b64decode(data_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 in 'data' field")

    satellite = body.get("satellite", "")
    norad_id = body.get("NORAD") or body.get("norad_id")
    station = body.get("station", "")
    frequency = body.get("frequency")
    rssi = body.get("rssi")
    snr = body.get("snr")
    crc_error = bool(body.get("crc_error", False))
    source = body.get("source", default_source)

    # Derive received_at from unix_GS_time if provided, otherwise use now
    unix_gs_time = body.get("unix_GS_time")
    if unix_gs_time is not None:
        try:
            received_at = datetime.fromtimestamp(float(unix_gs_time), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            received_at = datetime.now(timezone.utc).isoformat()
    else:
        received_at = datetime.now(timezone.utc).isoformat()

    # --- Decode beacon ---
    decoded = None
    telemetry_dict = {}
    try:
        result = beacon_decoder.decode_beacon(raw_frame)
        telemetry_dict = result.get("telemetry", {})
        decoded = telemetry_dict if telemetry_dict else None
    except Exception as exc:
        log.warning("Beacon decode failed: %s", exc)
        # Store the packet even if decoding fails

    # --- Store packet (deduplicate) ---
    try:
        packet_id, was_new = store.store_packet_if_new(
            satellite=satellite,
            norad_id=int(norad_id) if norad_id is not None else None,
            station=station,
            frequency_mhz=float(frequency) if frequency is not None else None,
            rssi=float(rssi) if rssi is not None else None,
            snr=float(snr) if snr is not None else None,
            crc_error=crc_error,
            raw_frame=raw_frame,
            decoded=decoded,
            source=source,
            received_at=received_at,
        )
    except Exception as exc:
        log.error("Failed to store packet: %s", exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if not was_new:
        log.info("Duplicate packet (id=%d) from %s", packet_id, source)
        return {"ok": True, "duplicate": True}

    # --- Store telemetry readings ---
    if telemetry_dict:
        try:
            readings = beacon_decoder.extract_telemetry_readings(telemetry_dict)
            if readings:
                store.store_telemetry_batch(packet_id, readings, timestamp=received_at)
        except Exception as exc:
            log.warning("Failed to store telemetry for packet %d: %s", packet_id, exc)

    log.info("Stored new packet id=%d from %s (%s)", packet_id, source, satellite)
    return {"ok": True, "packet_id": packet_id, "duplicate": False}


# ──────────────────────────────────────────────
# POST endpoints — packet ingest
# ──────────────────────────────────────────────

@app.post("/api/ingest/tinygs", dependencies=[Depends(verify_api_key)])
async def ingest_tinygs(request: Request):
    """Receive a TinyGS webhook POST and store the packet."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return _ingest_packet(body, default_source="tinygs_webhook")


@app.post("/api/ingest/groundstation", dependencies=[Depends(verify_api_key)])
async def ingest_groundstation(request: Request):
    """Receive a packet from the roof desktop ground station."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    return _ingest_packet(body, default_source="roof_groundstation")


# ──────────────────────────────────────────────
# GET endpoints — query & health
# ──────────────────────────────────────────────

@app.get("/api/packets", dependencies=[Depends(verify_api_key)])
async def get_packets(since: Optional[str] = None, limit: int = 1000):
    """Return stored packets as JSON.

    Query parameters:
        since — ISO-8601 timestamp; only return packets received at or after this time.
        limit — Maximum number of packets to return (default 1000).
    """
    if since is None:
        since = "1970-01-01T00:00:00+00:00"
    try:
        packets = store.packets_since(since, limit=limit)
    except Exception as exc:
        log.error("Failed to query packets: %s", exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    return {"ok": True, "count": len(packets), "packets": packets}


@app.get("/api/health")
async def health_check():
    """Return server health status and packet count."""
    try:
        count = store.packet_count()
    except Exception as exc:
        log.error("Health check DB error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"Database error: {exc}"},
        )
    return {
        "ok": True,
        "status": "running",
        "packet_count": count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
