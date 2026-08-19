#!/usr/bin/env python3
"""
audit_script.py

Audits the poster metadata and collection YAML files for consistency issues:

  1. Missing "# Posters from:" / "# Poster from:" header comment.
  2. Missing URL in the header comment block (first 5 lines).
  3. Files that reference theposterdb.com (excluding anime/ and
     anime-movies/ directories).

Both ``data/metadata`` and ``data/collections`` are scanned recursively and a
plain-text report is written to ``config/reports/poster_audit_report.txt``.

Usage:
    python scripts/audit_script.py

Output:
    config/reports/poster_audit_report.txt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# -- Paths --------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent
METADATA_ROOT = BASE / "data" / "metadata"
COLLECTIONS_ROOT = BASE / "data" / "collections"
REPORTS_DIR = BASE / "config" / "reports"
REPORT_PATH = REPORTS_DIR / "poster_audit_report.txt"

# -- Constants ----------------------------------------------------------------

HEADER_MARKERS = ("# Posters from:", "# Poster from:")
POSTERDB_DOMAIN = "theposterdb.com"
ANIME_PREFIXES = ("anime/", "anime-movies/")
HEADER_LINES = 5
REPORT_DIVIDER = "=" * 60

CATEGORY_MISSING_HEADER = "missing_header"
CATEGORY_MISSING_URL = "missing_url"
CATEGORY_POSTERDB = "has_posterdb"


@dataclass
class AuditResult:
    total: int = 0
    missing_header: list[str] = field(default_factory=list)
    missing_url: list[str] = field(default_factory=list)
    posterdb: list[str] = field(default_factory=list)


def audit_file(path: Path, root: Path) -> list[str]:
    """Return the list of issue categories for *path* (empty if clean).

    The posterdb check scans the entire file and is independent of the
    header/URL checks, so a file is flagged as using theposterdb.com even
    when it also has header or URL issues. The relative path (posix, without
    the scope prefix) drives the anime exclusion check.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = path.relative_to(root).as_posix()
    categories: list[str] = []

    head_lines = text.splitlines()[:HEADER_LINES]
    has_header = any(marker in line for line in head_lines for marker in HEADER_MARKERS)
    if not has_header:
        categories.append(CATEGORY_MISSING_HEADER)
    else:
        comment_lines = [line for line in head_lines if line.startswith("#")]
        has_url = any("http" in line.lower() for line in comment_lines)
        if not has_url:
            categories.append(CATEGORY_MISSING_URL)

    is_posterdb = POSTERDB_DOMAIN in text
    is_anime = rel.startswith(ANIME_PREFIXES)
    if is_posterdb and not is_anime:
        categories.append(CATEGORY_POSTERDB)

    return categories


def scan_scope(root: Path, scope_prefix: str) -> AuditResult:
    """Scan *root* recursively and bucket every flagged ``.yml`` file."""
    result = AuditResult()
    if not root.exists():
        return result

    yml_files = sorted(root.rglob("*.yml"), key=lambda p: str(p).lower())
    result.total = len(yml_files)

    for path in yml_files:
        categories = audit_file(path, root)
        if not categories:
            continue

        rel = path.relative_to(root).as_posix()
        labelled = f"{scope_prefix}/{rel}"

        for category in categories:
            if category == CATEGORY_MISSING_HEADER:
                result.missing_header.append(labelled)
            elif category == CATEGORY_MISSING_URL:
                result.missing_url.append(labelled)
            else:  # CATEGORY_POSTERDB
                result.posterdb.append(labelled)

    return result


def _section(title: str, items: list[str], note: str | None = None) -> list[str]:
    block = [REPORT_DIVIDER, title]
    if note:
        block.append(note)
    block.append(f"Count: {len(items)}")
    block.append(REPORT_DIVIDER)
    block.extend(sorted(items))
    return block


def build_report(metadata: AuditResult, collections: AuditResult) -> str:
    lines = [
        "POSTER AUDIT REPORT",
        REPORT_DIVIDER,
        f"Total .yml files scanned (metadata): {metadata.total}",
        f"Total .yml files scanned (collections): {collections.total}",
    ]

    sections = [
        ('SECTION 1: METADATA - MISSING "# Posters from:" HEADER', metadata.missing_header, None),
        ('SECTION 2: METADATA - MISSING URL AFTER SECOND "#" LINE', metadata.missing_url, None),
        ('SECTION 3: METADATA - FILES USING theposterdb.com', metadata.posterdb, "(Excludes anime/ and anime-movies/ directories)"),
        ('SECTION 4: COLLECTIONS - MISSING "# Poster from:" HEADER', collections.missing_header, None),
        ('SECTION 5: COLLECTIONS - MISSING URL AFTER SECOND "#" LINE', collections.missing_url, None),
        ('SECTION 6: COLLECTIONS - FILES USING theposterdb.com', collections.posterdb, "(Excludes anime/ and anime-movies/ directories)"),
    ]

    for title, items, note in sections:
        lines.append("")
        lines.extend(_section(title, items, note))

    return "\n".join(lines) + "\n"


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    metadata = scan_scope(METADATA_ROOT, "metadata")
    collections = scan_scope(COLLECTIONS_ROOT, "collections")

    REPORT_PATH.write_text(build_report(metadata, collections), encoding="utf-8")

    total_posterdb = len(metadata.posterdb) + len(collections.posterdb)
    total_missing_header = len(metadata.missing_header) + len(collections.missing_header)
    total_missing_url = len(metadata.missing_url) + len(collections.missing_url)

    print(f"Report written: {REPORT_PATH.as_posix()}")
    print(f"Metadata files: {metadata.total}, Collection files: {collections.total}")
    print(f"Missing header: {total_missing_header}, Missing URL: {total_missing_url}, Has posterdb (non-anime): {total_posterdb}")


if __name__ == "__main__":
    main()
