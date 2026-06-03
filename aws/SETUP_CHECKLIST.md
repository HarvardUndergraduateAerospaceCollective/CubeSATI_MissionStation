# AWS Pipeline Setup Checklist

## Current State — DEPLOYED AND TESTED (2026-06-02)

The AWS pipeline is deployed and verified end-to-end. We have:
- **SAM template** — API Gateway + 2 Lambdas (ingest + sync) + DynamoDB table
- **Ingest Lambda** — receives TinyGS POST, validates token, stores in DynamoDB (90-day TTL)
- **Sync Lambda** — Pi polls for new packets via GET
- **Local dev server** (FastAPI) — mirrors Lambda behavior with SQLite
- **Beacon decoder** — decodes OBC_v5d binary TLV frames
- **aws_sync.py** — Pi-side daemon that pulls from AWS and backfills local DB

## Next Steps

### 1. Deploy the SAM stack to AWS — DONE (2026-06-02)
- Stack `cubesat-pipeline` deployed to us-east-1
- Both ingest and sync endpoints verified working with test data
- Webhook URL sent to TinyGS admin for configuration
- **Note:** Consider setting `NORAD_ID` env var on the ingest Lambda to reject packets that don't match our satellite
- **Note:** Avoid special characters (`/`, `|`, `!`) in IngestSecret — they break URL routing

### 2. Test the ingest endpoint
```bash
curl -X POST <ApiEndpoint>/ingest/<your-secret> \
  -d '{"data":"<base64>","satellite":"CUBESAT-1",...}'
```
- Verify: 200 response with packet_id, row appears in DynamoDB

### 3. Test the sync endpoint
```bash
curl "<ApiEndpoint>/packets?since=2020-01-01T00:00:00Z"
```
- Verify: returns the packet you just ingested

### 4. Configure TinyGS webhook
- In TinyGS console, set POST URL to: `<ApiEndpoint>/ingest/<IngestSecret>`
- Verify: live packets start flowing into DynamoDB

### 5. Wire up Pi-side sync — DONE (2026-06-02)
- Updated to use existing `packet_store.store_packet_if_new()` (dedup by frame_hash)
- Tested on Pi: `aws_sync.py` successfully pulls packets from DynamoDB into local SQLite
- Verified satellite and station fields populate correctly after field name mapping fix

### 6. E2E pipeline tests — DONE (2026-06-02)
- 9 pytest tests in `aws/test_e2e_pipeline.py`, all passing
- Covers: byte integrity round-trip, beacon TLV decoding, frame-hash dedup, auth, edge cases
- Tests run against local FastAPI server with temp SQLite (no AWS needed)
- **Bugs fixed during test development:**
  - `tinygs_mqtt.py` — now uses `store_packet_if_new()` (was silently crashing on duplicate MQTT deliveries)
  - `aws_sync.py` — now reads `raw_data` field from DynamoDB (was silently dropping all raw bytes)
  - `aws_sync.py` — cursor no longer falls back to `gs_time` (was risking cursor poisoning)

### 7. Pi-side integration testing — DONE (2026-06-02)
- `test_pi_local.py`: 29/29 passed (beacon decoder, packet_store, dedup)
- `test_pi_aws.py`: 14/14 passed (live sync from DynamoDB)
- `simulate_packets.py`: tested both `--mqtt` and `--aws` modes on Pi
- Dashboard verified displaying simulated telemetry correctly
- Dedup verified: same-seed packets correctly rejected, different-seed accepted

### 8. Packet simulator — DONE (2026-06-02)
- `simulate_packets.py` generates realistic beacon packets with time-varying telemetry
- Modes: `--mqtt` (local pipeline injection) and `--aws` (POST to Lambda)
- Covers all dashboard-required fields: FSM_batt_v, FSM_magn_v_0, FSM_best_dir, rssi, FSM_state, etc.
- **Bug found & fixed:** `packet_store._init_tables` migration was broken on existing DBs — the `frame_hash` index was created inside `executescript` before the migration could add the column. Moved index creation to after the migration.

