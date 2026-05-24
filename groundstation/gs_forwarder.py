"""
Ground Station Packet Forwarder — Pluggable packet source with AWS ingest.

Reads packets from a configurable ground station source (antenna hardware
TBD) and POSTs them to the AWS ingest server for storage and processing.

The ``PacketSource`` abstract base class defines the interface — implement
``read_packets()`` to yield dicts from any hardware or software backend.
Three stub implementations are provided out of the box:

  * ``FileWatcherSource`` — watches a directory for new observation files
    (e.g. SatNOGS dump directories).
  * ``TcpSocketSource`` — listens on a TCP port for incoming packet data
    from a radio frontend.
  * ``StdinSource`` — reads JSON packets from stdin, suitable for piping
    from GNURadio or other command-line tools.

Each packet is forwarded as JSON to ``POST /api/ingest/groundstation``::

    {
      "satellite": "HUCSat",
      "data": "<base64 encoded raw bytes>",
      "station": "harvard-roof",
      "frequency": 436.7,
      "rssi": -95.0,
      "snr": 8.5,
      "source": "roof_groundstation",
      "unix_GS_time": 1716556800
    }

Authentication uses the ``X-API-Key`` header, with the key read from the
``CUBESAT_INGEST_KEY`` environment variable.

Usage
-----
Pipe JSON packets from GNURadio (or any tool) via stdin::

    gnuradio_decoder | python -m groundstation.gs_forwarder

Or import and wire up a custom source::

    from groundstation.gs_forwarder import AwsForwarder, TcpSocketSource
    source = TcpSocketSource(port=9999)
    fwd = AwsForwarder(source)
    fwd.run()

Configuration
-------------
Set the following environment variables (or edit the defaults below):

  CUBESAT_INGEST_KEY     API key for the AWS ingest server
  CUBESAT_INGEST_URL     Base URL of the ingest server
                         (default: http://localhost:8000)
  CUBESAT_STATION_ID     Station identifier (default: harvard-roof)
"""

import abc
import base64
import json
import logging
import os
import socket
import sys
import time
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Configuration (from environment)
# ──────────────────────────────────────────────

INGEST_BASE_URL = os.environ.get("CUBESAT_INGEST_URL", "http://localhost:8000")
INGEST_API_KEY  = os.environ.get("CUBESAT_INGEST_KEY", "")
STATION_ID      = os.environ.get("CUBESAT_STATION_ID", "harvard-roof")


# ──────────────────────────────────────────────
# PacketSource — abstract base class
# ──────────────────────────────────────────────

class PacketSource(abc.ABC):
    """Abstract interface for ground station packet sources.

    Subclass this and implement ``read_packets()`` to adapt any antenna
    hardware, SDR pipeline, or file-based workflow.  The method should
    yield one dict per received packet.  Each dict **must** contain at
    least a ``"data"`` key with base64-encoded raw bytes; additional
    metadata (frequency, rssi, snr, etc.) is optional but recommended.
    """

    @abc.abstractmethod
    def read_packets(self) -> Iterator[dict]:
        """Yield packet dicts from the ground station source.

        Each dict should follow the ingest schema::

            {
                "satellite": str,
                "data": str,          # base64-encoded raw bytes
                "station": str,
                "frequency": float,   # MHz
                "rssi": float,        # dBm
                "snr": float,         # dB
                "source": str,
                "unix_GS_time": int,  # Unix epoch seconds
            }

        Missing fields will be filled with defaults by ``AwsForwarder``.
        """
        ...


# ──────────────────────────────────────────────
# Stub implementations
# ──────────────────────────────────────────────

