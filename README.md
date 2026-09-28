# webcam-iot-ingest

FastAPI service that ingests webcam images, stores them in S3-compatible object storage, and publishes notifications to an MQTT broker.

## Project slides

A presentation describing the repository and the current Windy discovery
statistics is available on [GitHub Pages](https://nanopiero.github.io/webcam-iot-ingest/).
The slide set also includes dedicated pages for
[Fintraffic discovery](https://nanopiero.github.io/webcam-iot-ingest/fintraffic.html),
[Skaping discovery](https://nanopiero.github.io/webcam-iot-ingest/skaping.html),
[Windy ingestion](https://nanopiero.github.io/webcam-iot-ingest/ingestion.html),
[Fintraffic ingestion](https://nanopiero.github.io/webcam-iot-ingest/fintraffic-ingestion.html),
and [Skaping ingestion](https://nanopiero.github.io/webcam-iot-ingest/skaping-ingestion.html).

Missing site altitudes may be enriched with the
[Open-Meteo Elevation API](https://open-meteo.com/en/docs/elevation-api),
using elevation data from the Copernicus DEM GLO-90 dataset. Open-Meteo and
Copernicus attribution applies to those derived values.

## What it does

1. Accepts a `POST /upload` payload with a base64-encoded image
2. Validates and converts the image to JPEG, resizes to max 640×480
3. Uploads the file to an S3 bucket
4. Publishes an MQTT message with the object URL and metadata

## Quick start

Install the repository-pinned `just` version without root privileges:

```bash
./install-just.sh
```

Adjust env variables and run
```bash
cp .env.example .env
just up
```

API docs available at http://localhost:8009/docs

## Local pilot infrastructure

PostgreSQL and Mosquitto run locally through Docker Compose. Their host ports
bind to loopback by default. Create local configuration and a database password
before starting them:

```bash
cp .env.example .env
install -m 700 -d .secrets
touch .secrets/database_password
chmod 600 .secrets/database_password
# Open .secrets/database_password in an editor and enter the local password.
just infrastructure
uv run python -m database.healthcheck
```

`just infrastructure` runs
`docker compose --env-file .env up -d postgres mqtt`. It starts, or keeps
running, only the local PostgreSQL database and Mosquitto MQTT broker. It does
not start Prometheus, Grafana, discovery, or ingestion workers, and normal
invocations preserve the existing PostgreSQL volume and its data.

The `.env` and `.secrets/` paths are ignored by Git. Do not put provider,
database, MQTT, or S3 credentials in committed configuration. The Mosquitto
listener currently permits anonymous clients. Keep
`MQTT_BIND_ADDRESS=127.0.0.1` unless downstream services need direct access;
in that case, use only the VM's private tenancy address and restrict port 1883
to the trusted network. Do not bind it publicly without first adding broker
authentication and transport security.

The PostgreSQL container initializes the pilot `network`, `site`, and
`source_stream` tables from `database/schema/001_pilot_schema.sql` on a new
data volume. Normal container restarts preserve the volume and its schema.
Avoid `just destroy` unless deleting all local service data is intentional.

## Spool cleanup and database backup

The production scheduler runs transformation-scoped spool cleanup every two
hours, at each odd UTC hour, with a two-hour deletion threshold. Because an
object becomes eligible only after it is strictly older than two hours and is
then removed by the next run, nominal spool availability is approximately two
to four hours. This schedule is independent from daily discovery and database
backup.

Inspect canonical derived-image objects older than an exact number of hours:

```bash
just cleanup-spool 2 --dry-run
```

Remove the qualifying image objects:

```bash
just cleanup-spool 2
```

Only keys matching the configured prefix and canonical
`T0/{network}/{YYYY}/{MM}/{DD}/{HH}/...jpg` layout are eligible by default.
Malformed objects found inside the explicitly selected transformation prefix
fall back to their S3 `LastModified` timestamp, preventing abandoned or
non-canonical image objects from accumulating. This aggressive fallback is
never applied bucket-wide and does not include `backups/postgresql/`.
Select another version with `--transformation-prefix T1V0`, or explicitly use
`--all-transformation-prefixes` to clean every recognized version. Unknown
keys and database backups are never deleted. Use `--limit N` for a bounded
validation deletion, and combine it with `--show-keys` during dry-run.

Create a full PostgreSQL custom-format dump and upload it to the configured S3
bucket:

```bash
just backup-database --dry-run
just backup-database
```

Backups use timestamped keys below
`backups/postgresql/YYYY/MM/DD/`. The command verifies the stored length and
SHA-256 metadata after upload. Only after that verification, it removes the
latest preceding available daily dump when it belongs to the same month. The
last dump of each preceding month and every malformed key are retained.

List or safely validate stored dumps without changing PostgreSQL:

```bash
just list-database-backups
just validate-database-backup backups/postgresql/YYYY/MM/DD/<dump>.dump
just validate-database-restore backups/postgresql/YYYY/MM/DD/<dump>.dump
```

The last command restores into a disposable validation database and removes
that database afterward. A live restore is deliberately explicit:

```bash
just restore-database backups/postgresql/YYYY/MM/DD/<dump>.dump
```

It requires the exact selected object key, stops all three ingestion workers,
verifies and restores the dump, validates the restored registry, applies any
pending schema migrations, and restarts the workers only after success. It
preserves ingestion state, including `estimated_source_stream_period`. A
normal PostgreSQL restart always reuses the persistent volume; it never
automatically restores an S3 dump.

## Containerized pilot deployment

Build and start PostgreSQL, MQTT, monitoring, the three continuous provider
workers, and the maintenance scheduler:

```bash
just production-start
```

Run short-lived jobs through the same application image:

```bash
just discover-windy --dry-run
just discover-fintraffic --dry-run
just discover-skaping --dry-run

just ingest-windy --countries DK --limit 3 --dry-run
just ingest-fintraffic --limit 3 --dry-run
just ingest-skaping --limit 3 --dry-run

just cleanup-spool 2 --dry-run
just backup-database --dry-run
```

These ordinary operational recipes use the `webcam-job` Compose service.
Their `container-*` equivalents remain available for explicit administrative
use.

The same runner used by the scheduler can be invoked explicitly when needed:

```bash
just maintenance daily
just maintenance cleanup
```

For a bounded Windy discovery dry-run, an operator may override the configured
member-country scope without changing `.env`:

```bash
WINDY_MEMBER_COUNTRIES=DK just discover-windy --dry-run
```

Stop the stack without deleting persistent volumes:

```bash
just production-stop
```

The permanent `maintenance-scheduler` Compose service owns production
maintenance scheduling. It starts `daily` at 00:00 UTC and `cleanup` at each
odd UTC hour. `daily` attempts Windy, Fintraffic, and Skaping discovery in
that order, followed by verified PostgreSQL backup and backup retention. Every
step has a timeout; a failure is recorded but does not suppress later steps.
Task-specific locks prevent two executions of the same task from overlapping,
while `daily` and `cleanup` remain independent. Persisted scheduler state
collapses missed intervals into one catch-up execution after downtime.

The optional production `webcam-stack.service` in
`deployment/systemd/pilot/` may start and stop the complete Compose stack at
VM boot. It is the only supported systemd unit in this repository: systemd
does not schedule maintenance or launch host Python workers. Older
installations must disable and remove superseded discovery, maintenance,
cleanup, backup, and checkpoint timers to avoid duplicate executions.

Before production promotion, start the same runtime with a recorded three-day
observation window:

```bash
just three-day-production-test
```

The command refuses tracked changes or untracked deployment inputs, starts the
stack, and records the exact Git revision and 72-hour review deadline in
`var/validation/webcam-three-day-production-test.json`. The ignored `var/`
directory survives a normal VM restart. Re-running the command while that
record is active is rejected; an expired record is archived before a new
window starts. It does not schedule an automatic shutdown: after reviewing
three daily runs and approximately 36 cleanup slots, run `just production-stop`
or leave the validated production stack active.
The worker health and Prometheus endpoints use internal ports 8002 (Windy),
8003 (Fintraffic), and 8004 (Skaping). CPU, memory, and graceful-stop limits
are configurable through the deployment environment; reproducible defaults
are listed in `.env.example`. Readiness windows are deliberately longer than
a normal epoch so a healthy long epoch cannot trigger a false restart.
Prometheus scrapes the three production endpoints through one
`ingestion-workers` job on the Compose network. Its targets carry explicit
`source_network` labels and require no additional host port publication.

## Webcam discovery

Run a complete provider discovery pass with:

```bash
just discover-fintraffic
just discover-skaping
just discover-windy
```

Both recipes forward workflow options. Use `--dry-run` to retrieve, validate,
and compare a complete snapshot without changing the registry:

```bash
just discover-fintraffic --dry-run
just discover-skaping --dry-run
just discover-windy --dry-run
```

Fintraffic uses the non-secret `FINTRAFFIC_USER_HEADER` application identifier
as the official `Digitraffic-User` request header. Set it to a meaningful
application name; the repository name is the default. Windy discovery requires
the API-key file and geographic configuration documented in `.env.example`.
Skaping discovery reads its API key from the ignored
`.secrets/skaping_api_key` file and selects only `image` points of view with
the `mini` rendition; it never prints the key or complete provider payload.
Fintraffic discovery retrieves the complete station list and then paced details
for every eligible station so descriptive station and preset metadata are
retained in the registry. Internal site and source-stream identifiers are
stable, deterministic, alphanumeric, and are not truncated; complete provider
identifiers also remain in their dedicated registry fields. Fintraffic
uses `fin{station_id}` for sites and `fin{preset_id}` for streams after checking
that accepted preset identifiers embed their station identifier and remain
globally unique.

Run a bounded Fintraffic ingestion check without S3 or MQTT writes:

```bash
just ingest-fintraffic --limit 10 --dry-run
```

Run the equivalent ETag-based Skaping check, or explicitly exercise the full
T0/S3/MQTT path:

```bash
just ingest-skaping --limit 10 --dry-run
just ingest-skaping --limit 4 --publish
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `mqtt` | Broker hostname |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_BIND_ADDRESS` | `127.0.0.1` | Host interface publishing the broker; use only a trusted private address for remote consumers |
| `MQTT_USERNAME` | — | Broker username |
| `MQTT_PASSWORD` | — | Broker password |
| `MQTT_TLS` | `False` | Enable TLS |
| `MQTT_TOPIC_PREPEND` | - | Topic prefix |
| `DATABASE_HOST` | `localhost` | PostgreSQL hostname |
| `DATABASE_PORT` | `5432` | PostgreSQL port |
| `DATABASE_NAME` | `webcam_ingestion` | PostgreSQL database |
| `DATABASE_USER` | `webcam_ingestion` | PostgreSQL user |
| `DATABASE_PASSWORD_FILE` | `.secrets/database_password` | Path to the database password file |
| `FINTRAFFIC_USER_HEADER` | `webcam-iot-ingest` | Non-secret application identifier sent as `Digitraffic-User` |
| `WINDY_PERIOD_DIRECT_REPLACEMENT_MODULUS` | `250` | Deterministic one-in-N direct period-estimate replacement for Windy |
| `FINTRAFFIC_PERIOD_DIRECT_REPLACEMENT_MODULUS` | `250` | Deterministic one-in-N direct period-estimate replacement for Fintraffic |
| `SKAPING_PERIOD_DIRECT_REPLACEMENT_MODULUS` | `250` | Deterministic one-in-N direct period-estimate replacement for Skaping |
| `FINTRAFFIC_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Fintraffic bulk freshness request |
| `FINTRAFFIC_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Fintraffic JPEG request |
| `SKAPING_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Skaping HEAD/ETag request |
| `SKAPING_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Skaping mini-image request |
| `WINDY_FRESHNESS_QUERY_RETRY_COUNT` | `0` | Retries after the initial Windy metadata/freshness HTTP request |
| `WINDY_DOWNLOAD_RETRY_COUNT` | `0` | Retries after the initial Windy image-download HTTP request |
| `BUCKET_NAME` | - | Bucket name |
| `BUCKET_ACCESS_KEY_ID` | — | S3 access key |
| `BUCKET_SECRET_ACCESS_KEY` | — | S3 secret key |
| `BUCKET_ENDPOINT_URL` | — | S3 endpoint URL |
| `BUCKET_OBJECT_URL` | — | S3 object URL |
| `MAINTENANCE_METRICS_ENABLED` | `true` | Publish cleanup, backup, and restore metrics through Pushgateway |
| `MAINTENANCE_METRICS_GATEWAY_URL` | `http://localhost:9091` | Maintenance-job Pushgateway |
| `MAINTENANCE_METRICS_PUSH_TIMEOUT_S` | `5` | Maintenance metric push timeout |
| `WEBCAM_SPOOL_RETENTION_HOURS` | `2` | Strict production image-spool deletion threshold; cleanup remains configurable for manual runs |
| `WEBCAM_MAINTENANCE_SCHEDULER_METRICS_INTERVAL_S` | `60` | Scheduler heartbeat and state publication interval |
| `DATABASE_BACKUP_S3_PREFIX` | `backups/postgresql` | S3 namespace for timestamped database dumps |
| `PG_DUMP_MODE` | `docker-compose` in `.env.example` | Host-side legacy mode; the operational container recipe forces direct execution with its bundled PostgreSQL 16.9 client |

## Monitoring

Start the full monitoring stack (Prometheus + Alertmanager + Grafana):

```bash
just local
```

- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093
- Grafana: http://localhost:3000

Alertmanager groups Prometheus alerts and sends both firing and resolved email
notifications. The pilot routing configuration uses
`tpdeeplearning@gmail.com` as sender and
`pierre.lepetit@gmail.com` as recipient. Before starting monitoring, replace
the placeholder content of the ignored
`.secrets/alertmanager_smtp_password` file with a Google app password (without
spaces or a trailing newline) and retain mode `0600`. The normal `just local`
and `just monitoring` commands then include Alertmanager automatically. The
SMTP password is mounted as a Docker secret and is never stored in the
committed configuration. Compose preserves the mode-`0600` host ownership for
this file-backed secret. The Alertmanager container consequently uses UID 0
only to read it, while retaining only the `DAC_OVERRIDE` capability, using a
read-only root filesystem and `no-new-privileges`; the secret is not made
world-readable.

Run a detached, monitored Windy benchmark with
[just](https://github.com/casey/just):

```bash
just ingestion-test all_windy 20m
just ingestion-test all_windy 100m staggered-batched
```

`all_windy` selects every configured EUMETNET country and `20m` runs for 20
minutes. A country subset can be supplied as `FR,DE`; durations accept `s`,
`m`, or `h`. Once the deadline is reached, no new epoch starts, but an active
epoch finishes normally. Every invocation creates a uniquely hashed `screen`
session and a timestamped `/tmp` log; the recipe prints both names when it
starts.

Add the optional `staggered` mode to spread the first freshness checks over a
configurable window instead of creating a synchronized cold-start peak:

```bash
just ingestion-test FR,DE 20m staggered
```

Use `batched` to request freshness metadata for up to 50 explicitly selected
Windy webcam IDs per listing request. Use `staggered-batched` to combine that
path with the deterministic initial spread. The batch response supplies both
`lastUpdatedOn` and the current image URL; an ID omitted by Windy becomes a
controlled provider error for that epoch. The individual freshness path
remains the default.

Fintraffic has a separate worker, metrics endpoint, and Grafana dashboard, so
it can run concurrently with Windy. For example:

```bash
just ingestion-test-fintraffic 2h staggered
```

The Fintraffic staggered mode uses a fixed seed and a 600-second initial
window. Each epoch uses one bulk preset `measuredTime` snapshot as the provider
timestamp. A preset whose timestamp is unchanged stops before JPEG download.
For a changed timestamp, the full JPEG GET must return a coherent
`Last-Modified` and an ETag; an unchanged ETag stops transformation and
publication while retaining the download-decision state for the epoch batch.

Skaping has its own worker on metrics port 8015 and can run beside both other
providers:

```bash
just ingestion-test-skaping 2h staggered
```

It obtains the final mini object's ETag with a followed HEAD request, downloads
only changed objects, and validates that the GET response has the same ETag.
ETag alone determines freshness. A valid HTTP `Last-Modified` value is
optionally retained as the provider-availability timestamp for scheduling,
period learning, latency, and notification metadata; missing or invalid
`Last-Modified` does not reject an ETag-valid image. It is not claimed to be
the physical capture time. The sanitized resolved target path is included in
MQTT source-image provider metadata without treating the timestamp-like path
component as authoritative.

The three symmetric worker entry points are `ingestion.windy.worker`,
`ingestion.fintraffic.worker`, and `ingestion.skaping.worker`. Their common
per-stream processing, scheduling support, connection pooling, rate gating,
startup staggering, error accounting, and worker metrics live under
`ingestion.shared`; provider-neutral modules do not depend on a provider
package.

All networks use the provider timestamp for period learning, adaptive polling,
latency measurement, and notification metadata when it is available.
Freshness comparison uses the timestamp, opaque marker, or both, according to
what the provider exposes at assessment time. Only a freshness snapshot that
causes a download decision is retained. Source jobs return these state
transitions to the epoch coordinator, which writes them in one batch.
The processed timestamp advances only after MQTT publication succeeds, so a
failed pipeline remains eligible for retry. The deterministic stagger window
is 600 seconds.

All provider ingestion dashboards include source and derived image size, width, height,
color depth, format, and color-mode observability. Size, width, height, and
color-depth panels show rolling P50, P90, and P95 values over five minutes.
They also show one-minute-smoothed external image-download and successful S3
PUT payload throughput. S3 uploads are direct PUT operations without a
preliminary HEAD request.

This benchmark-only mode uses the fixed seed `windy-benchmark-v1` and a
deterministic hash of each source-stream ID to assign a phase in `[0, 600)`
seconds by default. `INITIAL_STAGGER_WINDOW_S` configures this window, and the
benchmark recipe sets it explicitly to 600 seconds. The initial phase is
independent of the source-period estimator and does not fabricate database
timestamps. Normal adaptive
scheduling takes over after a stream's first check. As with a direct run's
first epoch, period-estimate candidates from
each stream's staggered first check are discarded to avoid cold-start bias. The
worker prints the seed and phase window in its log.

The benchmark keeps two independent timing controls explicit:

- `WINDY_MINIMUM_INGESTION_INTERVAL_S=300` is the Windy per-webcam
  successful-publication guard;
- after a download, normal selection also waits until the stored provider
  timestamp plus `WINDY_POLLING_INTERVAL_FACTOR * estimated_source_stream_period`; the
  Windy factor is currently `0.7`;
- the shared bounded-minimum estimator initializes its database estimate as
  `NULL` and learns it from two distinct provider timestamps; all three
  providers use a 300-second lower bound;
- `INGESTION_MIN_EPOCH_PERIOD_S=15` prevents excessively rapid epochs;
- `INGESTION_IDLE_DELAY_S=0` adds no post-epoch pause; the 15-second minimum
  epoch period still prevents a tight loop for short or empty epochs.

To test the provider-timestamp bounded-minimum period estimator on the full
Windy registry for two hours, resetting only Windy's existing period estimates
to `NULL` before the run, use:

```bash
just windy-period-test 2h
```

The recipe retains the five-minute successful-publication guard, uses batched
freshness, deterministic initial staggering, a 15-second minimum epoch period,
and a polling factor of 0.7. Provider, download, and processed timestamps are
preserved by the estimate reset.

For a directly comparable two-hour control run without adaptive polling, use:

```bash
just windy-no-adaptive-test 2h
```

This keeps full-registry batched freshness queries, deterministic initial
staggering, the five-minute per-stream successful-publication guard, and the
15-second minimum epoch period. It sets the polling factor to zero, so stored
period estimates do not defer freshness queries. It does not reset them and
continues updating them with the same bounded-minimum estimator, isolating the
effect of job selection. Compare API request rate, epoch duration, and
provider-to-download latency with `windy-period-test` in the same Grafana
dashboard.

The follow-up adaptive experiment applies
`max(9 minutes, 0.7 * estimated period)` as its provider-time polling guard,
without resetting the learned estimates:

```bash
just windy-nine-minute-floor-test 2h
```

To run concurrent 40-minute Fintraffic and Skaping validation workers through
the complete publication pipeline, with half a Docker CPU per worker, use:

```bash
just ingestion-test-fintraffic-skaping 40m
```

The reduced-bandwidth comparison uses four Fintraffic threads with an
eight-minute polling floor and two Skaping threads with a four-minute polling
floor. Each container is limited to 0.5 CPU. Both
use deterministic ten-minute staggering, separate metrics ports 8014/8015,
normal S3 and MQTT publication, and remain available after completion for
`docker logs` inspection. The benchmark recipes bind worker metric ports to
Docker's host-gateway address (resolved from the Prometheus container), so
Prometheus can scrape them without exposing those ports on every host
interface.

For a quiet three-network run on an eight-core VM, use:

```bash
just ingestion-test-three-networks 90m
```

The optional second argument selects one or two maintenance cycles. For a
two-hour run with exactly one complete maintenance cycle at T+20 minutes, use:

```bash
just ingestion-test-three-networks 2h 1
```

To run the same full two-hour test with one maintenance cycle at T+20 minutes
and, on complete maintenance success, one Alertmanager email summarising the
number of streams seen by Windy, Fintraffic, Skaping, and all three providers
combined, run:

```bash
just two-hour-full-alert-test
```

This assigns Windy 6 CPUs and 84 threads, Fintraffic 0.5 CPU and 4 threads,
and Skaping 0.5 CPU and 2 threads. Its benchmark-only combined maintenance
helper first cleans T0 images older than 24 hours, then runs sequential Windy,
Fintraffic, and Skaping discovery, and finally creates and verifies the
PostgreSQL backup. It intentionally does not reproduce the independent
production schedules. The cycles start at T+20 and, when requested, T+60. At
peak, the configured quotas total 7.5 CPUs, leaving approximately 0.5 CPU on an
eight-core VM for PostgreSQL, MQTT, and monitoring. All three workers use
their normal Compose service aliases and internal metrics ports 8002, 8003,
and 8004, so Prometheus scrapes them through the production
`ingestion-workers` job. They retain their network-specific polling floors and
the complete S3/MQTT publication path. If the first maintenance cycle overruns
T+60, the second starts as soon as the first completes rather than overlapping
it.

For the historical one-day quiet benchmark with exactly one combined
maintenance run at the next 00:00 UTC, use:

```bash
just one-day-quiet-test
```

This historical detached benchmark lasts 24 hours from worker startup. It
explicitly restores the deterministic period-replacement modulus to 250 for
Windy, Fintraffic, and Skaping, uses the validated 6/0.5/0.5 CPU and 84/4/2
thread allocation, and invokes its benchmark-only combined maintenance
sequence once. It does not validate the current permanent scheduler. The
printed screen-session name and `/tmp` log path can be used to follow it.

For the corresponding historical 36-hour benchmark, run all three workers
with exactly one combined maintenance sequence at the next 00:00 UTC:

```bash
just final-36-hour-test
```

This historical benchmark uses the same 6/0.5/0.5 CPU and 84/4/2 thread
allocation, deterministic startup staggering, production period-replacement
modulus, complete S3/MQTT path, and benchmark-only combined maintenance
sequence as the one-day test. It prints the detached Screen session name and
log path.

### Reproducible monitoring versions

| Component | Version | Pin location |
|---|---:|---|
| Prometheus | 3.13.1 | `docker-compose.yml` image tag |
| Alertmanager | 0.34.0 | `docker-compose.yml` image tag |
| Prometheus Pushgateway | 1.11.3 | `docker-compose.yml` image tag |
| Grafana OSS | 11.2.0 | `docker-compose.yml` image tag |
| postgres_exporter | 0.19.1 | `docker-compose.yml` image tag |
| node_exporter | 1.11.1 | `docker-compose.yml` image tag |
| cAdvisor | 0.57.0 | `docker-compose.yml` image tag |
| just | 1.57.0 | `justfile` and `install-just.sh` |

The Compose image tags make monitoring recreation deterministic at the release
version level. For byte-identical container images, deployments may additionally
lock the resolved image digests in their deployment manifest.

Pre-built dashboards for FastAPI, MQTT, all three ingestion workers, and all
three discovery providers are provisioned automatically. Short-lived discovery
jobs publish their provider-specific discovery metrics to the locally bound
Pushgateway on port 9091. Cleanup, database backup, backup retention, and
explicit restore publish separate maintenance-job metrics. Prometheus scrapes
the Pushgateway and Grafana displays retained results after each process exits.

The monitoring profile also starts Alertmanager plus PostgreSQL, host, and
container exporters.
Grafana provisions an **Infrastructure health** dashboard and a
**Maintenance jobs** dashboard. Alert rules cover unavailable infrastructure
targets, unavailable checkpoint workers, low host disk space, PostgreSQL
failure, scheduler heartbeat or missed-slot failures, and cleanup or backup
failures. Alertmanager groups these alerts and delivers email notifications.
Cleanup email is failure-only: success clears its Prometheus alert state
without sending either a success or resolved email. Other alerts retain their
configured resolved notifications.

During pilot validation only, each successful daily discovery/database-backup
task also raises a short-lived informational email alert. This temporary
success notification is intentionally separate from the permanent failure
alerts and can be removed after scheduled maintenance has been established
operationally.

The checkpoint-13 unquiet recovery exercise starts the full three-network
stack, crashes only the Windy worker process after 15 minutes, restarts
PostgreSQL after 30 minutes, and starts its historical combined
cleanup/discovery/backup workflow after 45 minutes. It does not reproduce the
independent production schedules. It stops ingestion after the requested
duration but lets active maintenance finish:

```bash
just checkpoint13-unquiet-test 90m
```

The command prints its detached `screen` session and log path. Detailed
acceptance checks are in
[`manual_tests/validation_to_complete_checkpoint13`](manual_tests/validation_to_complete_checkpoint13).

All three providers normally update their period estimate with the same
300-second-bounded running-minimum rule. On a deterministic rare event derived
from SHA-256 of the source-stream
ID and newly observed provider timestamp, the latest valid provider-timestamp
gap directly replaces the estimate. This permits recovery from an erroneous
running minimum without counters or additional database state. Initial
learning is separate: direct replacement requires an existing estimate, and
the established first-epoch and excessive-epoch guards remain effective.

The bounded validation resets only the three networks' period estimates,
uses `N=10`, runs for 30 minutes, prints a final database/Prometheus snapshot,
and stops the workers:

```bash
just period-replacement-test 30m 10
```

## Running tests

```bash
pip install -e .
pytest tests/
```

## Example image ingestion
Example Python script to ingest a randomly colored 640x480 png image provided in `test_sender.py`
