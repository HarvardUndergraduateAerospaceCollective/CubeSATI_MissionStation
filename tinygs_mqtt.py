"""
TinyGS MQTT Listener — Receives satellite telemetry packets in real time.

Subscribes to the TinyGS MQTT broker and stores every received frame
in the local SQLite database via ``packet_store``.

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

import json
import logging
import os
import threading
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

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

# Which satellite(s) to follow  (NORAD catalog number as string)
# PIN: Replace with your CubeSAT's NORAD ID once it's assigned.
# Example: "25544" for ISS.  Multiple IDs separated by comma.
SAT_NORAD_IDS = os.environ.get("TINYGS_SAT_IDS", "25544").split(",")


# ──────────────────────────────────────────────
# Topic helpers
# ──────────────────────────────────────────────

def _subscribe_topics() -> list[str]:
    """Build MQTT topic filters for the configured satellites."""
    # TinyGS publishes received packets on:
    #   tinygs/satellite/<norad_id>/rx
    # and station-level data on:
    #   tinygs/station/<station_name>/rx
    # We subscribe to the satellite-level feed.
    topics = []
    for norad in SAT_NORAD_IDS:
        norad = norad.strip()
        if norad:
            topics.append(f"tinygs/satellite/{norad}/rx")
    return topics


# ──────────────────────────────────────────────
# Packet parsing
# ──────────────────────────────────────────────

def _parse_and_store(payload: bytes, topic: str):
    """Decode a TinyGS JSON message and persist it."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Not JSON — store raw frame only
        packet_store.store_packet(
            raw_frame=payload,
            source="tinygs_mqtt",
        )
        log.warning("Received non-JSON packet (%d bytes), stored raw", len(payload))
        return

    # Extract common TinyGS fields (best-effort; schema may vary)
    satellite  = data.get("satellite", data.get("sat", ""))
    norad_id   = data.get("norad", data.get("norad_id"))
    station    = data.get("station", data.get("stationName", ""))
    freq       = data.get("frequency", data.get("freq"))
    rssi       = data.get("rssi")
    snr        = data.get("snr")
    raw_hex    = data.get("data", data.get("payload", ""))
    raw_bytes  = bytes.fromhex(raw_hex) if isinstance(raw_hex, str) and raw_hex else None

    pkt_id = packet_store.store_packet(
        satellite=str(satellite),
        norad_id=int(norad_id) if norad_id is not None else None,
        station=str(station),
        frequency_mhz=float(freq) if freq is not None else None,
        rssi=float(rssi) if rssi is not None else None,
        snr=float(snr) if snr is not None else None,
        raw_frame=raw_bytes,
        decoded=data,
        source="tinygs_mqtt",
    )

    # Store numeric fields as telemetry rows for easy time-series queries
    readings = []
    if rssi is not None:
        readings.append(("rssi", float(rssi), "dBm"))
    if snr is not None:
        readings.append(("snr", float(snr), "dB"))

    # PIN: Add your CubeSAT-specific decoded fields here.
    # Example: if the decoded payload contains temperature, battery voltage, etc.
    #   temp = data.get("temperature")
    #   if temp is not None:
    #       readings.append(("temperature", float(temp), "°C"))
    #   batt = data.get("battery_voltage")
    #   if batt is not None:
    #       readings.append(("battery_voltage", float(batt), "V"))

    if readings:
        packet_store.store_telemetry_batch(pkt_id, readings)

    log.info("Stored packet #%d from station %s (sat=%s)", pkt_id, station, satellite)


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
    if rc != 0:
        log.warning("Unexpected MQTT disconnect (rc=%d), will auto-reconnect", rc)


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
