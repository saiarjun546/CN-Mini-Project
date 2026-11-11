
#!/usr/bin/env python3
"""Automated time synchronization baseline without background traffic."""

import argparse
import csv
import statistics
import subprocess
import sys
import time
from pathlib import Path

from mininet.log import setLogLevel


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_DIR = ROOT / "topology"

# Allow imports from topology/network_topology.py
sys.path.insert(0, str(TOPOLOGY_DIR))

from network_topology import build_network, start_network


SERVER_BINARY = ROOT / "server" / "time_server"
CLIENT_SCRIPT = ROOT / "client" / "time_client.py"
RESULTS_DIR = ROOT / "results"


def stop_process(process):
    """Stop a process gracefully, then force termination if necessary."""

    if process is None or process.poll() is not None:
        return

    process.terminate()

    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def summarize_results(csv_path):
    """Read the synchronization CSV and print summary statistics."""

    with csv_path.open(newline="") as file:
        rows = list(csv.DictReader(file))

    successful = [
        row for row in rows
        if row["status"] == "SUCCESS"
    ]

    total = len(rows)
    success_count = len(successful)

    print("\n=== BASELINE RESULTS ===")
    print(f"Requests: {total}")
    print(f"Successful: {success_count}")
    print(f"Failed: {total - success_count}")

    if total:
        print(f"Success rate: {success_count / total * 100:.1f}%")

    if successful:
        rtts = [
            float(row["rtt_ms"])
            for row in successful
        ]

        errors = [
            float(row["sync_error_ms"])
            for row in successful
        ]

        print(f"Mean RTT: {statistics.mean(rtts):.3f} ms")
        print(f"Median RTT: {statistics.median(rtts):.3f} ms")
        print(f"Mean sync error: {statistics.mean(errors):.3f} ms")
        print(f"Max sync error: {max(errors):.3f} ms")


def run_baseline(args):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    mode = "qos" if args.qos else "noqos"

    output_file = RESULTS_DIR / f"baseline-{mode}.csv"
    server_log = RESULTS_DIR / f"baseline-{mode}-server.log"
    client_log = RESULTS_DIR / f"baseline-{mode}-client.log"

    if not SERVER_BINARY.is_file():
        raise FileNotFoundError(
            f"Server binary missing: {SERVER_BINARY}. "
            "Compile server/time_server.c first."
        )

    if not CLIENT_SCRIPT.is_file():
        raise FileNotFoundError(
            f"Client script missing: {CLIENT_SCRIPT}"
        )

    # Reuse the shared four-host topology.
    net = build_network(
        bw=args.bw,
        delay=args.delay,
        loss=args.loss
    )

    server_process = None

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

        print(f"\nStarting baseline experiment ({mode})...")
        print(f"Bandwidth: {args.bw} Mbps")
        print(f"Delay: {args.delay}")
        print(f"Loss: {args.loss}%")

        # Start the actual C synchronization server.
        with server_log.open("w") as log:
            server_process = h1.popen(
                [
                    str(SERVER_BINARY),
                    "--host", "10.0.0.1",
                    "--port", "5005"
                ],
                stdout=log,
                stderr=subprocess.STDOUT
            )

        time.sleep(1)

        if server_process.poll() is not None:
            raise RuntimeError(
                f"Time server exited early. See {server_log}"
            )

        # Run the actual Python synchronization client.
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
            "--output", str(output_file)
        ]

        print("Running synchronization client on h2...")

        with client_log.open("w") as log:
            client_process = h2.popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT
            )
            exit_code = client_process.wait()

        if not output_file.exists():
            raise RuntimeError(
                f"No CSV generated. See {client_log}"
            )

        summarize_results(output_file)

        print(f"Client exit code: {exit_code}")
        print(f"Results saved to: {output_file}")

    finally:
        stop_process(server_process)
        net.stop()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Automated time synchronization baseline"
    )

    parser.add_argument("--bw", type=float, default=2.0)
    parser.add_argument("--delay", default="10ms")
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--count", type=int, default=20)

    parser.add_argument(
        "--qos",
        action="store_true",
        help="Enable synchronization traffic prioritization"
    )

    return parser.parse_args()


if __name__ == "__main__":
    setLogLevel("warning")
    run_baseline(parse_args())
