
#!/usr/bin/env python3
"""Repeatable time synchronization experiments under UDP congestion."""

import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from mininet.log import setLogLevel


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "topology"))

from network_topology import build_network, start_network
from traffic_generator import (
    start_udp_server,
    start_background_traffic,
    stop_traffic,
    is_running,
    wait_for_traffic_start,
)


SERVER_BINARY = ROOT / "server" / "time_server"
CLIENT_SCRIPT = ROOT / "client" / "time_client.py"
RESULTS_DIR = ROOT / "results" / "congestion"


def stop_process(process):
    """Terminate a process if it is still running."""
    if process is None or process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def capture_tc_stats(output_path):
    """Capture qdisc and HTB class statistics for both bottleneck ports."""

    with output_path.open("w") as log:
        for interface in ("s1-eth2", "s2-eth4"):
            log.write(f"\n=== {interface}: QDISC ===\n")
            result = subprocess.run(
                ["tc", "-s", "qdisc", "show", "dev", interface],
                capture_output=True,
                text=True
            )
            log.write(result.stdout)
            log.write(result.stderr)

            log.write(f"\n=== {interface}: CLASS ===\n")
            result = subprocess.run(
                ["tc", "-s", "class", "show", "dev", interface],
                capture_output=True,
                text=True
            )
            log.write(result.stdout)
            log.write(result.stderr)


def summarize_results(csv_path):
    """Return summary metrics for one synchronization trial."""

    with csv_path.open(newline="") as file:
        rows = list(csv.DictReader(file))

    successful = [
        row for row in rows
        if row["status"] == "SUCCESS"
    ]

    rtts = [float(r["rtt_ms"]) for r in successful]
    errors = [float(r["sync_error_ms"]) for r in successful]

    total = len(rows)

    summary = {
        "requests": total,
        "successful": len(successful),
        "failed": total - len(successful),
        "success_rate_pct": (
            100 * len(successful) / total if total else None
        ),
        "mean_rtt_ms": statistics.mean(rtts) if rtts else None,
        "median_rtt_ms": statistics.median(rtts) if rtts else None,
        "mean_sync_error_ms": (
            statistics.mean(errors) if errors else None
        ),
        "max_sync_error_ms": max(errors) if errors else None,
    }

    return summary


