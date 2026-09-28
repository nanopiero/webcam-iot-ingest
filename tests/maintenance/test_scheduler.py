from datetime import datetime, timezone

import pytest

from maintenance.scheduler import (
    FileStateStore,
    MaintenanceScheduler,
    schedule_slot,
    scheduled_tasks,
)


class Process:
    next_pid = 100

    def __init__(self, command):
        self.command = command
        self.returncode = None
        self.pid = Process.next_pid
        Process.next_pid += 1
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 28, hour, minute, second, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour", range(1, 24, 2))
def test_cleanup_is_due_on_every_odd_utc_hour(hour: int) -> None:
    assert scheduled_tasks(utc(hour)) == ("cleanup",)


def test_daily_is_due_only_at_midnight_utc() -> None:
    assert scheduled_tasks(utc(0)) == ("daily",)
    assert scheduled_tasks(utc(2)) == ()
    assert scheduled_tasks(utc(1, 1)) == ()


def test_naive_scheduler_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduled_tasks(datetime(2026, 9, 28))


def test_slot_is_stable_for_the_whole_scheduled_minute() -> None:
    assert schedule_slot(utc(1, 0, 1)) == schedule_slot(utc(1, 0, 59))


def test_daily_does_not_delay_cleanup_and_slots_launch_only_once() -> None:
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    scheduler = MaintenanceScheduler(process_factory=factory)
    scheduler.tick(utc(0, 0, 1))
    scheduler.tick(utc(0, 0, 59))
    scheduler.tick(utc(1, 0, 1))

    assert [process.command[-1] for process in processes] == ["daily", "cleanup"]
    assert set(scheduler.running) == {"daily", "cleanup"}


def test_same_task_overlap_is_prevented() -> None:
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    scheduler = MaintenanceScheduler(process_factory=factory)
    scheduler.tick(utc(1))
    scheduler.tick(utc(3))

    assert len(processes) == 1
    assert processes[0].command[-1] == "cleanup"


def test_completed_process_is_reaped_before_a_later_slot() -> None:
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    scheduler = MaintenanceScheduler(process_factory=factory)
    scheduler.tick(utc(1))
    processes[0].returncode = 0
    scheduler.tick(utc(3))

    assert len(processes) == 2
    assert scheduler.running["cleanup"].process is processes[1]


def test_shutdown_terminates_running_tasks() -> None:
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    scheduler = MaintenanceScheduler(process_factory=factory)
    scheduler.tick(utc(1))
    scheduler.shutdown()

    assert processes[0].terminated is True
    assert scheduler.running == {}


def test_fresh_state_outside_a_scheduled_minute_does_not_infer_downtime() -> None:
    processes: list[Process] = []
    scheduler = MaintenanceScheduler(
        process_factory=lambda command: processes.append(Process(command))
    )

    scheduler.tick(utc(10, 30))

    assert processes == []


def test_multiple_missed_cleanup_slots_collapse_to_one_catch_up(tmp_path) -> None:
    state = FileStateStore(tmp_path / "scheduler-state.json")
    first_processes: list[Process] = []

    def first_factory(command):
        process = Process(command)
        first_processes.append(process)
        return process

    first = MaintenanceScheduler(
        process_factory=first_factory,
        state_store=state,
    )
    first.tick(utc(1, 0, 1))
    first_processes[0].returncode = 0
    first.tick(utc(1, 1))

    catch_up_processes: list[Process] = []

    def catch_up_factory(command):
        process = Process(command)
        catch_up_processes.append(process)
        return process

    restarted = MaintenanceScheduler(
        process_factory=catch_up_factory,
        state_store=state,
    )
    restarted.tick(utc(7, 30))
    restarted.tick(utc(7, 31))

    assert [process.command[-1] for process in catch_up_processes] == ["cleanup"]
    stored = state.load()
    assert stored is not None
    assert stored["tasks"]["cleanup"]["last_started_slot"] == (
        "2026-09-28T07:00:00+00:00"
    )


def test_multiple_missed_daily_slots_collapse_to_one_catch_up(tmp_path) -> None:
    state = FileStateStore(tmp_path / "scheduler-state.json")
    initial = MaintenanceScheduler(
        process_factory=lambda command: Process(command),
        state_store=state,
    )
    initial.tick(utc(10, 30))
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    restarted = MaintenanceScheduler(process_factory=factory, state_store=state)
    restarted.tick(
        datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)
    )
    restarted.tick(
        datetime(2026, 10, 1, 12, 31, tzinfo=timezone.utc)
    )

    assert [process.command[-1] for process in processes].count("daily") == 1


def test_interrupted_task_is_retried_once_after_restart(tmp_path) -> None:
    state = FileStateStore(tmp_path / "scheduler-state.json")
    interrupted: list[Process] = []
    first = MaintenanceScheduler(
        process_factory=lambda command: interrupted.append(Process(command))
        or interrupted[-1],
        state_store=state,
    )
    first.tick(utc(1, 0, 1))

    retried: list[Process] = []
    restarted = MaintenanceScheduler(
        process_factory=lambda command: retried.append(Process(command))
        or retried[-1],
        state_store=state,
    )
    restarted.tick(utc(1, 15))
    restarted.tick(utc(1, 16))

    assert len(interrupted) == 1
    assert [process.command[-1] for process in retried] == ["cleanup"]


def test_completed_failure_is_not_retried_until_a_later_slot(tmp_path) -> None:
    state = FileStateStore(tmp_path / "scheduler-state.json")
    processes: list[Process] = []

    def factory(command):
        process = Process(command)
        processes.append(process)
        return process

    first = MaintenanceScheduler(process_factory=factory, state_store=state)
    first.tick(utc(1, 0, 1))
    processes[0].returncode = 1
    first.tick(utc(1, 1))

    restarted = MaintenanceScheduler(process_factory=factory, state_store=state)
    restarted.tick(utc(1, 30))
    assert len(processes) == 1

    restarted.tick(utc(3, 0, 1))
    assert len(processes) == 2


def test_file_state_is_atomic_and_contains_no_temporary_files(tmp_path) -> None:
    state_path = tmp_path / "state" / "scheduler-state.json"
    state = FileStateStore(state_path)
    scheduler = MaintenanceScheduler(
        process_factory=lambda command: Process(command),
        state_store=state,
    )

    scheduler.tick(utc(10, 30))

    assert state.load() is not None
    assert list(state_path.parent.iterdir()) == [state_path]
