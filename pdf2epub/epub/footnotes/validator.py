"""Whole-book invariants for generated footnote links."""

from __future__ import annotations

import posixpath
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


_FOOTNOTE_ID_PREFIXES = ("fn-", "fnref-", "fn:", "fnref:")
_REFERENCE_ID_PREFIXES = ("fnref-", "fnref:")


class FootnoteGraphError(ValueError):
    """Raised when generated footnote anchors do not form a valid link graph."""


def inspect_footnote_graph(
    html_files: Mapping[str, str],
    expected_references: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inspect forward links, backlinks, duplicate IDs, and conservative refs."""
    ids_by_file: dict[str, set[str]] = {}
    duplicate_ids: list[tuple[str, str, int]] = []
    invalid_ids: list[tuple[str, str]] = []
    hrefs: list[tuple[str, str, str]] = []
    unlinked: list[tuple[str, str, str]] = []
    missing_expected: list[tuple[str, str, str, int]] = []
    soups_by_file: dict[str, BeautifulSoup] = {}

    for filename, html in html_files.items():
        soup = BeautifulSoup(html, "html.parser")
        soups_by_file[filename] = soup
        id_counts = Counter(
            tag["id"]
            for tag in soup.find_all(attrs={"id": True})
            if tag.get("id")
        )
        ids_by_file[filename] = set(id_counts)
        duplicate_ids.extend(
            (filename, id_value, count)
            for id_value, count in id_counts.items()
            if count > 1 and id_value.startswith(_FOOTNOTE_ID_PREFIXES)
        )
        invalid_ids.extend(
            (filename, id_value)
            for id_value in id_counts
            if id_value.startswith(_FOOTNOTE_ID_PREFIXES)
            and (":" in id_value or any(character.isspace() for character in id_value))
        )

        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if "#" not in href:
                continue
            before_hash, target_id = href.split("#", 1)
            if not target_id.startswith(_FOOTNOTE_ID_PREFIXES):
                continue
            target_file = Path(before_hash).name if before_hash else filename
            hrefs.append((filename, target_file, target_id))

        for sup in soup.find_all("sup"):
            sup_id = sup.get("id", "")
            if not sup_id.startswith(_REFERENCE_ID_PREFIXES):
                continue
            if not sup.find("a", href=True):
                unlinked.append((filename, sup_id, sup.get_text("", strip=True)))

    for expected in expected_references or []:
        source_file = str(expected.get("source_file", ""))
        reference_id = str(expected.get("reference_id", ""))
        key = str(expected.get("key", ""))
        occurrence = int(expected.get("occurrence", 0) or 0)
        soup = soups_by_file.get(source_file)
        reference = soup.find("sup", id=reference_id) if soup and reference_id else None
        anchor = reference.find("a", href=True) if reference else None
        classes = set(anchor.get("class", [])) if anchor else set()
        target_file = str(expected.get("target_file", ""))
        target_id = str(expected.get("target_id", ""))
        parsed_href = urlsplit(str(anchor.get("href", ""))) if anchor else urlsplit("")
        source_path = source_file.replace("\\", "/")
        expected_target_path = posixpath.normpath(target_file.replace("\\", "/"))
        actual_target_path = posixpath.normpath(
            posixpath.join(
                posixpath.dirname(source_path),
                parsed_href.path.replace("\\", "/") or posixpath.basename(source_path),
            )
        )
        target_soup = soups_by_file.get(target_file)
        target_element = (
            target_soup.find(id=target_id)
            if target_soup and target_id
            else None
        )
        target_classes = set(target_element.get("class", [])) if target_element else set()
        valid_target = (
            bool(target_id)
            and target_id.startswith(("fn-", "fn:"))
            and not target_id.startswith(("fnref-", "fnref:"))
            and actual_target_path == expected_target_path
            and parsed_href.fragment == target_id
            and not parsed_href.scheme
            and not parsed_href.netloc
            and target_element is not None
            and "footnote-def" in target_classes
        )
        if not anchor or "footnote-ref" not in classes or not valid_target:
            missing_expected.append((source_file, reference_id, key, occurrence))

    forward_broken: list[tuple[str, str]] = []
    backref_broken: list[tuple[str, str]] = []
    forward_hrefs = 0
    backref_hrefs = 0
    for source_file, target_file, target_id in hrefs:
        if target_id.startswith(("fn-", "fn:")) and not target_id.startswith(
            _REFERENCE_ID_PREFIXES
        ):
            forward_hrefs += 1
            broken_bucket = forward_broken
        else:
            backref_hrefs += 1
            broken_bucket = backref_broken
        if target_id not in ids_by_file.get(target_file, set()):
            broken_bucket.append((source_file, f"{target_file}#{target_id}"))

    return {
        "hrefs": len(hrefs),
        "forward_hrefs": forward_hrefs,
        "backref_hrefs": backref_hrefs,
        "forward_broken_count": len(forward_broken),
        "backref_broken_count": len(backref_broken),
        "forward_broken_samples": forward_broken[:20],
        "backref_broken_samples": backref_broken[:20],
        "duplicate_footnote_id_count": len(duplicate_ids),
        "duplicate_footnote_id_samples": duplicate_ids[:20],
        "invalid_footnote_id_count": len(invalid_ids),
        "invalid_footnote_id_samples": invalid_ids[:20],
        "unlinked_sup_count": len(unlinked),
        "unlinked_samples": unlinked[:20],
        "expected_reference_count": len(expected_references or []),
        "missing_expected_reference_count": len(missing_expected),
        "missing_expected_reference_samples": missing_expected[:20],
    }


def validate_footnote_graph(
    html_files: Mapping[str, str],
    expected_references: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fail when generated links are broken or footnote IDs are ambiguous."""
    report = inspect_footnote_graph(html_files, expected_references)
    hard_failures = (
        report["forward_broken_count"]
        + report["backref_broken_count"]
        + report["duplicate_footnote_id_count"]
        + report["invalid_footnote_id_count"]
        + report["missing_expected_reference_count"]
    )
    if hard_failures:
        raise FootnoteGraphError(
            "Invalid generated footnote graph: "
            "Footnote graph contains unlinked references or invalid anchors. "
            f"{report['forward_broken_count']} broken forward link(s), "
            f"{report['backref_broken_count']} broken backlink(s), "
            f"{report['duplicate_footnote_id_count']} duplicate footnote ID(s), "
            f"{report['invalid_footnote_id_count']} invalid footnote ID(s). "
            f"{report['missing_expected_reference_count']} expected reference(s) missing or unlinked. "
            f"Samples: forward={report['forward_broken_samples'][:3]}, "
            f"back={report['backref_broken_samples'][:3]}, "
            f"duplicates={report['duplicate_footnote_id_samples'][:3]}, "
            f"invalid={report['invalid_footnote_id_samples'][:3]}, "
            f"expected={report['missing_expected_reference_samples'][:3]}"
        )
    return report
