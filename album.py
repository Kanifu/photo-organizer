#!/usr/bin/python3
"""
Photo Album Wizard — interactive interface for the photo organizer.
Run: python3 album.py
"""

import sys
from pathlib import Path

from organize import DEFAULT_EVENT_GAP_HOURS, run

# ── Helpers ───────────────────────────────────────────────────────────────────

def ask(prompt: str, default: str = "") -> str:
    display = f"{prompt} [{default}]: " if default else f"{prompt}: "
    try:
        answer = input(display).strip()
        return answer if answer else default
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(0)


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} [{hint}]").lower()
    if not answer:
        return default
    return answer.startswith("y")


def ask_folder(prompt: str, must_exist: bool = False) -> Path:
    while True:
        raw = ask(prompt)
        path = Path(raw).expanduser().resolve()
        if must_exist and not path.exists():
            print(f"  Folder not found: {path}")
            print("  Try again (tip: drag the folder into the terminal to paste the path)")
        else:
            return path


def ask_input_folders() -> list[Path]:
    folders = [ask_folder("Input folder (where your photos are)", must_exist=True)]
    while ask_yes_no("Add another input folder?", default=False):
        folders.append(ask_folder("Extra input folder", must_exist=True))
    return folders


def print_header():
    print()
    print("╔══════════════════════════════════════╗")
    print("║       Photo Album Organizer v2       ║")
    print("╚══════════════════════════════════════╝")
    print()


# ── Wizard ────────────────────────────────────────────────────────────────────

def wizard():
    print_header()
    print("This tool will scan your photos, remove duplicates, group them")
    print("into events by date and location, and copy them to an output folder.")
    print("Your originals are never moved or deleted.\n")

    # Input folders
    print("── Step 1: Folders ─────────────────────────────────────────────")
    input_dirs = ask_input_folders()
    output_dir = ask_folder("Output folder (where to put the organized photos)")

    if any(output_dir == input_dir or output_dir.is_relative_to(input_dir) for input_dir in input_dirs):
        print("Error: output must be outside every input folder.")
        sys.exit(1)

    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"\nWarning: output folder already has files: {output_dir}")
        if not ask_yes_no("Continue anyway?", default=False):
            sys.exit(0)

    # Options
    print("\n── Step 2: Options ─────────────────────────────────────────────")
    gap_hours = float(ask(
        "Hours gap between photos to start a new event",
        default=str(DEFAULT_EVENT_GAP_HOURS)
    ))

    skip_screenshots = ask_yes_no(
        "Skip screenshots (files named Screenshot_...)?",
        default=True
    )

    use_locations = ask_yes_no(
        "Use GPS location names for event folders? (requires internet, ~1s per unique location)",
        default=True
    )

    interactive = ask_yes_no(
        "Ask me to choose when duplicates can't be resolved automatically?",
        default=False
    )

    # Preview
    print("\n── Step 3: Preview (dry run) ────────────────────────────────────")
    print("Running a preview — nothing will be copied yet...\n")

    try:
        named_events = run(
            input_dir=input_dirs,
            output_dir=output_dir,
            gap_hours=gap_hours,
            dry_run=True,
            skip_screenshots=skip_screenshots,
            use_locations=use_locations,
            interactive=False,  # never interactive in dry run
        )
    except SystemExit:
        sys.exit(1)

    # Show summary
    print("\n── Event summary ───────────────────────────────────────────────")
    for event_key, photos in sorted(named_events.items()):
        print(f"  {event_key}/  ({len(photos)} photos)")

    # Confirm
    print()
    if not ask_yes_no("Everything looks good — copy the photos now?", default=True):
        print("Cancelled. Nothing was copied.")
        sys.exit(0)

    # Run for real
    print()
    run(
        input_dir=input_dirs,
        output_dir=output_dir,
        gap_hours=gap_hours,
        dry_run=False,
        skip_screenshots=skip_screenshots,
        use_locations=use_locations,
        interactive=interactive,
    )

    print("\nAll done! Open the output folder to review your organized photos.")
    print(f"  {output_dir}")


if __name__ == "__main__":
    wizard()
