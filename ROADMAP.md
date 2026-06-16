# Roadmap

## Goal

Create a reliable local tool that turns scattered personal photos from local folders and cloud sources into a strong, reviewable selection for an Albelli photo album.

## Phase 1 - Stabilize The Local App

- Add automated tests for date parsing, event clustering, duplicate grouping, and export behavior. Started with core scan/date/cluster tests and web scan API tests.
- Make duplicate detection safer by separating "exact duplicate", "likely duplicate", and "similar moment".
- Add a persistent project/session file so users can pause and continue a selection.
- Improve folder validation, error messages, and empty-state handling.
- Replace the single embedded HTML string with proper templates or a small frontend structure.
- Add clear install and run documentation.

## Phase 2 - Multiple Local Sources

- Allow users to add multiple local folders in one project. Implemented for the current web UI, API, CLI, and core scan pipeline.
- Track source folder per photo.
- Detect duplicate files across folders.
- Add include/exclude rules per source.
- Store scan results so a large library does not need to be fully rescanned every time.

## Phase 3 - Google Drive Import

- Add Google Drive as a source through OAuth.
- Support selecting specific Drive folders.
- Cache metadata locally and download thumbnails or originals only when needed.
- Clearly separate local-only processing from cloud download actions.
- Add privacy-first messaging around what data is accessed and stored.

## Phase 4 - Album Curation

- Add target album settings: theme, date range, max pages, max photos, preferred density.
- Rank photos by quality, uniqueness, faces/people, events, and user selections.
- Create a balanced shortlist across events instead of only selecting the highest-scored photos.
- Add manual review views for "must keep", "maybe", and "discard".
- Export a final folder with stable ordering and optional CSV/JSON manifest.

## Phase 5 - Albelli Preparation

- Research Albelli import constraints and supported workflows.
- Export photo sets with naming and ordering suitable for Albelli upload.
- Generate suggested page groups by event.
- Optionally generate an editable album plan with spreads, captions, and cover suggestions.
- Only consider direct Albelli integration if an official, stable integration path exists.

## Technical Principles

- Never mutate original photos.
- Prefer explicit user review over silent deletion.
- Keep cloud access optional and transparent.
- Keep AI suggestions explainable and overridable.
- Make the pipeline resumable before adding more automation.
