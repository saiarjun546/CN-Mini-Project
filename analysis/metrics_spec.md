# Metrics and Data Format Spec

Network Time Synchronization System. Owner: Member 3 (analysis).
This file describes the measurements the experiments produce, how they are analysed, and what the analysis outputs.

## 1. What is measured

Each sync request between a client and the server records four timestamps:

| Name | Meaning | Clock used |
|------|---------|-----------|
| T1 | Client sends request | Client |
| T2 | Server receives request | Server |
| T3 | Server sends reply | Server |
| T4 | Client receives reply | Client |

Formulas (computed by `client/time_client.py`):

- Round-trip delay (RTT) = (T4 - T1) - (T3 - T2)
- Estimated offset = ((T2 - T1) + (T3 - T4)) / 2

Sign convention: offset = server clock - client clock. A positive offset means the client clock is behind the server.

The offset formula assumes the path delay is the same in both directions. Congestion makes one direction slower than the other, which makes the estimate wrong. Under idealized assumptions (stable clocks, accurate timestamps) the error is at most about RTT / 2, so large delays allow large errors. This is a reference, not a guarantee.

## 2. How sync error is measured

All hosts in Mininet share one real clock, so the true offset would be zero. The client therefore runs on a virtual clock (`client/virtual_clock.py`) that is deliberately set about 5000 ms behind the server. This known value is logged as `actual_offset_ms`.

sync_error = estimated offset - actual offset

The analysis uses the absolute value of the error. A perfect sync has an error of 0.

## 3. Experiments

| Experiment | Clients | Background traffic | Modes | Trials |
|------------|---------|--------------------|-------|--------|
| congestion | h2 (one client) | 1, 2, 3 and 5 Mbps | noqos and qos | 3 per mode and level |
| prioritization | h2 and h4 (at the same time) | 3 Mbps | noqos and qos | 3 per mode |

Common settings: 2 Mbps bottleneck link, 10 ms link delay, 0% configured packet loss, 90 s of background traffic, 20 sync requests per client per trial, server at 10.0.0.1:5005.

Modes:
- `noqos`: sync packets share the bottleneck queue with the background traffic.
- `qos`: sync packets are prioritized over the background traffic.

A request that gets no valid reply within the 2 s timeout is recorded with status `FAILED`.

## 4. Data format

Input to the analysis: `results/final/all-sync-measurements.csv` (created by `experiments/consolidate_results.py`). One row per sync request.

| Column | Unit | Description |
|--------|------|-------------|
| experiment | - | `congestion` or `prioritization` |
| mode | - | `noqos` or `qos` |
| offered_traffic_mbps | Mbps | Background traffic rate |
| trial | - | Trial number (1 to 3) |
| client | - | Client host (h2 or h4) |
| status | - | `SUCCESS` or `FAILED` |
| sequence, request_id | - | Request number within the trial |
| t1, t2, t3, t4 | seconds | Timestamps (see section 1) |
| rtt_s, rtt_ms | s, ms | Round-trip delay |
| estimated_offset_s, estimated_offset_ms | s, ms | Estimated offset |
| actual_offset_s, actual_offset_ms | s, ms | Known virtual-clock offset |
| sync_error_s, sync_error_ms | s, ms | Difference between estimated and actual offset |
| retries | - | Retries used |
| error | - | Reason text for a failed request |

Failed rows have no timestamps after the request and no RTT or error values.

Per-trial summaries are in `results/final/congestion-summary.csv` and `results/final/prioritization-summary.csv`. The list of included and excluded runs is in `results/final/experiment-manifest.json`.

## 5. Metrics computed

For each mode and traffic level (and for each client in the prioritization experiment):

- Requests, successful, failed, success rate (%)
- RTT: mean, median, 95th percentile (ms)
- RTT standard deviation (`rtt_std_ms`): standard deviation of RTT across successful requests (ms). This is not the packet delay variation (jitter) that iperf reports
- Absolute sync error: mean, median, maximum (ms)

Failed requests are counted in the success rate only. All RTT and error statistics, in the tables and in every graph, use successful requests only, so a level with very few successes has averages built on very little data. The median is more reliable than the mean here, because a few late replies can pull the mean up.

## 6. Outputs

Run from the repository root:

```
pip install -r analysis/requirements.txt
python analysis/analyze_results.py
```

The script first checks that the input CSV has the required columns and stops with a clear message if the format has changed. It writes to `results/analysis/`:

- `metrics_congestion.csv`, `metrics_prioritization.csv`
- `1_success_rate_vs_traffic.png`: percentage of requests answered vs traffic
- `2_rtt_vs_traffic.png`: mean RTT vs traffic (log scale)
- `3_error_vs_traffic.png`: mean sync error vs traffic (log scale)
- `4_two_clients_priority.png`: h2 and h4 together, with and without QoS
- `5_error_vs_rtt.png`: sync error vs RTT, with an idealized RTT / 2 reference line
- `6_error_boxplot.png`: error distribution per traffic level

## 7. Repository layout

```
server/        C UDP time server
client/        Python client and virtual clock
topology/      Mininet topology, QoS and traffic scripts
experiments/   Experiment runners and result consolidation
analysis/      This spec, analyze_results.py, requirements.txt
results/       Raw results, results/final (consolidated) and results/analysis (graphs, tables)
tests/         Timestamp math tests
```

## 8. Known limits

- Link delay was fixed at 10 ms, so there is no delay sweep in the results.
- Only the 1 to 5 Mbps traffic levels on a 2 Mbps bottleneck were tested, with one client (congestion) or two clients (prioritization).
- At 3 Mbps without QoS, no request was answered in the congestion experiment, and at 5 Mbps without QoS only 3 of 60 were. Error figures for those points are based on very few or no samples.