class FileWatcherSource(PacketSource):
    """Watch a directory for new observation files.

    Intended for workflows where another tool (e.g. SatNOGS client) drops
    raw observation files into a spool directory.  This source polls for
    new files, reads their contents, and yields one packet per file.

    Parameters
    ----------
    watch_dir : str
        Path to the directory to watch for new files.
    poll_interval : float
        Seconds between directory polls (default 5.0).
    """

    def __init__(self, watch_dir: str, poll_interval: float = 5.0):
        self.watch_dir = watch_dir
        self.poll_interval = poll_interval

    def read_packets(self) -> Iterator[dict]:
        """Poll *watch_dir* for new files and yield a packet per file.

        Files are processed in alphabetical order.  Once a file has been
        read, it is renamed with a ``.processed`` suffix to avoid
        re-reading.

        .. note::
            Replace the body of this method with real parsing logic once
            the observation file format is known.
        """
        seen: set[str] = set()
        log.info("FileWatcherSource: watching %s (poll every %.1fs)",
                 self.watch_dir, self.poll_interval)

        while True:
            try:
                entries = sorted(os.listdir(self.watch_dir))
            except FileNotFoundError:
                log.warning("Watch directory %s does not exist yet", self.watch_dir)
                time.sleep(self.poll_interval)
                continue

            for filename in entries:
                if filename.endswith(".processed"):
                    continue
                filepath = os.path.join(self.watch_dir, filename)
                if not os.path.isfile(filepath) or filepath in seen:
                    continue

                try:
                    with open(filepath, "rb") as f:
                        raw_bytes = f.read()
                except OSError as exc:
                    log.warning("Could not read %s: %s", filepath, exc)
                    continue

                seen.add(filepath)

                # Rename to mark as processed
                processed_path = filepath + ".processed"
                try:
                    os.rename(filepath, processed_path)
                except OSError as exc:
                    log.warning("Could not rename %s: %s", filepath, exc)

                yield {
                    "satellite": "HUCSat",
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                    "station": STATION_ID,
                    "source": "file_watcher",
                    "unix_GS_time": int(time.time()),
                }

            time.sleep(self.poll_interval)


