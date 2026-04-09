#!/usr/bin/env python3
"""
check_missing_posters.py

Identifies which titles in the Plex library (from CSV exports) are missing
custom posters (i.e. have no matching url_poster entry in the YML metadata files).

Matching is done by ID (tvdb_id or tmdb_id) rather than title to avoid
false positives/negatives from title normalisation differences.

Usage:
    python scripts/check_missing_posters.py

Output:
    missing_posters.txt  -- sorted alphabetically, one entry per line
"""

import csv
import re
from pathlib import Path

# -- Paths --------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent  # repo root

# (csv_path, title_col, id_col)
CSV_FILES = {
    "Anime":        (BASE / "plex" / "Anime.csv",         "series_title", "tvdb_id"),
    "Anime-Movies": (BASE / "plex" / "Anime-Movies.csv",  "title",        "tmdb_id"),
    "Movies":       (BASE / "plex" / "Movies.csv",        "title",        "tmdb_id"),
    "TV-Shows":     (BASE / "plex" / "TV-Shows.csv",      "series_title", "tvdb_id"),
}

YML_DIRS = {
    "Anime":        BASE / "data" / "metadata" / "anime",
    "Anime-Movies": BASE / "data" / "metadata" / "anime-movies",
    "Movies":       BASE / "data" / "metadata" / "movies",
    "TV-Shows":     BASE / "data" / "metadata" / "shows",
}

OUTPUT_FILE = BASE / "config" / "reports" / "missing_posters.txt"

# -- Regex patterns -----------------------------------------------------------

# Matches top-level metadata ID lines:  "  12345: # Title (Year)"
# Captures the numeric ID and the optional title comment.
# Examples:
#   "  395486 : # Girlfriend, Girlfriend (2021)"  -> id=395486, title=Girlfriend, Girlfriend (2021)
#   "  550: # Fight Club (1999)"                  -> id=550,    title=Fight Club (1999)
RE_ENTRY  = re.compile(r'^\s+(\d+)\s*:\s*(?:#\s*(.+?))?\s*$')
# Matches a url_poster assignment (any indentation depth)
RE_POSTER = re.compile(r'^\s+url_poster\s*:')
# Marks the start of the next top-level metadata entry
RE_NEXT_ID = re.compile(r'^\s+\d+\s*:')


# -- Step 1: Build the set of IDs that HAVE a url_poster in YML ---------------

def parse_yml_posters(yml_dir: Path) -> tuple[set[str], int]:
    """
    Walk *yml_dir* recursively, parse every .yml file, and return:
      - set of string IDs where the top-level entry has url_poster
      - total number of .yml files found
    """
    has_poster: set[str] = set()
    yml_files = sorted(yml_dir.rglob("*.yml"))
    total_files = len(yml_files)

    for yml_path in yml_files:
        lines = yml_path.read_text(encoding="utf-8", errors="replace").splitlines()

        i = 0
        while i < len(lines):
            m = RE_ENTRY.match(lines[i])
            if m:
                entry_id = m.group(1)
                # Scan ahead for url_poster before the next top-level entry line
                found_poster = False
                j = i + 1
                while j < len(lines):
                    if RE_NEXT_ID.match(lines[j]):
                        break           # next top-level entry starts
                    if RE_POSTER.match(lines[j]):
                        found_poster = True
                        break
                    j += 1

                if found_poster:
                    has_poster.add(entry_id)
            i += 1

    return has_poster, total_files


# -- Step 2: Read CSV libraries -----------------------------------------------

def read_csv_entries(csv_path: Path, title_col: str, id_col: str) -> list[tuple[str, str, str]]:
    """
    Return list of (raw_title, year, entry_id) from a CSV file.
    The raw_title is kept for display; entry_id is used for matching.
    """
    entries: list[tuple[str, str, str]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_title = row.get(title_col, "").strip()
            year      = row.get("year", "").strip()
            entry_id  = row.get(id_col, "").strip()
            if raw_title:
                entries.append((raw_title, year, entry_id))
    return entries


# -- Step 3: Find missing titles and write output -----------------------------

def main() -> None:
    # Gather all YML poster keys per category
    print("Parsing YML metadata files...")
    yml_posters_by_cat: dict[str, set[str]] = {}
    total_yml_files = 0

    for category, yml_dir in YML_DIRS.items():
        posters, file_count = parse_yml_posters(yml_dir)
        yml_posters_by_cat[category] = posters
        total_yml_files += file_count
        print(f"  {category:15s} -> {file_count:4d} YML files, {len(posters):4d} IDs with custom posters")

    total_unique_ids = sum(len(v) for v in yml_posters_by_cat.values())
    print(f"\n  Total YML files scanned:        {total_yml_files}")
    print(f"  Total unique YML poster entries: {total_unique_ids}")

    # Compare each CSV entry against the category-specific YML poster set
    print("\nChecking CSV libraries against YML posters...")
    missing_by_cat: dict[str, list[str]] = {cat: [] for cat in CSV_FILES}

    for category, (csv_path, title_col, id_col) in CSV_FILES.items():
        csv_entries = read_csv_entries(csv_path, title_col, id_col)
        yml_ids = yml_posters_by_cat[category]

        for raw_title, year, entry_id in csv_entries:
            if entry_id not in yml_ids:
                # Only append (year) if the raw title doesn't already end with (YYYY)
                if year and not re.search(r'\(\d{4}\)\s*$', raw_title):
                    label = f"{raw_title} ({year})"
                else:
                    label = raw_title
                missing_by_cat[category].append(label)

        missing_by_cat[category].sort(key=str.lower)
        print(f"  {category:15s} -> {len(missing_by_cat[category]):4d} / {len(csv_entries)} titles missing posters")

    total_missing = sum(len(v) for v in missing_by_cat.values())

    # Build output: header then one section per category
    lines: list[str] = [
        f"Total YML files scanned: {total_yml_files}",
        f"Total unique YML poster entries: {total_unique_ids}",
        f"Total missing posters: {total_missing}",
        "-" * 60,
        "",
    ]

    for category, titles in missing_by_cat.items():
        lines.append(f"=== {category} ({len(titles)}) ===")
        lines.extend(titles)
        lines.append("")   # blank line between sections

    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone: {total_missing} missing-poster titles written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
