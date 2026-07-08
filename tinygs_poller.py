"""
tinygs_poller.py — live, network-wide packet capture via the TinyGS v3 API.

WHY THIS EXISTS
---------------
We consume the TinyGS network but run no receiver hardware, so MQTT (which only
delivers your OWN stations' packets) never sees HUCSat packets heard by other
stations. Those live in TinyGS's aggregated backend, reachable through the web
app's API — which tarpits any request that lacks a valid ``x-client-timestamp``
header (the app's JavaScript signs every request; unsigned requests just hang,
from any client or IP, browser or not).

The signing algorithm is a hardcoded repeating-key XOR (reverse-engineered from
the webapp bundle, ``index-*.js``)::

    x-client-timestamp = base64( XOR( str(Date.now()), "TinyGS-WebApp-2025-SecureKey" ) )

Reproducing it in Python lets a plain, stdlib-only client hit the API with NO
browser, NO login, NO Cloudflare workaround:

    GET https://api.tinygs.com/v3/packets?satellite=HUCSat
        headers: x-client-timestamp: <signed>
        -> { "packets": [ {raw, serverTime, norad, freq, satPos, parsed, ...}, ... ],
             "templates": {...} }

The endpoint returns ~5 packets/page, newest first; older pages via
``&before=<serverTime>``. Each ``raw`` is a base64 frame we decode with
``beacon_decoder`` (validated: 206-byte frames -> 19 fields, battery/gyro/state).

Packets are stored through the same path as ``tinygs_backfill`` (dedup by
frame_hash, original serverTime preserved), tagged ``source="tinygs_v3"``.

USAGE
-----
    python tinygs_poller.py                # poll HUCSat every 300s, store to DB
    python tinygs_poller.py --once         # single poll (cron / testing)
    python tinygs_poller.py --dry-run      # fetch + parse, write nothing
    python tinygs_poller.py --interval 180 # custom cadence (seconds)

In-process (from the dashboard)::

    import tinygs_poller
    tinygs_poller.start_poller()           # daemon thread; returns immediately
"""

import argparse
import base64
import json
import logging
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# Reuse the backfill decode + store path (same field mapping, dedup, telemetry).
from tinygs_backfill import _extract_list, parse_packet, store_one

log = logging.getLogger("tinygs_poller")

SOURCE = "tinygs_v3"
API_BASE = "https://api.tinygs.com"

# Reverse-engineered from the webapp: XOR key for the x-client-timestamp header.
_SIGN_KEY = b"TinyGS-WebApp-2025-SecureKey"

DEFAULT_SAT = os.environ.get("TINYGS_SAT_NAME", "HUCSat")
DEFAULT_INTERVAL = int(os.environ.get("TINYGS_POLL_INTERVAL", "300"))   # seconds
# How many pages (of ~5) to walk back per poll before giving up on "catching up".
# Normal polls stop after 1 page (all duplicates); this bounds post-downtime catch-up.
DEFAULT_MAX_PAGES = int(os.environ.get("TINYGS_POLL_MAX_PAGES", "10"))
MAX_BACKOFF = int(os.environ.get("TINYGS_POLL_MAX_BACKOFF", "3600"))
FETCH_TIMEOUT = 25
# Pause between successive catch-up pages so a long post-outage backfill
# doesn't machine-gun the API — rapid-fire requests are what get an IP
# tarpitted. Normal (caught-up) polls fetch one page and never sleep.
PAGE_PAUSE = float(os.environ.get("TINYGS_POLL_PAGE_PAUSE", "0.8"))

_HEADERS_BASE = {
    "Origin": "https://app.tinygs.com",
    "Referer": "https://app.tinygs.com/",
    "Accept": "application/json, text/plain, */*",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
}


def _client_timestamp() -> str:
    """Reproduce the webapp's x-client-timestamp: base64(xor(str(now_ms), key))."""
    msg = str(int(time.time() * 1000)).encode()
    signed = bytes(msg[i] ^ _SIGN_KEY[i % len(_SIGN_KEY)] for i in range(len(msg)))
    return base64.b64encode(signed).decode()


