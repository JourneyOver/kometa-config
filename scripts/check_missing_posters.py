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

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

# -- Paths --------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent  # repo root

OUTPUT_FILE = BASE / "config" / "reports" / "missing_posters.txt"

# -- Regex patterns -----------------------------------------------------------

# Matches top-level metadata ID lines:  "  12345: # Title (Year)"
# Captures leading spaces (indentation) and numeric ID.
RE_ENTRY = re.compile(r'^(\s+)(\d+)\s*:(?:\s*#.*)?$')

# Poster keys we care about at both entry and season level.
POSTER_KEYS = frozenset({"url_poster", "file_poster"})


# -- Category configuration ---------------------------------------------------

@dataclass(frozen=True)
class Category:
    name: str
    csv_path: Path
    title_col: str
    id_col: str
    yml_dir: Path
    is_tv: bool
    episode_csv: Path | None


CATEGORIES: list[Category] = [
    Category("Anime",        BASE / "plex" / "Anime.csv",         "series_title", "tvdb_id",
             BASE / "data" / "metadata" / "anime",         True,  BASE / "plex" / "Anime-episodes.csv"),
    Category("Anime-Movies", BASE / "plex" / "Anime-Movies.csv",  "title",        "tmdb_id",
             BASE / "data" / "metadata" / "anime-movies", False, None),
    Category("Movies",       BASE / "plex" / "Movies.csv",        "title",        "tmdb_id",
             BASE / "data" / "metadata" / "movies",       False, None),
    Category("TV-Shows",     BASE / "plex" / "TV-Shows.csv",      "series_title", "tvdb_id",
             BASE / "data" / "metadata" / "shows",        True,  BASE / "plex" / "TV-Shows-episodes.csv"),
]


# -- YML parsing --------------------------------------------------------------

def _poster_value(text: str) -> str:
    """Return the quote-stripped value after the first ':' in a 'key: value'
    line, or '' if there is no value. A blank/whitespace-only value means the
    poster is effectively absent."""
    if ':' not in text:
        return ''
    return text.split(':', 1)[1].strip().strip('"').strip()


def parse_yml(yml_dir: Path) -> tuple[set[str], dict[str, set[int]], int]:
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
                        # Commented poster lines with a real (non-blank) URL count
                        # as available (intentionally disabled but you have the asset).
                        # A blank URL ("") means no poster -> leave it missing so the
                        # season is flagged if it exists in the CSVs.
                        elif key in POSTER_KEYS and current_season >= 0 and _poster_value(body):
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
                        # Only count as having a main poster if the URL is real.
                        if _poster_value(stripped):
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
                    # Only count a season poster as available if the URL is real;
                    # a blank URL ("") means the season is still missing.
                    elif key in POSTER_KEYS and current_season >= 0 and _poster_value(stripped):
                        season_posters[entry_id].add(current_season)
                    i += 1

            if found_main:
                has_poster.add(entry_id)

    return has_poster, season_posters, total_files


# -- CSV reading --------------------------------------------------------------

def read_episode_seasons(csv_path: Path) -> dict[str, set[int]]:
    """
    Read an episode-level CSV (series_title, season_number, ...) and return
    a mapping of normalized series_title -> set of season numbers present.
    Season numbers are ints (0 == specials). Rows without a parseable
    integer season_number are skipped.
    """
    seasons_by_title: dict[str, set[int]] = {}
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            title = (row.get("series_title") or "").strip().lower()
            if not title:
                continue
            sn = (row.get("season_number") or "").strip()
            if not sn.lstrip("-").isdigit():
                continue
            seasons_by_title.setdefault(title, set()).add(int(sn))
    return seasons_by_title


def read_shows(cat: Category) -> list[tuple[str, str, str, int]]:
    """
    Return list of (raw_title, year, entry_id, seasons_count) from the
    category's CSV. The raw_title is kept for display; entry_id is used for
    matching. seasons_count is only extracted for TV categories.
    """
    entries: list[tuple[str, str, str, int]] = []
    with cat.csv_path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_title = row.get(cat.title_col, "").strip()
            year      = row.get("year", "").strip()
            entry_id  = row.get(cat.id_col, "").strip()

            # Identify total seasons based on the CSV "seasons" column
            seasons_str = row.get("seasons", "0").strip()
            seasons_count = 0
            if cat.is_tv and seasons_str.isdigit():
                seasons_count = int(seasons_str)

            if raw_title:
                entries.append((raw_title, year, entry_id, seasons_count))
    return entries