def run_trial(args, trial_number):
    """Run one complete congestion trial."""

    mode = "qos" if args.qos else "noqos"
    rate = f"{args.traffic:g}".replace(".", "p")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    prefix = f"{mode}-{rate}mbps-trial{trial_number}-{timestamp}"
    trial_dir = RESULTS_DIR / prefix
    trial_dir.mkdir(parents=True, exist_ok=False)

    sync_csv = trial_dir / "sync.csv"
    server_log = trial_dir / "time-server.log"
    client_log = trial_dir / "sync-client.log"
    iperf_server_log = trial_dir / "iperf-server.log"
    iperf_client_log = trial_dir / "iperf-client.log"
    tc_before = trial_dir / "tc-before.txt"
    tc_after = trial_dir / "tc-after.txt"
    summary_path = trial_dir / "summary.json"

    net = build_network(
        bw=args.bw,
        delay=args.delay,
        loss=args.loss
    )

    time_server = None
    iperf_server = None
    background = None
    client = None

    summary = {
        "trial": trial_number,
        "mode": mode,
        "bottleneck_mbps": args.bw,
        "delay": args.delay,
        "loss_pct": args.loss,
        "offered_traffic_mbps": args.traffic,
        "traffic_duration_s": args.duration,
        "requested_sync_count": args.count,
        "trial_valid": False,
    }

    try:
        start_network(
            net,
            bw=args.bw,
            delay=args.delay,
            loss=args.loss,
            qos=args.qos
        )

        h1 = net.get("h1")
        h2 = net.get("h2")
        h3 = net.get("h3")

        print(f"\n=== TRIAL {trial_number} ({mode}) ===")
        print(f"Background offered load: {args.traffic} Mbps")
        print(f"Background duration: {args.duration} s")

        # Start the time synchronization server.
        with server_log.open("w") as log:
            time_server = h1.popen(
                [
                    str(SERVER_BINARY),
                    "--host", "10.0.0.1",
                    "--port", "5005"
                ],
                stdout=log,
                stderr=subprocess.STDOUT
            )

        # Start the background traffic receiver.
        iperf_server = start_udp_server(
            h1,
            port=6000,
            log_path=str(iperf_server_log)
        )

        time.sleep(1)

        if not is_running(time_server):
            raise RuntimeError("Time server exited unexpectedly")

        if not is_running(iperf_server):
            raise RuntimeError("iperf receiver exited unexpectedly")

        # Capture the initial queue state.
        capture_tc_stats(tc_before)

        # Launch fixed-duration competing traffic.
        background = start_background_traffic(
            h3,
            destination="10.0.0.1",
            bandwidth_mbps=args.traffic,
            duration=args.duration,
            port=6000,
            log_path=str(iperf_client_log)
        )

        traffic_start = time.monotonic()

        wait_for_traffic_start(background, seconds=2)

        print("Background traffic started.")

        # Launch synchronization while congestion is active.
        command = [
            sys.executable,
            str(CLIENT_SCRIPT),
            "--host", "10.0.0.1",
            "--port", "5005",
            "--count", str(args.count),
            "--interval", "1",
            "--timeout", "2",
            "--retries", "0",
            "--offset", "-5",
            "--drift", "100",
            "--no-correct",
            "--output", str(sync_csv)
        ]

        with client_log.open("w") as log:
            client = h2.popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT
            )
            client_exit_code = client.wait()

        sync_end = time.monotonic()

        traffic_active_at_sync_end = is_running(background)

        summary["client_exit_code"] = client_exit_code
        summary["traffic_active_at_sync_end"] = (
            traffic_active_at_sync_end
        )
        summary["sync_end_elapsed_s"] = (
            sync_end - traffic_start
        )

        if not sync_csv.exists():
            raise RuntimeError("Synchronization CSV was not generated")

        summary.update(summarize_results(sync_csv))

        # Capture queue state immediately after sync measurements.
        capture_tc_stats(tc_after)

        # Allow iperf to finish normally.
        print("Waiting for background traffic to finish...")

        remaining = max(
            0,
            args.duration - (time.monotonic() - traffic_start)
        )

        try:
            background_exit_code = background.wait(
                timeout=remaining + 15
            )
        except subprocess.TimeoutExpired:
            background_exit_code = None
            stop_traffic(background)

        summary["iperf_exit_code"] = background_exit_code

        # Allow receiver time to process the final datagrams.
        time.sleep(1)

        summary["trial_valid"] = (
            traffic_active_at_sync_end
            and background_exit_code == 0
            and summary["requests"] == args.count
        )

        print("\n=== TRIAL RESULTS ===")
        print(f"Success: {summary['successful']}/{summary['requests']}")
        print(f"Success rate: {summary['success_rate_pct']:.1f}%")
        print(f"Mean RTT: {summary['mean_rtt_ms']}")
        print(f"Mean sync error: {summary['mean_sync_error_ms']}")
        print(f"Traffic active through sync: {traffic_active_at_sync_end}")
        print(f"Trial valid: {summary['trial_valid']}")
        print(f"Saved to: {trial_dir}")

    finally:
        stop_process(client)
        stop_traffic(background)
        stop_traffic(iperf_server)
        stop_process(time_server)
        net.stop()

        with summary_path.open("w") as file:
            json.dump(summary, file, indent=2)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Repeatable synchronization congestion experiments"
    )

    parser.add_argument("--bw", type=float, default=2.0)
    parser.add_argument("--delay", default="10ms")
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--traffic", type=float, default=3.0)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--duration", type=int, default=90)
    parser.add_argument("--trials", type=int, default=1)

    parser.add_argument(
        "--qos",
        action="store_true",
        help="Enable QoS"
    )

    args = parser.parse_args()

    if args.count <= 0 or args.duration <= 0 or args.trials <= 0:
        parser.error("count, duration and trials must be positive")

    if args.traffic <= 0:
        parser.error("traffic must be positive")

    return args


if __name__ == "__main__":
    setLogLevel("warning")

    args = parse_args()

    if not SERVER_BINARY.is_file():
        raise SystemExit(
            f"Compile the C server first: {SERVER_BINARY}"
        )

    if not CLIENT_SCRIPT.is_file():
        raise SystemExit(
            f"Missing synchronization client: {CLIENT_SCRIPT}"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    for trial in range(1, args.trials + 1):
        run_trial(args, trial)
