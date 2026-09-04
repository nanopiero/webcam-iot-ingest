"""Sample canonical S3 images and summarize their aspect ratios."""

from __future__ import annotations

import argparse
import io
import json
import random
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

from config.deployment_config import S3Config
from storage.s3_spool_cleanup import image_download_timestamp
from storage.s3_storage import create_s3_client


_NETWORK_FROM_KEY = re.compile(r"(?:^|/)T0/(?P<network>win|fin|ska)/")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sample")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _reservoir_sample(
    objects: Iterable[dict[str, Any]],
    *,
    sample_size: int,
    configured_prefix: str,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], int]:
    sample: list[dict[str, Any]] = []
    eligible = 0
    for item in objects:
        key = str(item.get("Key", ""))
        if image_download_timestamp(
            key, configured_prefix, transformation_prefix="T0"
        ) is None:
            continue
        eligible += 1
        if len(sample) < sample_size:
            sample.append(item)
            continue
        replacement = rng.randrange(eligible)
        if replacement < sample_size:
            sample[replacement] = item
    return sample, eligible


def _bounded_network_objects(
    client: Any,
    *,
    bucket: str,
    prefix: str,
    limit: int,
) -> Iterable[dict[str, Any]]:
    paginator = client.get_paginator("list_objects_v2")
    yield from (
        item
        for page in paginator.paginate(
            Bucket=bucket,
            Prefix=f"{prefix.strip('/')}/",
            PaginationConfig={"MaxItems": limit},
        )
        for item in page.get("Contents", ())
    )


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ratios = [float(row["aspect_ratio"]) for row in rows]
    orientations = Counter(str(row["orientation"]) for row in rows)
    dimensions = Counter(f'{row["width"]}x{row["height"]}' for row in rows)
    if not ratios:
        return {"count": 0}
    return {
        "count": len(rows),
        "aspect_ratio": {
            "min": round(min(ratios), 4),
            "p10": round(_percentile(ratios, 0.10), 4),
            "p25": round(_percentile(ratios, 0.25), 4),
            "median": round(_percentile(ratios, 0.50), 4),
            "mean": round(sum(ratios) / len(ratios), 4),
            "p75": round(_percentile(ratios, 0.75), 4),
            "p90": round(_percentile(ratios, 0.90), 4),
            "p95": round(_percentile(ratios, 0.95), 4),
            "max": round(max(ratios), 4),
        },
        "orientation_counts": dict(sorted(orientations.items())),
        "panoramic_threshold": 1.8,
        "panoramic_count": sum(ratio >= 1.8 for ratio in ratios),
        "most_common_dimensions": [
            {"dimensions": dimensions_value, "count": count}
            for dimensions_value, count in dimensions.most_common(10)
        ],
    }


def sample_aspect_ratios(
    *,
    config: S3Config,
    sample_size: int = 500,
    seed: int = 20260826,
    candidate_limit_per_network: int = 2000,
    client: Any | None = None,
) -> dict[str, Any]:
    if sample_size < 1:
        raise ValueError("sample size must be positive")
    if candidate_limit_per_network < 1:
        raise ValueError("candidate limit must be positive")
    client = client or create_s3_client(config)
    rng = random.Random(seed)
    selected: list[dict[str, Any]] = []
    candidates_by_network: dict[str, int] = {}
    base_size, remainder = divmod(sample_size, 3)
    for index, network in enumerate(("win", "fin", "ska")):
        listing_prefix = "/".join(
            part
            for part in (config.prefix.strip("/"), "T0", network)
            if part
        )
        network_sample, eligible = _reservoir_sample(
            _bounded_network_objects(
                client,
                bucket=config.bucket,
                prefix=listing_prefix,
                limit=candidate_limit_per_network,
            ),
            sample_size=base_size + (1 if index < remainder else 0),
            configured_prefix=config.prefix,
            rng=rng,
        )
        candidates_by_network[network] = eligible
        selected.extend(network_sample)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for item in selected:
        key = str(item["Key"])
        try:
            response = client.get_object(Bucket=config.bucket, Key=key)
            content = response["Body"].read()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
            if width < 1 or height < 1:
                raise ValueError("invalid image dimensions")
            ratio = width / height
            network_match = _NETWORK_FROM_KEY.search(key)
            rows.append(
                {
                    "network": network_match.group("network") if network_match else "unknown",
                    "width": width,
                    "height": height,
                    "aspect_ratio": ratio,
                    "orientation": (
                        "square" if 0.95 <= ratio <= 1.05
                        else "landscape" if ratio > 1.05
                        else "portrait"
                    ),
                }
            )
        except (OSError, ValueError, KeyError, UnidentifiedImageError) as error:
            failures.append({"key": key, "reason": type(error).__name__})

    by_network: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_network[str(row["network"])].append(row)
    return {
        "bucket": config.bucket,
        "transformation_prefix": "T0",
        "seed": seed,
        "sampling_method": "network-stratified bounded candidate windows",
        "candidate_limit_per_network": candidate_limit_per_network,
        "candidate_objects_by_network": candidates_by_network,
        "requested_sample_size": sample_size,
        "selected_objects": len(selected),
        "readable_images": len(rows),
        "failed_images": len(failures),
        "overall": _summary(rows),
        "by_network": {
            network: _summary(network_rows)
            for network, network_rows in sorted(by_network.items())
        },
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--candidate-limit-per-network", type=int, default=2000)
    args = parser.parse_args()
    result = sample_aspect_ratios(
        config=S3Config.from_environment(),
        sample_size=args.sample_size,
        seed=args.seed,
        candidate_limit_per_network=args.candidate_limit_per_network,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
