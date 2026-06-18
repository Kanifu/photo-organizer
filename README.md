# Photo Organizer

Local photo organizer for preparing a clean set of photos for a printed photo album.

The current prototype scans one or more local folders, filters screenshots, groups similar photos, optionally scores photos with Claude, clusters selected photos into date-based events, and exports copies into event folders. It now supports a saved project with multiple target albums such as `Zoon 2026` and `Dochter 2026`. Original files are not moved or deleted.

## Current Features

- Multi-folder local scan for `.jpg`, `.jpeg`, `.png`, `.heic`, `.tiff`, and `.tif`.
- Saved project file in the output folder so a curation session can be resumed.
- Multiple target albums within one project, with per-photo album assignment.
- Native macOS folder picker for choosing input and output folders, with an in-app browser fallback.
- Screenshot filtering based on filename.
- Perceptual-hash duplicate grouping.
- Manual duplicate review in the web UI.
- Optional Claude photo scoring with an API key.
- Event grouping based on photo dates.
- Optional GPS-based location names through OpenStreetMap Nominatim.
- Export to a clean folder structure for manual review.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run The Web UI

```bash
python app.py
```

Open:

```text
http://127.0.0.1:5050
```

## Run The CLI Wizard

```bash
python album.py
```

## Run Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Important Notes

- The app is meant to run locally. Do not expose it publicly.
- Originals are copied, not modified.
- Duplicate detection should be treated carefully. The web UI asks for duplicate review, but the CLI can still auto-resolve likely duplicates.
- The output folder must be outside every input folder. This avoids rescanning exported album copies in later runs.
- Project settings are saved to `photo-organizer-project.json` inside the output folder.
- HEIC support may require extra Pillow support depending on the local Python environment.
- Claude scoring is optional and only runs when an API key is entered in the web UI.
- Only Anthropic/Claude API keys are supported today. Other AI providers need a provider abstraction because every vision API has its own SDK, model names, request format, response format, and pricing behavior.
- Google Drive support is planned but not implemented yet. It should be added with explicit OAuth consent and local token handling.

## Product Direction

The intended product goal is to help produce a high-quality curated photo set for an Albelli photo album. The first milestone should be a reliable local curation workflow. After that, the app can add Google Drive imports, multiple source folders, quality scoring, stronger duplicate handling, and eventually album-layout export or assisted composition.

See [ROADMAP.md](ROADMAP.md) for the development plan.
