import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deployment/maintenance/run"


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_host_wrapper_runs_one_in_container_common_runner(tmp_path: Path) -> None:
    commands = tmp_path / "commands.log"
    binary = tmp_path / "bin"
    binary.mkdir()
    _executable(
        binary / "docker",
        '#!/usr/bin/env bash\necho "$*" > "$COMMAND_LOG"\n',
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binary}:{environment['PATH']}",
            "COMMAND_LOG": str(commands),
        }
    )

    completed = subprocess.run(
        [str(SCRIPT), "daily"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert commands.read_text(encoding="utf-8").strip() == (
        "compose --env-file .env --profile jobs run --rm "
        "webcam-job python -m maintenance.runner daily"
    )


def test_host_wrapper_contains_no_maintenance_task_definitions() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "maintenance.runner" in content
    assert "discovery.windy" not in content
    assert "discovery.fintraffic" not in content
    assert "discovery.skaping" not in content
    assert "database.database_backup" not in content
    assert "storage.s3_spool_cleanup" not in content
