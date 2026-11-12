#!/usr/bin/env python3
"""
The client talks to ``server/time_server.c`` over UDP, exchanges the four
timestamps T1..T4, and computes::

    RTT   = (T4 - T1) - (T3 - T2)
    theta = ((T2 - T1) + (T3 - T4)) / 2          theta = server clock - client clock

    theta > 0 : the client clock is BEHIND the server
    theta < 0 : the client clock is AHEAD of the server
"""

import argparse
import csv
import json
import math
import socket
import struct
import sys
import time

from virtual_clock import VirtualClock

# --------------------------------------------------------------------------- #
# Protocol definition (must match time_server.c)
# --------------------------------------------------------------------------- #
PROTO_MAGIC = 0x5453
PROTO_VERSION = 1
MSG_REQUEST = 1
MSG_RESPONSE = 2

HEADER_FORMAT = "!HBBQ"
REQUEST_FORMAT = "!HBBQd"
RESPONSE_FORMAT = "!HBBQdd"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)        # 12
REQUEST_SIZE = struct.calcsize(REQUEST_FORMAT)      # 20
RESPONSE_SIZE = struct.calcsize(RESPONSE_FORMAT)    # 28
RECV_BUFFER = 2048

# --------------------------------------------------------------------------- #
# Measurement record layout (identical for every request, success or failure)
# --------------------------------------------------------------------------- #
FIELDS = [
    "sequence", "request_id", "server",
    "t1", "t2", "t3", "t4",
    "rtt_s", "rtt_ms",
    "estimated_offset_s", "estimated_offset_ms",
    "actual_offset_s", "actual_offset_ms",
    "sync_error_s", "sync_error_ms",
    "retries", "status",
    "correction_applied", "offset_after_correction_s", "offset_after_correction_ms",
    "drift_ppm", "error",
]


