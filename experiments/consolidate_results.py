#!/usr/bin/env python3
"""Validate and consolidate Network Time Sync experiment results.

Read-only with respect to existing experiments. Writes only to results/final/.
Run from any directory: python3 experiments/consolidate_results.py
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
FINAL = RESULTS / 'final'

# These are preliminary/smoke-test runs, not part of the repeated-trial design.
EXCLUDED = {
    'congestion': {
        'noqos-3mbps-trial1-20261111-180655-394521': 'preliminary 3 Mbps run',
        'qos-3mbps-trial1-20261111-181147-120061': 'preliminary 3 Mbps run',
    },
    'prioritization': {
        'qos-3mbps-trial1-20261111-191806-853566': 'five-request smoke test',
    },
}
EXPECTED_CONGESTION = {(mode, rate, trial) for mode in ('noqos', 'qos')
                       for rate in (1, 2, 3, 5) for trial in (1, 2, 3)}
EXPECTED_PRIORITIZATION = {(mode, 3, trial) for mode in ('noqos', 'qos')
                           for trial in (1, 2, 3)}
NAME_RE = re.compile(r'^(noqos|qos)-(\d+(?:p\d+)?)mbps-trial(\d+)-\d{8}-\d{6}-\d+$')


def read_json(path):
    with path.open(encoding='utf-8') as handle:
        obj = json.load(handle)
    if not isinstance(obj, dict):
        raise ValueError(f'{path}: JSON root must be an object')
    return obj


def read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f'{path}: missing CSV header')
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f'{path}: duplicate CSV header fields')
        return list(reader), reader.fieldnames


def write_csv(path, rows, preferred=()):
    keys = list(preferred)
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def trial_key(name):
    match = NAME_RE.fullmatch(name)
    if not match:
        raise ValueError(f'Unrecognized trial directory: {name}')
    mode, rate, trial = match.groups()
    return mode, float(rate.replace('p', '.')), int(trial)


def check_measurements(path, expected_count, issues, label):
    if not path.is_file():
        issues.append(f'{label}: missing {path.name}')
        return [], []
    try:
        rows, columns = read_csv(path)
    except (OSError, ValueError) as exc:
        issues.append(f'{label}: {exc}')
        return [], []
    if len(rows) != expected_count:
        issues.append(f'{label}: {path.name} has {len(rows)} rows; expected {expected_count}')
    if 'status' not in columns:
        issues.append(f'{label}: {path.name} has no status column')
    return rows, columns


def find_client_csvs(directory, issues):
    """Use the established filenames; do not silently guess at alternate data."""
    paths = {client: directory / f'{client}-sync.csv' for client in ('h2', 'h4')}
    for client, path in paths.items():
        if not path.is_file():
            issues.append(f'{directory.name}: missing {client}-sync.csv')
    return paths


def collect(kind, expected, issues, excluded_log):
    base = RESULTS / kind
    if not base.is_dir():
        issues.append(f'Missing directory: {base}')
        return [], [], set()
    summaries, measurements, observed = [], [], set()
    for directory in sorted(base.iterdir()):
        if not directory.is_dir():
            continue
        if directory.name in EXCLUDED[kind]:
            excluded_log.append({'experiment': kind, 'directory': str(directory.relative_to(ROOT)),
                                 'reason': EXCLUDED[kind][directory.name]})
            continue
        try:
            mode, rate, trial = trial_key(directory.name)
        except ValueError as exc:
            issues.append(str(exc))
            continue
        key = (mode, rate, trial)
        if key not in expected:
            issues.append(f'Unexpected formal trial: {directory.name}')
            continue
        if key in observed:
            issues.append(f'Duplicate formal trial for {kind}: {key}')
            continue
        observed.add(key)
        summary_path = directory / 'summary.json'
        if not summary_path.is_file():
            issues.append(f'{directory.name}: missing summary.json')
            continue
        try:
            summary = read_json(summary_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            issues.append(f'{directory.name}: invalid summary.json: {exc}')
            continue
        label = f'{kind}/{directory.name}'
        if summary.get('trial_valid') is not True:
            issues.append(f'{label}: trial_valid is not true')
        if summary.get('mode') != mode:
            issues.append(f'{label}: summary mode mismatch')
        if summary.get('trial') != trial:
            issues.append(f'{label}: summary trial mismatch')
        if 'offered_traffic_mbps' in summary:
            try:
                if abs(float(summary['offered_traffic_mbps']) - rate) > 1e-6:
                    issues.append(f'{label}: offered traffic mismatch')
            except (TypeError, ValueError):
                issues.append(f'{label}: invalid offered traffic value')
        if summary.get('traffic_active_at_sync_end') is not True:
            issues.append(f'{label}: background not active at sync end')
        if summary.get('iperf_exit_code') != 0:
            issues.append(f'{label}: iperf did not exit successfully')
        for filename in ('iperf-server.log', 'iperf-client.log', 'tc-before.txt', 'tc-after.txt'):
            if not (directory / filename).is_file():
                issues.append(f'{label}: missing {filename}')
        common = {'experiment': kind, 'result_directory': str(directory.relative_to(ROOT)),
                  'mode': mode, 'offered_traffic_mbps': rate, 'trial': trial}
        if kind == 'congestion':
            expected_count = int(summary.get('requested_sync_count', 20))
            csv_rows, columns = check_measurements(directory / 'sync.csv', expected_count, issues, label)
            if columns and 'status' in columns:
                succeeded = sum(r['status'] == 'SUCCESS' for r in csv_rows)
                if summary.get('successful') != succeeded or summary.get('requests') != len(csv_rows):
                    issues.append(f'{label}: summary counts disagree with sync.csv')
            summaries.append({**summary, **common})
            measurements.extend({**row, **common, 'client': 'h2',
                                 'source_csv': str((directory / 'sync.csv').relative_to(ROOT))}
                                for row in csv_rows)
        else:
            paths = find_client_csvs(directory, issues)
            # Prioritization summary format is retained verbatim; also add
            # standardized, independently validated per-client count fields.
            flattened = {**common}
            for k, v in summary.items():
                if isinstance(v, (dict, list)):
                    flattened[f'{k}_json'] = json.dumps(v, sort_keys=True)
                else:
                    flattened[k] = v
            expected_count = int(summary.get('requested_sync_count', 20))
            for client, path in paths.items():
                rows, columns = check_measurements(path, expected_count, issues, f'{label}/{client}')
                success = sum(r.get('status') == 'SUCCESS' for r in rows)
                flattened[f'{client}_requests_verified'] = len(rows)
                flattened[f'{client}_successful_verified'] = success
                flattened[f'{client}_failed_verified'] = len(rows) - success
                flattened[f'{client}_success_rate_pct_verified'] = round(100 * success / len(rows), 6) if rows else ''
                measurements.extend({**row, **common, 'client': client,
                                     'source_csv': str(path.relative_to(ROOT))} for row in rows)
            summaries.append(flattened)
    missing = expected - observed
    for key in sorted(missing):
        issues.append(f'Missing formal {kind} trial: {key}')
    return summaries, measurements, observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=FINAL,
                        help='Output directory (default: results/final)')
    args = parser.parse_args()
    issues, exclusions = [], []
    congestion, sync1, _ = collect('congestion', EXPECTED_CONGESTION, issues, exclusions)
    prioritization, sync2, _ = collect('prioritization', EXPECTED_PRIORITIZATION, issues, exclusions)
    measurements = sync1 + sync2
    output = args.output.resolve()
    # Prevent writing over any raw experiment directories.
    if output == RESULTS.resolve() or output in ((RESULTS / 'congestion').resolve(), (RESULTS / 'prioritization').resolve(), (RESULTS / 'sweeps').resolve()):
        parser.error('Output must not overwrite an original results directory')
    if issues:
        print('VALIDATION FAILED; no consolidated files written:', file=sys.stderr)
        for issue in issues:
            print(' -', issue, file=sys.stderr)
        return 1
    if len(congestion) != 24 or len(prioritization) != 6 or len(measurements) != 720:
        print(f'Unexpected totals: {len(congestion)} congestion trials, '
              f'{len(prioritization)} prioritization trials, '
              f'{len(measurements)} measurement rows. No files written.', file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / 'congestion-summary.csv', congestion,
              ('experiment', 'result_directory', 'mode', 'offered_traffic_mbps', 'trial'))
    write_csv(output / 'prioritization-summary.csv', prioritization,
              ('experiment', 'result_directory', 'mode', 'offered_traffic_mbps', 'trial'))
    write_csv(output / 'all-sync-measurements.csv', measurements,
              ('experiment', 'result_directory', 'mode', 'offered_traffic_mbps',
               'trial', 'client', 'source_csv', 'status'))
    counts = Counter((r['experiment'], r['mode']) for r in measurements)
    manifest = {
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'purpose': 'Formal analysis selection; original experiment results are unchanged',
        'selection': {'congestion': '3 trials per QoS mode at 1, 2, 3 and 5 Mbps',
                      'prioritization': '3 trials per QoS mode at 3 Mbps, 2 clients per trial'},
        'totals': {'congestion_trials': len(congestion),
                   'prioritization_trials': len(prioritization),
                   'sync_measurement_rows': len(measurements)},
        'measurement_counts_by_experiment_and_mode': {
            f'{kind}:{mode}': count for (kind, mode), count in sorted(counts.items())},
        'excluded_runs': exclusions,
        'included_congestion_directories': [r['result_directory'] for r in congestion],
        'included_prioritization_directories': [r['result_directory'] for r in prioritization],
        'validation_issues': [],
    }
    with (output / 'experiment-manifest.json').open('w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2)
    print('Consolidation complete.')
    print(f'Congestion trials: {len(congestion)}')
    print(f'Prioritization trials: {len(prioritization)}')
    print(f'Synchronization request rows: {len(measurements)}')
    print(f'Excluded preliminary/smoke runs: {len(exclusions)}')
    print(f'Output: {output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
