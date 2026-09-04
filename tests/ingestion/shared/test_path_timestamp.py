from datetime import UTC, datetime

import pytest

from ingestion.shared.path_timestamp import timestamp_from_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/site/2026/07/23/mini/19-05.jpg",
            datetime(2026, 7, 23, 17, 5, tzinfo=UTC),
        ),
        (
            "https://objects.example/site/2026-07-23/mini/image_19_05_12.jpg?token=x",
            datetime(2026, 7, 23, 17, 5, 12, tzinfo=UTC),
        ),
        (
            "/site/20260723/mini/1905.jpg",
            datetime(2026, 7, 23, 17, 5, tzinfo=UTC),
        ),
    ],
)
def test_extracts_flexible_local_path_timestamp_as_utc(path, expected):
    assert timestamp_from_path(path, "Europe/Paris") == expected


@pytest.mark.parametrize(
    ("path", "timezone_name"),
    [
        (None, "Europe/Paris"),
        ("/site/no-date/19-05.jpg", "Europe/Paris"),
        ("/site/2026/02/30/19-05.jpg", "Europe/Paris"),
        ("/site/2026/07/23/no-time.jpg", "Europe/Paris"),
        ("/site/2026/07/23/19-05.jpg", "Invalid/Timezone"),
        ("/site/2026/07/23/19-05.jpg", None),
    ],
)
def test_returns_none_when_path_timestamp_cannot_be_normalized(path, timezone_name):
    assert timestamp_from_path(path, timezone_name) is None
