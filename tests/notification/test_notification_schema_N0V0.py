import io
from dataclasses import replace
from datetime import UTC, datetime

from PIL import Image

from config.deployment_config import TransformationConfig
from ingestion.notification.notification_schema_N0V0 import build_notification
from ingestion.shared.source_image_validation import validate_source_image
from ingestion.transformation.transformation_T0 import transform
from storage.s3_storage import StoredObject
from tests.ingestion.windy.test_windy_ingestion_workflow import job


def test_builds_complete_versioned_payload() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "blue").save(buffer, "PNG")
    source = validate_source_image(buffer.getvalue())
    derived = transform(
        source,
        TransformationConfig("T0", 288, 90, 50_000, 200_000, 2.0),
    )
    timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    payload = build_notification(
        job=job(),
        source=source,
        derived=derived,
        derived_stream_id="win42T0",
        image_id="20260721T120000Z_win42T0.jpg",
        stored=StoredObject("webcam", "T0/win/image.jpg", "https://x/image.jpg"),
        download_timestamp=timestamp,
        provider_update_timestamp=timestamp,
        provider_url_timestamp=timestamp,
        publication_timestamp=timestamp,
    )

    assert payload["schema_version"] == "N0V0"
    assert payload["storage"]["object_key"] == "T0/win/image.jpg"
    assert payload["source_stream"]["provider_stream_id"] == "42"
    assert payload["source_stream"]["latitude"] == 55.0
    assert payload["source_stream"]["longitude"] == 12.0
    assert payload["derived_stream"]["transformation_version"] == "T0"
    assert payload["timestamps"]["download_timestamp"] == "2026-07-21T12:00:00Z"
    assert payload["timestamps"]["provider_url_timestamp"] == "2026-07-21T12:00:00Z"
    assert payload["derived_image"]["format"] == "JPEG"


def test_exposes_provider_semantics_and_downstream_access_information() -> None:
    windy = replace(
        job(),
        network_id="win",
        source_stream_metadata={
            "title": "Lake view",
            "categories": ["beach", "city", "beach"],
        },
    )
    fintraffic = replace(
        job(),
        network_id="fin",
        site_metadata={"properties": {"name": "vt5_Hyrynsalmi", "purpose": "keli"}},
        source_stream_metadata={
            "presentationName": "Kuusamoon",
            "imageUrl": "https://example.test/fin.jpg",
        },
    )
    skaping = replace(
        job(),
        network_id="ska",
        source_stream_metadata={
            "tags": "mountain snow mountain",
            "title": "Summit panorama",
            "url": "https://example.test/view",
        },
    )

    windy_payload = _notification_for(windy)
    fintraffic_payload = _notification_for(fintraffic)
    skaping_payload = _notification_for(skaping)

    assert "url" not in windy_payload
    assert windy_payload["source_stream"]["name"] == "Lake view"
    assert windy_payload["source_stream"]["tags"] == ["beach", "city"]
    assert windy_payload["source_stream"]["downstream_user_connexion_infos"] == "useAPI"
    assert fintraffic_payload["source_stream"]["name"] == "vt5_Hyrynsalmi --- Kuusamoon"
    assert fintraffic_payload["source_stream"]["tags"] == ["keli"]
    assert (
        fintraffic_payload["source_stream"]["downstream_user_connexion_infos"]
        == "https://example.test/fin.jpg"
    )
    assert skaping_payload["source_stream"]["name"] == "Summit panorama"
    assert skaping_payload["source_stream"]["tags"] == ["mountain", "snow"]
    assert (
        skaping_payload["source_stream"]["downstream_user_connexion_infos"]
        == "https://example.test/view"
    )

    skaping_without_title = replace(
        skaping,
        source_stream_metadata={"label": "Photo", "url": "https://example.test/view"},
    )
    assert _notification_for(skaping_without_title)["source_stream"]["name"] == ""


def test_windy_source_stream_coordinates_use_camera_metadata() -> None:
    windy_job = replace(
        job(),
        source_stream_metadata={
            "title": "Harbour view",
            "location": {"latitude": 55.00005, "longitude": 12.00005},
        },
    )

    payload = _notification_for(windy_job)

    assert payload["site"]["latitude"] == 55.0
    assert payload["site"]["longitude"] == 12.0
    assert payload["site"]["altitude"] == 5.0
    assert payload["source_stream"]["latitude"] == 55.00005
    assert payload["source_stream"]["longitude"] == 12.00005


def test_other_network_source_stream_coordinates_use_site_coordinates() -> None:
    for network_id in ("fin", "ska"):
        provider_job = replace(
            job(),
            network_id=network_id,
            source_stream_metadata={
                "location": {"latitude": 1.0, "longitude": 2.0}
            },
        )

        payload = _notification_for(provider_job)

        assert payload["source_stream"]["latitude"] == 55.0
        assert payload["source_stream"]["longitude"] == 12.0


def _notification_for(notification_job):
    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), "blue").save(buffer, "PNG")
    source = validate_source_image(buffer.getvalue())
    derived = transform(
        source,
        TransformationConfig("T0", 288, 90, 50_000, 200_000, 2.0),
    )
    timestamp = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    return build_notification(
        job=notification_job,
        source=source,
        derived=derived,
        derived_stream_id=f"{notification_job.source_stream_id}T0",
        image_id=f"20260721T120000Z_{notification_job.source_stream_id}T0.jpg",
        stored=StoredObject("webcam", "T0/image.jpg", "https://x/image.jpg"),
        download_timestamp=timestamp,
        provider_update_timestamp=timestamp,
        publication_timestamp=timestamp,
    )