### 9. Code review fixes — DONE (2026-06-02)
- Multi-agent code review identified 7 critical/high issues, 6 fixed:
- **Fix 1 (CRITICAL):** `decoded_json` beacon fields now merged at top level in `tinygs_mqtt.py`, `aws_sync.py`, `compare.py` — FSM dashboard was broken for all real packets
- **Fix 2 (CRITICAL):** TOCTOU race in `store_packet_if_new()` — SELECT + INSERT collapsed into single transaction in both `packet_store.py` and `aws/aws_packet_store.py`
- **Fix 3 (CRITICAL):** `compare.py` no longer passes `decoded=None` — beacon decode runs before store
- **Fix 4 (HIGH):** `aws/beacon_decoder.py` marked as Lambda-required copy with sync warning
- **Fix 5 (HIGH):** `aws/aws_packet_store.py` migration ordering fixed (ALTER TABLE + CREATE INDEX separated)
- **Fix 6 (HIGH):** Non-JSON MQTT fallback in `tinygs_mqtt.py` now uses `store_packet_if_new()`
- **Fix 7 (MEDIUM):** Stale "upsert" references updated; `_process_and_upsert` renamed to `_process_and_store`
- `test_fixes.py`: 16/16 passed — validates FSM consumer path and concurrent dedup (10 threads)

## Known Issues
- ~~`packet_store.upsert_packet()` needs implementation~~ — resolved: aws_sync.py now uses `store_packet_if_new()`
- NORAD ID is still the ISS placeholder (`25544`) — swap to actual satellite ID

## To Investigate
- **DynamoDB 90-day TTL** — packets auto-expire after 90 days. Need to investigate a long-term storage solution (S3 archival, DynamoDB backup, etc.) so mission-critical data is never lost

## Remaining Review Items (lower priority)
- **`telemetry_series()` unbounded queries** — no LIMIT clause, will slow down as mission data grows. Add a default limit or pagination.
- **`export_packets_csv` OOM risk** — loads all packets into memory via `recent_packets(n=10_000_000)`. Switch to cursor-based streaming for large exports.
- **Slack notification duplicated 4x** — `_notify_slack()` copy-pasted across `missioncontrol.py`, `web_server.py`, `pass_predictor.py`, `alert_manager.py`. Extract to a shared utility.
- **`_check_new_packets` polling overhead** — polls `recent_packets(5)` every 5s; `SELECT MAX(id)` would be cheaper.
- **Silent `except Exception: pass`** in `web_server.py` background loop — should log at debug level so persistent DB errors are visible.

## Test Files (remove before production)
- `aws/test_e2e_pipeline.py` — pytest E2E tests for FastAPI ingest server
- `test_pi_local.py` — standalone Pi-local tests (beacon decoder, packet_store, dedup)
- `test_pi_aws.py` — Pi + AWS integration tests (live sync path)
- `test_fixes.py` — validates decoded_json merge + concurrent dedup (TOCTOU fix)
- `simulate_packets.py` — packet simulator for dashboard testing

## Production Code Changes (this session)
- `aws_sync.py` — switched from `upsert_packet()` to `store_packet_if_new()`; fixed `raw_data`/`data` field name; removed `gs_time` cursor fallback; fixed `satellite_id`/`ground_station` field name mapping; replaced em dash with ASCII dash; merged beacon fields at top level of `decoded_json`; renamed `_process_and_upsert` to `_process_and_store`; updated stale docstrings
- `tinygs_mqtt.py` — switched from `store_packet()` to `store_packet_if_new()` with dedup; merged beacon fields at top level of `decoded_json`; non-JSON fallback now uses `store_packet_if_new()`
- `packet_store.py` — fixed `_init_tables` migration ordering for `frame_hash` column/index; fixed TOCTOU race in `store_packet_if_new()` (atomic SELECT+INSERT)
- `aws/aws_packet_store.py` — fixed TOCTOU race (matching Pi-side); fixed migration ordering (matching Pi-side)
- `compare.py` — beacon decode now runs before store; `decoded_combined` passed with top-level beacon fields
- `aws/beacon_decoder.py` — added sync warning comment
- `aws/sync/app.py` — updated stale dedup comment
- `test_pi_aws.py` — updated `_process_and_upsert` references to `_process_and_store`