def _fetch_page(satellite: str, before=None, timeout: int = FETCH_TIMEOUT,
                retries: int = 2) -> list:
    """GET one page of /v3/packets (newest first, or before a cursor).

    Transient network timeouts are retried a couple times with a fresh signature
    — the roof/institutional network occasionally drops a request. HTTP status
    errors (404/429) are meaningful and raised straight to the caller.
    """
    url = f"{API_BASE}/v3/packets?satellite={quote(satellite)}"
    if before is not None:
        url += f"&before={before}"
    last_exc = None
    for attempt in range(retries + 1):
        headers = dict(_HEADERS_BASE)
        headers["x-client-timestamp"] = _client_timestamp()  # fresh signature per try
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=timeout) as resp:
                return _extract_list(json.loads(resp.read().decode()))
        except HTTPError:
            raise
        except (URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt < retries:
                log.debug("fetch timed out (attempt %d), retrying...", attempt + 1)
                time.sleep(3)
    raise last_exc


def poll_once(satellite: str = DEFAULT_SAT, max_pages: int = DEFAULT_MAX_PAGES,
              dry_run: bool = False) -> dict:
    """One poll: walk pages newest->older, storing new packets, stopping once a
    whole page is already known (caught up) or max_pages is reached.
    Returns counts; raises on network/HTTP error (caller handles backoff)."""
    counts = {"captured": 0, "new": 0, "dup": 0, "nodata": 0, "error": 0, "pages": 0}
    before = None
    for _ in range(max_pages):
        packets = _fetch_page(satellite, before)
        if not packets:
            break
        counts["pages"] += 1
        page_new = 0
        oldest = None
        for pkt in packets:
            if not isinstance(pkt, dict):
                continue
            counts["captured"] += 1
            st = pkt.get("serverTime")
            if st is not None and (oldest is None or st < oldest):
                oldest = st
            if dry_run:
                continue
            try:
                fields = parse_packet(pkt)
                res = store_one(fields, pkt, source=SOURCE)
                counts[res] += 1
                if res == "new":
                    page_new += 1
            except Exception as exc:
                counts["error"] += 1
                log.debug("store failed for one packet: %s", exc, exc_info=True)
        # Caught up: this whole page was already in the DB (or dry-run single page).
        if dry_run or page_new == 0 or oldest is None:
            break
        before = oldest
        time.sleep(PAGE_PAUSE)  # politeness between catch-up pages
    return counts


def run_forever(satellite: str = DEFAULT_SAT, interval: int = DEFAULT_INTERVAL,
                max_pages: int = DEFAULT_MAX_PAGES, dry_run: bool = False):
    """Poll forever on a gentle cadence, backing off exponentially on errors."""
    global _last_poll_ok
    log.info("v3 poller started: satellite=%s interval=%ds source=%s%s",
             satellite, interval, SOURCE, "  (DRY RUN)" if dry_run else "")
    _last_poll_ok = time.time()   # start healthy
    delay = interval
    while True:
        try:
            c = poll_once(satellite, max_pages=max_pages, dry_run=dry_run)
            _last_poll_ok = time.time()   # a completed poll (even 0 new) = healthy
            log.info("poll: pages=%d captured=%d new=%d dup=%d nodata=%d err=%d",
                     c["pages"], c["captured"], c["new"], c["dup"], c["nodata"], c["error"])
            delay = interval  # healthy — reset cadence
        except HTTPError as exc:
            # 404 = "no packets" (normal empty). 429 = rate-limited -> back off hard.
            # Other HTTP / transient network errors -> just retry next interval so a
            # flaky network doesn't spiral the poller into an hour-long backoff.
            if exc.code == 404:
                # "No packets" is a completed poll — count it as healthy or the
                # watchdog trips during long quiet stretches.
                _last_poll_ok = time.time()
                log.info("poll: no packets (404)")
                delay = interval
            elif exc.code == 429:
                delay = min(max(delay * 2, interval), MAX_BACKOFF)
                log.warning("HTTP 429 rate-limited; backing off to %ds", delay)
            else:
                log.warning("HTTP %s from v3 API; retrying in %ds", exc.code, interval)
                delay = interval
        except (URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
            log.warning("poll failed (%s: %s); retrying in %ds",
                        type(exc).__name__, exc, interval)
            delay = interval
        except Exception:
            log.exception("unexpected poll error; retrying in %ds", interval)
            delay = interval
        time.sleep(delay)


# Poller health — epoch seconds of the last poll that COMPLETED (even with 0 new
# packets). The watchdog recovers IN-PROCESS if this goes stale: it must never
# exit, because the poller shares a process with the Flask dashboard and
# os._exit() took the whole dashboard down with it (2026-07-08).
_last_poll_ok = None


def _watchdog(interval: int, satellite: str, poller: threading.Thread):
    """Recover a stalled poller without killing the process. If the poll thread
    died, start a replacement; if it is alive but stalled (e.g. an endless
    network outage), leave it to its own retry loop — either way the dashboard
    keeps serving. Rearms after acting so it reports once per stall window."""
    global _last_poll_ok
    limit = int(os.environ.get("TINYGS_POLL_STALL_LIMIT", str(max(600, interval * 8))))
    log.info("poll watchdog armed (stall limit %ds)", limit)
    while True:
        time.sleep(60)
        last = _last_poll_ok
        if last is None:
            continue
        stale = time.time() - last
        if stale > limit:
            if poller.is_alive():
                log.error("WATCHDOG: no completed poll in %.0fs (limit %ds) — poll "
                          "thread alive but stalled; leaving it to retry",
                          stale, limit)
            else:
                log.error("WATCHDOG: poll thread died and no completed poll in "
                          "%.0fs — starting a replacement poller thread", stale)
                poller = threading.Thread(
                    target=run_forever,
                    kwargs={"satellite": satellite, "interval": interval},
                    name="tinygs-v3-poller", daemon=True)
                poller.start()
            _last_poll_ok = time.time()


def start_poller(satellite: str = DEFAULT_SAT, interval: int = DEFAULT_INTERVAL) -> threading.Thread:
    """Start the poller + a stall watchdog as background daemon threads
    (mirrors tinygs_mqtt.start_listener)."""
    t = threading.Thread(target=run_forever,
                         kwargs={"satellite": satellite, "interval": interval},
                         name="tinygs-v3-poller", daemon=True)
    t.start()
    threading.Thread(target=_watchdog,
                     kwargs={"interval": interval, "satellite": satellite, "poller": t},
                     name="tinygs-poll-watchdog", daemon=True).start()
    log.info("v3 poller thread started (satellite=%s, every %ds)", satellite, interval)
    return t


def main():
    ap = argparse.ArgumentParser(
        description="Poll the TinyGS v3 API for a satellite's packets (headless, signed).")
    ap.add_argument("--satellite", default=DEFAULT_SAT,
                    help=f"Satellite name (default {DEFAULT_SAT}).")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"Seconds between polls (default {DEFAULT_INTERVAL}).")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                    help=f"Max pages (~5 each) to walk back per poll (default {DEFAULT_MAX_PAGES}).")
    ap.add_argument("--once", action="store_true", help="Poll once and exit.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + parse but write nothing to the DB.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)-14s %(levelname)-7s %(message)s")

    if args.once:
        try:
            c = poll_once(args.satellite, max_pages=args.max_pages, dry_run=args.dry_run)
            print(f"pages={c['pages']} captured={c['captured']} new={c['new']} "
                  f"dup={c['dup']} nodata={c['nodata']} err={c['error']}")
            return 0
        except Exception as exc:
            print(f"poll failed: {type(exc).__name__}: {exc}")
            return 1
    run_forever(args.satellite, args.interval, max_pages=args.max_pages, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
