# Metrics and Data Format Spec

Network Time Synchronization System. Owner: Member 3 (analysis). Everyone's output code must follow this file.

## 1. What is measured

Each sync exchange between a client and the server records four timestamps:

| Name | Meaning | Clock used |
|------|---------|-----------|
| T1 | Client sends request | Client |
| T2 | Server receives request | Server |
| T3 | Server sends reply | Server |
| T4 | Client receives reply | Client |

Formulas (computed by the client):

- Round-trip delay = (T4 - T1) - (T3 - T2)
- Estimated offset = ((T2 - T1) + (T3 - T4)) / 2

The offset formula assumes the path delay is the same in both directions. If it is not (for example under congestion), the estimate is wrong. Measuring this is the purpose of the project.

## 2. The true offset problem

In testing, all hosts share one real clock, so the real offset is 0 and the error would not be meaningful.

Fix: the client adds a known fake offset to its own clock when it takes T1 and T4 (for example +2500 ms). This value is logged as `true_offset_ms`.

error_ms = est_offset_ms - true_offset_ms

Sign convention: the offset is "server clock minus client clock". If the client clock is 2500 ms ahead, the true offset is -2500 ms. The client person must pick one convention and use it consistently in `est_offset_ms` and `true_offset_ms`.

## 3. CSV format

One file per experiment run. One row per sync exchange (one request/reply attempt). Header row required.

File name: `<scenario>_<run_id>.csv`, for example `congested_prio_run1.csv`. Place in the `data/` folder.

| Column | Type | Unit | Description |
|--------|------|------|-------------|
| scenario | string | - | Scenario name from section 4 |
| run_id | int | - | Repeat number of this scenario (1, 2, 3 ...) |
| param | string | - | Sweep value, such as `delay=50` or `clients=3`. Use `none` if not a sweep |
| client_id | int | - | Client number (1, 2, 3 ...) |
| seq | int | - | Sequence number of the request, starting at 0 per client |
| t1 | int | ns | Client send time |
| t2 | int | ns | Server receive time |
| t3 | int | ns | Server send time |
| t4 | int | ns | Client receive time |
| delay_ms | float | ms | Round-trip delay from the formula |
| est_offset_ms | float | ms | Estimated offset from the formula |
| true_offset_ms | float | ms | Fake offset injected into the client clock |
| error_ms | float | ms | est_offset_ms - true_offset_ms |
| lost | int | - | 1 if no reply arrived (timeout), else 0 |

Rules:
- Timestamps are integers in nanoseconds. Derived values are floats in milliseconds. Nobody mixes units.
- For a lost packet, write the row with lost=1 and leave t2, t3, t4, delay_ms, est_offset_ms, error_ms empty.
- Do not drop lost packets from the file. Packet loss is a metric.
- Timeout for declaring a loss: 1000 ms (adjust if the group agrees).

Example rows:

```
scenario,run_id,param,client_id,seq,t1,t2,t3,t4,delay_ms,est_offset_ms,true_offset_ms,error_ms,lost
baseline,1,none,1,0,1000000000,1010100000,1010200000,1020400000,20.3,-2500.1,-2500.0,-0.1,0
congested_noprio,1,none,1,5,5000000000,,,,,,-2500.0,,1
```

## 4. Scenarios

| Scenario | Description |
|----------|-------------|
| baseline | No background traffic, no priority |
| congested_noprio | Background traffic on the bottleneck link, sync traffic not prioritized |
| congested_prio | Same background traffic, sync traffic prioritized |
| delay_sweep | Link delay set to 10, 50, 100, 200 ms (param = `delay=<ms>`) |
| clients_sweep | 1, 3, 5 clients at the same time (param = `clients=<n>`) |

Run each scenario at least 3 times (run_id 1 to 3). Send at least 200 exchanges per client per run, one every 100 ms or similar.

## 5. Metrics computed by the analysis scripts

Per scenario (and per param value for sweeps):

- Delay: mean, median, p95, p99 (ms)
- Jitter: standard deviation of delay (ms)
- Offset error: mean absolute error, max absolute error, standard deviation (ms)
- Packet loss: lost rows / total rows (%)
- Per client: mean absolute error for each client_id

## 6. Graphs produced

1. Box plot of error_ms per scenario (main comparison)
2. CDF of delay_ms: congested_noprio vs congested_prio
3. Delay over time (seq) per scenario
4. Bar chart of mean absolute error per scenario
5. Mean absolute error vs link delay (delay_sweep)
6. Mean absolute error vs number of clients (clients_sweep)

All graphs are saved as PNG in `results/`.

## 7. Folder layout in the repo

```
server/      C server
client/      Python client
mininet/     network topology and traffic scripts
analysis/    this spec, analysis scripts, requirements.txt
data/        CSV files from experiments
results/     graphs and metrics tables
```

## 8. Open items to agree as a group

- Sign convention for the offset (section 2)
- Fake offset value (suggested: 2500 ms)
- Timeout for packet loss (suggested: 1000 ms)
- Who owns the Mininet part
- Packet binary format between C server and Python client (suggested: network byte order, client_id 4 bytes, seq 4 bytes, then T1, T2, T3 as 8-byte integers)
