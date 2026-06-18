#!/usr/bin/python3
"""
Photo Album Organizer — Web UI
Run: python3 app.py  →  opens http://localhost:5050
"""

import base64
import io
import json
import shutil
import subprocess
import threading
import webbrowser
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template_string, request
from PIL import Image

from organize import (
    DEFAULT_EVENT_GAP_HOURS,
    cluster_into_events,
    compute_hash,
    get_location_name_for_event,
    get_photo_date,
    get_resolution,
    normalize_input_dirs,
    read_gps_from_exif,
    scan_photos,
    validate_source_dirs,
)

app = Flask(__name__)
app.secret_key = "photo-organizer-local"

# ── State ─────────────────────────────────────────────────────────────────────

state: dict = {
    "project_name": "",
    "albums": [],
    "project_file": None,
    "input_dir": None,
    "input_dirs": [],
    "output_dir": None,
    "gap_hours": DEFAULT_EVENT_GAP_HOURS,
    "use_locations": True,
    "api_key": "",
    "scan_status": "idle",      # idle | running | done | error
    "scan_progress": "",
    "kept_photos": [],
    "dup_groups": [],           # List[List[Path]] — groups of similar photos
    "ai_scores": {},            # str(path) -> {score, reason}
    "ai_running": False,
    "events": {},               # event_key -> [Path]
    "photo_labels": {},         # photo_id -> {excluded: bool, albums: [album_id]}
    "event_renames": {},        # event_key -> folder name
    "deleted_events": [],
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def thumb_b64(path: Path, size=(280, 280)) -> str:
    try:
        with Image.open(path) as img:
            img.thumbnail(size, Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=82)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

def path_id(path: Path) -> str:
    return base64.urlsafe_b64encode(str(path).encode()).decode()

def id_path(pid: str) -> Path:
    return Path(base64.urlsafe_b64decode(pid.encode()).decode())

def photo_info(path: Path) -> dict:
    date, source = get_photo_date(path)
    gps = read_gps_from_exif(path)
    score_data = state["ai_scores"].get(str(path))
    try:
        size_kb = path.stat().st_size // 1024
    except Exception:
        size_kb = 0
    res = get_resolution(path)
    return {
        "id": path_id(path),
        "name": path.name,
        "date": date.strftime("%Y-%m-%d %H:%M"),
        "date_source": source,
        "size_kb": size_kb,
        "resolution": f"{res // 1000}K px" if res else "?",
        "has_gps": gps is not None,
        "ai_score": score_data["score"] if score_data else None,
        "ai_reason": score_data["reason"] if score_data else None,
        "thumb": thumb_b64(path),
    }


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "album"


def normalize_albums(raw_albums: List[dict]) -> List[dict]:
    albums = []
    used_ids = set()
    for item in raw_albums:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        album_id = slugify(item.get("id") or name)
        suffix = 2
        base_id = album_id
        while album_id in used_ids:
            album_id = f"{base_id}-{suffix}"
            suffix += 1
        albums.append({"id": album_id, "name": name})
        used_ids.add(album_id)
    return albums


def project_file_for_output_dir(output_dir: Path) -> Path:
    return output_dir / "photo-organizer-project.json"


def default_photo_labels_for_path(path: Path) -> dict:
    photo_id = path_id(path)
    return {
        "excluded": False,
        "albums": [album["id"] for album in state["albums"]],
        "photo_id": photo_id,
    }


def ensure_photo_labels(paths: List[Path]) -> None:
    for path in paths:
        photo_id = path_id(path)
        existing = state["photo_labels"].get(photo_id)
        if not existing:
            state["photo_labels"][photo_id] = default_photo_labels_for_path(path)
            continue
        existing_albums = set(existing.get("albums", []))
        for album in state["albums"]:
            if album["id"] not in existing_albums:
                existing.setdefault("albums", []).append(album["id"])
        existing["albums"] = [album_id for album_id in existing.get("albums", []) if any(album["id"] == album_id for album in state["albums"])]


def serialize_project_state() -> dict:
    return {
        "project_name": state["project_name"],
        "albums": state["albums"],
        "input_dirs": [str(path) for path in state["input_dirs"]],
        "output_dir": str(state["output_dir"]) if state["output_dir"] else "",
        "gap_hours": state["gap_hours"],
        "use_locations": state["use_locations"],
        "photo_labels": state["photo_labels"],
        "event_renames": state["event_renames"],
        "deleted_events": state["deleted_events"],
    }


def save_project_state() -> Optional[Path]:
    output_dir = state.get("output_dir")
    if not output_dir:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    project_file = project_file_for_output_dir(output_dir)
    project_file.write_text(json.dumps(serialize_project_state(), indent=2), encoding="utf-8")
    state["project_file"] = project_file
    return project_file


def load_project_state(project_file: Path) -> dict:
    data = json.loads(project_file.read_text(encoding="utf-8"))
    output_dir = Path(data["output_dir"]).expanduser().resolve()
    input_dirs = normalize_input_dirs(data.get("input_dirs", []))
    validate_source_dirs(input_dirs, output_dir)
    albums = normalize_albums(data.get("albums", []))
    return {
        "project_name": data.get("project_name", ""),
        "albums": albums,
        "input_dirs": input_dirs,
        "output_dir": output_dir,
        "gap_hours": float(data.get("gap_hours", DEFAULT_EVENT_GAP_HOURS)),
        "use_locations": bool(data.get("use_locations", True)),
        "photo_labels": data.get("photo_labels", {}),
        "event_renames": data.get("event_renames", {}),
        "deleted_events": data.get("deleted_events", []),
        "project_file": project_file,
    }


def browse_dir_info(path: Path) -> dict:
    home = Path.home().resolve()
    resolved = path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Folder not found: {resolved}")

    dirs = []
    try:
        children = sorted(
            [p for p in resolved.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        children = []

    for child in children:
        dirs.append({
            "name": child.name,
            "path": str(child),
        })

    parent = resolved.parent if resolved.parent != resolved else None
    quick = [
        {"label": "Home", "path": home},
        {"label": "Pictures", "path": home / "Pictures"},
        {"label": "Desktop", "path": home / "Desktop"},
        {"label": "Documents", "path": home / "Documents"},
        {"label": "Downloads", "path": home / "Downloads"},
    ]

    return {
        "path": str(resolved),
        "home": str(home),
        "parent": str(parent) if parent else None,
        "dirs": dirs,
        "quick": [
            {"label": item["label"], "path": str(item["path"])}
            for item in quick
            if item["path"].exists() and item["path"].is_dir()
        ],
    }


def choose_native_folder(title: str) -> Optional[Path]:
    script = f'POSIX path of (choose folder with prompt "{title}")'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise RuntimeError("Native folder picker is unavailable on this system.") from e
    if result.returncode != 0:
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "Native folder picker failed.")

    raw_path = result.stdout.strip()
    if raw_path != "/":
        raw_path = raw_path.rstrip("/")
    return Path(raw_path).expanduser().resolve()


# ── Duplicate grouping (union-find) ───────────────────────────────────────────

def find_duplicate_groups(photos: List[Path], threshold: int = 8) -> Tuple[List[Path], List[List[Path]]]:
    """
    Returns (singletons, groups) where groups are clusters of similar photos.
    Singletons are auto-kept; groups need user review.
    """
    n = len(photos)
    hashes = [compute_hash(p) for p in photos]

    # Union-Find
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(n):
        for j in range(i + 1, n):
            if hashes[i] is not None and hashes[j] is not None:
                if hashes[i] - hashes[j] <= threshold:
                    union(i, j)

    clusters: dict = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    singletons = []
    groups = []
    for indices in clusters.values():
        if len(indices) == 1:
            singletons.append(photos[indices[0]])
        else:
            # Sort group by resolution descending so best is first
            group = sorted([photos[i] for i in indices], key=get_resolution, reverse=True)
            groups.append(group)

    return singletons, groups


# ── AI scoring ────────────────────────────────────────────────────────────────

def score_photo_with_ai(path: Path, api_key: str) -> dict:
    import anthropic
    try:
        with Image.open(path) as img:
            img.thumbnail((512, 512), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=75)
            img_b64 = base64.b64encode(buf.getvalue()).decode()

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": """You are rating photos for a family photo album. Look carefully at the image and give an honest, spread-out score.

SCORING GUIDE:
1: Completely irrelevant — screenshot, receipt, barcode, shopping cart, food label, app UI, document scan.
2: Accidental or useless — blurry mess, photo of the floor/wall/sky with nothing in it, fingers over lens.
3: Very poor quality — severely blurry or dark, badly cropped so subject is unrecognisable.
4: Poor — recognisable but blurry, overexposed, or bad composition with no clear subject.
5: Mediocre — technically okay photo but nothing meaningful (random object, empty room, boring snapshot).
6: Decent — clear photo of something real but not particularly memorable or well composed.
7: Good — clear photo of a person, place, or event. Something worth keeping.
8: Very good — nice composition, real moment, people enjoying something. Clearly album-worthy.
9: Excellent — sharp, well-framed, emotionally meaningful family moment.
10: Outstanding — beautiful, perfectly composed, emotionally powerful memory.

Most good family photos should score 6-8. Reserve 9-10 for truly special shots. Give 1-3 only for genuinely bad/irrelevant photos.

Reply ONLY in this exact format (nothing else): SCORE|reason in max 7 words
Example reply: 7|Kids at the playground, nicely framed"""},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        score_str, _, reason = raw.partition("|")
        score = max(1, min(10, int(score_str.strip())))
        return {"score": score, "reason": reason.strip()}
    except Exception as e:
        return {"score": -1, "reason": f"Error: {e}"}


def run_ai_scoring(paths: List[Path], api_key: str):
    state["ai_running"] = True
    for path in paths:
        if str(path) not in state["ai_scores"]:
            state["ai_scores"][str(path)] = score_photo_with_ai(path, api_key)
    state["ai_running"] = False


# ── Background scan ───────────────────────────────────────────────────────────

def do_scan(input_dirs: List[Path], skip_screenshots: bool):
    try:
        state["scan_status"] = "running"
        state["scan_progress"] = "Scanning folders…"

        photos, skipped = scan_photos(input_dirs, skip_screenshots=skip_screenshots)

        skipped_msg = f" Skipped {skipped} screenshots." if skipped else ""
        state["scan_progress"] = f"Found {len(photos)} photos.{skipped_msg} Computing hashes…"

        singletons, groups = find_duplicate_groups(photos)

        state["kept_photos"] = singletons
        state["dup_groups"] = groups
        ensure_photo_labels(singletons)
        state["scan_progress"] = (
            f"Done. {len(photos)} photos scanned from {len(input_dirs)} source folder(s), "
            f"{len(groups)} duplicate groups found ({sum(len(g) for g in groups)} photos)."
        )
        state["scan_status"] = "done"
        save_project_state()
    except Exception as e:
        state["scan_status"] = "error"
        state["scan_progress"] = str(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/browse")
def api_browse():
    raw_path = request.args.get("path") or str(Path.home())
    try:
        return jsonify(browse_dir_info(Path(raw_path)))
    except ValueError as e:
        return jsonify(error=str(e)), 400

@app.route("/api/native-folder", methods=["POST"])
def api_native_folder():
    data = request.json or {}
    mode = data.get("mode", "input")
    title = "Choose input folder" if mode == "input" else "Choose output folder"
    try:
        folder = choose_native_folder(title)
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    if folder is None:
        return jsonify(canceled=True)
    return jsonify(path=str(folder), canceled=False)


@app.route("/api/project", methods=["GET"])
def api_project_load():
    raw_output_dir = request.args.get("output_dir", "").strip()
    if not raw_output_dir:
        return jsonify(error="Output folder is required."), 400
    output_dir = Path(raw_output_dir).expanduser().resolve()
    project_file = project_file_for_output_dir(output_dir)
    if not project_file.exists():
        return jsonify(error=f"No project file found in {output_dir}"), 404

    try:
        loaded = load_project_state(project_file)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return jsonify(error=str(e)), 400

    state.update({
        "project_name": loaded["project_name"],
        "albums": loaded["albums"],
        "input_dir": loaded["input_dirs"][0] if loaded["input_dirs"] else None,
        "input_dirs": loaded["input_dirs"],
        "output_dir": loaded["output_dir"],
        "gap_hours": loaded["gap_hours"],
        "use_locations": loaded["use_locations"],
        "photo_labels": loaded["photo_labels"],
        "event_renames": loaded["event_renames"],
        "deleted_events": loaded["deleted_events"],
        "project_file": loaded["project_file"],
    })
    return jsonify({
        "project_name": state["project_name"],
        "albums": state["albums"],
        "input_dirs": [str(path) for path in state["input_dirs"]],
        "output_dir": str(state["output_dir"]),
        "gap_hours": state["gap_hours"],
        "use_locations": state["use_locations"],
        "project_file": str(state["project_file"]),
    })


@app.route("/api/project", methods=["POST"])
def api_project_save():
    data = request.json or {}
    raw_input_dirs = data.get("input_dirs") or [data.get("input_dir")]
    output_dir = Path(data["output_dir"]).expanduser().resolve()
    albums = normalize_albums(data.get("albums", []))
    project_name = str(data.get("project_name", "")).strip()

    try:
        input_dirs = normalize_input_dirs(raw_input_dirs)
        validate_source_dirs(input_dirs, output_dir)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    if not project_name:
        return jsonify(error="Project name is required."), 400
    if not albums:
        return jsonify(error="Add at least one album."), 400

    state.update({
        "project_name": project_name,
        "albums": albums,
        "input_dir": input_dirs[0],
        "input_dirs": input_dirs,
        "output_dir": output_dir,
        "gap_hours": float(data.get("gap_hours", DEFAULT_EVENT_GAP_HOURS)),
        "use_locations": data.get("use_locations", True),
    })
    ensure_photo_labels(state.get("kept_photos", []))
    project_file = save_project_state()
    return jsonify(ok=True, project_file=str(project_file))

@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.json
    raw_input_dirs = data.get("input_dirs") or [data.get("input_dir")]
    output_dir = Path(data["output_dir"]).expanduser().resolve()
    albums = normalize_albums(data.get("albums", []))
    project_name = str(data.get("project_name", "")).strip()

    try:
        input_dirs = normalize_input_dirs(raw_input_dirs)
        validate_source_dirs(input_dirs, output_dir)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not project_name:
        return jsonify(error="Project name is required."), 400
    if not albums:
        return jsonify(error="Add at least one album."), 400

    state.update({
        "project_name": project_name,
        "albums": albums,
        "project_file": project_file_for_output_dir(output_dir),
        "input_dir": input_dirs[0],
        "input_dirs": input_dirs,
        "output_dir": output_dir,
        "gap_hours": float(data.get("gap_hours", DEFAULT_EVENT_GAP_HOURS)),
        "use_locations": data.get("use_locations", True),
        "api_key": data.get("api_key", ""),
        "ai_scores": {},
        "dup_groups": [],
        "kept_photos": [],
        "photo_labels": {},
        "scan_status": "idle",
        "event_renames": {},
        "deleted_events": [],
    })

    threading.Thread(
        target=do_scan,
        args=(input_dirs, data.get("skip_screenshots", True)),
        daemon=True
    ).start()
    return jsonify(started=True, project_file=str(state["project_file"]))

@app.route("/api/scan-progress")
def api_scan_progress():
    groups_data = []
    for group in state["dup_groups"]:
        groups_data.append([photo_info(p) for p in group])
    return jsonify(
        status=state["scan_status"],
        message=state["scan_progress"],
        singletons=len(state["kept_photos"]),
        dup_groups=groups_data,
    )

@app.route("/api/resolve-duplicates", methods=["POST"])
def api_resolve_duplicates():
    """
    decisions: { group_index: [photo_id, ...] }  — list of IDs to keep from each group
    Empty list = delete all from group.
    """
    decisions: dict = request.json.get("decisions", {})
    kept = list(state["kept_photos"])

    for idx_str, keep_ids in decisions.items():
        idx = int(idx_str)
        if idx >= len(state["dup_groups"]):
            continue
        group = state["dup_groups"][idx]
        keep_set = set(keep_ids)
        for p in group:
            if path_id(p) in keep_set:
                kept.append(p)
            # else: discard

    state["kept_photos"] = kept
    ensure_photo_labels(kept)
    save_project_state()
    return jsonify(ok=True, total=len(kept))

@app.route("/api/ai-scores")
def api_ai_scores():
    return jsonify(has_api_key=bool(state.get("api_key")))

@app.route("/api/run-ai", methods=["POST"])
def api_run_ai():
    api_key = state.get("api_key", "")
    if not api_key:
        return jsonify(error="No API key"), 400
    threading.Thread(
        target=run_ai_scoring,
        args=(state["kept_photos"], api_key),
        daemon=True
    ).start()
    return jsonify(started=True)

@app.route("/api/ai-progress")
def api_ai_progress():
    photos = state["kept_photos"]
    scored = sum(1 for p in photos if str(p) in state["ai_scores"])
    ensure_photo_labels(photos)
    return jsonify(
        done=scored,
        total=len(photos),
        finished=not state["ai_running"] and scored >= len(photos),
        albums=state["albums"],
        photos=[
            {
                **photo_info(p),
                "labels": state["photo_labels"].get(path_id(p), default_photo_labels_for_path(p)),
            }
            for p in photos
        ],
    )


@app.route("/api/photo-labels", methods=["POST"])
def api_photo_labels():
    labels = request.json.get("photo_labels", {})
    valid_albums = {album["id"] for album in state["albums"]}
    for photo_id, label_data in labels.items():
        state["photo_labels"][photo_id] = {
            "excluded": bool(label_data.get("excluded", False)),
            "albums": [album_id for album_id in label_data.get("albums", []) if album_id in valid_albums],
            "photo_id": photo_id,
        }
    save_project_state()
    return jsonify(ok=True, total=len(state["photo_labels"]))

@app.route("/api/events", methods=["POST"])
def api_events():
    kept_ids = set(request.json.get("kept_ids", []))
    photos = [p for p in state["kept_photos"] if path_id(p) in kept_ids]
    if not photos:
        state["events"] = {}
        return jsonify(events=[], total=0)

    photos_with_dates = [(p, get_photo_date(p)[0]) for p in photos]
    _, raw_clusters = cluster_into_events(photos_with_dates, state["gap_hours"])

    named_events: dict = {}
    used_keys: set = set()
    for idx, (event_start, event_photos) in enumerate(raw_clusters, 1):
        if not event_photos:
            continue
        date_str = event_start.strftime("%Y-%m-%d")
        place = get_location_name_for_event(event_photos) if state.get("use_locations") else None
        base_key = f"{date_str}_{place}" if place else f"{date_str}_event{idx:02d}"
        key, suffix = base_key, 2
        while key in used_keys:
            key, suffix = f"{base_key}_{suffix}", suffix + 1
        used_keys.add(key)
        named_events[key] = event_photos

    state["events"] = named_events
    save_project_state()
    events_data = [
        {"key": k, "photos": [photo_info(p) for p in v]}
        for k, v in sorted(named_events.items())
    ]
    return jsonify(events=events_data, total=sum(len(v) for v in named_events.values()))

@app.route("/api/export", methods=["POST"])
def api_export():
    data = request.json
    renames: dict = data.get("renames", {})
    deleted: set = set(data.get("deleted_events", []))
    output_dir: Path = state["output_dir"]
    labels = request.json.get("photo_labels", state["photo_labels"])
    state["event_renames"] = renames
    state["deleted_events"] = list(deleted)
    total = 0
    exported_events = 0
    exported_albums = 0

    for album in state["albums"]:
        album_dir = output_dir / slugify(album["name"])
        album_total = 0
        for key, photos in sorted(state["events"].items()):
            if key in deleted:
                continue
            folder_name = renames.get(key, key)
            selected = []
            for photo in photos:
                photo_id = path_id(photo)
                label_data = labels.get(photo_id, {})
                if label_data.get("excluded"):
                    continue
                if album["id"] not in label_data.get("albums", []):
                    continue
                selected.append(photo)
            if not selected:
                continue
            event_dir = album_dir / folder_name
            event_dir.mkdir(parents=True, exist_ok=True)
            for i, src in enumerate(selected, 1):
                shutil.copy2(src, event_dir / f"{i:03d}_{src.name}")
                total += 1
                album_total += 1
            exported_events += 1
        if album_total:
            exported_albums += 1

    save_project_state()
    return jsonify(total=total, events=exported_events, albums=exported_albums, output_dir=str(output_dir))


# ── HTML / JS ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Photo Album Organizer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f0f2f5;color:#1a1a1a}
header{background:#1a1a2e;color:#fff;padding:16px 32px;display:flex;align-items:center;gap:14px}
header h1{font-size:1.25rem;font-weight:700}
header span{font-size:.82rem;opacity:.55}
.wrap{max-width:1100px;margin:28px auto;padding:0 22px}
.card{background:#fff;border-radius:12px;padding:26px;margin-bottom:22px;box-shadow:0 1px 5px rgba(0,0,0,.07)}
.card h2{font-size:.82rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#888;margin-bottom:16px}
label{display:block;font-size:.88rem;color:#555;margin-bottom:4px}
input[type=text],input[type=number],input[type=password],textarea{width:100%;padding:9px 13px;border:1.5px solid #e0e0e0;border-radius:8px;font-size:.93rem;margin-bottom:12px;outline:none;transition:border-color .15s;font-family:inherit}
textarea{min-height:92px;resize:vertical;line-height:1.35}
input:focus,textarea:focus{border-color:#4f8ef7}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.toggle{display:flex;align-items:center;gap:9px;margin-bottom:11px;font-size:.9rem;color:#444;cursor:pointer}
.toggle input{width:16px;height:16px;cursor:pointer}
button{padding:10px 20px;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.83}
button:disabled{opacity:.35;cursor:not-allowed}
.bp{background:#4f8ef7;color:#fff}
.bg{background:#2ecc71;color:#fff}
.br{background:#e74c3c;color:#fff}
.bz{background:#bbb;color:#fff}
.bo{background:#f39c12;color:#fff}
.sm{padding:6px 13px;font-size:.82rem}
.alert{border-radius:8px;padding:11px 15px;margin-bottom:18px;font-size:.88rem}
.alert-info{background:#eef2ff;color:#4f5fa7}
.alert-ok{background:#efffef;color:#27ae60}
.alert-err{background:#fff0f0;color:#c0392b}
.alert-warn{background:#fffbee;color:#b7770d}
.progress{height:5px;background:#e8eaef;border-radius:4px;overflow:hidden;margin-top:8px}
.pbar{height:100%;background:#4f8ef7;transition:width .4s}
/* Steps */
.steps{display:flex;gap:7px;margin-bottom:22px;flex-wrap:wrap}
.step{padding:6px 16px;border-radius:20px;font-size:.82rem;background:#e8eaef;color:#999;font-weight:600}
.step.nav{cursor:pointer}
.step.on{background:#4f8ef7;color:#fff}
.step.ok{background:#2ecc71;color:#fff}
section{display:none}
section.on{display:block}
/* Dup groups */
.dup-group{border:1.5px solid #e8e8e8;border-radius:10px;margin-bottom:18px;overflow:hidden}
.dup-group-header{background:#f7f8fa;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.dup-group-header span{font-size:.88rem;color:#666}
.dup-photos{display:flex;flex-wrap:wrap;gap:12px;padding:14px}
.dup-photo{border-radius:8px;overflow:hidden;border:3px solid transparent;cursor:pointer;position:relative;width:200px;flex-shrink:0;transition:border-color .15s,opacity .15s}
.dup-photo.sel{border-color:#4f8ef7}
.dup-photo.dropped{opacity:.28;border-color:#e74c3c}
.dup-photo img{width:100%;height:160px;object-fit:cover;display:block}
.dup-photo .dmeta{padding:6px 8px;font-size:.75rem;color:#666;background:#fafafa}
.dup-photo .dmeta strong{display:block;color:#222;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dup-photo .sel-badge{position:absolute;top:6px;left:6px;background:#4f8ef7;color:#fff;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;display:none}
.dup-photo.sel .sel-badge{display:flex}
.dup-actions{display:flex;gap:8px;padding:0 14px 14px;flex-wrap:wrap}
/* AI grid */
.photo-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.pc{border-radius:9px;overflow:hidden;background:#fafafa;border:2px solid transparent;position:relative;transition:border-color .15s,opacity .15s}
.pc.ex{opacity:.3}
.pc img{width:100%;aspect-ratio:1;object-fit:cover;display:block}
.pc .pmeta{padding:7px 9px;font-size:.75rem;color:#666}
.pc .pmeta strong{display:block;color:#222;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip-row{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.chip{padding:4px 8px;border-radius:999px;border:1px solid #d6dbe7;background:#fff;color:#516074;font-size:.72rem;cursor:pointer}
.chip.on{background:#1f7a5a;color:#fff;border-color:#1f7a5a}
.chip.alt{background:#f5f7fb}
.muted{font-size:.8rem;color:#8a8f99}
.ai-badge{position:absolute;top:7px;right:7px;border-radius:20px;padding:2px 8px;font-size:.75rem;font-weight:700;color:#fff}
.ai-h{background:#2ecc71}
.ai-m{background:#f39c12}
.ai-l{background:#e74c3c}
.xbtn{position:absolute;top:7px;left:7px;background:rgba(0,0,0,.52);color:#fff;border:none;border-radius:50%;width:26px;height:26px;font-size:.9rem;cursor:pointer;display:flex;align-items:center;justify-content:center;line-height:1}
.pc.ex .xbtn{background:#e74c3c}
/* Events */
.ev-block{border:1.5px solid #e8e8e8;border-radius:10px;margin-bottom:14px;overflow:hidden}
.ev-hdr{background:#f7f8fa;padding:11px 15px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.ev-hdr strong{font-size:.92rem;min-width:80px}
.ev-hdr input{margin:0;width:260px;font-size:.87rem;padding:6px 10px}
.ev-hdr span{font-size:.82rem;color:#999;margin-left:auto}
.ev-photos{padding:12px;display:flex;flex-wrap:wrap;gap:8px}
.ev-photos img{width:80px;height:80px;object-fit:cover;border-radius:6px}
/* Folder browser */
.field-actions{display:flex;gap:8px;align-items:flex-start}
.field-actions textarea,.field-actions input{flex:1}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.35);display:none;align-items:center;justify-content:center;padding:22px;z-index:20}
.modal.on{display:flex}
.modal-box{background:#fff;border-radius:10px;width:min(720px,100%);max-height:82vh;display:flex;flex-direction:column;box-shadow:0 12px 40px rgba(0,0,0,.22)}
.modal-h{padding:14px 18px;border-bottom:1px solid #eee;display:flex;gap:10px;align-items:center}
.modal-h strong{font-size:.98rem}
.modal-h span{font-size:.78rem;color:#888;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-body{padding:14px 18px;overflow:auto}
.quick{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.dir-row{width:100%;text-align:left;background:#f7f8fa;color:#222;border:1px solid #eceef2;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.dir-row:hover{background:#eef2ff;opacity:1}
.modal-actions{padding:14px 18px;border-top:1px solid #eee;display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
</style>
</head>
<body>
<header>
  <h1>📷 Photo Album Organizer</h1>
  <span>Organize · Deduplicate · AI filter · Export</span>
</header>
<div class="wrap">
  <div class="steps">
    <div class="step on nav" id="st1" onclick="goToStep(1)">1. Setup</div>
    <div class="step" id="st2" onclick="goToStep(2)">2. Duplicates</div>
    <div class="step" id="st3" onclick="goToStep(3)">3. AI Filter</div>
    <div class="step" id="st4" onclick="goToStep(4)">4. Export</div>
  </div>
  <div id="alert" style="display:none" class="alert"></div>

  <!-- 1. Setup -->
  <section class="on" id="s1">
    <div class="card">
      <h2>Project</h2>
      <div class="row3">
        <div>
          <label>Project name</label>
          <input type="text" id="project-name" placeholder="Familie 2026">
        </div>
        <div>
          <label>Albums (one per line)</label>
          <textarea id="albums" placeholder="Zoon 2026&#10;Dochter 2026"></textarea>
        </div>
        <div>
          <label>Project file</label>
          <div class="muted" id="project-file">No project saved yet.</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
            <button class="sm bz" type="button" onclick="loadProject()">Load project</button>
            <button class="sm bp" type="button" onclick="saveProject()">Save project</button>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>Folders</h2>
      <div class="row2">
        <div>
          <label>Input folders (one per line)</label>
          <div class="field-actions">
            <textarea id="in-dirs" placeholder="/Users/jordy/Pictures/fotoboeken&#10;/Users/jordy/Desktop/telefoon-fotos"></textarea>
            <button class="sm bp" type="button" onclick="openNativeFolderPicker('input')">Browse</button>
          </div>
        </div>
        <div>
          <label>Output folder (organized copy)</label>
          <div class="field-actions">
            <input type="text" id="out-dir" placeholder="/Users/jordy/Desktop/album-output">
            <button class="sm bp" type="button" onclick="openNativeFolderPicker('output')">Browse</button>
          </div>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>Options</h2>
      <div class="row2">
        <div>
          <label>Hours gap to start a new event</label>
          <input type="number" id="gap" value="4" min="1" max="72">
        </div>
        <div>
          <label>Claude API key <span style="color:#aaa;font-weight:400">(optional, for AI scoring)</span></label>
          <input type="password" id="apikey" placeholder="sk-ant-…">
        </div>
      </div>
      <label class="toggle"><input type="checkbox" id="opt-loc" checked> Name events by GPS location (OpenStreetMap)</label>
      <label class="toggle"><input type="checkbox" id="opt-ss" checked> Skip screenshots automatically</label>
    </div>
    <div id="scan-status" style="display:none" class="card">
      <div id="scan-msg">Scanning…</div>
      <div class="progress"><div class="pbar" id="spbar" style="width:30%;animation:pulse 1.2s infinite alternate"></div></div>
    </div>
    <button class="bp" id="scan-btn" onclick="startScan()">Scan photos →</button>
    <style>@keyframes pulse{from{opacity:.5}to{opacity:1}}</style>
  </section>

  <!-- 2. Duplicates -->
  <section id="s2">
    <div class="card">
      <h2>Resolve duplicate groups</h2>
      <p style="font-size:.88rem;color:#777;margin-bottom:16px">
        Click photos to select which ones to keep. By default the highest-resolution photo is pre-selected per group.
        You can keep multiple, or delete the whole group.
      </p>
      <div id="dup-list"></div>
      <button class="bg" onclick="submitDuplicates()">Continue →</button>
    </div>
  </section>

  <!-- 3. AI -->
  <section id="s3">
    <div class="card">
      <h2>AI relevance filter</h2>
      <p style="font-size:.88rem;color:#777;margin-bottom:14px">
        Claude rates each photo 1–10 for album-worthiness. Click ✕ to exclude, ↩ to bring back.
      </p>
      <div id="ai-prog" style="display:none;margin-bottom:14px">
        Analysing… <span id="ai-n">0</span>/<span id="ai-tot">0</span>
        <div class="progress"><div class="pbar" id="ai-bar" style="width:0%"></div></div>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
        <button class="sm bz" onclick="autoExclude(4)">Auto-exclude score &lt; 4</button>
        <button class="sm bz" onclick="autoExclude(0)">Reset all exclusions</button>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap" id="album-batch-actions"></div>
      <div class="photo-grid" id="ai-grid"></div>
      <br>
      <button class="bg" onclick="goExport()">Continue to review →</button>
    </div>
  </section>

  <!-- 4. Export -->
  <section id="s4">
    <div class="card">
      <h2>Review events &amp; export</h2>
      <p style="font-size:.88rem;color:#777;margin-bottom:16px">Rename event folders if needed, then export.</p>
      <div id="ev-list"></div>
      <br>
      <button class="bg" onclick="doExport()">Export to output folder →</button>
    </div>
  </section>
</div>

<div class="modal" id="folder-modal">
  <div class="modal-box">
    <div class="modal-h">
      <strong id="folder-title">Choose folder</strong>
      <span id="folder-path"></span>
    </div>
    <div class="modal-body">
      <div class="quick" id="folder-quick"></div>
      <div id="folder-list"></div>
    </div>
    <div class="modal-actions">
      <button class="bz" type="button" onclick="closeFolderBrowser()">Cancel</button>
      <button class="bg" type="button" onclick="chooseCurrentFolder()">Use this folder</button>
    </div>
  </div>
</div>

<script>
let allPhotos = [];
let excluded = new Set();
let dupGroups = [];      // [{photos:[...], decision:{kept:Set}}]
let scanTimer;
let folderMode = 'input';
let currentFolderPath = '';
let albums = [];
let photoLabels = {};
let currentEvents = [];
let currentStep = 1;
let maxUnlockedStep = 1;

function showAlert(msg, type='info'){
  const el = document.getElementById('alert');
  el.textContent = msg; el.style.display='block';
  el.className = 'alert alert-'+type;
}

function albumDefinitionsFromInput(){
  return document.getElementById('albums').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean).map(name=>({name}));
}

function syncProjectFileLabel(value){
  document.getElementById('project-file').textContent = value || 'No project saved yet.';
}

function setStep(n){
  currentStep = n;
  if(n > maxUnlockedStep) maxUnlockedStep = n;
  [1,2,3,4].forEach(i=>{
    const unlocked = i <= maxUnlockedStep;
    document.getElementById('st'+i).className='step'+(unlocked?' nav':'')+(i<n?' ok':i===n?' on':'');
    document.getElementById('s'+i).className='section'+(i===n?' on':'');
    document.getElementById('s'+i).style.display=i===n?'block':'none';
  });
}

function goToStep(n){
  if(n === 1){
    setStep(1);
    return;
  }
  if(n > maxUnlockedStep) return;
  if(n === 2 && !dupGroups.length) return;
  if(n === 3 && !allPhotos.length && !Object.keys(photoLabels).length) return;
  if(n === 4 && !currentEvents.length) return;
  setStep(n);
}

// ── Folder browser ───────────────────────────────────────────────────────────
async function openNativeFolderPicker(mode){
  const r = await fetch('/api/native-folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
  const d = await r.json();
  if(!r.ok){
    showAlert(`${d.error} Opening fallback browser.`, 'warn');
    await openFolderBrowser(mode);
    return;
  }
  if(d.canceled) return;
  applyChosenFolder(mode, d.path);
}

async function openFolderBrowser(mode){
  folderMode = mode;
  document.getElementById('folder-title').textContent = mode === 'input' ? 'Choose input folder' : 'Choose output folder';
  document.getElementById('folder-modal').classList.add('on');
  const existing = mode === 'output' ? document.getElementById('out-dir').value.trim() : '';
  await loadFolder(existing);
}

function closeFolderBrowser(){
  document.getElementById('folder-modal').classList.remove('on');
}

async function loadFolder(path=''){
  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  const r = await fetch(url);
  const d = await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}

  currentFolderPath = d.path;
  document.getElementById('folder-path').textContent = d.path;

  const quick = document.getElementById('folder-quick');
  quick.innerHTML = d.quick.map(q=>`<button class="sm bz" type="button" onclick="loadFolder('${escapeJs(q.path)}')">${escapeHtml(q.label)}</button>`).join('');

  const list = document.getElementById('folder-list');
  const up = d.parent ? `<button class="dir-row" type="button" onclick="loadFolder('${escapeJs(d.parent)}')"><span>..</span><span>Up</span></button>` : '';
  const rows = d.dirs.map(dir=>`
    <button class="dir-row" type="button" onclick="loadFolder('${escapeJs(dir.path)}')">
      <span>${escapeHtml(dir.name)}</span><span>Open</span>
    </button>`).join('');
  list.innerHTML = up + (rows || '<p style="color:#999;font-size:.88rem">No subfolders found.</p>');
}

function chooseCurrentFolder(){
  applyChosenFolder(folderMode, currentFolderPath);
  closeFolderBrowser();
}

function applyChosenFolder(mode, path){
  if(mode === 'output'){
    document.getElementById('out-dir').value = path;
  } else {
    const input = document.getElementById('in-dirs');
    const existing = input.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
    if(!existing.includes(path)) existing.push(path);
    input.value = existing.join('\n');
  }
}

function escapeHtml(value){
  return String(value).replace(/[&<>"']/g, ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function escapeJs(value){
  return String(value).replace(/\\/g,'\\\\').replace(/'/g,"\\'");
}

// ── Step 1: Scan ──────────────────────────────────────────────────────────────
async function startScan(){
  const projectName=document.getElementById('project-name').value.trim();
  const albumDefs=albumDefinitionsFromInput();
  const inputDirs=document.getElementById('in-dirs').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean);
  const outDir=document.getElementById('out-dir').value.trim();
  if(!projectName||!albumDefs.length||!inputDirs.length||!outDir){showAlert('Fill in project, albums, source folders, and output folder.','err');return;}

  document.getElementById('scan-btn').disabled=true;
  document.getElementById('scan-status').style.display='block';
  document.getElementById('scan-msg').textContent='Starting…';

  const r = await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      project_name:projectName,
      albums:albumDefs,
      input_dirs:inputDirs, output_dir:outDir,
      gap_hours:parseFloat(document.getElementById('gap').value)||4,
      use_locations:document.getElementById('opt-loc').checked,
      skip_screenshots:document.getElementById('opt-ss').checked,
      api_key:document.getElementById('apikey').value.trim()
    })
  });
  if(!r.ok){showAlert((await r.json()).error,'err');document.getElementById('scan-btn').disabled=false;return;}
  const payload = await r.json();
  syncProjectFileLabel(payload.project_file || '');
  scanTimer = setInterval(pollScan, 900);
}

async function pollScan(){
  const r = await fetch('/api/scan-progress');
  const d = await r.json();
  document.getElementById('scan-msg').textContent = d.message;
  if(d.status==='done'){
    clearInterval(scanTimer);
    document.getElementById('scan-status').style.display='none';
    document.getElementById('scan-btn').disabled=false;
    if(d.dup_groups.length>0){
      dupGroups = d.dup_groups.map(g=>({photos:g, kept:new Set([g[0].id])})); // pre-select best (first=highest res)
      renderDups();
      setStep(2);
    } else {
      allPhotos = [];  // will be populated after resolve
      await resolveDuplicates({});
    }
  } else if(d.status==='error'){
    clearInterval(scanTimer);
    showAlert(d.message,'err');
    document.getElementById('scan-btn').disabled=false;
    document.getElementById('scan-status').style.display='none';
  }
}

// ── Step 2: Duplicates ────────────────────────────────────────────────────────
function renderDups(){
  const list=document.getElementById('dup-list');
  list.innerHTML='';
  if(!dupGroups.length){list.innerHTML='<p style="color:#aaa">No ambiguous duplicates.</p>';return;}
  dupGroups.forEach((grp,gi)=>{
    const photos=grp.photos;
    const thumbs=photos.map(p=>`
      <div class="dup-photo ${grp.kept.has(p.id)?'sel':''}" id="dp-${gi}-${p.id}" onclick="toggleDup(${gi},'${p.id}')">
        <div class="sel-badge">✓</div>
        <img src="data:image/jpeg;base64,${p.thumb}" alt="${p.name}">
        <div class="dmeta"><strong>${p.name}</strong>${p.resolution} · ${p.size_kb} KB<br>${p.date}</div>
      </div>`).join('');
    list.innerHTML+=`
      <div class="dup-group">
        <div class="dup-group-header">
          <span><strong>${photos.length} similar photos</strong> — click to select which to keep</span>
          <div style="display:flex;gap:7px">
            <button class="sm bp" onclick="keepBest(${gi})">Keep best only</button>
            <button class="sm bz" onclick="keepAll(${gi})">Keep all</button>
            <button class="sm br" onclick="keepNone(${gi})">Delete all</button>
          </div>
        </div>
        <div class="dup-photos" id="dg-${gi}">${thumbs}</div>
      </div>`;
  });
}

function toggleDup(gi, pid){
  const grp=dupGroups[gi];
  if(grp.kept.has(pid)) grp.kept.delete(pid);
  else grp.kept.add(pid);
  grp.photos.forEach(p=>{
    const el=document.getElementById(`dp-${gi}-${p.id}`);
    if(el) el.className='dup-photo'+(grp.kept.has(p.id)?' sel':'');
  });
}
function keepBest(gi){
  const grp=dupGroups[gi];
  grp.kept=new Set([grp.photos[0].id]);
  renderDups();
}
function keepAll(gi){
  const grp=dupGroups[gi];
  grp.kept=new Set(grp.photos.map(p=>p.id));
  renderDups();
}
function keepNone(gi){
  dupGroups[gi].kept=new Set();
  renderDups();
}

async function submitDuplicates(){
  const decisions={};
  dupGroups.forEach((grp,gi)=>{
    decisions[gi]=[...grp.kept];
  });
  await resolveDuplicates(decisions);
}

async function resolveDuplicates(decisions){
  const r=await fetch('/api/resolve-duplicates',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({decisions})});
  const d=await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}
  await loadAI();
}

// ── Step 3: AI ────────────────────────────────────────────────────────────────
async function loadAI(){
  setStep(3);
  const info=await (await fetch('/api/ai-scores')).json();
  if(info.has_api_key){
    document.getElementById('ai-prog').style.display='block';
    fetch('/api/run-ai',{method:'POST'});
    pollAI();
  } else {
    showAlert('No API key set — photos shown without AI scores. Manually exclude what you don\'t want.','warn');
    const prog=await (await fetch('/api/ai-progress')).json();
    albums = prog.albums || [];
    allPhotos=prog.photos;
    allPhotos.forEach(p=>{photoLabels[p.id]=p.labels;});
    document.getElementById('ai-tot').textContent=allPhotos.length;
    renderAIGrid();
  }
}

let aiTimer;
async function pollAI(){
  const d=await (await fetch('/api/ai-progress')).json();
  document.getElementById('ai-n').textContent=d.done;
  document.getElementById('ai-tot').textContent=d.total;
  document.getElementById('ai-bar').style.width=(d.total?d.done/d.total*100:0)+'%';
  albums = d.albums || [];
  allPhotos=d.photos;
  allPhotos.forEach(p=>{photoLabels[p.id]=p.labels;});
  renderAIGrid();
  if(!d.finished) aiTimer=setTimeout(pollAI,1600);
  else{
    document.getElementById('ai-prog').style.display='none';
    showAlert('AI analysis complete.','ok');
  }
}

function renderAIGrid(){
  const grid=document.getElementById('ai-grid');
  grid.innerHTML='';
  renderAlbumBatchActions();
  allPhotos.forEach(p=>{
    const labels = photoLabels[p.id] || {excluded:false, albums: albums.map(a=>a.id)};
    const ex=Boolean(labels.excluded) || excluded.has(p.id);
    const s=p.ai_score;
    const cls=s==null?'':s>=7?'ai-h':s>=4?'ai-m':'ai-l';
    const badge=s!=null?`<div class="ai-badge ${cls}">${s}/10</div>`:'';
    const reason=p.ai_reason?`<div style="font-size:.72rem;color:#999;margin-top:2px">${p.ai_reason}</div>`:'';
    const chips = albums.map(album=>`<button class="chip ${isExclusiveAlbumSelection(labels, album.id)?'on':'alt'}" type="button" onclick="assignPhotoToAlbum('${p.id}','${album.id}')">${escapeHtml(album.name)}</button>`).join('');
    const sharedChip = albums.length > 1 ? `<button class="chip ${isAllAlbumsSelection(labels)?'on':'alt'}" type="button" onclick="assignPhotoToAllAlbums('${p.id}')">Beiden</button>` : '';
    grid.innerHTML+=`
      <div class="pc ${ex?'ex':''}" id="pc-${p.id}">
        <img src="data:image/jpeg;base64,${p.thumb}" alt="${p.name}">
        ${badge}
        <button class="xbtn" onclick="toggleEx('${p.id}')" title="${ex?'Include':'Exclude'}">${ex?'↩':'✕'}</button>
        <div class="pmeta"><strong>${p.name}</strong><div>${p.date}</div>${reason}<div class="chip-row">${chips}${sharedChip}</div></div>
      </div>`;
  });
}

function renderAlbumBatchActions(){
  const wrap = document.getElementById('album-batch-actions');
  if(!wrap) return;
  const buttons = albums.map(album=>`<button class="sm bp" type="button" onclick="applyAlbumPresetToVisible('${album.id}')">${escapeHtml(album.name)} only</button>`).join('');
  const allAlbums = albums.length ? `<button class="sm bz" type="button" onclick="applyAllAlbumsToVisible()">All albums</button>` : '';
  wrap.innerHTML = buttons + allAlbums;
}

function toggleEx(id){
  if(!photoLabels[id]) photoLabels[id] = {excluded:false, albums: albums.map(a=>a.id)};
  photoLabels[id].excluded = !photoLabels[id].excluded;
  photoLabels[id].albums = photoLabels[id].albums || [];
  const card=document.getElementById('pc-'+id);
  card.classList.toggle('ex');
  card.querySelector('.xbtn').textContent=photoLabels[id].excluded?'↩':'✕';
  persistPhotoLabels();
}

function isExclusiveAlbumSelection(labels, albumId){
  const selected = labels.albums || [];
  return selected.length === 1 && selected[0] === albumId;
}

function isAllAlbumsSelection(labels){
  const selected = new Set(labels.albums || []);
  return albums.length > 1 && albums.every(album => selected.has(album.id));
}

function assignPhotoToAlbum(photoId, albumId){
  if(!photoLabels[photoId]) photoLabels[photoId] = {excluded:false, albums: albums.map(a=>a.id)};
  photoLabels[photoId].albums = [albumId];
  photoLabels[photoId].excluded = false;
  renderAIGrid();
  persistPhotoLabels();
}

function assignPhotoToAllAlbums(photoId){
  const allAlbumIds = albums.map(album=>album.id);
  if(!photoLabels[photoId]) photoLabels[photoId] = {excluded:false, albums: allAlbumIds};
  photoLabels[photoId].albums = [...allAlbumIds];
  photoLabels[photoId].excluded = false;
  renderAIGrid();
  persistPhotoLabels();
}

function applyAlbumPresetToVisible(albumId){
  allPhotos.forEach(photo=>{
    if(!photoLabels[photo.id]) photoLabels[photo.id] = {excluded:false, albums: albums.map(a=>a.id)};
    photoLabels[photo.id].albums = [albumId];
    photoLabels[photo.id].excluded = false;
  });
  renderAIGrid();
  persistPhotoLabels();
}

function applyAllAlbumsToVisible(){
  const allAlbumIds = albums.map(album=>album.id);
  allPhotos.forEach(photo=>{
    if(!photoLabels[photo.id]) photoLabels[photo.id] = {excluded:false, albums: allAlbumIds};
    photoLabels[photo.id].albums = [...allAlbumIds];
    photoLabels[photo.id].excluded = false;
  });
  renderAIGrid();
  persistPhotoLabels();
}

async function persistPhotoLabels(){
  await fetch('/api/photo-labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({photo_labels:photoLabels})});
}

function autoExclude(threshold){
  if(threshold===0){allPhotos.forEach(p=>{if(photoLabels[p.id]) photoLabels[p.id].excluded=false;});}
  else{allPhotos.forEach(p=>{if(p.ai_score!=null&&p.ai_score<threshold){if(!photoLabels[p.id]) photoLabels[p.id]={excluded:false, albums: albums.map(a=>a.id)}; photoLabels[p.id].excluded=true;}});}
  renderAIGrid();
  persistPhotoLabels();
}

// ── Step 4: Export ────────────────────────────────────────────────────────────
async function goExport(){
  setStep(4);
  showAlert('Grouping photos into events…','info');
  const keptIds=allPhotos.filter(p=>!(photoLabels[p.id]?.excluded)).map(p=>p.id);
  const r=await fetch('/api/events',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({kept_ids:keptIds})});
  const d=await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}
  renderEvents(d.events);
  showAlert(`${d.total} photos ready in ${d.events.length} events.`,'ok');
}

let deletedEvents = new Set();

function renderEvents(events){
  currentEvents = events;
  const list=document.getElementById('ev-list');
  list.innerHTML='';
  events.forEach(ev=>{
    const del=deletedEvents.has(ev.key);
    const thumbs=ev.photos.slice(0,10).map(p=>`<img src="data:image/jpeg;base64,${p.thumb}" title="${p.name}">`).join('');
    const more=ev.photos.length>10?`<span style="align-self:center;color:#bbb;font-size:.8rem">+${ev.photos.length-10}</span>`:'';
    const assignButtons = albums.map(album=>`<button class="sm bz" type="button" onclick="assignEventToAlbum('${ev.key}','${album.id}')">${escapeHtml(album.name)} only</button>`).join('');
    const sharedButton = albums.length > 1 ? `<button class="sm bp" type="button" onclick="assignEventToAllAlbums('${ev.key}')">All albums</button>` : '';
    list.innerHTML+=`
      <div class="ev-block" id="evb-${btoa(ev.key)}" style="${del?'opacity:.3;pointer-events:none':''}">
        <div class="ev-hdr">
          <input type="text" class="ev-rename" data-key="${ev.key}" value="${ev.key}" title="Rename folder" style="font-weight:600">
          <span>${ev.photos.length} photos</span>
          <button class="sm br" onclick="toggleDeleteEvent('${ev.key}')" style="pointer-events:auto">${del?'↩ Restore':'✕ Skip'}</button>
        </div>
        <div style="padding:10px 12px 0;display:flex;gap:8px;flex-wrap:wrap">${assignButtons}${sharedButton}</div>
        <div class="ev-photos">${thumbs}${more}</div>
      </div>`;
  });
}

function toggleDeleteEvent(key){
  const id='evb-'+btoa(key);
  const block=document.getElementById(id);
  if(deletedEvents.has(key)){
    deletedEvents.delete(key);
    block.style.opacity='1';
    block.style.pointerEvents='';
    block.querySelector('.br').textContent='✕ Skip';
  } else {
    deletedEvents.add(key);
    block.style.opacity='.3';
    block.querySelector('.br').textContent='↩ Restore';
  }
}

function assignEventToAlbum(eventKey, albumId){
  const event = currentEvents.find(item => item.key === eventKey);
  if(!event) return;
  event.photos.forEach(photo=>{
    if(!photoLabels[photo.id]) photoLabels[photo.id] = {excluded:false, albums: albums.map(a=>a.id)};
    photoLabels[photo.id].albums = [albumId];
    photoLabels[photo.id].excluded = false;
  });
  persistPhotoLabels();
  showAlert(`Assigned ${event.photos.length} photos from ${eventKey} to one album.`, 'ok');
}

function assignEventToAllAlbums(eventKey){
  const event = currentEvents.find(item => item.key === eventKey);
  if(!event) return;
  const allAlbumIds = albums.map(album=>album.id);
  event.photos.forEach(photo=>{
    if(!photoLabels[photo.id]) photoLabels[photo.id] = {excluded:false, albums: allAlbumIds};
    photoLabels[photo.id].albums = [...allAlbumIds];
    photoLabels[photo.id].excluded = false;
  });
  persistPhotoLabels();
  showAlert(`Assigned ${event.photos.length} photos from ${eventKey} to all albums.`, 'ok');
}

async function doExport(){
  showAlert('Exporting…','info');
  const renames={};
  document.querySelectorAll('.ev-rename').forEach(el=>{
    const orig=el.dataset.key;
    if(el.value.trim() && el.value.trim()!==orig) renames[orig]=el.value.trim();
  });
  const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({renames,deleted_events:[...deletedEvents],photo_labels:photoLabels})});
  const d=await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}
  showAlert(`Done! Copied ${d.total} photos into ${d.events} event folders across ${d.albums} album exports → ${d.output_dir}`,'ok');
}

async function saveProject(){
  const payload = {
    project_name: document.getElementById('project-name').value.trim(),
    albums: albumDefinitionsFromInput(),
    input_dirs: document.getElementById('in-dirs').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean),
    output_dir: document.getElementById('out-dir').value.trim(),
    gap_hours: parseFloat(document.getElementById('gap').value)||4,
    use_locations: document.getElementById('opt-loc').checked
  };
  const r = await fetch('/api/project',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d = await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}
  syncProjectFileLabel(d.project_file);
  showAlert('Project saved.','ok');
}

async function loadProject(){
  const outputDir = document.getElementById('out-dir').value.trim();
  if(!outputDir){showAlert('Choose an output folder first.','err');return;}
  const r = await fetch(`/api/project?output_dir=${encodeURIComponent(outputDir)}`);
  const d = await r.json();
  if(!r.ok){showAlert(d.error,'err');return;}
  document.getElementById('project-name').value = d.project_name;
  document.getElementById('albums').value = d.albums.map(album=>album.name).join('\n');
  document.getElementById('in-dirs').value = d.input_dirs.join('\n');
  document.getElementById('out-dir').value = d.output_dir;
  document.getElementById('gap').value = d.gap_hours;
  document.getElementById('opt-loc').checked = d.use_locations;
  syncProjectFileLabel(d.project_file);
  showAlert('Project loaded. Run a scan to restore the photo list.','ok');
}

setStep(1);
</script>
</body>
</html>
"""

# ── Launch ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 5050
    print(f"\nPhoto Album Organizer → http://localhost:{port}")
    threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{port}")).start()
    app.run(port=port, debug=False)
