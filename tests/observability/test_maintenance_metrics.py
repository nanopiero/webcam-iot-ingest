from unittest.mock import Mock

from prometheus_client import generate_latest

from config.deployment_config import MaintenanceMetricsConfig
from observability.maintenance_metrics import MaintenanceJobMetrics


def config(*, enabled: bool = False) -> MaintenanceMetricsConfig:
    return MaintenanceMetricsConfig(enabled, "http://pushgateway:9091", 5)


def test_maintenance_metrics_use_unambiguous_names() -> None:
    metrics = MaintenanceJobMetrics("spool_cleanup", config())

    pushed = metrics.publish(
        success=True,
        duration_s=2.5,
        items={"deleted": 3},
        bytes_by_outcome={"deleted": 1200},
        stages={"listing": 0.5},
        retention_hours=24,
    )

    assert pushed is False
    events = generate_latest(metrics.events)
    state = generate_latest(metrics.state)
    outcome = generate_latest(metrics.outcome_state)
    assert b"webcam_maintenance_run_total" in events
    assert b"webcam_maintenance_items_total" in events
    assert b"webcam_maintenance_bytes_total" in events
    assert b"webcam_maintenance_stage_duration_seconds" in events
    assert b"webcam_maintenance_last_success_unixtime" in state
    assert b'webcam_maintenance_last_run_unixtime{result="success"}' in outcome
    assert b"webcam_batch_" not in events + state + outcome


def test_pushgateway_jobs_and_grouping_use_maintenance_names(monkeypatch) -> None:
    push_events = Mock()
    push_state = Mock()
    monkeypatch.setattr(
        "observability.maintenance_metrics.pushadd_to_gateway", push_events
    )
    monkeypatch.setattr(
        "observability.maintenance_metrics.push_to_gateway", push_state
    )
    metrics = MaintenanceJobMetrics("database_backup", config(enabled=True))

    assert metrics.publish(
        success=True,
        duration_s=4,
        backup_size_bytes=2048,
    )

    assert push_events.call_args.kwargs["job"] == "webcam_maintenance_events"
    assert push_events.call_args.kwargs["grouping_key"] == {
        "maintenance_job": "database_backup"
    }
    state_calls = push_state.call_args_list
    assert [call.kwargs["job"] for call in state_calls] == [
        "webcam_maintenance_state",
        "webcam_maintenance_outcome",
    ]
    assert all(call.kwargs["grouping_key"] == {
        "maintenance_job": "database_backup"
    } for call in state_calls)


def test_failure_publishes_latest_outcome_without_success_state(monkeypatch) -> None:
    push_events = Mock()
    push_state = Mock()
    monkeypatch.setattr(
        "observability.maintenance_metrics.pushadd_to_gateway", push_events
    )
    monkeypatch.setattr(
        "observability.maintenance_metrics.push_to_gateway", push_state
    )
    metrics = MaintenanceJobMetrics("spool_cleanup", config(enabled=True))

    assert metrics.publish(success=False, duration_s=3)

    assert push_events.call_count == 1
    assert push_state.call_count == 1
    assert push_state.call_args.kwargs["job"] == "webcam_maintenance_outcome"
    outcome = generate_latest(push_state.call_args.kwargs["registry"])
    assert b'webcam_maintenance_last_run_unixtime{result="failure"}' in outcome


def test_dry_run_does_not_replace_real_outcome_state(monkeypatch) -> None:
    push_state = Mock()
    monkeypatch.setattr(
        "observability.maintenance_metrics.pushadd_to_gateway", Mock()
    )
    monkeypatch.setattr(
        "observability.maintenance_metrics.push_to_gateway", push_state
    )
    metrics = MaintenanceJobMetrics("spool_cleanup", config(enabled=True))

    assert metrics.publish(success=True, dry_run=True, duration_s=1)

    push_state.assert_not_called()
