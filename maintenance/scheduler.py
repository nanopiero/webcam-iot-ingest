"""Schedule independent maintenance tasks inside the Compose application stack."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Protocol, Sequence

from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

from config.deployment_config import MaintenanceMetricsConfig


TASK_DAILY = "daily"
TASK_CLEANUP = "cleanup"
TASKS = (TASK_DAILY, TASK_CLEANUP)
STATE_VERSION = 1


def scheduled_tasks(now: datetime) -> tuple[str, ...]:
    """Return tasks due in the current UTC minute."""
    if now.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if now.minute != 0:
        return ()
    tasks: list[str] = []
    if now.hour == 0:
        tasks.append(TASK_DAILY)
    if now.hour % 2 == 1:
        tasks.append(TASK_CLEANUP)
    return tuple(tasks)


def schedule_slot(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    return now.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def latest_schedule_slot(task: str, now: datetime) -> datetime:
    """Return the most recent slot at or before ``now``."""
    if now.tzinfo is None:
        raise ValueError("scheduler time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    candidate = now.replace(minute=0, second=0, microsecond=0)
    if task == TASK_DAILY:
        return candidate.replace(hour=0)
    if task == TASK_CLEANUP:
        if candidate.hour % 2 == 0:
            candidate -= timedelta(hours=1)
        return candidate
    raise ValueError("unsupported maintenance task")


def previous_schedule_slot(task: str, slot: datetime) -> datetime:
    if task == TASK_DAILY:
        return slot - timedelta(days=1)
    if task == TASK_CLEANUP:
        return slot - timedelta(hours=2)
    raise ValueError("unsupported maintenance task")


def next_schedule_slot(task: str, now: datetime) -> datetime:
    latest = latest_schedule_slot(task, now)
    if task == TASK_DAILY:
        return latest + timedelta(days=1)
    if task == TASK_CLEANUP:
        return latest + timedelta(hours=2)
    raise ValueError("unsupported maintenance task")


def _slot_text(slot: datetime) -> str:
    return slot.astimezone(timezone.utc).isoformat()


def _parse_slot(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("scheduler slot must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("scheduler slot is not a valid timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("scheduler slot must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _initial_state(now: datetime) -> dict[str, object]:
    due_now = set(scheduled_tasks(now))
    tasks: dict[str, object] = {}
    for task in TASKS:
        latest = latest_schedule_slot(task, now)
        if task in due_now:
            latest = previous_schedule_slot(task, latest)
        slot = _slot_text(latest)
        tasks[task] = {
            "last_completed_slot": slot,
            "last_result": "initialized",
            "last_started_slot": slot,
        }
    return {"tasks": tasks, "version": STATE_VERSION}


def _validate_state(state: object) -> dict[str, object]:
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise ValueError("unsupported maintenance scheduler state")
    tasks = state.get("tasks")
    if not isinstance(tasks, dict) or set(tasks) != set(TASKS):
        raise ValueError("maintenance scheduler state has invalid tasks")
    for task in TASKS:
        task_state = tasks[task]
        if not isinstance(task_state, dict):
            raise ValueError("maintenance scheduler task state must be an object")
        started = _parse_slot(task_state.get("last_started_slot"))
        completed = _parse_slot(task_state.get("last_completed_slot"))
        if completed > started:
            raise ValueError("completed scheduler slot cannot follow started slot")
        if not isinstance(task_state.get("last_result"), str):
            raise ValueError("maintenance scheduler task result must be a string")
    return state


class StateStore(Protocol):
    def load(self) -> dict[str, object] | None: ...

    def save(self, state: dict[str, object]) -> None: ...


class MemoryStateStore:
    def __init__(self) -> None:
        self.state: dict[str, object] | None = None

    def load(self) -> dict[str, object] | None:
        return self.state

    def save(self, state: dict[str, object]) -> None:
        self.state = json.loads(json.dumps(state))


class FileStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "FileStateStore":
        directory = Path(
            environment.get(
                "WEBCAM_MAINTENANCE_STATE_DIRECTORY",
                "/var/lib/webcam-maintenance",
            )
        )
        return cls(directory / "scheduler-state.json")

    def load(self) -> dict[str, object] | None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _validate_state(json.loads(content))

    def save(self, state: dict[str, object]) -> None:
        _validate_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                encoding="utf-8",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(state, temporary, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


def _emit(payload: Mapping[str, object], *, error: bool = False) -> None:
    print(
        json.dumps(payload, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
        flush=True,
    )


@dataclass
class RunningTask:
    process: object
    slot: str


class SchedulerMetrics:
    def __init__(
        self,
        config: MaintenanceMetricsConfig,
        *,
        interval_s: float = 60,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("scheduler metrics interval must be positive")
        self.config = config
        self.interval_s = interval_s
        self.last_published_monotonic: float | None = None

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "SchedulerMetrics":
        try:
            interval = float(
                environment.get(
                    "WEBCAM_MAINTENANCE_SCHEDULER_METRICS_INTERVAL_S", "60"
                )
            )
        except ValueError as error:
            raise ValueError(
                "WEBCAM_MAINTENANCE_SCHEDULER_METRICS_INTERVAL_S must be positive"
            ) from error
        enabled_value = environment.get(
            "MAINTENANCE_METRICS_ENABLED",
            environment.get("BATCH_METRICS_ENABLED", "true"),
        ).lower()
        if enabled_value not in {"true", "false"}:
            raise ValueError("MAINTENANCE_METRICS_ENABLED must be true or false")
        gateway = environment.get(
            "MAINTENANCE_METRICS_GATEWAY_URL",
            environment.get(
                "BATCH_METRICS_GATEWAY_URL",
                environment.get(
                    "DISCOVERY_METRICS_GATEWAY_URL", "http://localhost:9091"
                ),
            ),
        ).rstrip("/")
        timeout = float(
            environment.get(
                "MAINTENANCE_METRICS_PUSH_TIMEOUT_S",
                environment.get("BATCH_METRICS_PUSH_TIMEOUT_S", "5"),
            )
        )
        if not gateway.startswith(("http://", "https://")):
            raise ValueError("maintenance metrics gateway URL must use HTTP or HTTPS")
        if timeout <= 0:
            raise ValueError("maintenance metrics push timeout must be positive")
        return cls(
            MaintenanceMetricsConfig(
                enabled=enabled_value == "true",
                gateway_url=gateway,
                push_timeout_s=timeout,
            ),
            interval_s=interval,
        )

    def publish(
        self,
        scheduler: "MaintenanceScheduler",
        now: datetime,
        *,
        force: bool = False,
    ) -> bool:
        monotonic_now = time.monotonic()
        if (
            not force
            and self.last_published_monotonic is not None
            and monotonic_now - self.last_published_monotonic < self.interval_s
        ):
            return False
        if not self.config.enabled:
            self.last_published_monotonic = monotonic_now
            return False
        registry = CollectorRegistry()
        heartbeat = Gauge(
            "webcam_maintenance_scheduler_heartbeat_unixtime",
            "Unix timestamp of the latest scheduler heartbeat",
            registry=registry,
        )
        next_slot = Gauge(
            "webcam_maintenance_scheduler_next_slot_unixtime",
            "Unix timestamp of the next scheduled task slot",
            ["task"],
            registry=registry,
        )
        last_started = Gauge(
            "webcam_maintenance_scheduler_last_started_slot_unixtime",
            "Unix timestamp of the latest started scheduled slot",
            ["task"],
            registry=registry,
        )
        last_completed = Gauge(
            "webcam_maintenance_scheduler_last_completed_slot_unixtime",
            "Unix timestamp of the latest completed scheduled slot",
            ["task"],
            registry=registry,
        )
        task_running = Gauge(
            "webcam_maintenance_scheduler_task_running",
            "Whether a scheduled maintenance task is currently running",
            ["task"],
            registry=registry,
        )
        slot_pending = Gauge(
            "webcam_maintenance_scheduler_slot_pending",
            "Whether the latest due slot has not completed yet",
            ["task"],
            registry=registry,
        )
        last_result = Gauge(
            "webcam_maintenance_scheduler_last_result",
            "Latest scheduler result state for each maintenance task",
            ["task", "result"],
            registry=registry,
        )
        heartbeat.set(now.astimezone(timezone.utc).timestamp())
        for task in TASKS:
            task_state = scheduler._task_state(task)
            started = _parse_slot(task_state["last_started_slot"])
            completed = _parse_slot(task_state["last_completed_slot"])
            due = latest_schedule_slot(task, now)
            next_slot.labels(task).set(next_schedule_slot(task, now).timestamp())
            last_started.labels(task).set(started.timestamp())
            last_completed.labels(task).set(completed.timestamp())
            task_running.labels(task).set(1 if task in scheduler.running else 0)
            slot_pending.labels(task).set(1 if completed < due else 0)
            last_result.labels(task, str(task_state["last_result"])).set(1)
        try:
            push_to_gateway(
                self.config.gateway_url,
                job="webcam_maintenance_scheduler",
                registry=registry,
                timeout=self.config.push_timeout_s,
            )
        except Exception as error:
            self.last_published_monotonic = monotonic_now
            _emit(
                {
                    "maintenance_scheduler_metrics": {
                        "error": type(error).__name__,
                        "outcome": "publish_error",
                    }
                },
                error=True,
            )
            return False
        self.last_published_monotonic = monotonic_now
        return True


class MaintenanceScheduler:
    """Launch due tasks without blocking scheduling of the other task."""

    def __init__(
        self,
        *,
        process_factory: Callable[..., object] = subprocess.Popen,
        state_store: StateStore | None = None,
    ) -> None:
        self.process_factory = process_factory
        self.state_store = state_store or MemoryStateStore()
        self.state: dict[str, object] | None = None
        self.running: dict[str, RunningTask] = {}
        self.reported_overlaps: dict[str, str] = {}

    def _ensure_state(self, now: datetime) -> bool:
        if self.state is not None:
            return False
        state = self.state_store.load()
        initialized = state is None
        if state is None:
            state = _initial_state(now)
            self.state_store.save(state)
            _emit({"maintenance_scheduler": {"event": "state_initialized"}})
        self.state = _validate_state(state)
        return initialized

    def _task_state(self, task: str) -> dict[str, object]:
        assert self.state is not None
        tasks = self.state["tasks"]
        assert isinstance(tasks, dict)
        task_state = tasks[task]
        assert isinstance(task_state, dict)
        return task_state

    def _save(self) -> None:
        assert self.state is not None
        self.state_store.save(self.state)

    def _reap(self) -> bool:
        changed = False
        for task, running in tuple(self.running.items()):
            returncode = running.process.poll()
            if returncode is None:
                continue
            if returncode == 0:
                result = "success"
            elif returncode == 75:
                result = "already_running"
            else:
                result = "failure"
            _emit(
                {
                    "maintenance_scheduler": {
                        "event": "task_completed",
                        "result": result,
                        "returncode": returncode,
                        "slot": running.slot,
                        "task": task,
                    }
                }
            )
            task_state = self._task_state(task)
            task_state["last_completed_slot"] = running.slot
            task_state["last_result"] = result
            self._save()
            del self.running[task]
            self.reported_overlaps.pop(task, None)
            changed = True
        return changed

    def tick(self, now: datetime) -> bool:
        changed = self._ensure_state(now)
        changed = self._reap() or changed
        due_now = set(scheduled_tasks(now))
        for task in TASKS:
            due_slot = latest_schedule_slot(task, now)
            task_state = self._task_state(task)
            completed_slot = _parse_slot(task_state["last_completed_slot"])
            if completed_slot >= due_slot:
                continue
            if task in self.running:
                deferred_slot = _slot_text(due_slot)
                if self.reported_overlaps.get(task) != deferred_slot:
                    _emit(
                        {
                            "maintenance_scheduler": {
                                "event": "overlap_prevented",
                                "slot": deferred_slot,
                                "task": task,
                            }
                        }
                    )
                    self.reported_overlaps[task] = deferred_slot
                continue
            slot = _slot_text(due_slot)
            task_state["last_started_slot"] = slot
            task_state["last_result"] = "running"
            self._save()
            command = (sys.executable, "-m", "maintenance.runner", task)
            try:
                process = self.process_factory(command)
            except Exception as error:
                task_state["last_completed_slot"] = slot
                task_state["last_result"] = "launch_failure"
                self._save()
                changed = True
                _emit(
                    {
                        "maintenance_scheduler": {
                            "error": type(error).__name__,
                            "event": "launch_error",
                            "slot": slot,
                            "task": task,
                        }
                    },
                    error=True,
                )
                continue
            self.running[task] = RunningTask(process=process, slot=slot)
            changed = True
            _emit(
                {
                    "maintenance_scheduler": {
                        "catch_up": task not in due_now,
                        "event": "task_started",
                        "pid": getattr(process, "pid", None),
                        "slot": slot,
                        "task": task,
                    }
                }
            )
        return changed

    def shutdown(self, timeout_s: float = 30) -> None:
        for running in self.running.values():
            if running.process.poll() is None:
                running.process.terminate()
        deadline = time.monotonic() + timeout_s
        for running in self.running.values():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                running.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                running.process.kill()
                running.process.wait()
        for task, running in tuple(self.running.items()):
            _emit(
                {
                    "maintenance_scheduler": {
                        "event": "task_interrupted",
                        "slot": running.slot,
                        "task": task,
                    }
                }
            )
            del self.running[task]


def _positive_poll_interval(environment: Mapping[str, str]) -> float:
    try:
        value = float(environment.get("WEBCAM_MAINTENANCE_SCHEDULER_POLL_S", "1"))
    except ValueError as error:
        raise ValueError(
            "WEBCAM_MAINTENANCE_SCHEDULER_POLL_S must be positive"
        ) from error
    if value <= 0:
        raise ValueError("WEBCAM_MAINTENANCE_SCHEDULER_POLL_S must be positive")
    return value


def run_scheduler(
    *,
    environment: Mapping[str, str] = os.environ,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    poll_interval = _positive_poll_interval(environment)
    scheduler = MaintenanceScheduler(
        state_store=FileStateStore.from_environment(environment)
    )
    metrics = SchedulerMetrics.from_environment(environment)
    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _emit({"maintenance_scheduler": {"event": "started"}})
    try:
        while not stopping:
            current = now()
            changed = scheduler.tick(current)
            metrics.publish(scheduler, current, force=changed)
            sleep(poll_interval)
    finally:
        scheduler.shutdown()
        if scheduler.state is not None:
            metrics.publish(scheduler, now(), force=True)
        _emit({"maintenance_scheduler": {"event": "stopped"}})


def main(argv: Sequence[str] | None = None) -> None:
    del argv
    run_scheduler()


if __name__ == "__main__":
    main()
