#!/usr/bin/env python3
"""
mediux_author_audit.py

Scans metadata YAML files for MediuX set URLs, scrapes each unique set page
to identify the uploader, and reports files that reference sets not created by
approved authors.

Usage:
    python scripts/mediux_author_audit.py
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# -- Paths --------------------------------------------------------------------

BASE = Path(__file__).resolve().parent.parent
DATA_ROOT = BASE / "data"
METADATA_ROOTS = [
    DATA_ROOT / "metadata" / "anime",
    DATA_ROOT / "metadata" / "anime-movies",
    DATA_ROOT / "metadata" / "movies",
    DATA_ROOT / "metadata" / "shows",
]
REPORTS_DIR = BASE / "config" / "reports"
REPORT_PATH = REPORTS_DIR / "mediux_author_report.txt"
CACHE_PATH = REPORTS_DIR / "mediux_author_cache.json"


# -- Constants -----------------------------------------------------------------

ALLOWED_AUTHORS = {"journeyover", "pejamas"}
BATCH_SIZE = 100
REQUEST_DELAY_SECONDS = 0.5
REQUEST_TIMEOUT_SECONDS = 15
ERROR_CACHE_TTL_HOURS = 24
REPORT_TITLE = "MEDIUX AUTHOR AUDIT REPORT"
REPORT_DIVIDER = "=" * 60

SET_URL_RE = re.compile(r"https://mediux\.pro/sets/(\d+)", re.IGNORECASE)
USERNAME_RE = re.compile(r'"user_created"\s*:\s*\{[^}]*"username"\s*:\s*"([^"]+)"', re.IGNORECASE)
ISO_Z_RE = re.compile(r"Z$")


def sort_key_text(value: str) -> tuple[str, str]:
    return (value.lower(), value)


def sort_key_id(value: str) -> tuple[int, str]:
    return (0, value.lower())


def discover_yml_files(target_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for target_dir in target_dirs:
        if target_dir.exists():
            files.extend(target_dir.rglob("*.yml"))
    unique_files = sorted({path.resolve() for path in files}, key=lambda p: str(p).lower())
    return unique_files


def normalize_report_path(path: Path, data_root: Path) -> str:
    return path.resolve().relative_to(data_root.resolve()).as_posix()


def extract_set_ids(text: str) -> list[str]:
    seen: set[str] = set()
    set_ids: list[str] = []
    for match in SET_URL_RE.finditer(text):
        set_id = match.group(1)
        if set_id not in seen:
            seen.add(set_id)
            set_ids.append(set_id)
    return set_ids


def scan_metadata_files(
    target_dirs: list[Path],
    data_root: Path,
) -> tuple[dict[str, list[str]], dict[str, set[str]], int, int]:
    file_to_sets: dict[str, list[str]] = {}
    set_to_files: dict[str, set[str]] = {}
    yml_files = discover_yml_files(target_dirs)
    total_yml = len(yml_files)
    files_with_urls = 0

    for yml_path in yml_files:
        text = yml_path.read_text(encoding="utf-8", errors="replace")
        set_ids = extract_set_ids(text)
        if not set_ids:
            continue

        files_with_urls += 1
        report_path = normalize_report_path(yml_path, data_root)
        file_to_sets[report_path] = set_ids
        for set_id in set_ids:
            set_to_files.setdefault(set_id, set()).add(report_path)

    return file_to_sets, set_to_files, total_yml, files_with_urls


def default_cache() -> dict:
    return {"version": 1, "success": {}, "errors": {}}


def load_cache(cache_path: Path) -> dict:
    if not cache_path.exists():
        return default_cache()

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        print(f"Warning: cache file is corrupted, rebuilding from scratch: {cache_path}")
        return default_cache()

    if not isinstance(payload, dict) or payload.get("version") != 1:
        print(f"Warning: cache file is invalid, rebuilding from scratch: {cache_path}")
        return default_cache()

    success = payload.get("success", {})
    errors = payload.get("errors", {})
    if not isinstance(success, dict) or not isinstance(errors, dict):
        print(f"Warning: cache file is invalid, rebuilding from scratch: {cache_path}")
        return default_cache()

    clean_success = {str(key): str(value) for key, value in success.items() if value is not None}
    clean_errors: dict[str, dict] = {}
    for key, value in errors.items():
        if isinstance(value, dict) and "status" in value and "checked_at" in value:
            clean_errors[str(key)] = {
                "status": str(value["status"]),
                "checked_at": str(value["checked_at"]),
            }

    return {"version": 1, "success": clean_success, "errors": clean_errors}


def save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def is_retryable_error(cache_entry: dict, now_utc: datetime) -> bool:
    checked_at = cache_entry.get("checked_at")
    if not checked_at:
        return True

    try:
        parsed = datetime.fromisoformat(ISO_Z_RE.sub("+00:00", checked_at))
    except Exception:
        return True

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return now_utc - parsed >= timedelta(hours=ERROR_CACHE_TTL_HOURS)


def get_ids_to_scrape(unique_set_ids: list[str], cache: dict, now: datetime) -> list[str]:
    success_cache = cache.get("success", {}) if isinstance(cache, dict) else {}
    error_cache = cache.get("errors", {}) if isinstance(cache, dict) else {}

    ids_to_scrape: list[str] = []
    for set_id in sorted(set(unique_set_ids), key=sort_key_id):
        if set_id in success_cache:
            continue

        error_entry = error_cache.get(set_id)
        if isinstance(error_entry, dict) and not is_retryable_error(error_entry, now):
            continue

        ids_to_scrape.append(set_id)

    return ids_to_scrape


def extract_username_from_html(html: str) -> str | None:
    match = USERNAME_RE.search(html)
    if match:
        return match.group(1)
    return None


def fetch_set_author(session: requests.Session, set_id: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> tuple[str | None, str | None]:
    url = f"https://mediux.pro/sets/{set_id}"

    for attempt in range(2):
        try:
            response = session.get(url, timeout=timeout)
        except requests.Timeout:
            return None, "timeout"
        except requests.RequestException:
            return None, "network_error"

        status = response.status_code
        if status == 200:
            author = extract_username_from_html(response.text)
            if author:
                return author, None
            return None, "parse_error"

        if status == 404:
            return None, "http_404"

        if status == 429:
            if attempt == 0:
                time.sleep(REQUEST_DELAY_SECONDS)
                continue
            return None, "rate_limited"

        if 500 <= status <= 599:
            if attempt == 0:
                time.sleep(REQUEST_DELAY_SECONDS)
                continue
            return None, "http_500" if status == 500 else "http_5xx"

        return None, "network_error"

    return None, "network_error"


def scrape_set_authors(
    ids_to_scrape: list[str],
    cache: dict,
    batch_size: int = BATCH_SIZE,
    delay: float = REQUEST_DELAY_SECONDS,
) -> tuple[dict[str, str], dict[str, str], int]:
    author_by_set: dict[str, str] = {}
    error_by_set: dict[str, str] = {}
    scraped = 0

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Kometa MediuX Author Audit/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "RSC": "1",
        }
    )

    cache.setdefault("version", 1)
    cache.setdefault("success", {})
    cache.setdefault("errors", {})

    for batch_start in range(0, len(ids_to_scrape), batch_size):
        batch = ids_to_scrape[batch_start : batch_start + batch_size]
        for index, set_id in enumerate(batch):
            if batch_start > 0 or index > 0:
                time.sleep(delay)

            author, error = fetch_set_author(session, set_id)
            scraped += 1

            if author is not None:
                author_by_set[set_id] = author
                cache["success"][set_id] = author
                cache["errors"].pop(set_id, None)
                continue

            if error is None:
                error = "parse_error"

            error_by_set[set_id] = error
            cache["errors"][set_id] = {
                "status": error,
                "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

            cache["success"].pop(set_id, None)

        save_cache(CACHE_PATH, cache)
        print(f"  Batch done: scraped {scraped} sets so far ({len(author_by_set)} succeeded, {len(error_by_set)} failed)")

    return author_by_set, error_by_set, scraped


def analyze_files(
    file_to_sets: dict[str, list[str]],
    author_by_set: dict[str, str],
    error_by_set: dict[str, str],
    allowed_authors: set[str],
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]]]:
    flagged_files: dict[str, list[tuple[str, str]]] = {}
    unresolved_files: dict[str, list[tuple[str, str]]] = {}

    for report_path in sorted(file_to_sets, key=sort_key_text):
        flagged: list[tuple[str, str]] = []
        unresolved: list[tuple[str, str]] = []

        for set_id in file_to_sets[report_path]:
            if set_id in error_by_set:
                unresolved.append((set_id, error_by_set[set_id]))
                continue

            author = author_by_set.get(set_id)
            if author is None:
                unresolved.append((set_id, "parse_error"))
                continue

            if author.lower() not in allowed_authors:
                flagged.append((set_id, author))

        if flagged:
            flagged_files[report_path] = sorted(flagged, key=lambda item: sort_key_id(item[0]))
        if unresolved:
            unresolved_files[report_path] = sorted(unresolved, key=lambda item: sort_key_id(item[0]))

    return flagged_files, unresolved_files


def build_report(
    total_yml: int,
    files_with_urls: int,
    unique_set_ids: list[str],
    cached_reused: int,
    scraped: int,
    flagged_files: dict[str, list[tuple[str, str]]],
    unresolved_files: dict[str, list[tuple[str, str]]],
    error_by_set: dict[str, str],
    set_to_files: dict[str, set[str]],
) -> str:
    flagged_count = len(flagged_files)
    unresolved_count = len(unresolved_files)
    failed_count = len(error_by_set)

    lines: list[str] = [
        REPORT_TITLE,
        REPORT_DIVIDER,
        f"Total YML files scanned: {total_yml}",
        f"Total files with MediuX set URLs: {files_with_urls}",
        f"Total unique MediuX set IDs found: {len(unique_set_ids)}",
        f"Cached authors reused: {cached_reused}",
        f"Set pages scraped this run: {scraped}",
        f"Files with non-approved authors: {flagged_count}",
        f"Files with unresolved set authors: {unresolved_count}",
        f"Set scrape failures: {failed_count}",
        "",
        f"=== Files with non-approved authors ({flagged_count}) ===",
    ]

    for report_path in sorted(flagged_files, key=sort_key_text):
        lines.append(report_path)
        for set_id, author in flagged_files[report_path]:
            lines.append(f"  - {set_id} | {author}")

    lines.extend([
        "",
        f"=== Files with unresolved set authors ({unresolved_count}) ===",
    ])

    for report_path in sorted(unresolved_files, key=sort_key_text):
        lines.append(report_path)
        for set_id, status in unresolved_files[report_path]:
            lines.append(f"  - {set_id} | {status}")

    lines.extend([
        "",
        f"=== Set scrape failures ({failed_count}) ===",
    ])

    for set_id in sorted(error_by_set, key=sort_key_id):
        status = error_by_set[set_id]
        lines.append(f"https://mediux.pro/sets/{set_id} | {status}")
        for report_path in sorted(set_to_files.get(set_id, set()), key=sort_key_text):
            lines.append(f"  - {report_path}")

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    print("Scanning metadata files...")
    file_to_sets, set_to_files, total_yml, files_with_urls = scan_metadata_files(METADATA_ROOTS, DATA_ROOT)

    unique_set_ids = sorted(set(set_to_files), key=sort_key_id)
    now = datetime.now(timezone.utc)
    cache = load_cache(CACHE_PATH)

    print(
        f"Scanned {total_yml} YML files, found {files_with_urls} files with MediuX URLs, {len(unique_set_ids)} unique set IDs"
    )

    cached_reused = sum(1 for set_id in unique_set_ids if set_id in cache.get("success", {}))
    ids_to_scrape = get_ids_to_scrape(unique_set_ids, cache, now)

    author_by_set: dict[str, str] = {
        set_id: str(author)
        for set_id, author in cache.get("success", {}).items()
        if set_id in set_to_files
    }
    error_by_set: dict[str, str] = {}
    for set_id in unique_set_ids:
        entry = cache.get("errors", {}).get(set_id)
        if isinstance(entry, dict) and not is_retryable_error(entry, now):
            error_by_set[set_id] = str(entry.get("status", "network_error"))

    print(f"Scraping {len(ids_to_scrape)} uncached/expired set IDs (batch size={BATCH_SIZE}, {REQUEST_DELAY_SECONDS}s delay)")

    if ids_to_scrape:
        scraped_authors, scraped_errors, scraped = scrape_set_authors(ids_to_scrape, cache)
        author_by_set.update(scraped_authors)
        error_by_set.update(scraped_errors)
    else:
        scraped = 0

    print(f"Scraping complete: {scraped} attempts, {len(author_by_set)} succeeded, {len(error_by_set)} failed")

    flagged_files, unresolved_files = analyze_files(file_to_sets, author_by_set, error_by_set, ALLOWED_AUTHORS)
    print(f"Analysis complete: {len(flagged_files)} flagged files, {len(unresolved_files)} unresolved files")
    report = build_report(
        total_yml=total_yml,
        files_with_urls=files_with_urls,
        unique_set_ids=unique_set_ids,
        cached_reused=cached_reused,
        scraped=scraped,
        flagged_files=flagged_files,
        unresolved_files=unresolved_files,
        error_by_set=error_by_set,
        set_to_files=set_to_files,
    )

    REPORT_PATH.write_text(report, encoding="utf-8")
    save_cache(CACHE_PATH, cache)

    print(f"Report: {REPORT_PATH.as_posix()}")
    print(f"Cache: {CACHE_PATH.as_posix()}")


if __name__ == "__main__":
    main()
