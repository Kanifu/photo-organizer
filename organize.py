#!/usr/bin/python3
"""
Photo Organizer — v2
Core pipeline: scan, deduplicate, date-parse, cluster by time+location, copy.
"""

import json
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import imagehash
from PIL import Image

# ── Config ────────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".tiff", ".tif"}
DEFAULT_EVENT_GAP_HOURS = 4
DUPLICATE_HASH_THRESHOLD = 8

EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
EXIF_DATE_TAG_IDS = {
    36867: "DateTimeOriginal",
    36868: "DateTimeDigitized",
    306:   "DateTime",
}

# File name patterns to skip (screenshots, etc.)
SKIP_PATTERNS = [
    re.compile(r"^screenshot", re.IGNORECASE),
    re.compile(r"^screen shot", re.IGNORECASE),
]

# Filename date patterns (e.g. 20260423_143946 or 2026-04-23)
FILENAME_DATE_PATTERNS = [
    (re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"), "%Y%m%d%H%M%S"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"),                      "%Y-%m-%d"),
    (re.compile(r"(\d{4})(\d{2})(\d{2})"),                        "%Y%m%d"),
]

# ── Screenshot filter ─────────────────────────────────────────────────────────

def is_screenshot(path: Path) -> bool:
    return any(p.match(path.name) for p in SKIP_PATTERNS)


# ── EXIF: date ────────────────────────────────────────────────────────────────

