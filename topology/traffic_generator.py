
"""
Manage background UDP traffic for Mininet experiments.

Uses iperf to generate competing traffic on UDP port 6000.
"""

import subprocess
import time


def start_udp_server(host, port=6000, log_path="/tmp/iperf-server.log"):
    """Start an iperf UDP server inside a Mininet host."""

    log = open(log_path, "w")

    try:
        process = host.popen(
            ["iperf", "-s", "-u", "-p", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT
        )
    finally:
        log.close()

    return process


def start_background_traffic(
    host,
    destination="10.0.0.1",
    bandwidth_mbps=3,
    duration=60,
    port=6000,
    log_path="/tmp/iperf-client.log"
):
    """Start background UDP traffic without blocking the experiment."""

    if bandwidth_mbps <= 0:
        raise ValueError("Bandwidth must be positive")

    if duration <= 0:
        raise ValueError("Duration must be positive")

    log = open(log_path, "w")

    try:
        process = host.popen(
            [
                "iperf",
                "-c", destination,
                "-u",
                "-p", str(port),
                "-b", f"{bandwidth_mbps}M",
                "-t", str(duration)
            ],
            stdout=log,
            stderr=subprocess.STDOUT
        )
    finally:
        log.close()

    return process


def stop_traffic(process):
    """Terminate a running traffic process."""

    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def is_running(process):
    """Check whether the background process is still active."""

    return process is not None and process.poll() is None


def wait_for_traffic_start(process, seconds=1):
    """Allow the process to initialize and check for early exit."""

    time.sleep(seconds)

    if not is_running(process):
        raise RuntimeError(
            "Background traffic process exited unexpectedly"
        )