# -- Checking -----------------------------------------------------------------

def check_category(cat: Category,
                   has_poster: set[str],
                   season_posters: dict[str, set[int]],
                   episode_seasons: dict[str, set[int]]) -> tuple[list[str], list[str], int]:
    """
    Compare the category's CSV entries against its YML poster data.
    Returns (missing_main, missing_seasons, total_entries) lists of display labels.
    """
    missing_main: list[str] = []
    missing_seasons: list[str] = []

    entries = read_shows(cat)
    for raw_title, year, entry_id, seasons_count in entries:
        # Only append (year) if the raw title doesn't already end with (YYYY)
        if year and not re.search(r'\(\d{4}\)\s*$', raw_title):
            label = f"{raw_title} ({year})"
        else:
            label = raw_title

        # Check for top-level show/movie posters
        if entry_id not in has_poster:
            missing_main.append(label)

        # Check for seasonal posters using actual episode data
        if cat.is_tv:
            available_seasons = season_posters.get(entry_id, set())
            missing = []

            # Prefer real seasons from the episode export; fall back to the
            # CSV "seasons" count only if a title has no episode data.
            norm_title = raw_title.strip().lower()
            seasons_set = episode_seasons.get(norm_title)
            if seasons_set is None and seasons_count > 0:
                seasons_set = set(range(1, seasons_count + 1))

            if seasons_set:
                for s in sorted(seasons_set):
                    if s not in available_seasons:
                        missing.append(str(s))

            if missing:
                missing_seasons.append(f"{label} (Missing Seasons: {', '.join(missing)})")

    missing_main.sort(key=str.lower)
    missing_seasons.sort(key=str.lower)
    return missing_main, missing_seasons, len(entries)


# -- Output -------------------------------------------------------------------

def main() -> None:
    # Gather all YML poster keys per category
    print("Parsing YML metadata files...")
    yml_posters_by_cat: dict[str, set[str]] = {}
    yml_season_posters_by_cat: dict[str, dict[str, set[int]]] = {}
    total_yml_files = 0

    for cat in CATEGORIES:
        posters, season_posters, file_count = parse_yml(cat.yml_dir)
        yml_posters_by_cat[cat.name] = posters
        yml_season_posters_by_cat[cat.name] = season_posters
        total_yml_files += file_count
        print(f"  {cat.name:15s} -> {file_count:4d} YML files, {len(posters):4d} IDs with custom posters")

    total_unique_ids = sum(len(v) for v in yml_posters_by_cat.values())
    print(f"\n  Total YML files scanned:        {total_yml_files}")
    print(f"  Total unique YML poster entries: {total_unique_ids}")

    # Build episode-derived season maps for TV categories (source of truth
    # for which seasons actually exist in Plex).
    episode_seasons_by_cat: dict[str, dict[str, set[int]]] = {}
    for cat in CATEGORIES:
        if cat.is_tv and cat.episode_csv and cat.episode_csv.exists():
            episode_seasons_by_cat[cat.name] = read_episode_seasons(cat.episode_csv)

    # Compare each CSV entry against the category-specific YML poster set
    print("\nChecking CSV libraries against YML posters...")
    missing_by_cat: dict[str, list[str]] = {}
    missing_seasons_by_cat: dict[str, list[str]] = {}

    for cat in CATEGORIES:
        missing_main, missing_seasons, total_entries = check_category(
            cat,
            yml_posters_by_cat[cat.name],
            yml_season_posters_by_cat[cat.name],
            episode_seasons_by_cat.get(cat.name, {}),
        )
        missing_by_cat[cat.name] = missing_main
        if cat.is_tv:
            missing_seasons_by_cat[cat.name] = missing_seasons

        print(f"  {cat.name:15s} -> {len(missing_main):4d} / {total_entries} titles missing posters")

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
    for cat in CATEGORIES:
        titles = missing_by_cat[cat.name]
        lines.append(f"=== {cat.name} (Missing Series/Movie Posters: {len(titles)}) ===")
        lines.extend(titles)
        lines.append("")   # blank line between sections

    # Append Seasonal Poster Missing Lists
    for cat in CATEGORIES:
        if cat.is_tv:
            titles = missing_seasons_by_cat[cat.name]
            lines.append(f"=== {cat.name} (Missing Seasonal Posters: {len(titles)}) ===")
            lines.extend(titles)
            lines.append("")   # blank line between sections

    # Ensure the directory exists before writing
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nDone: Results written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()