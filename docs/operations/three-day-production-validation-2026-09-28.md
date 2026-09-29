# Three-day production validation — interim report

Report captured: 2026-09-29 19:33 UTC  
Observation started: 2026-09-28 01:37:58 UTC  
Review deadline: 2026-10-01 01:37:58 UTC  
Runtime after the deadline: continuous until `just production-stop`

## Status

The production pilot is operating normally at this interim checkpoint.

- Windy, Fintraffic, and Skaping workers are healthy and actively completing
  epochs.
- The three worker containers and the maintenance-scheduler container have
  recorded zero Docker restarts since the observation began.
- PostgreSQL and MQTT are healthy.
- The maintenance scheduler heartbeat is current, with no task pending or
  stuck.
- One daily discovery/database-backup slot and 21 independent cleanup slots
  have completed.
- Prometheus has no firing alerts.

The observation record names commit `98907088f0db3c12650177294ccd78bfb56fb47a`.
During the window, MQTT host publication was changed and deployed from commit
`ccad0b2` so that trusted tenancy clients can reach `192.168.1.97:1883`.
Consequently, this validates service continuity across that controlled change,
but it is not a three-day test of one immutable revision.

## Ingestion

Counters below cover the current worker processes, started with the observation
window. Latencies use the 30 minutes preceding this report.

| Network | Successful epochs | Failed epochs | Published images | S3 successes | MQTT successes | Epoch p50 / p90 | Provider-to-job-end p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Windy | 2,743 | 0 | 4,850,263 | 4,850,263 | 4,850,263 | 28.8 s / 155.7 s | 224.6 s / 467.4 s |
| Fintraffic | 8,210 | 1 | 388,412 | 388,412 | 388,412 | 2.0 s / 31.6 s | 84.2 s / 151.4 s |
| Skaping | 10,061 | 1 | 7,201 | 7,201 | 7,201 | 3.1 s / 4.7 s | 126.0 s / 278.6 s |

No final S3-upload or MQTT-publication failure series is present. The equality
between published jobs, successful S3 operations, and successful MQTT
operations is exact for each network.

The two failed epochs occurred for Fintraffic and Skaping around the deliberate
MQTT container recreation; both workers recovered without a container restart.

Job outcomes also show provider-side imperfections that do not currently stop
the pipeline:

- Windy: 338,206 provider errors and 620 download errors among approximately
  29.38 million completed jobs.
- Fintraffic: 13,855 provider errors, 156,108 download errors, and 657 invalid
  images among approximately 6.31 million completed jobs. Download errors are
  about 2.5% of completed Fintraffic jobs and remain the main item to watch.
- Skaping: 43 provider errors and one download error among 204,233 completed
  jobs.

## Daily discovery and backup

The first daily slot, scheduled for 2026-09-29 00:00 UTC, completed
successfully at 00:11:43 UTC.

| Network | Completion | Duration | Streams seen | Streams added | Identifier violations |
|---|---|---:|---:|---:|---:|
| Windy | 00:08:58 UTC | 537.4 s | 25,540 | 35 | 0 |
| Fintraffic | 00:11:40 UTC | 162.3 s | 2,260 | 0 | 0 |
| Skaping | 00:11:41 UTC | 0.4 s | 42 | 0 | 0 |

The PostgreSQL backup then succeeded in 1.1 seconds. Its stored dump size is
4,067,901 bytes, and the associated backup-retention step also succeeded.
The next daily slot is 2026-09-30 00:00 UTC.

## Spool cleanup

Prometheus records 21 changes of the scheduler's completed cleanup slot during
the first 42 hours. The latest slot was scheduled for 2026-09-29 19:00 UTC and
completed successfully at 19:04:38 UTC.

Latest cleanup snapshot:

- duration: 278.0 seconds;
- examined: 524,732 objects;
- eligible and deleted: 275,440 objects;
- failed deletions: 0;
- malformed or unknown candidates: 0.

The next cleanup slot is 2026-09-29 21:00 UTC.

## Resource snapshot

This is an instantaneous Docker snapshot, not a time average.

| Service | CPU | Memory / limit |
|---|---:|---:|
| Windy worker | 3.24% | 513.9 MiB / 2 GiB |
| Fintraffic worker | 45.81% | 626.9 MiB / 1 GiB |
| Skaping worker | 2.02% | 118.5 MiB / 512 MiB |
| Maintenance scheduler | 0.00% | 63.3 MiB / 1 GiB |
| PostgreSQL | 0.32% | 160.7 MiB / 15.36 GiB |
| MQTT | 0.21% | 2.0 MiB / 15.36 GiB |

Memory headroom is satisfactory. The Fintraffic CPU value is a point-in-time
measurement during active work and should not be interpreted as a sustained
42-hour average.

## Interim decision

No missing runtime service or operational blocker is visible. The stack can
remain running beyond the review deadline; the deadline is an operator review
marker, not an automatic stop. At the final review, confirm the remaining two
daily slots, continued two-hour cleanup progression, absence of firing alerts,
and stable worker latency/resource usage.

## Evidence sources

This report uses the persistent validation JSON record, Docker container state
and restart counters, one Docker resource snapshot, and Prometheus metrics from
the ingestion workers, Pushgateway, and maintenance scheduler.
