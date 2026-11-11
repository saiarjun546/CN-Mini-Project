
#!/usr/bin/env python3
"""Automate congestion experiments across traffic levels and QoS modes."""

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "congestion.py"
RESULTS = ROOT / "results" / "congestion"
SWEEP_RESULTS = ROOT / "results" / "sweeps"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a Mininet congestion/QoS experiment sweep."
    )
    parser.add_argument(
        "--rates", nargs="+", type=float,
        default=[2, 3, 5],
        help="Background traffic rates in Mbps"
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--bw", type=float, default=2.0)
    parser.add_argument("--delay", default="10ms")
    parser.add_argument("--loss", type=float, default=0.0)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--duration", type=int, default=90)
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("Run with sudo (Mininet requires root).")
    if not args.rates or any(r <= 0 for r in args.rates):
        parser.error("All traffic rates must be positive.")
    if args.trials < 1 or args.count < 1 or args.duration < 1:
        parser.error("Trials, count and duration must be positive.")
    if args.bw <= 0 or not 0 <= args.loss <= 100:
        parser.error("Invalid bandwidth or loss percentage.")

    return args


def existing_trial_dirs():
    return {p.resolve() for p in RESULTS.iterdir() if p.is_dir()}


def run_configuration(args, rate, qos):
    mode = "qos" if qos else "noqos"

    print(
        f"\n=== Running {mode}, {rate:g} Mbps, "
        f"{args.trials} trials ===",
        flush=True
    )

    # Clean stale Mininet resources before each configuration.
    subprocess.run(["mn", "-c"], check=True)

    before = existing_trial_dirs()

    command = [
        sys.executable, str(EXPERIMENT),
        "--bw", str(args.bw),
        "--delay", args.delay,
        "--loss", str(args.loss),
        "--traffic", str(rate),
        "--count", str(args.count),
        "--duration", str(args.duration),
        "--trials", str(args.trials),
    ]

    if qos:
        command.append("--qos")

    subprocess.run(command, cwd=ROOT, check=True)

    after = existing_trial_dirs()
    new_dirs = sorted(after - before)

    if len(new_dirs) != args.trials:
        raise RuntimeError(
            f"Expected {args.trials} new trial directories "
            f"for {mode} {rate:g} Mbps, found {len(new_dirs)}."
        )

    summaries = []

    for directory in new_dirs:
        path = directory / "summary.json"
        with path.open(encoding="utf-8") as f:
            summary = json.load(f)

        if not summary.get("trial_valid", False):
            raise RuntimeError(f"Invalid trial: {directory}")

        if summary.get("mode") != mode:
            raise RuntimeError(f"Mode mismatch: {directory}")

        if abs(
            float(summary.get("offered_traffic_mbps", -1)) - rate
        ) > 1e-6:
            raise RuntimeError(f"Traffic rate mismatch: {directory}")

        summary["result_directory"] = str(directory.relative_to(ROOT))
        summaries.append(summary)

    return summaries


def save_summary(rows, output):
    fields = [
        "result_directory",
        "trial",
        "mode",
        "bottleneck_mbps",
        "delay",
        "loss_pct",
        "offered_traffic_mbps",
        "traffic_duration_s",
        "requested_sync_count",
        "requests",
        "successful",
        "failed",
        "success_rate_pct",
        "mean_rtt_ms",
        "median_rtt_ms",
        "mean_sync_error_ms",
        "max_sync_error_ms",
        "sync_end_elapsed_s",
        "traffic_active_at_sync_end",
        "client_exit_code",
        "iperf_exit_code",
        "trial_valid",
    ]

    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=fields, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    SWEEP_RESULTS.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = SWEEP_RESULTS / f"sweep-{stamp}"
    output_dir.mkdir()

    summary_path = output_dir / "sweep-summary.csv"
    all_rows = []

    try:
        for rate in args.rates:
            for qos in (False, True):
                rows = run_configuration(args, rate, qos)
                all_rows.extend(rows)

                # Save incrementally, preserving completed configurations.
                save_summary(all_rows, summary_path)

        print("\n=== SWEEP COMPLETE ===")
        print(f"Trials completed: {len(all_rows)}")
        print(f"Summary: {summary_path}")

    finally:
        # Save any successfully collected results even if later work fails.
        save_summary(all_rows, summary_path)
        subprocess.run(["mn", "-c"], check=False)


if __name__ == "__main__":
    main()
