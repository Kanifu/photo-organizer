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


def test_index_contains_project_and_batch_assignment_ui():
    client = photo_app.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="project-name"' in html
    assert 'id="albums"' in html
    assert 'id="album-batch-actions"' in html
    assert "assignEventToAlbum" in html
    assert "assignPhotoToAlbum" in html
    assert "assignPhotoToAllAlbums" in html
    assert 'onclick="goToStep(1)"' in html
    assert "function goToStep(n)" in html


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
            "project_name": "Familie 2026",
            "albums": [{"name": "Zoon 2026"}, {"name": "Dochter 2026"}],
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
            "project_name": "Familie 2026",
            "albums": [{"name": "Zoon 2026"}],
            "input_dirs": [str(source)],
            "output_dir": str(output),
        },
    )

    assert response.status_code == 400
    assert "outside every input folder" in response.get_json()["error"]


def test_browse_api_lists_child_folders(tmp_path):
    source = tmp_path / "photos"
    child = source / "camera"
    child.mkdir(parents=True)
    (source / "notes.txt").write_text("not a folder")

    client = photo_app.app.test_client()
    response = client.get("/api/browse", query_string={"path": str(source)})

    assert response.status_code == 200
    data = response.get_json()
    assert data["path"] == str(source.resolve())
    assert {"name": "camera", "path": str(child.resolve())} in data["dirs"]


def test_browse_api_rejects_missing_folder(tmp_path):
    client = photo_app.app.test_client()
    response = client.get("/api/browse", query_string={"path": str(tmp_path / "missing")})

    assert response.status_code == 400
    assert "Folder not found" in response.get_json()["error"]


def test_native_folder_api_returns_selected_path(monkeypatch, tmp_path):
    selected = tmp_path / "photos"
    selected.mkdir()

    monkeypatch.setattr(photo_app, "choose_native_folder", lambda title: selected)

    client = photo_app.app.test_client()
    response = client.post("/api/native-folder", json={"mode": "input"})

    assert response.status_code == 200
    assert response.get_json() == {"path": str(selected), "canceled": False}


def test_native_folder_api_handles_cancel(monkeypatch):
    monkeypatch.setattr(photo_app, "choose_native_folder", lambda title: None)

    client = photo_app.app.test_client()
    response = client.post("/api/native-folder", json={"mode": "output"})

    assert response.status_code == 200
    assert response.get_json() == {"canceled": True}


def test_project_save_and_load_roundtrip(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    output = tmp_path / "export"

    client = photo_app.app.test_client()
    save_response = client.post(
        "/api/project",
        json={
            "project_name": "Familie 2026",
            "albums": [{"name": "Zoon 2026"}, {"name": "Dochter 2026"}],
            "input_dirs": [str(source)],
            "output_dir": str(output),
            "gap_hours": 6,
            "use_locations": False,
        },
    )

    assert save_response.status_code == 200
    project_file = output / "photo-organizer-project.json"
    assert project_file.exists()

    load_response = client.get("/api/project", query_string={"output_dir": str(output)})
    assert load_response.status_code == 200
    data = load_response.get_json()
    assert data["project_name"] == "Familie 2026"
    assert [album["name"] for album in data["albums"]] == ["Zoon 2026", "Dochter 2026"]
    assert data["input_dirs"] == [str(source.resolve())]


def test_export_creates_separate_album_folders(tmp_path):
    source = tmp_path / "photos"
    output = tmp_path / "export"
    photo_a = make_image(source / "20260601_100000_a.jpg", 7)
    photo_b = make_image(source / "20260601_100500_b.jpg", 31)

    client = photo_app.app.test_client()
    scan_response = client.post(
        "/api/scan",
        json={
            "project_name": "Familie 2026",
            "albums": [{"name": "Zoon 2026"}, {"name": "Dochter 2026"}],
            "input_dirs": [str(source)],
            "output_dir": str(output),
            "gap_hours": 4,
            "use_locations": False,
            "skip_screenshots": True,
            "api_key": "",
        },
    )
    assert scan_response.status_code == 200
    wait_for_scan(client)

    resolve_response = client.post("/api/resolve-duplicates", json={"decisions": {}})
    assert resolve_response.status_code == 200

    ai_progress = client.get("/api/ai-progress").get_json()
    photos = ai_progress["photos"]
    labels = {}
    for photo in photos:
        labels[photo["id"]] = {
            "excluded": False,
            "albums": ["zoon-2026"] if photo["name"] == photo_a.name else ["dochter-2026"],
        }

    events_response = client.post(
        "/api/events",
        json={"kept_ids": [photo["id"] for photo in photos]},
    )
    assert events_response.status_code == 200

    export_response = client.post(
        "/api/export",
        json={"renames": {}, "deleted_events": [], "photo_labels": labels},
    )
    assert export_response.status_code == 200
    assert export_response.get_json()["albums"] == 2
    assert (output / "zoon-2026").exists()
    assert (output / "dochter-2026").exists()