class TcpSocketSource(PacketSource):
    """Listen on a TCP port for incoming packet data.

    Each connection is expected to send one complete packet as a JSON
    object (terminated by closing the connection or a newline).  This is
    suitable for radio frontends that can push decoded frames over TCP.

    Parameters
    ----------
    host : str
        Bind address (default ``"0.0.0.0"``).
    port : int
        TCP port to listen on (default ``9999``).
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 9999):
        self.host = host
        self.port = port

    def read_packets(self) -> Iterator[dict]:
        """Accept TCP connections and yield one packet per connection.

        Each client should send a JSON object with at least a ``"data"``
        field (base64-encoded).  The connection is closed after reading.

        .. note::
            Adjust the framing protocol once the radio frontend is chosen.
        """
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(5)
        log.info("TcpSocketSource: listening on %s:%d", self.host, self.port)

        try:
            while True:
                conn, addr = srv.accept()
                log.debug("TCP connection from %s:%d", *addr)
                try:
                    chunks = []
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)

                    try:
                        packet = json.loads(raw)
                    except json.JSONDecodeError:
                        # Treat the entire payload as raw binary data
                        packet = {
                            "satellite": "HUCSat",
                            "data": base64.b64encode(raw).decode("ascii"),
                            "station": STATION_ID,
                            "source": "tcp_socket",
                            "unix_GS_time": int(time.time()),
                        }

                    yield packet

                except OSError as exc:
                    log.warning("Error reading from %s:%d: %s", *addr, exc)
                finally:
                    conn.close()
        finally:
            srv.close()


class StdinSource(PacketSource):
    """Read JSON packets from stdin, one per line.

    Designed for piping from GNURadio, ``rtl_fm``, or any tool that
    outputs one JSON object per line to stdout.  Blank lines and lines
    starting with ``#`` are skipped.
    """

    def read_packets(self) -> Iterator[dict]:
        """Read lines from stdin, parse as JSON, and yield packet dicts.

        Non-JSON lines are treated as raw hex or base64 data and wrapped
        in the standard packet envelope.
        """
        log.info("StdinSource: reading JSON packets from stdin (one per line)")

        for line in sys.stdin:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                packet = json.loads(line)
            except json.JSONDecodeError:
                # Treat the line as raw base64 data
                log.debug("Non-JSON line, wrapping as raw data")
                packet = {
                    "satellite": "HUCSat",
                    "data": line,
                    "station": STATION_ID,
                    "source": "stdin",
                    "unix_GS_time": int(time.time()),
                }

            yield packet


# ──────────────────────────────────────────────
# AWS Ingest Forwarder
# ──────────────────────────────────────────────

class AwsForwarder:
    """Forward packets from a ``PacketSource`` to the AWS ingest endpoint.

    Parameters
    ----------
    source : PacketSource
        The packet source to read from.
    base_url : str
        Base URL of the ingest server (default from ``CUBESAT_INGEST_URL``).
    api_key : str
        API key sent in the ``X-API-Key`` header (default from
        ``CUBESAT_INGEST_KEY``).
    max_retries : int
        Number of retries on transient HTTP errors (default 3).
    retry_delay : float
        Base delay in seconds between retries, doubled each attempt
        (default 2.0).
    """

    INGEST_PATH = "/api/ingest/groundstation"

    def __init__(
        self,
        source: PacketSource,
        base_url: str = INGEST_BASE_URL,
        api_key: str = INGEST_API_KEY,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._url = f"{self.base_url}{self.INGEST_PATH}"

    def _fill_defaults(self, packet: dict) -> dict:
        """Ensure required fields are present, filling with defaults."""
        defaults = {
            "satellite": "HUCSat",
            "station": STATION_ID,
            "source": "roof_groundstation",
            "unix_GS_time": int(time.time()),
        }
        merged = {**defaults, **packet}
        return merged

    def _post_packet(self, packet: dict) -> bool:
        """POST a single packet to the ingest endpoint. Returns True on success."""
        payload = json.dumps(packet).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        delay = self.retry_delay
        for attempt in range(1, self.max_retries + 1):
            try:
                req = Request(self._url, data=payload, headers=headers, method="POST")
                resp = urlopen(req, timeout=15)
                status = resp.getcode()
                log.info("POST %s -> %d (%d bytes)", self._url, status, len(payload))
                return True

            except HTTPError as exc:
                # 4xx errors are not retryable (bad request, auth failure, etc.)
                if 400 <= exc.code < 500:
                    log.error("POST %s -> %d (not retryable): %s",
                              self._url, exc.code, exc.reason)
                    return False
                log.warning("POST %s -> %d (attempt %d/%d): %s",
                            self._url, exc.code, attempt, self.max_retries, exc.reason)

            except URLError as exc:
                log.warning("POST %s failed (attempt %d/%d): %s",
                            self._url, attempt, self.max_retries, exc.reason)

            except OSError as exc:
                log.warning("POST %s failed (attempt %d/%d): %s",
                            self._url, attempt, self.max_retries, exc)

            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2

        log.error("Giving up on packet after %d attempts", self.max_retries)
        return False

    def run(self):
        """Read packets from the source and forward each to AWS.

        Runs until the source is exhausted or interrupted.  Failures on
        individual packets are logged but do not stop the loop.
        """
        if not self.api_key:
            log.warning(
                "CUBESAT_INGEST_KEY not set — requests will be sent without "
                "authentication. Set the environment variable to enable auth."
            )

        log.info("Forwarder started: %s -> %s", type(self.source).__name__, self._url)
        forwarded = 0
        failed = 0

        try:
            for packet in self.source.read_packets():
                packet = self._fill_defaults(packet)
                ok = self._post_packet(packet)
                if ok:
                    forwarded += 1
                else:
                    failed += 1
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            log.info("Forwarder stopped: %d forwarded, %d failed", forwarded, failed)


# ──────────────────────────────────────────────
# Standalone entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    if not INGEST_API_KEY:
        print(
            "WARNING: CUBESAT_INGEST_KEY is not set.\n"
            "Packets will be forwarded without authentication.\n"
            "Set the environment variable before running in production.\n"
        )

    print(
        f"Ground Station Forwarder — reading JSON from stdin\n"
        f"  Ingest URL : {INGEST_BASE_URL}{AwsForwarder.INGEST_PATH}\n"
        f"  Station    : {STATION_ID}\n"
        f"\n"
        f"Paste one JSON packet per line, or pipe from GNURadio.\n"
        f"Press Ctrl+C to stop.\n"
    )

    source = StdinSource()
    forwarder = AwsForwarder(source)
    forwarder.run()
