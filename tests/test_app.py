import time
from pathlib import Path

from PIL import Image

import app as photo_app


def make_image(path: Path, seed: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (80, 60))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = (
                (x * seed + y * 3) % 255,
                (y * seed + x * 5) % 255,
                ((x + y) * seed) % 255,
            )
    image.save(path, "JPEG")
    return path


def wait_for_scan(client):
    for _ in range(100):
        data = client.get("/api/scan-progress").get_json()
        if data["status"] in {"done", "error"}:
            return data
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


def test_scan_api_accepts_multiple_input_dirs(tmp_path):
    source_a = tmp_path / "camera"
    source_b = tmp_path / "phone"
    output = tmp_path / "album-output"
    make_image(source_a / "20260601_100000_a.jpg", 7)
    make_image(source_b / "20260602_100000_b.jpg", 23)

    client = photo_app.app.test_client()
    response = client.post(
        "/api/scan",
        json={
            "input_dirs": [str(source_a), str(source_b)],
            "output_dir": str(output),
            "gap_hours": 4,
            "use_locations": False,
            "skip_screenshots": True,
            "api_key": "",
        },
    )

    assert response.status_code == 200
    progress = wait_for_scan(client)
    assert progress["status"] == "done"
    assert progress["singletons"] == 2
    assert "2 source folder" in progress["message"]


def test_scan_api_rejects_output_inside_input_dir(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    output = source / "album-output"

    client = photo_app.app.test_client()
    response = client.post(
        "/api/scan",
        json={
            "input_dirs": [str(source)],
            "output_dir": str(output),
        },
    )

    assert response.status_code == 400
    assert "outside every input folder" in response.get_json()["error"]
