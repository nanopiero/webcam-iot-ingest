"""Run one bounded maintenance task inside the application container."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Iterator, Mapping, Sequence
from urllib import request


LOCK_ERROR_EXIT = 73
ALREADY_RUNNING_EXIT = 75
SUPPORTED_TASKS = ("daily", "cleanup")


@dataclass(frozen=True)
class MaintenanceStep:
    name: str
    timeout_s: float
    command: tuple[str, ...]


class MaintenanceLockError(RuntimeError):
    """The task lock could not be created or opened."""


class MaintenanceAlreadyRunning(RuntimeError):
    """Another execution of the same task owns its lock."""


def _positive_number(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    try:
        value = float(environment.get(name, default))
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def maintenance_steps(
    task: str, environment: Mapping[str, str] = os.environ
) -> tuple[MaintenanceStep, ...]:
    """Return the single authoritative ordered definition for a task."""
    if task not in SUPPORTED_TASKS:
        raise ValueError("maintenance task must be daily or cleanup")
    python = sys.executable
    if task == "cleanup":
        retention_hours = _positive_number(
            environment, "WEBCAM_SPOOL_RETENTION_HOURS", 24
        )
        return (
            MaintenanceStep(
                "spool_cleanup",
                _positive_number(environment, "WEBCAM_CLEANUP_TIMEOUT_S", 3600),
                (
                    python,
                    "-m",
                    "storage.s3_spool_cleanup",
                    "--older-than-hours",
                    str(retention_hours),
                    "--transformation-prefix",
                    environment.get("WEBCAM_TRANSFORMATION_PREFIX", "T0"),
                ),
            ),
        )
    return (
        MaintenanceStep(
            "discovery_windy",
            _positive_number(environment, "WEBCAM_WINDY_DISCOVERY_TIMEOUT_S", 1800),
            (python, "-m", "discovery.windy.windy_discovery_workflow"),
        ),
        MaintenanceStep(
            "discovery_fintraffic",
            _positive_number(
                environment, "WEBCAM_FINTRAFFIC_DISCOVERY_TIMEOUT_S", 900
            ),
            (python, "-m", "discovery.fintraffic.fintraffic_discovery_workflow"),
        ),
        MaintenanceStep(
            "discovery_skaping",
            _positive_number(
                environment, "WEBCAM_SKAPING_DISCOVERY_TIMEOUT_S", 300
            ),
            (python, "-m", "discovery.skaping.skaping_discovery_workflow"),
        ),
        MaintenanceStep(
            "database_backup",
            _positive_number(environment, "WEBCAM_DATABASE_BACKUP_TIMEOUT_S", 1800),
            (
                python,
                "-m",
                "database.database_backup",
                "--pg-dump-mode",
                "direct",
            ),
        ),
    )


def task_lock_path(task: str, environment: Mapping[str, str] = os.environ) -> Path:
    if task not in SUPPORTED_TASKS:
        raise ValueError("maintenance task must be daily or cleanup")
    directory = Path(
        environment.get(
            "WEBCAM_MAINTENANCE_STATE_DIRECTORY", "/var/lib/webcam-maintenance"
        )
    )
    return directory / f"{task}.lock"


@contextmanager
def acquire_task_lock(
    task: str, environment: Mapping[str, str] = os.environ
) -> Iterator[Path]:
    path = task_lock_path(task, environment)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.open("a+")
    except OSError as error:
        raise MaintenanceLockError(str(path)) from error
    try:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceAlreadyRunning(task) from error
        except OSError as error:
            raise MaintenanceLockError(str(path)) from error
        yield path
    finally:
        lock.close()


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    print(
        json.dumps(payload, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
        flush=True,
    )


def publish_metrics(
    gateway_url: str,
    *,
    task: str,
    step: str | None,
    result: str,
    duration_s: float,
    timestamp: int,
) -> None:
    if step is None:
        path = f"/metrics/job/webcam_maintenance_task/task/{task}"
        prefix = "webcam_maintenance_task"
    else:
        path = f"/metrics/job/webcam_maintenance_task/task/{task}/step/{step}"
        prefix = "webcam_maintenance_step"
    payload = (
        f"# TYPE {prefix}_last_run_unixtime gauge\n"
        f'{prefix}_last_run_unixtime{{result="{result}"}} {timestamp}\n'
        f"# TYPE {prefix}_last_duration_seconds gauge\n"
        f'{prefix}_last_duration_seconds{{result="{result}"}} {duration_s:.6f}\n'
    ).encode()
    try:
        request.urlopen(
            request.Request(
                gateway_url.rstrip("/") + path,
                data=payload,
                method="PUT",
            ),
            timeout=5,
        ).close()
    except Exception as error:
        _emit(
            {
                "maintenance_metrics": {
                    "error": type(error).__name__,
                    "outcome": "publish_error",
                    "task": task,
                }
            },
            error=True,
        )


def _publish_legacy_daily_result(
    gateway_url: str, *, result: str, duration_s: float, timestamp: int
) -> None:
    payload = (
        "# TYPE webcam_maintenance_sequence_last_run_unixtime gauge\n"
        f'webcam_maintenance_sequence_last_run_unixtime{{result="{result}"}} '
        f"{timestamp}\n"
        "# TYPE webcam_maintenance_sequence_last_duration_seconds gauge\n"
        f'webcam_maintenance_sequence_last_duration_seconds{{result="{result}"}} '
        f"{duration_s:.6f}\n"
    ).encode()
    try:
        request.urlopen(
            request.Request(
                gateway_url.rstrip("/")
                + "/metrics/job/webcam_maintenance_sequence",
                data=payload,
                method="PUT",
            ),
            timeout=5,
        ).close()
    except Exception as error:
        _emit(
            {
                "maintenance_metrics": {
                    "error": type(error).__name__,
                    "outcome": "publish_error",
                    "task": "daily",
                }
            },
            error=True,
        )


def run_task(
    task: str,
    *,
    environment: Mapping[str, str] = os.environ,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    publisher: Callable[..., None] = publish_metrics,
) -> bool:
    """Run all steps, continuing after failures and returning aggregate success."""
    steps = maintenance_steps(task, environment)
    gateway = environment.get(
        "MAINTENANCE_METRICS_GATEWAY_URL", "http://pushgateway:9091"
    )
    task_started = time.monotonic()
    successful = True
    _emit({"maintenance_task": {"event": "started", "task": task}})
    for step in steps:
        started = time.monotonic()
        _emit(
            {
                "maintenance_step": {
                    "event": "started",
                    "step": step.name,
                    "task": task,
                }
            }
        )
        try:
            completed = runner(step.command, timeout=step.timeout_s, check=False)
            result = "success" if completed.returncode == 0 else "failure"
        except subprocess.TimeoutExpired:
            result = "failure"
        except Exception as error:
            result = "failure"
            _emit(
                {
                    "maintenance_step": {
                        "error": type(error).__name__,
                        "event": "internal_error",
                        "step": step.name,
                        "task": task,
                    }
                },
                error=True,
            )
        duration = time.monotonic() - started
        successful = successful and result == "success"
        publisher(
            gateway,
            task=task,
            step=step.name,
            result=result,
            duration_s=duration,
            timestamp=int(time.time()),
        )
        _emit(
            {
                "maintenance_step": {
                    "duration_s": round(duration, 3),
                    "event": "completed",
                    "result": result,
                    "step": step.name,
                    "task": task,
                }
            }
        )
    duration = time.monotonic() - task_started
    result = "success" if successful else "failure"
    timestamp = int(time.time())
    publisher(
        gateway,
        task=task,
        step=None,
        result=result,
        duration_s=duration,
        timestamp=timestamp,
    )
    if task == "daily":
        _publish_legacy_daily_result(
            gateway, result=result, duration_s=duration, timestamp=timestamp
        )
    _emit(
        {
            "maintenance_task": {
                "duration_s": round(duration, 3),
                "event": "completed",
                "result": result,
                "task": task,
            }
        }
    )
    return successful


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=SUPPORTED_TASKS)
    args = parser.parse_args(argv)
    try:
        with acquire_task_lock(args.task):
            successful = run_task(args.task)
    except MaintenanceAlreadyRunning:
        _emit(
            {
                "maintenance_task": {
                    "outcome": "already_running",
                    "task": args.task,
                }
            }
        )
        raise SystemExit(ALREADY_RUNNING_EXIT)
    except MaintenanceLockError:
        _emit(
            {
                "maintenance_task": {
                    "outcome": "lock_error",
                    "task": args.task,
                }
            },
            error=True,
        )
        raise SystemExit(LOCK_ERROR_EXIT)
    raise SystemExit(0 if successful else 1)


if __name__ == "__main__":
    main()
