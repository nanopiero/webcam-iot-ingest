from pathlib import Path
import subprocess

import pytest

from maintenance.runner import (
    MaintenanceAlreadyRunning,
    acquire_task_lock,
    maintenance_steps,
    run_task,
    task_lock_path,
)


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "WEBCAM_MAINTENANCE_STATE_DIRECTORY": str(tmp_path),
        "WEBCAM_WINDY_DISCOVERY_TIMEOUT_S": "11",
        "WEBCAM_FINTRAFFIC_DISCOVERY_TIMEOUT_S": "12",
        "WEBCAM_SKAPING_DISCOVERY_TIMEOUT_S": "13",
        "WEBCAM_DATABASE_BACKUP_TIMEOUT_S": "14",
        "WEBCAM_CLEANUP_TIMEOUT_S": "15",
        "WEBCAM_SPOOL_RETENTION_HOURS": "7",
        "WEBCAM_TRANSFORMATION_PREFIX": "T0",
    }


def test_daily_steps_have_one_authoritative_order(tmp_path: Path) -> None:
    steps = maintenance_steps("daily", _environment(tmp_path))

    assert [step.name for step in steps] == [
        "discovery_windy",
        "discovery_fintraffic",
        "discovery_skaping",
        "database_backup",
    ]
    assert [step.timeout_s for step in steps] == [11, 12, 13, 14]
    modules = [step.command[2] for step in steps]
    assert modules == [
        "discovery.windy.windy_discovery_workflow",
        "discovery.fintraffic.fintraffic_discovery_workflow",
        "discovery.skaping.skaping_discovery_workflow",
        "database.database_backup",
    ]
    assert all("storage.s3_spool_cleanup" not in step.command for step in steps)


def test_cleanup_selects_only_scoped_spool_cleanup(tmp_path: Path) -> None:
    steps = maintenance_steps("cleanup", _environment(tmp_path))

    assert len(steps) == 1
    step = steps[0]
    assert step.name == "spool_cleanup"
    assert step.timeout_s == 15
    assert step.command[2] == "storage.s3_spool_cleanup"
    assert step.command[step.command.index("--older-than-hours") + 1] == "7.0"
    assert step.command[step.command.index("--transformation-prefix") + 1] == "T0"


def test_daily_failure_does_not_prevent_later_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[str, ...]] = []
    published: list[dict[str, object]] = []

    def runner(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1 if "discovery.windy.windy_discovery_workflow" in command else 0,
        )

    def publisher(_gateway, **values):
        published.append(values)

    monkeypatch.setattr(
        "maintenance.runner._publish_legacy_daily_result", lambda *a, **k: None
    )
    successful = run_task(
        "daily",
        environment=_environment(tmp_path),
        runner=runner,
        publisher=publisher,
    )

    assert successful is False
    assert [command[2] for command in commands] == [
        "discovery.windy.windy_discovery_workflow",
        "discovery.fintraffic.fintraffic_discovery_workflow",
        "discovery.skaping.skaping_discovery_workflow",
        "database.database_backup",
    ]
    assert [item["result"] for item in published] == [
        "failure",
        "success",
        "success",
        "success",
        "failure",
    ]


def test_same_task_lock_blocks_but_different_task_lock_does_not(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)

    with acquire_task_lock("daily", environment):
        with pytest.raises(MaintenanceAlreadyRunning):
            with acquire_task_lock("daily", environment):
                pass
        with acquire_task_lock("cleanup", environment) as cleanup_lock:
            assert cleanup_lock == tmp_path / "cleanup.lock"


def test_task_lock_paths_are_stable_and_task_specific(tmp_path: Path) -> None:
    environment = _environment(tmp_path)

    assert task_lock_path("daily", environment) == tmp_path / "daily.lock"
    assert task_lock_path("cleanup", environment) == tmp_path / "cleanup.lock"


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_cleanup_rejects_invalid_retention(tmp_path: Path, value: str) -> None:
    environment = _environment(tmp_path)
    environment["WEBCAM_SPOOL_RETENTION_HOURS"] = value

    with pytest.raises(ValueError, match="WEBCAM_SPOOL_RETENTION_HOURS"):
        maintenance_steps("cleanup", environment)


def test_unknown_task_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="daily or cleanup"):
        maintenance_steps("unknown", _environment(tmp_path))
