import io

from PIL import Image

from config.deployment_config import S3Config
from storage.s3_image_aspect_ratios import sample_aspect_ratios


def jpeg(width, height):
    output = io.BytesIO()
    Image.new("RGB", (width, height)).save(output, format="JPEG")
    return output.getvalue()


class Body:
    def __init__(self, content):
        self.content = content

    def read(self):
        return self.content


class Paginator:
    def paginate(self, **kwargs):
        prefix = kwargs["Prefix"]
        assert kwargs["PaginationConfig"]["MaxItems"] == 2000
        return [{"Contents": [
            {"Key": key} for key in CLIENT.images if key.startswith(prefix)
        ]}]


class Client:
    images = {
        "T0/win/2026/08/26/10/20260826T100000Z_win1T0.jpg": jpeg(400, 200),
        "T0/fin/2026/08/26/10/20260826T100001Z_fin1T0.jpg": jpeg(200, 200),
        "T0/ska/2026/08/26/10/20260826T100002Z_ska1T0.jpg": jpeg(100, 200),
    }

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return Paginator()

    def get_object(self, *, Bucket, Key):
        assert Bucket == "bucket"
        return {"Body": Body(self.images[Key])}


CLIENT = Client()


def config():
    return S3Config(
        endpoint_url="https://s3.example",
        bucket="bucket",
        prefix="",
        public_url_base="https://s3.example/bucket",
        region=None,
        access_key_file=None,
        secret_key_file=None,
        retry_count=0,
        retry_backoff_s=0,
    )


def test_samples_only_canonical_images_and_reports_ratios():
    result = sample_aspect_ratios(
        config=config(), sample_size=500, seed=1, client=CLIENT
    )

    assert result["candidate_objects_by_network"] == {"win": 1, "fin": 1, "ska": 1}
    assert result["readable_images"] == 3
    assert result["overall"]["aspect_ratio"]["median"] == 1.0
    assert result["overall"]["panoramic_count"] == 1
    assert result["overall"]["panoramic_threshold"] == 1.8
    assert result["overall"]["orientation_counts"] == {
        "landscape": 1,
        "portrait": 1,
        "square": 1,
    }
    assert set(result["by_network"]) == {"fin", "ska", "win"}