def read_exif_date(path: Path) -> Optional[datetime]:
    try:
        with Image.open(path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return None
            for tag_id in EXIF_DATE_TAG_IDS:
                if tag_id in exif_data:
                    raw = str(exif_data[tag_id]).strip()
                    try:
                        return datetime.strptime(raw, EXIF_DATE_FORMAT)
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def parse_date_from_filename(path: Path) -> Optional[datetime]:
    """Try to extract a date from the filename itself."""
    name = path.stem
    for pattern, fmt in FILENAME_DATE_PATTERNS:
        m = pattern.search(name)
        if m:
            date_str = "".join(m.groups())
            try:
                return datetime.strptime(date_str, fmt.replace("-", "").replace("_", ""))
            except ValueError:
                continue
    return None


def get_photo_date(path: Path) -> Tuple[datetime, str]:
    """Return (date, source) where source is 'exif', 'filename', or 'filedate'."""
    d = read_exif_date(path)
    if d:
        return d, "exif"
    d = parse_date_from_filename(path)
    if d:
        return d, "filename"
    return datetime.fromtimestamp(path.stat().st_mtime), "filedate"


# ── EXIF: GPS ─────────────────────────────────────────────────────────────────

def read_gps_from_exif(path: Path) -> Optional[Tuple[float, float]]:
    """Return (lat, lon) in decimal degrees, or None."""
    try:
        with Image.open(path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return None
            gps_info = exif_data.get(34853)  # GPSInfo IFD tag
            if not gps_info:
                return None

            def to_decimal(values, ref):
                d, m, s = [float(v) for v in values]
                dec = d + m / 60 + s / 3600
                if ref in ("S", "W"):
                    dec = -dec
                return dec

            lat = to_decimal(gps_info[2], gps_info[1])
            lon = to_decimal(gps_info[4], gps_info[3])
            return lat, lon
    except Exception:
        return None


# ── Reverse geocoding (Nominatim/OSM, no API key needed) ─────────────────────

_geocode_cache: Dict[Tuple, Optional[str]] = {}


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Return a clean place name for coordinates, or None."""
    key = (round(lat, 2), round(lon, 2))  # ~1km grid
    if key in _geocode_cache:
        return _geocode_cache[key]

    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&zoom=12"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "photo-organizer/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        address = data.get("address", {})
        place = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
            or address.get("country")
        )
        if place:
            # Sanitize for use in folder name
            place = re.sub(r"[^\w\s-]", "", place).strip().lower().replace(" ", "-")
        _geocode_cache[key] = place
        time.sleep(1.1)  # Nominatim: max 1 req/sec
        return place
    except Exception:
        _geocode_cache[key] = None
        return None


def get_location_name_for_event(photos: List[Path]) -> Optional[str]:
    """Best location name for a group of photos."""
    places = []
    for path in photos:
        gps = read_gps_from_exif(path)
        if gps:
            place = reverse_geocode(*gps)
            if place:
                places.append(place)
    if not places:
        return None
    return Counter(places).most_common(1)[0][0]


# ── Perceptual hashing / deduplication ───────────────────────────────────────

def compute_hash(path: Path):
    try:
        with Image.open(path) as img:
            return imagehash.phash(img)
    except Exception:
        return None


def get_resolution(path: Path) -> int:
    try:
        with Image.open(path) as img:
            return img.size[0] * img.size[1]
    except Exception:
        return 0


def deduplicate(
    photos: List[Path],
    threshold: int = DUPLICATE_HASH_THRESHOLD,
    interactive: bool = False,
) -> List[Path]:
    print(f"  Deduplicating {len(photos)} photos...")
    hashes: Dict = {}
    kept: List[Path] = []
    removed = 0

    for path in photos:
        h = compute_hash(path)
        if h is None:
            kept.append(path)
            continue

        duplicate_of = None
        for existing_hash, existing_path in hashes.items():
            if h - existing_hash <= threshold:
                duplicate_of = existing_path
                break

        if duplicate_of is None:
            hashes[h] = path
            kept.append(path)
        else:
            new_res = get_resolution(path)
            old_res = get_resolution(duplicate_of)

            if new_res > old_res * 1.1:
                # New is clearly better
                kept.remove(duplicate_of)
                kept.append(path)
                old_h = compute_hash(duplicate_of)
                if old_h in hashes:
                    del hashes[old_h]
                hashes[h] = path
                removed += 1
            elif old_res > new_res * 1.1:
                # Existing is clearly better
                removed += 1
            elif interactive:
                # Too close to call — ask the user
                print(f"\n  Ambiguous duplicate:")
                print(f"    [1] {duplicate_of.name}  ({old_res // 1000}K px, {duplicate_of.stat().st_size // 1024} KB)")
                print(f"    [2] {path.name}  ({new_res // 1000}K px, {path.stat().st_size // 1024} KB)")
                choice = input("  Keep which? [1/2/b=both, default=1]: ").strip().lower()
                if choice == "2":
                    kept.remove(duplicate_of)
                    kept.append(path)
                    old_h = compute_hash(duplicate_of)
                    if old_h in hashes:
                        del hashes[old_h]
                    hashes[h] = path
                    removed += 1
                elif choice == "b":
                    kept.append(path)
                    hashes[h] = path
                else:
                    removed += 1  # keep existing, discard new
            else:
                # Auto: keep existing (slightly higher or equal res)
                removed += 1

    print(f"  Removed {removed} duplicates, kept {len(kept)}")
    return kept


# ── Event clustering ──────────────────────────────────────────────────────────

def cluster_into_events(
    photos_with_dates: List[Tuple[Path, datetime]],
    gap_hours: float,
    use_locations: bool = True,
) -> Tuple[Dict[str, List[Path]], List[Tuple[datetime, List[Path]]]]:
    if not photos_with_dates:
        return {}, []

    sorted_photos = sorted(photos_with_dates, key=lambda x: x[1])
    gap = timedelta(hours=gap_hours)

    raw_clusters: List[Tuple[datetime, List[Path]]] = []
    current_start: Optional[datetime] = None
    current_photos: List[Tuple[Path, datetime]] = []
    prev_date: Optional[datetime] = None

    for path, date in sorted_photos:
        if current_start is None:
            current_start = date
            current_photos = [(path, date)]
        elif prev_date is not None and date - prev_date > gap:
            raw_clusters.append((current_start, [p for p, _ in current_photos]))
            current_start = date
            current_photos = [(path, date)]
        else:
            current_photos.append((path, date))
        prev_date = date

    if current_photos:
        raw_clusters.append((current_start, [p for p, _ in current_photos]))

    # Name events
    events: Dict[str, List[Path]] = {}
    index = 1
    location_count = 0

    for event_start, photos in raw_clusters:
        date_str = event_start.strftime("%Y-%m-%d")

        if use_locations:
            gps_photos = [p for p in photos if read_gps_from_exif(p)]
            if gps_photos:
                location_count += 1

        # Build key — location lookup happens in run() with a progress message
        key = f"{date_str}_event{index:02d}"
        events[key] = photos
        index += 1

    return events, raw_clusters


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(
    input_dir: Path,
    output_dir: Path,
    gap_hours: float = DEFAULT_EVENT_GAP_HOURS,
    dry_run: bool = False,
    skip_screenshots: bool = True,
    use_locations: bool = True,
    interactive: bool = False,
):
    print(f"\nPhoto Organizer v2")
    print(f"  Input:  {input_dir}")
    print(f"  Output: {output_dir}")
    print(f"  Event gap: {gap_hours}h | Screenshots: {'skip' if skip_screenshots else 'include'} | "
          f"Locations: {'yes' if use_locations else 'no'} | Dry run: {dry_run}\n")

    # 1. Scan
    print("Step 1/5: Scanning photos...")
    all_photos = [
        p for p in input_dir.rglob("*")
        if p.suffix.lower() in SUPPORTED_EXTENSIONS and p.is_file()
    ]

    if skip_screenshots:
        skipped = [p for p in all_photos if is_screenshot(p)]
        all_photos = [p for p in all_photos if not is_screenshot(p)]
        if skipped:
            print(f"  Skipped {len(skipped)} screenshots")

    print(f"  Found {len(all_photos)} photos")
    if not all_photos:
        print("No photos found. Check the input folder.")
        sys.exit(1)

    # 2. Deduplicate
    print("\nStep 2/5: Deduplicating...")
    unique_photos = deduplicate(all_photos, interactive=interactive)

    # 3. Read dates
    print("\nStep 3/5: Reading dates...")
    source_counts = Counter()
    photos_with_dates: List[Tuple[Path, datetime]] = []
    for path in unique_photos:
        date, source = get_photo_date(path)
        source_counts[source] += 1
        photos_with_dates.append((path, date))

    print(f"  Date sources: {source_counts['exif']} from EXIF, "
          f"{source_counts['filename']} from filename, "
          f"{source_counts['filedate']} from file date (fallback)")
    dates = [d for _, d in photos_with_dates]
    print(f"  Date range: {min(dates).strftime('%Y-%m-%d')} -> {max(dates).strftime('%Y-%m-%d')}")

    # 4. Cluster
    print(f"\nStep 4/5: Clustering into events (gap = {gap_hours}h)...")
    events, raw_clusters = cluster_into_events(photos_with_dates, gap_hours, use_locations)
    print(f"  Found {len(raw_clusters)} events")

    # 5. Name events by location (if enabled)
    named_events: Dict[str, List[Path]] = {}
    if use_locations:
        gps_count = sum(1 for p, _ in photos_with_dates if read_gps_from_exif(p))
        if gps_count > 0:
            print(f"\nStep 5/5: Looking up locations ({gps_count} photos have GPS)...")
            index = 1
            used_keys = set()
            for event_start, photos in raw_clusters:
                date_str = event_start.strftime("%Y-%m-%d")
                place = get_location_name_for_event(photos)
                if place:
                    base_key = f"{date_str}_{place}"
                else:
                    base_key = f"{date_str}_event{index:02d}"

                # Ensure uniqueness
                key = base_key
                suffix = 2
                while key in used_keys:
                    key = f"{base_key}_{suffix}"
                    suffix += 1
                used_keys.add(key)

                print(f"  {key}/ ({len(photos)} photos)")
                named_events[key] = photos
                index += 1
        else:
            print("\nStep 5/5: No GPS data found — using date-based names.")
            named_events = {k: v for k, v in events.items()}
    else:
        print("\nStep 5/5: Skipping location lookup.")
        named_events = {k: v for k, v in events.items()}

    # 6. Copy
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Copying photos...")
    total_copied = 0
    for event_key, photos in sorted(named_events.items()):
        event_dir = output_dir / event_key
        if not dry_run:
            event_dir.mkdir(parents=True, exist_ok=True)
        for i, src in enumerate(photos, start=1):
            dest = event_dir / f"{i:03d}_{src.name}"
            if not dry_run:
                shutil.copy2(src, dest)
            total_copied += 1

    print(f"\nDone! {'Would copy' if dry_run else 'Copied'} {total_copied} photos "
          f"into {len(named_events)} events.")
    if not dry_run:
        print(f"Output: {output_dir}")

    return named_events
