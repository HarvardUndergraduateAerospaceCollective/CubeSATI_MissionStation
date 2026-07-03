"""
TinyGS browser poller — live, network-wide packet capture via the real web app.

WHY THIS EXISTS
---------------
TinyGS "personal" MQTT credentials only deliver packets received by your *own*
ground stations (broker topics are ``tinygs/<user>/<station>/...`` plus a
``tinygs/global/...`` control channel — there is no global packet feed). We
operate no receiver hardware, so MQTT never sees the packets that other stations
around the world hear for HUCSat. Those live only in TinyGS's aggregated
backend, reachable through the ``api.tinygs.com/v4/packets`` endpoint — which
tarpits scripted clients (the site's JavaScript signs each request with an
``x-client-timestamp`` anti-bot header, so a plain ``urllib`` request just hangs
while a browser succeeds).

The robust way around that is to *be* the browser: this tool drives a real,
logged-in Chromium via Playwright, lets the TinyGS web app issue its own
authenticated requests, and intercepts the ``/v4/packets`` responses. Captured
packets flow through the exact same decode + store path as ``tinygs_backfill``
(dedup by frame_hash, original serverTime preserved as ``received_at``), tagged
``source="tinygs_browser"`` so live rows are distinguishable in the DB.

USAGE
-----
1. One-time login (opens a visible window; needs a display):

     python tinygs_browser_poller.py --login

   Log in to TinyGS in the window, then press ENTER in the terminal. The session
   is saved to the persistent profile dir and reused by every later run.

2. Continuous capture (headless, polls every 120 s):

     python tinygs_browser_poller.py

   Watch it work with a visible window:  add ``--headed``
   One cycle then exit (cron / testing): add ``--once``
   Parse but don't write to the DB:      add ``--dry-run``

On the Raspberry Pi, use the system browser instead of a downloaded one:

     TINYGS_BROWSER_PATH=/usr/bin/chromium-browser python tinygs_browser_poller.py

CONFIG (env var / CLI flag)
---------------------------
  TINYGS_SAT_NAME     / --satellite     satellite page to watch   (default HUCSat)
  TINYGS_PROFILE_DIR  / --profile       persistent browser profile (default ./.tinygs_profile)
  TINYGS_BROWSER_PATH / --browser-path  chromium executable        (default: Playwright's)
  TINYGS_POLL_INTERVAL/ --interval      seconds between cycles      (default 120)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# Reuse the backfill tool's decode+store path so live and historical packets are
# handled identically (same field mapping, same dedup, same telemetry rows).
import tinygs_backfill
from tinygs_backfill import _extract_list, parse_packet, store_one

log = logging.getLogger("tinygs_browser")

SOURCE = "tinygs_browser"
APP_BASE = "https://app.tinygs.com"
DEFAULT_SAT = os.environ.get("TINYGS_SAT_NAME", "HUCSat")
DEFAULT_PROFILE = os.environ.get(
    "TINYGS_PROFILE_DIR", str(Path(__file__).parent / ".tinygs_profile")
)
DEFAULT_BROWSER = os.environ.get("TINYGS_BROWSER_PATH") or None
DEFAULT_INTERVAL = int(os.environ.get("TINYGS_POLL_INTERVAL", "120"))
# Optional one-shot login link from TinyGS (app.tinygs.com?loginToken=...&userId=...).
# Lets us establish the session with NO interactive window — ideal for the Pi.
# It's a credential: keep it in mission.env (gitignored), never commit it.
DEFAULT_LOGIN_URL = os.environ.get("TINYGS_LOGIN_URL") or None

# Reduce the "I'm automated" signal a little. We're authenticated anyway, so this
# is belt-and-suspenders rather than essential.
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _make_context(p, profile_dir: str, browser_path, headless: bool):
    """Launch a persistent Chromium context (keeps the TinyGS login across runs)."""
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=headless,
        executable_path=browser_path,
        args=_LAUNCH_ARGS,
        user_agent=_UA,
        viewport={"width": 1366, "height": 900},
    )


def do_login(profile_dir: str, browser_path, satellite: str, token_url=None):
    """One-time login; persists the session into the profile dir.

    If ``token_url`` (a TinyGS ``?loginToken=...&userId=...`` link) is given, logs
    in non-interactively and headless — no window, no keypress. Otherwise opens a
    visible browser for you to log in by hand.
    """
    with sync_playwright() as p:
        if token_url:
            print("Logging in via token link (headless, no window)...")
            ctx = _make_context(p, profile_dir, browser_path, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(token_url, wait_until="networkidle", timeout=60000)
            except PWTimeout:
                pass
            page.wait_for_timeout(4000)  # let the app exchange the token for a session
        else:
            print("Opening a browser window. Log in to TinyGS (top-right 'Login').")
            print("When you can see your account / the satellite page, return here.\n")
            ctx = _make_context(p, profile_dir, browser_path, headless=False)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(f"{APP_BASE}/login", wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                page.goto(APP_BASE, wait_until="domcontentloaded", timeout=60000)
            try:
                input("Press ENTER once you're logged in... ")
            except EOFError:
                print("No interactive stdin; waiting 90 s for you to log in instead.")
                page.wait_for_timeout(90000)
        # Verify by loading the satellite page and watching for an authorized fetch.
        ok = _probe_logged_in(page, satellite)
        ctx.close()
    if ok:
        print(f"\nLogin saved to {profile_dir}. You can now run the poller headless.")
        return 0
    print("\nCould not confirm a logged-in session. Re-check the login link / "
          "credentials and try again.")
    return 1


def _probe_logged_in(page, satellite: str) -> bool:
    """Load the satellite page once; return True if an authorized /v4/packets fired."""
    seen = {"status": None, "count": 0}

    def on_response(resp):
        if "/v4/packets" in resp.url:
            seen["status"] = resp.status
            seen["count"] += 1

    page.on("response", on_response)
    try:
        page.goto(f"{APP_BASE}/satellite/{satellite}",
                  wait_until="networkidle", timeout=45000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2500)
    page.remove_listener("response", on_response)
    if "/login" in page.url:
        return False
    return seen["count"] > 0 and (seen["status"] or 0) < 400


def _run_cycle(page, satellite: str, dry_run: bool) -> dict:
    """One capture cycle: (re)load the satellite page, intercept packets, store new."""
    payloads: list = []
    statuses: list = []

    def on_response(resp):
        if "/v4/packets" not in resp.url:
            return
        statuses.append(resp.status)
        if not resp.ok:
            return
        try:
            payloads.append(resp.json())
        except Exception:
            log.debug("could not JSON-decode a /v4/packets response", exc_info=True)

    page.on("response", on_response)
    try:
        page.goto(f"{APP_BASE}/satellite/{satellite}",
                  wait_until="networkidle", timeout=45000)
    except PWTimeout:
        log.warning("page load timed out; processing whatever was captured")
    page.wait_for_timeout(2500)  # let any trailing XHR land
    page.remove_listener("response", on_response)

    counts = {"captured": 0, "new": 0, "dup": 0, "nodata": 0, "error": 0,
              "responses": len(statuses), "statuses": statuses,
              "logged_out": "/login" in page.url}

    for payload in payloads:
        for pkt in _extract_list(payload):
            if not isinstance(pkt, dict):
                continue
            counts["captured"] += 1
            if dry_run:
                continue
            try:
                fields = parse_packet(pkt)
                counts[store_one(fields, pkt, source=SOURCE)] += 1
            except Exception as exc:
                counts["error"] += 1
                log.debug("store failed for one packet: %s", exc, exc_info=True)
    return counts


def run_poll(profile_dir, browser_path, satellite, interval, headed, once, dry_run,
             token_url=None):
    log.info("Starting browser poller: sat=%s interval=%ds headless=%s profile=%s",
             satellite, interval, not headed, profile_dir)
    if dry_run:
        log.info("DRY RUN — packets will be parsed but NOT written to the DB.")
    # If a login token is configured and we have no saved session yet, log in first
    # so the very first run works unattended (single-command startup on the Pi).
    if token_url and not Path(profile_dir).exists():
        log.info("No saved profile; performing token login first.")
        do_login(profile_dir, browser_path, satellite, token_url=token_url)
    with sync_playwright() as p:
        ctx = _make_context(p, profile_dir, browser_path, headless=not headed)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        warned_logout = False
        relogin_tried = False
        try:
            while True:
                try:
                    c = _run_cycle(page, satellite, dry_run)
                except Exception:
                    log.exception("cycle crashed; retrying after interval")
                    if once:
                        return 1
                    page.wait_for_timeout(interval * 1000)
                    continue

                if c["logged_out"] or (c["responses"] and all(s >= 400 for s in c["statuses"])):
                    # Try to silently re-auth once with the token link, if we have one.
                    if token_url and not relogin_tried:
                        relogin_tried = True
                        log.warning("session looks logged-out; refreshing via token link")
                        try:
                            page.goto(token_url, wait_until="networkidle", timeout=60000)
                            page.wait_for_timeout(4000)
                        except PWTimeout:
                            pass
                        continue  # retry a cycle immediately
                    if not warned_logout:
                        log.error(
                            "NOT LOGGED IN (statuses=%s). Run:  "
                            "python tinygs_browser_poller.py --login  "
                            "(or set TINYGS_LOGIN_URL)",
                            c["statuses"] or "no /v4/packets response",
                        )
                        warned_logout = True
                elif c["responses"] == 0:
                    log.warning("no /v4/packets response seen this cycle "
                                "(page layout changed, or slow network?)")
                    warned_logout = False
                else:
                    warned_logout = False
                    relogin_tried = False
                    log.info("cycle: captured=%d  new=%d  dup=%d  nodata=%d  err=%d",
                             c["captured"], c["new"], c["dup"], c["nodata"], c["error"])

                if once:
                    return 0
                page.wait_for_timeout(interval * 1000)
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            ctx.close()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Live TinyGS packet capture through a real logged-in browser.")
    ap.add_argument("--login", action="store_true",
                    help="One-time login; saves the session, then exits. Interactive "
                         "unless --login-token / TINYGS_LOGIN_URL is set (then headless).")
    ap.add_argument("--login-token", default=DEFAULT_LOGIN_URL,
                    help="TinyGS login link (app.tinygs.com?loginToken=...&userId=...) "
                         "for unattended headless login. A credential: keep it in mission.env.")
    ap.add_argument("--satellite", default=DEFAULT_SAT,
                    help=f"Satellite page to watch (default {DEFAULT_SAT}).")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="Persistent browser profile dir (holds the login).")
    ap.add_argument("--browser-path", default=DEFAULT_BROWSER,
                    help="Chromium executable (default: Playwright's bundled one; "
                         "on the Pi use /usr/bin/chromium-browser).")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help=f"Seconds between capture cycles (default {DEFAULT_INTERVAL}).")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser window (default: headless).")
    ap.add_argument("--once", action="store_true",
                    help="Run a single cycle and exit (cron / testing).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse packets but do not write to the DB.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-16s %(levelname)-7s %(message)s",
    )

    if args.login:
        return do_login(args.profile, args.browser_path, args.satellite,
                        token_url=args.login_token)
    return run_poll(args.profile, args.browser_path, args.satellite,
                    args.interval, args.headed, args.once, args.dry_run,
                    token_url=args.login_token)


if __name__ == "__main__":
    sys.exit(main())
