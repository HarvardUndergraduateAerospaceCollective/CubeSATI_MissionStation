"""
TinyGS MQTT Listener — Receives satellite telemetry packets in real time.

Subscribes to the TinyGS MQTT broker and stores every received frame
in the local SQLite database via ``packet_store``.

TinyGS MQTT message format (per ground station reception)::

    {
      "satellite": "CubeSatI",
      "NORAD": 99999,
      "station_location": [lat, lon],
      "mode": "LoRa",
      "frequency": 436.7,
      "rssi": -118.0,
      "snr": -6.75,
      "data": "<base64 encoded raw satellite bytes>",
      "crc_error": false,
      "unix_GS_time": 1711123456,
      ...
    }

The ``data`` field is **base64-encoded** raw bytes from the satellite radio.
For the CubeSAT-I, those bytes are a 6-byte PacketManager header followed
by a BinaryEncoder TLV payload (see ``beacon_decoder.py``).

Usage
-----
As a background thread inside the dashboard::

    from tinygs_mqtt import start_listener
    start_listener()          # returns immediately; runs in daemon thread

Standalone (for testing / headless collection)::

    python tinygs_mqtt.py

Configuration
-------------
Fill in the constants below or set the corresponding environment variables.
"""

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

import beacon_decoder
import packet_store

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Connection settings
# ──────────────────────────────────────────────

