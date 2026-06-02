# AWS Pipeline Setup Checklist

## Current State

The AWS pipeline is fully written but not deployed. We have:
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

### 5. Wire up Pi-side sync
- Updated to use existing `packet_store.store_packet_if_new()` (dedup by frame_hash)
- **TODO: Test on Pi:**
  ```bash
  export AWS_SYNC_URL="https://1avcugfjtg.execute-api.us-east-1.amazonaws.com/prod"
  export AWS_SYNC_API_KEY=""
  python aws_sync.py
  ```
- Verify: pulls the test packet from step 2 and stores it in local SQLite

### 6. E2E pipeline tests — DONE (2026-06-02)
- 9 pytest tests in `aws/test_e2e_pipeline.py`, all passing
- Covers: byte integrity round-trip, beacon TLV decoding, frame-hash dedup, auth, edge cases
- Tests run against local FastAPI server with temp SQLite (no AWS needed)
- **Bugs fixed during test development:**
  - `tinygs_mqtt.py` — now uses `store_packet_if_new()` (was silently crashing on duplicate MQTT deliveries)
  - `aws_sync.py` — now reads `raw_data` field from DynamoDB (was silently dropping all raw bytes)
  - `aws_sync.py` — cursor no longer falls back to `gs_time` (was risking cursor poisoning)

### 7. Test Pi-side sync (TODO)
- Set env vars and run `aws_sync.py` on the Pi
- Verify packets flow from DynamoDB → local SQLite intact
- Run additional integration tests on Pi hardware

## Known Issues
- ~~`packet_store.upsert_packet()` needs implementation~~ — resolved: aws_sync.py now uses `store_packet_if_new()`
- NORAD ID is still the ISS placeholder (`25544`) — swap to actual satellite ID

## To Investigate
- **DynamoDB 90-day TTL** — packets auto-expire after 90 days. Need to investigate a long-term storage solution (S3 archival, DynamoDB backup, etc.) so mission-critical data is never lost
