#!/usr/bin/env python3
"""
check_missing_posters.py

Identifies which titles in the Plex library (from CSV exports) are missing
custom posters (i.e. have no matching url_poster/file_poster entry in the YML
metadata files). Anime and TV-Shows are also checked for missing seasonal
posters.

Matching is done by ID (tvdb_id or tmdb_id) rather than title to avoid
false positives/negatives from title normalisation differences.

Rules:
  * Titles in EXCLUDED_TITLES are skipped entirely (known-unreliable IDs, but
    present in the library).
  * Commented-out poster lines, and poster keys with an empty value
    (``""``, ``''``, ``null``, ``~``, or only an inline comment), do not
    count: Kometa does not apply them.
  * Only seasons that exist in the Plex library are ever reported.

Usage:
    python scripts/check_missing_posters.py

Output:
    config/reports/missing_posters.txt -- sorted alphabetically, one entry per line
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# -- Paths --------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent  # repo root
OUTPUT_FILE = BASE / "config" / "reports" / "missing_posters.txt"

# -- Regex patterns / constants -----------------------------------------------

# Top-level metadata ID line, e.g. "  12345: # Title (Year)".
# Captures the indentation and the numeric ID.
RE_ENTRY = re.compile(r'^(\s+)(\d+)\s*:(?:\s*#.*)?$')

# YAML inline comment: whitespace followed by '#' (so '#' inside a URL is safe).
RE_INLINE_COMMENT = re.compile(r'\s+#.*$')

# Trailing "(YYYY)" on a title.
RE_YEAR_SUFFIX = re.compile(r'\s*\(\d{4}\)\s*$')

# Poster keys we care about at both entry and season level.
POSTER_KEYS = frozenset({"url_poster", "file_poster"})

# Values that mean "no poster".
EMPTY_VALUES = frozenset({"", "null", "~"})


# -- Exclusions ---------------------------------------------------------------

# Titles that must never be reported as missing. Write them as they appear in
# the Plex CSV, with or without the "(YYYY)" suffix. Matching ignores case and
# a trailing year, so "Biohazard (2026)" also excludes a bare "Biohazard".
EXCLUDED_TITLES: frozenset[str] = frozenset({})


def normalize_title(title: str) -> str:
    """Case-fold *title* and drop a trailing "(YYYY)" (used for exclusions)."""
    return RE_YEAR_SUFFIX.sub('', title).strip().casefold()


def series_key(title: str) -> str:
    """Key for matching a series across CSVs. Keeps the year, so same-named
    shows from different years are not merged."""
    return title.strip().casefold()


_EXCLUDED_NORMALIZED = frozenset(normalize_title(t) for t in EXCLUDED_TITLES)


def is_excluded(title: str) -> bool:
    return normalize_title(title) in _EXCLUDED_NORMALIZED


# -- Data model ---------------------------------------------------------------

@dataclass(frozen=True)
class Category:
    name: str
    csv_path: Path
    title_col: str
    id_col: str
    yml_dir: Path
    episode_csv: Path | None = None

    @property
    def is_tv(self) -> bool:
        return self.episode_csv is not None


@dataclass(frozen=True)
class Entry:
    """One row of a library CSV."""
    title: str
    year: str
    id: str
    seasons: int = 0

    @property
    def label(self) -> str:
        """Display label; appends the year unless the title already ends in one."""
        if self.year and not RE_YEAR_SUFFIX.search(self.title):
            return f"{self.title} ({self.year})"
        return self.title


@dataclass
class PosterIndex:
    """What the YML files provide."""
    has_poster: set[str] = field(default_factory=set)
    season_posters: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    file_count: int = 0


@dataclass
class CategoryReport:
    total: int = 0
    excluded: int = 0
    missing_main: list[str] = field(default_factory=list)
    missing_seasons: list[str] = field(default_factory=list)


CATEGORIES: list[Category] = [
    Category("Anime", BASE / "plex" / "Anime.csv", "series_title", "tvdb_id",
             BASE / "data" / "metadata" / "anime",
             BASE / "plex" / "Anime-episodes.csv"),
    Category("Anime-Movies", BASE / "plex" / "Anime-Movies.csv", "title", "tmdb_id",
             BASE / "data" / "metadata" / "anime-movies"),
    Category("Movies", BASE / "plex" / "Movies.csv", "title", "tmdb_id",
             BASE / "data" / "metadata" / "movies"),
    Category("TV-Shows", BASE / "plex" / "TV-Shows.csv", "series_title", "tvdb_id",
             BASE / "data" / "metadata" / "shows",
             BASE / "plex" / "TV-Shows-episodes.csv"),
]


# -- YML parsing --------------------------------------------------------------

def has_value(raw: str) -> bool:
    """True if the text after a 'key:' holds a real value.

    Strips inline comments and surrounding quotes, so ``""``, ``''``, ``null``,
    ``~`` and ``# todo`` all count as empty.
    """
    value = RE_INLINE_COMMENT.sub('', raw).strip().strip('"\'').strip()
    return value.casefold() not in EMPTY_VALUES


def _scan_lines(lines: list[str], index: PosterIndex) -> None:
    """Single pass over one YML file, recording posters into *index*.

    Only keys at the expected nesting level count: entry-level poster keys sit
    at the entry's body indent, season-level ones at that season's body indent.
    This stops e.g. an episode-level poster nested under a season from being
    mistaken for the season poster.
    """
    entry_id: str | None = None
    entry_indent = 0
    body_indent: int | None = None          # indent of the entry's direct children

    seasons_indent: int | None = None       # indent of the "seasons:" line
    season_key_indent: int | None = None    # indent of "1:", "2:", ... keys
    season_body_indent: int | None = None   # indent of a season's direct children
    season = -1

    for line in lines:
        stripped = line.strip()
        # Commented-out lines are never applied by Kometa, so they never count.
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())

        # Outside an entry body (or leaving it): look for the next entry header.
        if entry_id is None or indent <= entry_indent:
            entry_id = None
            m = RE_ENTRY.match(line)
            if m:
                entry_id = m.group(2)
                entry_indent = len(m.group(1))
                body_indent = seasons_indent = None
                season_key_indent = season_body_indent = None
                season = -1
            continue

        key, _, raw_value = stripped.partition(':')
        key = key.strip()
        if body_indent is None:
            body_indent = indent

        # Left the seasons block? Re-evaluate this line at entry level.
        if seasons_indent is not None and indent <= seasons_indent:
            seasons_indent = season_key_indent = season_body_indent = None
            season = -1

        if seasons_indent is None:
            if indent == body_indent:
                if key in POSTER_KEYS:
                    if has_value(raw_value):
                        index.has_poster.add(entry_id)
                elif key == 'seasons':
                    seasons_indent = indent
            continue

        # Inside a seasons block.
        if key.isdigit() and season_key_indent in (None, indent):
            season_key_indent = indent
            season = int(key)
            season_body_indent = None
        elif season >= 0:
            if season_body_indent is None:
                season_body_indent = indent
            if indent == season_body_indent and key in POSTER_KEYS and has_value(raw_value):
                index.season_posters[entry_id].add(season)


def parse_yml(yml_dir: Path) -> PosterIndex:
    """Parse every .yml file under *yml_dir* into a PosterIndex."""
    index = PosterIndex()
    yml_files = sorted(yml_dir.rglob("*.yml"))
    index.file_count = len(yml_files)
    for path in yml_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        _scan_lines(text.splitlines(), index)
    return index


# -- CSV reading --------------------------------------------------------------

def read_entries(cat: Category) -> list[Entry]:
    """Read the category CSV into Entry rows (rows without a title are skipped)."""
    entries: list[Entry] = []
    with cat.csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            title = (row.get(cat.title_col) or "").strip()
            if not title:
                continue
            seasons_str = (row.get("seasons") or "").strip()
            seasons = int(seasons_str) if cat.is_tv and seasons_str.isdigit() else 0
            entries.append(Entry(
                title=title,
                year=(row.get("year") or "").strip(),
                id=(row.get(cat.id_col) or "").strip(),
                seasons=seasons,
            ))
    return entries


def read_episode_seasons(csv_path: Path) -> dict[str, set[int]]:
    """Map series key -> season numbers present in an episode-level CSV.

    Season 0 is specials. Rows without an integer season_number are skipped.
    """
    seasons_by_title: dict[str, set[int]] = defaultdict(set)
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            title = (row.get("series_title") or "").strip()
            sn = (row.get("season_number") or "").strip()
            if title and sn.lstrip("-").isdigit():
                seasons_by_title[series_key(title)].add(int(sn))
    return seasons_by_title


# -- Checking -----------------------------------------------------------------

def find_missing_seasons(entry: Entry,
                         have: set[int],
                         episode_seasons: dict[str, set[int]]) -> list[int]:
    """Seasons that exist in Plex for *entry* but have no poster.

    Prefers the real seasons from the episode export; falls back to the CSV
    "seasons" count only when the title has no episode data.
    """
    seasons = episode_seasons.get(series_key(entry.title))
    if seasons is None and entry.seasons > 0:
        seasons = set(range(1, entry.seasons + 1))
    return sorted(seasons - have) if seasons else []


def check_category(cat: Category,
                   index: PosterIndex,
                   episode_seasons: dict[str, set[int]]) -> CategoryReport:
    entries = read_entries(cat)
    report = CategoryReport(total=len(entries))

    for entry in entries:
        # Present in the library but with unreliable IDs: skip, don't report.
        if is_excluded(entry.title):
            report.excluded += 1
            continue

        if entry.id not in index.has_poster:
            report.missing_main.append(entry.label)

        if cat.is_tv:
            missing = find_missing_seasons(
                entry, index.season_posters.get(entry.id, set()), episode_seasons)
            if missing:
                nums = ", ".join(map(str, missing))
                report.missing_seasons.append(f"{entry.label} (Missing Seasons: {nums})")

    report.missing_main.sort(key=str.lower)
    report.missing_seasons.sort(key=str.lower)
    return report


# -- Output -------------------------------------------------------------------

def build_report(indexes: dict[str, PosterIndex],
                 reports: dict[str, CategoryReport]) -> list[str]:
    total_files = sum(i.file_count for i in indexes.values())
    total_ids = sum(len(i.has_poster) for i in indexes.values())
    total_main = sum(len(r.missing_main) for r in reports.values())
    total_seasonal = sum(len(r.missing_seasons) for r in reports.values())
    total_excluded = sum(r.excluded for r in reports.values())

    lines = [
        f"Total YML files scanned: {total_files}",
        f"Total unique YML poster entries: {total_ids}",
        f"Total missing series/movie posters: {total_main}",
        f"Total missing seasonal posters: {total_seasonal}",
        f"Total excluded titles: {total_excluded}",
        "-" * 60,
        "",
    ]

    for cat in CATEGORIES:
        titles = reports[cat.name].missing_main
        lines.append(f"=== {cat.name} (Missing Series/Movie Posters: {len(titles)}) ===")
        lines.extend(titles)
        lines.append("")

    for cat in CATEGORIES:
        if cat.is_tv:
            titles = reports[cat.name].missing_seasons
            lines.append(f"=== {cat.name} (Missing Seasonal Posters: {len(titles)}) ===")
            lines.extend(titles)
            lines.append("")

    # List skipped titles so the report is self-explanatory.
    if EXCLUDED_TITLES:
        lines.append(f"=== Excluded Titles (Not Checked: {len(EXCLUDED_TITLES)}) ===")
        lines.extend(sorted(EXCLUDED_TITLES, key=str.lower))
        lines.append("")

    return lines


def validate_inputs() -> None:
    """Fail early with a clear message instead of a mid-run traceback."""
    problems = []
    for cat in CATEGORIES:
        if not cat.csv_path.is_file():
            problems.append(f"missing CSV: {cat.csv_path}")
        if cat.episode_csv and not cat.episode_csv.is_file():
            problems.append(f"missing episode CSV: {cat.episode_csv}")
        if not cat.yml_dir.is_dir():
            problems.append(f"missing YML directory: {cat.yml_dir}")
    if problems:
        sys.exit("Cannot run:\n  " + "\n  ".join(problems))


def main() -> None:
    validate_inputs()

    print("Parsing YML metadata files...")
    indexes: dict[str, PosterIndex] = {}
    for cat in CATEGORIES:
        indexes[cat.name] = idx = parse_yml(cat.yml_dir)
        print(f"  {cat.name:15s} -> {idx.file_count:4d} YML files, "
              f"{len(idx.has_poster):4d} IDs with custom posters")

    print(f"\n  Total YML files scanned:         {sum(i.file_count for i in indexes.values())}")
    print(f"  Total unique YML poster entries: {sum(len(i.has_poster) for i in indexes.values())}")

    print("\nChecking CSV libraries against YML posters...")
    reports: dict[str, CategoryReport] = {}
    for cat in CATEGORIES:
        episode_seasons = read_episode_seasons(cat.episode_csv) if cat.episode_csv else {}
        reports[cat.name] = r = check_category(cat, indexes[cat.name], episode_seasons)
        suffix = f" ({r.excluded} excluded)" if r.excluded else ""
        print(f"  {cat.name:15s} -> {len(r.missing_main):4d} / {r.total} "
              f"titles missing posters{suffix}")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(build_report(indexes, reports)), encoding="utf-8")
    print(f"\nDone: Results written to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