# PIN: You need TinyGS MQTT credentials.
# Sign up at https://tinygs.com and find your MQTT credentials
# on the TinyGS console (Personal > MQTT).
# Set these via environment variables or edit directly here.
MQTT_BROKER   = os.environ.get("TINYGS_MQTT_BROKER", "mqtt.tinygs.com")
MQTT_PORT     = int(os.environ.get("TINYGS_MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("TINYGS_MQTT_USER", "")      # <-- your TinyGS username
MQTT_PASSWORD = os.environ.get("TINYGS_MQTT_PASS", "")      # <-- your TinyGS API key / password

# Which satellite(s) to follow  (NORAD catalog number as int)
# PIN: Replace with your CubeSAT's NORAD ID once it's assigned.
# Multiple IDs separated by comma.  Leave blank to accept ALL satellites.
SAT_NORAD_IDS: set[int] = set()
_raw_ids = os.environ.get("TINYGS_SAT_IDS", "")
for _id in _raw_ids.split(","):
    _id = _id.strip()
    if _id.isdigit():
        SAT_NORAD_IDS.add(int(_id))


# ──────────────────────────────────────────────
# Slack notifications
# ──────────────────────────────────────────────

_last_disconnect_slack = 0.0
_DISCONNECT_SLACK_COOLDOWN = 300  # seconds between disconnect alerts


def _notify_slack(message: str):
    """Post to Slack webhook. No-op when CUBESAT_SLACK_WEBHOOK is unset."""
    webhook = os.environ.get("CUBESAT_SLACK_WEBHOOK", "")
    if not webhook:
        return
    payload = json.dumps({
        "text": f"*HUCSAT Mission Control* — {message}",
        "username": "MissionStation",
    })
    try:
        req = Request(webhook, data=payload.encode(),
                      headers={"Content-Type": "application/json"})
        urlopen(req, timeout=10)
    except Exception as exc:
        log.debug("Slack notify failed: %s", exc)


# ──────────────────────────────────────────────
# Topic helpers
# ──────────────────────────────────────────────

def _subscribe_topics() -> list[str]:
    """Build MQTT topic filters for TinyGS.

    TinyGS stations publish received packets on ``tinygs/tele/rx``.
    We subscribe to that global feed and filter by NORAD ID in the
    message handler.
    """
    return ["tinygs/tele/rx"]


# ──────────────────────────────────────────────
# Packet parsing
# ──────────────────────────────────────────────

def _parse_and_store(payload: bytes, topic: str):
    """Decode a TinyGS JSON message, decode the beacon, and persist everything."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        packet_store.store_packet(raw_frame=payload, source="tinygs_mqtt")
        log.warning("Received non-JSON packet (%d bytes), stored raw", len(payload))
        return

    # ── Extract TinyGS envelope fields ──────────────────────────
    satellite  = data.get("satellite", "")
    norad_id   = data.get("NORAD", data.get("norad"))
    station    = data.get("station", data.get("stationName", ""))
    freq       = data.get("frequency")
    rssi       = data.get("rssi")
    snr        = data.get("snr")
    crc_error  = data.get("crc_error", False)

    # Filter by NORAD ID if configured (empty set = accept all)
    if SAT_NORAD_IDS and norad_id is not None and int(norad_id) not in SAT_NORAD_IDS:
        return

    # ── Decode the raw satellite payload (base64 → bytes) ──────
    raw_b64 = data.get("data", "")
    try:
        raw_bytes = base64.b64decode(raw_b64) if raw_b64 else None
    except Exception:
        raw_bytes = None
        log.warning("Failed to base64-decode 'data' field")

    # ── Run the CubeSAT-I beacon decoder ───────────────────────
    beacon_telemetry: dict = {}
    if raw_bytes and not crc_error:
        try:
            result = beacon_decoder.decode_beacon(raw_bytes)
            beacon_telemetry = result.get("telemetry", {})
        except Exception:
            log.debug("Beacon decode failed (may not be our satellite)", exc_info=True)

    # Merge TinyGS envelope + decoded beacon for the decoded_json column
    decoded_combined = {**data}
    if beacon_telemetry:
        decoded_combined["_beacon"] = beacon_telemetry

    # ── Persist packet ─────────────────────────────────────────
    pkt_id, was_new = packet_store.store_packet_if_new(
        satellite=str(satellite),
        norad_id=int(norad_id) if norad_id is not None else None,
        station=str(station),
        frequency_mhz=float(freq) if freq is not None else None,
        rssi=float(rssi) if rssi is not None else None,
        snr=float(snr) if snr is not None else None,
        crc_error=bool(crc_error),
        raw_frame=raw_bytes,
        decoded=decoded_combined,
        source="tinygs_mqtt",
    )

    if not was_new:
        log.debug("tinygs_mqtt: duplicate packet skipped (station=%s)", station)
        return

    # ── Store telemetry time-series rows ───────────────────────
    readings: list[tuple[str, float, str]] = []

    # RF-level readings (always available from TinyGS envelope)
    if rssi is not None:
        readings.append(("rssi", float(rssi), "dBm"))
    if snr is not None:
        readings.append(("snr", float(snr), "dB"))

    # Satellite beacon telemetry (from binary decoder)
    if beacon_telemetry:
        readings.extend(beacon_decoder.extract_telemetry_readings(beacon_telemetry))

    if readings:
        packet_store.store_telemetry_batch(pkt_id, readings)

    decoded_count = len(beacon_telemetry)
    log.info(
        "Stored packet #%d  sat=%s  station=%s  rssi=%.1f  snr=%.1f  beacon_fields=%d",
        pkt_id, satellite, station,
        float(rssi) if rssi is not None else 0,
        float(snr) if snr is not None else 0,
        decoded_count,
    )


# ──────────────────────────────────────────────
# MQTT client callbacks
# ──────────────────────────────────────────────

def _on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error("MQTT connection failed (rc=%d). Check credentials.", rc)
        return
    log.info("Connected to %s:%d", MQTT_BROKER, MQTT_PORT)
    topics = _subscribe_topics()
    for t in topics:
        client.subscribe(t)
        log.info("Subscribed to %s", t)


def _on_message(client, userdata, msg):
    log.debug("Message on %s (%d bytes)", msg.topic, len(msg.payload))
    try:
        _parse_and_store(msg.payload, msg.topic)
    except Exception:
        log.exception("Error processing MQTT message on %s", msg.topic)


def _on_disconnect(client, userdata, rc):
    global _last_disconnect_slack
    if rc != 0:
        log.warning("Unexpected MQTT disconnect (rc=%d), will auto-reconnect", rc)
        now = time.time()
        if now - _last_disconnect_slack > _DISCONNECT_SLACK_COOLDOWN:
            _last_disconnect_slack = now
            _notify_slack(
                f"TinyGS MQTT disconnected (rc={rc}) — packets may be missed. Auto-reconnecting."
            )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def create_client() -> mqtt.Client:
    """Build and configure an MQTT client (not yet connected)."""
    client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect    = _on_connect
    client.on_message    = _on_message
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def start_listener() -> threading.Thread:
    """Connect to TinyGS MQTT and start a background daemon thread.

    Returns the thread object (already started).
    """
    if not MQTT_USERNAME:
        log.warning(
            "MQTT credentials not set. Set TINYGS_MQTT_USER / TINYGS_MQTT_PASS "
            "environment variables, or edit tinygs_mqtt.py directly."
        )

    client = create_client()

    def _run():
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception:
            log.exception("MQTT listener crashed")

    t = threading.Thread(target=_run, name="tinygs-mqtt", daemon=True)
    t.start()
    log.info("MQTT listener thread started")
    return t


# ──────────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if not MQTT_USERNAME:
        print(
            "WARNING: No MQTT credentials configured.\n"
            "Set environment variables TINYGS_MQTT_USER and TINYGS_MQTT_PASS,\n"
            "or edit the constants at the top of tinygs_mqtt.py.\n"
        )

    print("Starting TinyGS MQTT packet collector (Ctrl+C to stop) ...")
    client = create_client()
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        client.disconnect()
