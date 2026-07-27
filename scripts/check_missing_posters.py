#!/usr/bin/env python3
"""
check_missing_posters.py

Identifies which titles in the Plex library (from CSV exports) are missing
custom posters (i.e. have no matching url_poster entry in the YML metadata files).
Additionally checks Anime and TV-Shows for missing seasonal posters.

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
# Captures leading spaces (indentation) and numeric ID.
RE_ENTRY = re.compile(r'^(\s+)(\d+)\s*:(?:\s*#.*)?$')

# Poster keys we care about at both entry and season level.
POSTER_KEYS = frozenset({"url_poster", "file_poster"})


# -- Step 1: Parse YML files for Main & Seasonal Posters ----------------------

def parse_yml_metadata(yml_dir: Path) -> tuple[set[str], dict[str, set[int]], int]:
    """
    Walk *yml_dir* recursively, parse every .yml file, and return:
      - set of string IDs where the top-level entry has a poster
      - dict mapping IDs to a set of season numbers that have posters
      - total number of .yml files found
    """
    has_poster: set[str] = set()
    season_posters: dict[str, set[int]] = {}

    yml_files = sorted(yml_dir.rglob("*.yml"))
    total_files = len(yml_files)

    for path in yml_files:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        i = 0
        while i < len(lines):
            m = RE_ENTRY.match(lines[i])
            if not m:
                i += 1
                continue

            entry_id = m.group(2)
            entry_indent = len(m.group(1))
            season_posters.setdefault(entry_id, set())
            found_main = False

            # Walk this entry's body until the next entry at same/lesser indent
            i += 1
            in_seasons = False
            seasons_indent = -1
            current_season = -1

            while i < len(lines):
                line = lines[i]
                stripped = line.strip()

                if not stripped:
                    i += 1
                    continue

                # -- Comment handling -------------------------------------------------
                # Commented lines inside the seasons block may represent intentionally
                # disabled poster sets (e.g. season 0/specials).  Extract the content
                # after '#' so they're counted as available when present.
                if stripped.startswith('#'):
                    if in_seasons:
                        body = stripped.lstrip('#').strip()
                        key = body.split(':')[0].strip() if body else ''
                        if key.isdigit():
                            current_season = int(key)
                        elif key in POSTER_KEYS and current_season >= 0:
                            season_posters[entry_id].add(current_season)
                    i += 1
                    continue

                # -- Real (non-comment) lines -----------------------------------------
                indent = len(line) - len(line.lstrip())
                if indent <= entry_indent:
                    break  # next sibling entry or end of file

                key = stripped.split(':')[0].strip()

                if not in_seasons:
                    if key in POSTER_KEYS:
                        found_main = True
                    elif key == 'seasons':
                        in_seasons = True
                        seasons_indent = indent
                    i += 1
                else:
                    # Inside a seasons block
                    if indent <= seasons_indent:
                        in_seasons = False
                        continue  # re-evaluate this line at entry level
                    if key.isdigit():
                        current_season = int(key)
                    elif key in POSTER_KEYS and current_season >= 0:
                        season_posters[entry_id].add(current_season)
                    i += 1

            if found_main:
                has_poster.add(entry_id)

    return has_poster, season_posters, total_files


# -- Step 2: Read CSV libraries -----------------------------------------------

def read_csv_entries(csv_path: Path, title_col: str, id_col: str, is_tv: bool) -> list[tuple[str, str, str, int]]:
    """
    Return list of (raw_title, year, entry_id, seasons_count) from a CSV file.
    The raw_title is kept for display; entry_id is used for matching.
    seasons_count is extracted for TV-Shows and Anime categories.
    """
    entries: list[tuple[str, str, str, int]] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_title = row.get(title_col, "").strip()
            year      = row.get("year", "").strip()
            entry_id  = row.get(id_col, "").strip()

            # Identify total seasons based on the CSV "seasons" column
            seasons_str = row.get("seasons", "0").strip()
            seasons_count = 0
            if is_tv and seasons_str.isdigit():
                seasons_count = int(seasons_str)

            if raw_title:
                entries.append((raw_title, year, entry_id, seasons_count))
    return entries


# -- Step 3: Find missing titles and write output -----------------------------

def main() -> None:
    # Gather all YML poster keys per category
    print("Parsing YML metadata files...")
    yml_posters_by_cat: dict[str, set[str]] = {}
    yml_season_posters_by_cat: dict[str, dict[str, set[int]]] = {}
    total_yml_files = 0

    for category, yml_dir in YML_DIRS.items():
        posters, season_posters, file_count = parse_yml_metadata(yml_dir)
        yml_posters_by_cat[category] = posters
        yml_season_posters_by_cat[category] = season_posters
        total_yml_files += file_count
        print(f"  {category:15s} -> {file_count:4d} YML files, {len(posters):4d} IDs with custom posters")

    total_unique_ids = sum(len(v) for v in yml_posters_by_cat.values())
    print(f"\n  Total YML files scanned:        {total_yml_files}")
    print(f"  Total unique YML poster entries: {total_unique_ids}")

    # Compare each CSV entry against the category-specific YML poster set
    print("\nChecking CSV libraries against YML posters...")
    missing_by_cat: dict[str, list[str]] = {cat: [] for cat in CSV_FILES}
    missing_seasons_by_cat: dict[str, list[str]] = {cat: [] for cat in ("Anime", "TV-Shows")}

    for category, (csv_path, title_col, id_col) in CSV_FILES.items():
        is_tv = category in ("Anime", "TV-Shows")
        csv_entries = read_csv_entries(csv_path, title_col, id_col, is_tv)

        yml_ids = yml_posters_by_cat[category]
        yml_seasons = yml_season_posters_by_cat.get(category, {})

        for raw_title, year, entry_id, seasons_count in csv_entries:
            # Only append (year) if the raw title doesn't already end with (YYYY)
            if year and not re.search(r'\(\d{4}\)\s*$', raw_title):
                label = f"{raw_title} ({year})"
            else:
                label = raw_title

            # Check for top-level show/movie posters
            if entry_id not in yml_ids:
                missing_by_cat[category].append(label)

            # Check for seasonal posters
            if is_tv and seasons_count > 0:
                available_seasons = yml_seasons.get(entry_id, set())
                missing_seasons = []

                # Adjust for Plex including season 0/specials in the count.
                # If YML has season 0 configured, the CSV "seasons" count includes it,
                # so effective regular seasons = count - 1.
                # ponytail: infers from YML poster presence; shows with season 0 but
                # no poster configured will still check the full (over)count.
                effective_count = seasons_count - 1 if 0 in available_seasons else seasons_count

                # Check 1 through effective_count
                for s in range(1, effective_count + 1):
                    if s not in available_seasons:
                        missing_seasons.append(str(s))

                if missing_seasons:
                    missing_seasons_by_cat[category].append(f"{label} (Missing Seasons: {', '.join(missing_seasons)})")

        # Sort the output lists alphabetically
        missing_by_cat[category].sort(key=str.lower)
        if is_tv:
            missing_seasons_by_cat[category].sort(key=str.lower)

        print(f"  {category:15s} -> {len(missing_by_cat[category]):4d} / {len(csv_entries)} titles missing posters")

    total_missing = sum(len(v) for v in missing_by_cat.values())
    total_missing_seasonal = sum(len(v) for v in missing_seasons_by_cat.values())

    # Build output: header then one section per category
    lines: list[str] = [
        f"Total YML files scanned: {total_yml_files}",
        f"Total unique YML poster entries: {total_unique_ids}",
        f"Total missing series/movie posters: {total_missing}",
        f"Total missing seasonal posters: {total_missing_seasonal}",
        "-" * 60,
        "",
    ]

    # Append Main Poster Missing Lists
    for category, titles in missing_by_cat.items():
        lines.append(f"=== {category} (Missing Series/Movie Posters: {len(titles)}) ===")
        lines.extend(titles)
        lines.append("")   # blank line between sections

    # Append Seasonal Poster Missing Lists
    for category, titles in missing_seasons_by_cat.items():
        lines.append(f"=== {category} (Missing Seasonal Posters: {len(titles)}) ===")
        lines.extend(titles)
        lines.append("")   # blank line between sections

    # Ensure the directory exists before writing
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone: Results written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