class ResponseRejected(Exception):
    """A received datagram was not an acceptable response to the current request."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind            # "malformed" or "stale"


class AttemptFailed(Exception):
    """One transmission attempt did not produce a valid measurement."""


# --------------------------------------------------------------------------- #
# Protocol and calculation helpers
# --------------------------------------------------------------------------- #
def build_request(request_id, t1):
    """Encode a REQUEST datagram."""
    return struct.pack(REQUEST_FORMAT, PROTO_MAGIC, PROTO_VERSION, MSG_REQUEST, request_id, t1)


def parse_response(data, expected_id):
    """Validate a RESPONSE datagram and return ``(t2, t3)``.

    Raises ResponseRejected("malformed", ...) for bad length/magic/version/type
    or non-finite timestamps, and ResponseRejected("stale", ...) when the
    request identifier does not match ``expected_id``.
    """
    if len(data) != RESPONSE_SIZE:
        raise ResponseRejected("malformed", "bad length %d (expected %d)" % (len(data), RESPONSE_SIZE))
    magic, version, msg_type, request_id, t2, t3 = struct.unpack(RESPONSE_FORMAT, data)
    if magic != PROTO_MAGIC:
        raise ResponseRejected("malformed", "bad magic 0x%04x" % magic)
    if version != PROTO_VERSION:
        raise ResponseRejected("malformed", "unsupported protocol version %d" % version)
    if msg_type != MSG_RESPONSE:
        raise ResponseRejected("malformed", "unexpected message type %d" % msg_type)
    if not (math.isfinite(t2) and math.isfinite(t3)):
        raise ResponseRejected("malformed", "non-finite timestamp in response")
    if request_id != expected_id:
        raise ResponseRejected("stale", "request id %d does not match current id %d"
                               % (request_id, expected_id))
    return t2, t3


def compute_sync(t1, t2, t3, t4):
    """Return ``(rtt, theta)`` in seconds from the four timestamps.

    rtt   = (T4 - T1) - (T3 - T2)
    theta = ((T2 - T1) + (T3 - T4)) / 2   (server clock - client clock)
    """
    rtt = (t4 - t1) - (t3 - t2)
    theta = ((t2 - t1) + (t3 - t4)) / 2.0
    return rtt, theta


def _ms(value):
    """Seconds -> milliseconds, preserving None."""
    return None if value is None else value * 1000.0


# --------------------------------------------------------------------------- #
# Synchronization client
# --------------------------------------------------------------------------- #
class SyncClient:
    """Performs synchronization exchanges against one UDP time server."""

    def __init__(self, host, port, clock, timeout=2.0, retries=3, correct=False, log=None):
        self.host = host
        self.port = port
        self.clock = clock
        self.timeout = timeout
        self.retries = retries
        self.correct = correct
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self._next_id = 1
        self.sock = None
        self.counters = {"malformed": 0, "stale": 0}

    def open(self):
        """Resolve the server address and create the (connected) UDP socket."""
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_DGRAM)
        family, socktype, proto, _canon, addr = infos[0]
        self.sock = socket.socket(family, socktype, proto)
        # connect() on UDP only fixes the peer: the kernel then drops datagrams
        # from any other source address/port.
        self.sock.connect(addr)
        self.server_label = "%s:%d" % (addr[0], addr[1])

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    # -- one transmission attempt ------------------------------------------ #
    def _attempt(self, request_id):
        """Send one request and wait for its response.

        Returns ``(t1, t2, t3, t4, offset_at_t1, offset_at_t4)``; raises
        AttemptFailed on timeout, network error or an unusable response.
        """
        buf = bytearray(REQUEST_SIZE)
        struct.pack_into("!HBBQ", buf, 0, PROTO_MAGIC, PROTO_VERSION, MSG_REQUEST, request_id)

        # T1: read as late as possible, right before the datagram is sent.
        t1, off1 = self.clock.get_time_and_offset()
        struct.pack_into("!d", buf, HEADER_SIZE, t1)
        try:
            self.sock.send(bytes(buf))
        except OSError as exc:
            raise AttemptFailed("send failed: %s" % exc)

        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AttemptFailed("timeout after %.3f s (no valid response)" % self.timeout)
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(RECV_BUFFER)
            except socket.timeout:
                raise AttemptFailed("timeout after %.3f s (no valid response)" % self.timeout)
            except ConnectionRefusedError:
                # ICMP port-unreachable: nobody is listening. Wait out the
                # timeout so retries stay paced instead of firing instantly.
                time.sleep(max(0.0, deadline - time.monotonic()))
                raise AttemptFailed("server unavailable (connection refused)")
            except OSError as exc:
                raise AttemptFailed("receive failed: %s" % exc)

            # T4: first thing after the datagram is received.
            t4, off4 = self.clock.get_time_and_offset()

            try:
                t2, t3 = parse_response(data, request_id)
            except ResponseRejected as exc:
                self.counters[exc.kind] += 1
                self._log("warning: discarded %s response: %s" % (exc.kind, exc))
                continue                    # keep waiting until the deadline

            if t3 < t2:
                raise AttemptFailed("invalid response: server T3 (%.6f) < T2 (%.6f)" % (t3, t2))
            return t1, t2, t3, t4, off1, off4

    # -- one logical synchronization (with retries) ------------------------- #
    def synchronize_once(self, sequence):
        """Run one synchronization, retrying on failure. Returns a record dict
        containing every field in FIELDS."""
        record = dict.fromkeys(FIELDS)
        record.update(sequence=sequence, server=self.server_label, status="FAILED",
                      correction_applied=False, drift_ppm=self.clock.get_drift())
        last_error = "no attempt made"

        for attempt in range(self.retries + 1):
            request_id = self._next_id
            self._next_id += 1
            record["request_id"] = request_id
            record["retries"] = attempt
            try:
                t1, t2, t3, t4, off1, off4 = self._attempt(request_id)
            except AttemptFailed as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    self._log("request %d: %s; retrying (%d/%d)"
                              % (request_id, last_error, attempt + 1, self.retries))
                continue

            rtt, theta = compute_sync(t1, t2, t3, t4)
            if rtt < 0:
                last_error = "invalid sample: negative round-trip delay (%.6f s)" % rtt
                if attempt < self.retries:
                    self._log("request %d: %s; retrying (%d/%d)"
                              % (request_id, last_error, attempt + 1, self.retries))
                continue

            # Actual offset in theta convention (reference - virtual), averaged
            # over the exchange because drift changes the offset while we wait.
            actual = 0.0 - (off1 + off4) / 2.0      # '0.0 -' avoids printing -0.0
            record.update(t1=t1, t2=t2, t3=t3, t4=t4, rtt_s=rtt, rtt_ms=_ms(rtt),
                          estimated_offset_s=theta, estimated_offset_ms=_ms(theta),
                          actual_offset_s=actual, actual_offset_ms=_ms(actual),
                          sync_error_s=abs(theta - actual), sync_error_ms=_ms(abs(theta - actual)),
                          status="SUCCESS", error=None)

            if self.correct:
                self.clock.adjust_clock(theta)
                after = self.clock.get_offset_to_reference()
                record.update(correction_applied=True, offset_after_correction_s=after,
                              offset_after_correction_ms=_ms(after))
            return record

        record["error"] = "%s (gave up after %d attempt(s))" % (last_error, self.retries + 1)
        return record


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def format_text(record):
    """Human-readable block for one record."""
    def ms(value):
        return "n/a" if value is None else "%+.3f ms" % value

    def ts(value):
        return "n/a" if value is None else "%.6f" % value

    lines = ["=" * 40, "UDP TIME SYNCHRONIZATION", "",
             "Server: %s" % record["server"], "",
             "Request ID: %s   (sequence %d, retries %d)"
             % (record["request_id"], record["sequence"], record["retries"] or 0), "",
             "T1: %s   T2: %s" % (ts(record["t1"]), ts(record["t2"])),
             "T3: %s   T4: %s" % (ts(record["t3"]), ts(record["t4"])), ""]
    if record["status"] == "SUCCESS":
        lines += ["Round Trip Delay: %.3f ms" % record["rtt_ms"], "",
                  "Estimated Clock Offset: %s" % ms(record["estimated_offset_ms"]),
                  "Actual Clock Offset: %s" % ms(record["actual_offset_ms"]), "",
                  "Synchronization Error: %.3f ms" % record["sync_error_ms"], ""]
        if record["correction_applied"]:
            lines += ["Offset before correction: %s" % ms(record["actual_offset_ms"]),
                      "Offset after correction: %s" % ms(record["offset_after_correction_ms"]), ""]
    else:
        lines += ["Error: %s" % record["error"], ""]
    lines += ["Status: %s" % record["status"], "=" * 40]
    return "\n".join(lines)


def summarize(records):
    """Aggregate statistics over successful records only."""
    ok = [r for r in records if r["status"] == "SUCCESS"]
    summary = {"requests": len(records), "successful": len(ok), "failed": len(records) - len(ok),
               "total_retries": sum(r["retries"] or 0 for r in records)}
    if ok:
        rtts = [r["rtt_ms"] for r in ok]
        errs = [r["sync_error_ms"] for r in ok]
        summary.update(rtt_ms_min=min(rtts), rtt_ms_mean=sum(rtts) / len(rtts), rtt_ms_max=max(rtts),
                       sync_error_ms_mean=sum(errs) / len(errs), sync_error_ms_max=max(errs))
    return summary


def format_summary(summary):
    lines = ["", "SUMMARY: %d request(s): %d successful, %d failed, %d retransmission(s)"
             % (summary["requests"], summary["successful"], summary["failed"], summary["total_retries"])]
    if summary["successful"]:
        lines.append("  RTT ms (min/mean/max): %.3f / %.3f / %.3f"
                     % (summary["rtt_ms_min"], summary["rtt_ms_mean"], summary["rtt_ms_max"]))
        lines.append("  Sync error ms (mean/max): %.3f / %.3f"
                     % (summary["sync_error_ms_mean"], summary["sync_error_ms_max"]))
    return "\n".join(lines)


class OutputManager:
    """Routes records to the terminal (text/json/csv) and an optional file.

    * ``fmt='text'`` : human-readable blocks on stdout.
    * ``fmt='csv'``  : CSV rows streamed to stdout (diagnostics go to stderr).
    * ``fmt='json'`` : one JSON document on stdout at the end.
    * ``path``       : also write the records to a file: JSON if the name ends in
                       ``.json``, otherwise CSV (independent of ``fmt``).
    """

    def __init__(self, fmt, path):
        self.fmt = fmt
        self.path = path
        self.records = []
        self.file_is_json = bool(path) and path.lower().endswith(".json")
        self._file = None
        self._file_writer = None
        self._stdout_writer = None

    def open(self):
        """Open outputs early so problems (e.g. unwritable path) surface before any work."""
        if self.path:
            self._file = open(self.path, "w", newline="", encoding="utf-8")
            if not self.file_is_json:
                self._file_writer = csv.DictWriter(self._file, fieldnames=FIELDS, lineterminator="\n")
                self._file_writer.writeheader()
                self._file.flush()
        if self.fmt == "csv":
            self._stdout_writer = csv.DictWriter(sys.stdout, fieldnames=FIELDS, lineterminator="\n")
            self._stdout_writer.writeheader()

    def emit(self, record):
        self.records.append(record)
        if self.fmt == "text":
            print(format_text(record))
        elif self._stdout_writer:
            self._stdout_writer.writerow(record)
        if self._file_writer:
            self._file_writer.writerow(record)
            self._file.flush()
        sys.stdout.flush()

    def close(self, server_label, summary):
        document = {"server": server_label, "records": self.records, "summary": summary}
        if self.fmt == "json":
            print(json.dumps(document, indent=2))
        if self._file:
            if self.file_is_json:
                json.dump(document, self._file, indent=2)
                self._file.write("\n")
            self._file.close()
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="UDP time synchronization client. Exchanges four timestamps (T1..T4) with "
                    "time_server and estimates the clock offset (theta = server - client) and "
                    "round-trip delay. The client clock is simulated by a VirtualClock.",
        epilog="Example: python3 time_client.py --host 127.0.0.1 --port 5005 --count 10 "
               "--offset -5 --drift 100 --correct")
    p.add_argument("--host", default="127.0.0.1", help="server IP address or hostname (default: %(default)s)")
    p.add_argument("--port", type=int, default=5005, help="server UDP port (default: %(default)s)")
    p.add_argument("--count", type=int, default=10, help="number of synchronization requests (default: %(default)s)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between requests (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=2.0, help="seconds to wait for each response (default: %(default)s)")
    p.add_argument("--retries", type=int, default=3,
                   help="retransmissions after a timeout/failed attempt (default: %(default)s)")
    p.add_argument("--offset", type=float, default=0.0,
                   help="simulated initial client clock offset in seconds, client - reference; "
                        "positive = client AHEAD, negative = client BEHIND (default: %(default)s)")
    p.add_argument("--drift", type=float, default=0.0,
                   help="simulated client clock drift in ppm; positive = runs faster (default: %(default)s)")
    p.add_argument("--output", metavar="FILE", default=None,
                   help="also write all records to FILE: CSV by default, JSON if FILE ends in .json")
    p.add_argument("--format", choices=("text", "json", "csv"), default="text",
                   help="terminal output format (default: %(default)s); json/csv are machine-readable "
                        "and keep stdout clean (diagnostics go to stderr)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--correct", dest="correct", action="store_true",
                       help="apply each estimated offset to the virtual clock")
    group.add_argument("--no-correct", dest="correct", action="store_false",
                       help="only measure; never modify the virtual clock (default)")
    p.set_defaults(correct=False)

    args = p.parse_args(argv)
    if not 1 <= args.port <= 65535:
        p.error("--port must be between 1 and 65535")
    if args.count < 1:
        p.error("--count must be at least 1")
    if not (math.isfinite(args.interval) and args.interval >= 0):
        p.error("--interval must be a finite number >= 0")
    if not (math.isfinite(args.timeout) and args.timeout > 0):
        p.error("--timeout must be a finite number > 0")
    if args.retries < 0:
        p.error("--retries must be >= 0")
    if not math.isfinite(args.offset):
        p.error("--offset must be a finite number")
    if not math.isfinite(args.drift) or args.drift <= -1e6:
        p.error("--drift must be a finite number greater than -1000000 ppm")
    return args


def main(argv=None):
    args = parse_args(argv)
    machine_readable = args.format != "text"

    def log(message):
        print(message, file=sys.stderr)

    try:
        clock = VirtualClock(initial_offset=args.offset, drift_ppm=args.drift)
    except ValueError as exc:
        log("error: %s" % exc)
        return 2

    client = SyncClient(args.host, args.port, clock, timeout=args.timeout, retries=args.retries,
                        correct=args.correct, log=log)
    try:
        client.open()
    except OSError as exc:      # includes socket.gaierror
        log("error: cannot set up UDP socket to %s:%d: %s" % (args.host, args.port, exc))
        return 2

    output = OutputManager(args.format, args.output)
    try:
        output.open()
    except OSError as exc:
        log("error: cannot open output file %s: %s" % (args.output, exc))
        client.close()
        return 2

    interrupted = False
    try:
        for sequence in range(1, args.count + 1):
            output.emit(client.synchronize_once(sequence))
            if sequence < args.count:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted; reporting measurements collected so far")
    finally:
        client.close()

    summary = summarize(output.records)
    output.close(client.server_label, summary)
    summary_text = format_summary(summary)
    if client.counters["malformed"] or client.counters["stale"]:
        summary_text += "\n  Discarded datagrams: %d malformed, %d stale" % (
            client.counters["malformed"], client.counters["stale"])
    print(summary_text, file=sys.stderr if machine_readable else sys.stdout)

    if interrupted or summary["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
