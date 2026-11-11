#!/usr/bin/env python3
"""Compare concurrent h2/h4 time synchronization with and without QoS."""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from mininet.log import setLogLevel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'topology'))
from network_topology import build_network, start_network
from traffic_generator import (start_udp_server, start_background_traffic,
                               stop_traffic, is_running, wait_for_traffic_start)
from congestion import stop_process, capture_tc_stats, summarize_results

SERVER = ROOT / 'server' / 'time_server'
CLIENT = ROOT / 'client' / 'time_client.py'
OUTPUT = ROOT / 'results' / 'prioritization'


def run_trial(args, number):
    mode = 'qos' if args.qos else 'noqos'
    rate = f'{args.traffic:g}'.replace('.', 'p')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    folder = OUTPUT / f'{mode}-{rate}mbps-trial{number}-{stamp}'
    folder.mkdir(parents=True, exist_ok=False)

    summary = dict(trial=number, mode=mode, bottleneck_mbps=args.bw,
                   delay=args.delay, loss_pct=args.loss,
                   offered_traffic_mbps=args.traffic,
                   traffic_duration_s=args.duration,
                   requested_sync_count_per_client=args.count,
                   trial_valid=False)
    net = build_network(bw=args.bw, delay=args.delay, loss=args.loss)
    server = receiver = traffic = None
    clients = {}
    try:
        start_network(net, bw=args.bw, delay=args.delay,
                      loss=args.loss, qos=args.qos)
        h1, h2, h3, h4 = (net.get(x) for x in ('h1', 'h2', 'h3', 'h4'))
        with (folder / 'time-server.log').open('w') as log:
            server = h1.popen([str(SERVER), '--host', '10.0.0.1',
                               '--port', '5005'], stdout=log,
                              stderr=subprocess.STDOUT)
        receiver = start_udp_server(h1, port=6000,
                                    log_path=str(folder / 'iperf-server.log'))
        time.sleep(1)
        if not is_running(server) or not is_running(receiver):
            raise RuntimeError('Time server or iperf receiver exited early')

        capture_tc_stats(folder / 'tc-before.txt')
        traffic = start_background_traffic(
            h3, destination='10.0.0.1', bandwidth_mbps=args.traffic,
            duration=args.duration, port=6000,
            log_path=str(folder / 'iperf-client.log'))
        traffic_start = time.monotonic()
        wait_for_traffic_start(traffic, seconds=2)
        if not is_running(traffic):
            raise RuntimeError('Background traffic ended before sync started')

        for name, host in (('h2', h2), ('h4', h4)):
            cmd = [sys.executable, str(CLIENT), '--host', '10.0.0.1',
                   '--port', '5005', '--count', str(args.count),
                   '--interval', '1', '--timeout', '2', '--retries', '0',
                   '--offset', '-5', '--drift', '100', '--no-correct',
                   '--output', str(folder / f'{name}-sync.csv')]
            with (folder / f'{name}-client.log').open('w') as log:
                clients[name] = host.popen(cmd, stdout=log,
                                           stderr=subprocess.STDOUT)

        exit_codes = {name: process.wait() for name, process in clients.items()}
        sync_end = time.monotonic()
        active = is_running(traffic)
        summary['client_exit_codes'] = exit_codes
        summary['traffic_active_at_sync_end'] = active
        summary['sync_end_elapsed_s'] = sync_end - traffic_start
        summary['clients'] = {}
        for name in ('h2', 'h4'):
            csv_path = folder / f'{name}-sync.csv'
            if not csv_path.is_file():
                raise RuntimeError(f'Missing {csv_path}')
            summary['clients'][name] = summarize_results(csv_path)

        summary['total_requests'] = sum(x['requests'] for x in summary['clients'].values())
        summary['total_successful'] = sum(x['successful'] for x in summary['clients'].values())
        summary['overall_success_rate_pct'] = (
            100 * summary['total_successful'] / summary['total_requests'])
        capture_tc_stats(folder / 'tc-after.txt')

        remaining = max(0, args.duration - (time.monotonic() - traffic_start))
        try:
            iperf_code = traffic.wait(timeout=remaining + 15)
        except subprocess.TimeoutExpired:
            iperf_code = None
            stop_traffic(traffic)
        summary['iperf_exit_code'] = iperf_code
        time.sleep(1)
        summary['trial_valid'] = (active and iperf_code == 0 and
            all(x['requests'] == args.count for x in summary['clients'].values()))
        print(f'\n{mode.upper()} trial {number}: {folder}')
        for name, metrics in summary['clients'].items():
            print(f"  {name}: {metrics['successful']}/{metrics['requests']} success, "
                  f"mean RTT={metrics['mean_rtt_ms']} ms, "
                  f"mean error={metrics['mean_sync_error_ms']} ms")
        print(f"  Valid: {summary['trial_valid']}; background active: {active}")
        return summary
    except Exception as exc:
        summary['error'] = str(exc)
        raise
    finally:
        for proc in clients.values():
            stop_process(proc)
        stop_traffic(traffic)
        stop_traffic(receiver)
        stop_process(server)
        net.stop()
        with (folder / 'summary.json').open('w') as out:
            json.dump(summary, out, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bw', type=float, default=2.0)
    parser.add_argument('--delay', default='10ms')
    parser.add_argument('--loss', type=float, default=0.0)
    parser.add_argument('--traffic', type=float, default=3.0)
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--duration', type=int, default=90)
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--qos', action='store_true')
    args = parser.parse_args()
    if min(args.bw, args.traffic, args.count, args.duration, args.trials) <= 0:
        parser.error('bw, traffic, count, duration and trials must be positive')
    if not 0 <= args.loss <= 100:
        parser.error('loss must be between 0 and 100')
    # With two clients, each request can last up to 2 seconds on timeout.
    if args.duration <= 2 + args.count * 2 + 5:
        parser.error('duration too short to ensure traffic throughout synchronization; increase it')
    if not SERVER.is_file() or not CLIENT.is_file():
        parser.error('Compile server/time_server and verify client/time_client.py')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    setLogLevel('warning')
    for trial in range(1, args.trials + 1):
        run_trial(args, trial)


if __name__ == '__main__':
    main()
