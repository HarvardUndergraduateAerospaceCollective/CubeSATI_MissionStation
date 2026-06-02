import base64
import json
import os
import struct
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

import aws_packet_store as store
import aws_server
from aws_server import app

TEST_API_KEY = "test-key-abc123"


# ──────────────────────────────────────────────
# Beacon frame builder
# ──────────────────────────────────────────────

def _circuitpython_hash(s: str) -> int:
    h = 5381
    for ch in s.encode("utf-8"):
        h = ((h << 5) + h) ^ ch
        h &= 0xFFFFFFFF
    h &= 0xFFFF
    return h if h != 0 else 1


def _tlv_string(key: str, value: str) -> bytes:
    enc = value.encode("utf-8")
    return struct.pack(">IB", _circuitpython_hash(key), 0x00) + bytes([len(enc)]) + enc


def _tlv_float32(key: str, value: float) -> bytes:
    return struct.pack(">IBf", _circuitpython_hash(key), 0x05, value)


def _tlv_uint32(key: str, value: int) -> bytes:
    return struct.pack(">IBI", _circuitpython_hash(key), 0x0D, value)


def _build_beacon_frame() -> bytes:
    header = bytes([0x01]) + struct.pack(">HH", 0, 1) + bytes([80])
    payload = b""
    payload += _tlv_string("name", "CubeSATI")
    payload += _tlv_string("FSM_state", "nominal")
    payload += _tlv_float32("FSM_magn_v_0", 23.5)
    payload += _tlv_float32("FSM_batt_v", 3.87)
    payload += _tlv_uint32("uptime", 7200)
    return header + payload


BEACON_FRAME = _build_beacon_frame()
BEACON_B64 = base64.b64encode(BEACON_FRAME).decode("ascii")

TINYGS_PAYLOAD = {
    "satellite": "CUBESAT-1",
    "NORAD": 99999,
    "station": "test-station",
    "frequency": 436.7,
    "rssi": -118.0,
    "snr": -6.75,
    "data": BEACON_B64,
    "crc_error": False,
    "unix_GS_time": 1711123456,
}

AUTH = {"X-API-Key": TEST_API_KEY}


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(store, "_DB_PATH", db_file)
    # Clear thread-local connection so _get_conn() opens fresh DB at new path
    if hasattr(store._local, "conn"):
        try:
            store._local.conn.close()
        except Exception:
            pass
        del store._local.conn
    yield db_file
    if hasattr(store._local, "conn"):
        try:
            store._local.conn.close()
        except Exception:
            pass
        del store._local.conn


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("CUBESAT_INGEST_KEY", TEST_API_KEY)
    monkeypatch.setattr(aws_server, "INGEST_KEY", TEST_API_KEY)


@pytest.fixture
def client():
    return TestClient(app)


# ──────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────

def test_health_endpoint(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["packet_count"] == 0


def test_ingest_and_retrieve(client):
    resp = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "packet_id" in body

    resp2 = client.get("/api/packets", params={"since": "1970-01-01T00:00:00Z"}, headers=AUTH)
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["count"] == 1
    pkt = data["packets"][0]
    assert pkt["raw_frame"] == BEACON_B64


def test_byte_integrity_roundtrip(client):
    client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers=AUTH)

    resp = client.get("/api/packets", params={"since": "1970-01-01T00:00:00Z"}, headers=AUTH)
    pkt = resp.json()["packets"][0]
    returned_bytes = base64.b64decode(pkt["raw_frame"])
    assert returned_bytes == BEACON_FRAME


def test_beacon_decoding(client):
    resp = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers=AUTH)
    assert resp.status_code == 200

    resp2 = client.get("/api/packets", params={"since": "1970-01-01T00:00:00Z"}, headers=AUTH)
    pkt = resp2.json()["packets"][0]
    assert pkt["decoded_json"] is not None
    decoded = json.loads(pkt["decoded_json"])
    assert decoded["FSM_state"] == "nominal"
    assert decoded["name"] == "CubeSATI"
    assert decoded["FSM_batt_v"] == pytest.approx(3.87, abs=1e-3)
    assert decoded["FSM_magn_v_0"] == pytest.approx(23.5, abs=1e-3)
    assert decoded["uptime"] == 7200


def test_deduplication(client):
    resp1 = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers=AUTH)
    assert resp1.status_code == 200
    assert resp1.json()["duplicate"] is False

    resp2 = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers=AUTH)
    assert resp2.status_code == 200
    assert resp2.json()["duplicate"] is True

    resp3 = client.get("/api/health")
    assert resp3.json()["packet_count"] == 1


def test_ingest_missing_data(client):
    payload = {k: v for k, v in TINYGS_PAYLOAD.items() if k != "data"}
    resp = client.post("/api/ingest/tinygs", json=payload, headers=AUTH)
    assert resp.status_code == 400


def test_ingest_no_api_key(client):
    resp = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD)
    assert resp.status_code == 401


def test_ingest_wrong_api_key(client):
    resp = client.post("/api/ingest/tinygs", json=TINYGS_PAYLOAD, headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_ingest_crc_error(client):
    payload = {**TINYGS_PAYLOAD, "crc_error": True}
    resp = client.post("/api/ingest/tinygs", json=payload, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp2 = client.get("/api/packets", params={"since": "1970-01-01T00:00:00Z"}, headers=AUTH)
    pkt = resp2.json()["packets"][0]
    assert pkt["crc_error"] == 1
