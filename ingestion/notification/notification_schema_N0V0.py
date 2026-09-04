"""Build the pilot N0V0 notification payload."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from database.registry_queries import DueSourceStream
from ingestion.shared.source_image_validation import SourceImage
from ingestion.transformation.transformation_T0 import DerivedImage
from storage.s3_storage import StoredObject


SCHEMA_VERSION = "N0V0"


def build_notification(
    *,
    job: DueSourceStream,
    source: SourceImage,
    derived: DerivedImage,
    derived_stream_id: str,
    image_id: str,
    stored: StoredObject,
    download_timestamp: datetime,
    provider_update_timestamp: datetime | None,
    provider_url_timestamp: datetime | None = None,
    source_image_provider_metadata: dict[str, Any] | None = None,
    publication_timestamp: datetime | None = None,
) -> dict[str, Any]:
    published = publication_timestamp or datetime.now(UTC)
    source_stream_latitude, source_stream_longitude = _source_stream_coordinates(job)
    return {
        "schema_version": SCHEMA_VERSION,
        "image_id": image_id,
        "storage": {
            "type": "s3",
            "bucket": stored.bucket,
            "object_key": stored.object_key,
        },
        "network": {"network_id": job.network_id},
        "site": {
            "site_id": job.site_id,
            "latitude": job.latitude,
            "longitude": job.longitude,
            "altitude": job.altitude,
            "country": job.country,
            "corrected_latitude": job.corrected_latitude,
            "corrected_longitude": job.corrected_longitude,
            "corrected_altitude": job.corrected_altitude,
            "provider_metadata": job.site_metadata,
        },
        "source_stream": {
            "source_stream_id": job.source_stream_id,
            "selected_rendition": job.selected_rendition,
            "provider_stream_id": job.provider_source_stream_id,
            "latitude": source_stream_latitude,
            "longitude": source_stream_longitude,
            "name": _source_stream_name(job),
            "tags": _semantic_tags(job),
            "downstream_user_connexion_infos": _downstream_connection_info(job),
            "provider_metadata": job.source_stream_metadata,
        },
        "derived_stream": {
            "derived_stream_id": derived_stream_id,
            "transformation_version": derived.transformation_version,
            "transformation_metadata": {
                "image_signature": derived.image_signature,
                "jpeg_quality": derived.jpeg_quality,
                "panoramic": derived.panoramic,
            },
        },
        "timestamps": {
            "download_timestamp": _timestamp(download_timestamp),
            "provider_update_timestamp": _timestamp(provider_update_timestamp),
            "provider_url_timestamp": _timestamp(provider_url_timestamp),
            "publication_timestamp": _timestamp(published),
        },
        "source_image": {
            "width": source.width,
            "height": source.height,
            "format": source.format,
            "size_bytes": source.size_bytes,
            "colour_mode": source.color_mode,
            "provider_metadata": source_image_provider_metadata or {},
        },
        "derived_image": {
            "width": derived.width,
            "height": derived.height,
            "format": derived.format,
            "size_bytes": derived.size_bytes,
            "colour_mode": derived.color_mode,
            "colour_depth": derived.color_depth,
        },
    }


def _source_stream_coordinates(job: DueSourceStream) -> tuple[float, float]:
    latitude = job.latitude
    longitude = job.longitude
    if job.network_id == "win":
        location = job.source_stream_metadata.get("location")
        if isinstance(location, dict):
            camera_latitude = location.get("latitude")
            camera_longitude = location.get("longitude")
            if (
                isinstance(camera_latitude, (int, float))
                and not isinstance(camera_latitude, bool)
                and isinstance(camera_longitude, (int, float))
                and not isinstance(camera_longitude, bool)
            ):
                latitude = float(camera_latitude)
                longitude = float(camera_longitude)
    return latitude, longitude


def _semantic_tags(job: DueSourceStream) -> list[str]:
    if job.network_id == "win":
        return _string_list(job.source_stream_metadata.get("categories"))
    if job.network_id == "fin":
        properties = job.site_metadata.get("properties")
        purpose = properties.get("purpose") if isinstance(properties, dict) else None
        return _string_list(purpose)
    if job.network_id == "ska":
        return _string_list(job.source_stream_metadata.get("tags"))
    return []


def _source_stream_name(job: DueSourceStream) -> str | None:
    stream_metadata = job.source_stream_metadata
    if job.network_id == "win":
        return _nonempty_string(stream_metadata.get("title"))
    if job.network_id == "fin":
        properties = job.site_metadata.get("properties")
        station_name = (
            _nonempty_string(properties.get("name"))
            if isinstance(properties, dict)
            else None
        )
        presentation_name = _nonempty_string(
            stream_metadata.get("presentationName")
        )
        return " --- ".join(
            value for value in (station_name, presentation_name) if value
        ) or None
    if job.network_id == "ska":
        return _nonempty_string(stream_metadata.get("title")) or ""
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return list(dict.fromkeys(part for part in value.split() if part))
    if isinstance(value, (list, tuple)):
        return list(
            dict.fromkeys(
                item.strip()
                for item in value
                if isinstance(item, str) and item.strip()
            )
        )
    return []


def _downstream_connection_info(job: DueSourceStream) -> str | None:
    if job.network_id == "win":
        return "useAPI"
    metadata_key = {"fin": "imageUrl", "ska": "url"}.get(job.network_id)
    if metadata_key is None:
        return None
    return _nonempty_string(job.source_stream_metadata.get(metadata_key))


def _nonempty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("notification timestamps must be UTC-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
