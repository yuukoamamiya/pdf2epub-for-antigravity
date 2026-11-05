import pytest

from pdf2epub.refine.footnote_layout import body_flow_warning, region_layout_evidence
from pdf2epub.refine.footnote_regions import build_page_footnote_regions
from pdf2epub.refine.footnote_review_packet import build_review_packet, write_review_packet
from pdf2epub.refine.footnote_handoff import _write_unit_contexts
import json


def _sidecar(blocks):
    return {"coordinate_system": "normalized", "page_box": [0, 0, 1000, 1000], "blocks": blocks}


def _block(text, bbox, label="Text"):
    return {"text": text, "bbox": bbox, "label": label}


def test_other_column_text_does_not_count_as_body_after_left_note():
    sidecar = _sidecar([
        _block("TextLeft", [50, 100, 450, 700]),
        _block("1 note", [50, 800, 450, 850], "Footnote"),
        _block("TextRight", [550, 860, 950, 950]),
    ])
    assert body_flow_warning(sidecar, 1, [0.05, 0.8, 0.45, 0.85]) is None
    evidence = region_layout_evidence(sidecar, [1])
    assert evidence["body_continues_after_region"] is False


def test_same_lane_body_after_note_remains_a_warning():
    sidecar = _sidecar([
        _block("1 note", [50, 700, 450, 750], "Footnote"),
        _block("body resumes", [50, 800, 450, 950]),
    ])
    assert body_flow_warning(sidecar, 0, [0.05, 0.7, 0.45, 0.75]) == "ordinary_text_below_numeric_block"
    assert region_layout_evidence(sidecar, [0])["body_continues_after_region"] is True


def test_full_width_body_after_note_remains_a_warning():
    sidecar = _sidecar([
        _block("1 note", [50, 700, 450, 750], "Footnote"),
        _block("bridge prose", [50, 800, 950, 950]),
    ])
    assert body_flow_warning(sidecar, 0, [0.05, 0.7, 0.45, 0.75]) == "ordinary_text_below_numeric_block"
    assert region_layout_evidence(sidecar, [0])["body_continues_after_region"] is True


@pytest.mark.parametrize("wide_first", [True, False])
def test_aligned_wide_and_narrow_notes_join_region_in_either_order(wide_first):
    note_blocks = (
        [_block("1 First note.", [100, 700, 900, 720], "Footnote"), _block("2 Last note.", [100, 730, 500, 790], "Footnote")]
        if wide_first else
        [_block("1 First note.", [100, 700, 500, 720], "Footnote"), _block("2 Last note.", [100, 730, 900, 790], "Footnote")]
    )
    sidecar = _sidecar([_block("body", [100, 100, 900, 500]), *note_blocks])
    candidates = [
        {"block": 1, "key": "1", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 2, "key": "2", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert len(regions) == 1
    assert regions[0]["block_indices"] == [1, 2]


def test_full_width_bridge_does_not_fold_two_column_lanes():
    sidecar = _sidecar([
        _block("left note", [50, 800, 450, 840], "Footnote"),
        _block("bridge note", [50, 850, 950, 875], "Footnote"),
        _block("right note", [550, 800, 950, 840], "Footnote"),
    ])
    candidates = [
        {"block": i, "key": str(i), "label": "Footnote", "confidence": "review", "local_role": "footnote_start"}
        for i in range(3)
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert len(regions) >= 2
    assert any("layout_lane_bridge_ambiguity" in region.get("reasons", []) for region in regions)


def test_fold_does_not_transitively_absorb_unlabelled_body_candidate():
    sidecar = _sidecar([
        _block("body above", [100, 100, 900, 500]),
        _block("1 wide note", [100, 700, 900, 720], "Footnote"),
        _block("2 narrow note", [100, 730, 500, 750], "Footnote"),
        _block("302 ordinary text", [100, 760, 500, 820], "Text"),
    ])
    candidates = [
        {"block": 1, "key": "1", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 2, "key": "2", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 3, "key": "302", "label": "Text", "confidence": "review", "local_role": "body"},
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert any(3 in region["block_indices"] and region["disposition"] == "visual_audit_required" for region in regions)


def test_packet_keeps_thin_high_confidence_neighbor_fact_and_resolvable_window_refs():
    left = {"page": 1, "source": "primary", "block": 2, "confidence": "review", "key": "1", "text": "1 uncertain note"}
    right = {"page": 2, "source": "secondary", "block": 4, "confidence": "high", "key": "1", "local_role": "footnote_continuation"}
    packet = build_review_packet({
        "schema_version": 4, "source_kind": "ocr", "ocr_evidence_mode": "two_ocr",
        "review_pages": [1], "visual_evidence": {"files": {"1": "ocr_review_images/page_001.png", "2": "ocr_review_images/page_002.png"}}, "pages": [
            {"page": 1, "candidates": [left], "sidecar": "pages/page_001.ocr.json",
             "footnote_regions": [{"region_id": "r1", "page": 1, "disposition": "visual_audit_required",
                                   "blocks": [{"page": 1, "source": "primary", "block": 2}]}]},
            {"page": 2, "secondary_only_candidates": [right], "secondary_sidecar": "ocr_secondary/page_002.ocr.json"},
        ],
        "cross_page_windows": [{"pages": [1, 2], "left_candidates": [left], "right_candidates": [right], "reason": "adjacent"}],
        "continuation_reviews": [], "bottom_detection": {},
    })
    refs = packet["cross_page_windows"][0]["candidate_refs"]
    assert set(refs) == {"1:primary:2", "2:secondary:4"}
    assert all(ref in packet["candidates"] for ref in refs)
    assert packet["candidates"]["2:secondary:4"]["confidence"] == "high"
    assert packet["visual_pages"][1]["secondary_sidecar"] == "ocr_secondary/page_002.ocr.json"
    assert packet["visual_pages"][1]["visual_file"] == "ocr_review_images/page_002.png"
    assert packet["regions"][0]["candidate_refs"] == ["1:primary:2"]


def test_multi_part_polish_contexts_are_small_and_part_scoped(tmp_path):
    import hashlib
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "file": "chapter.part1.md", "part_files": ["chapter.part1.md", "chapter.part2.md"],
        "page_range": [1, 2],
    }]}), encoding="utf-8")
    huge = "verbose candidate body " * 10000
    (tmp_path / "pages").mkdir()
    sidecars = {}
    for page in (1, 2):
        sidecar_path = tmp_path / "pages" / f"page_{page:03d}.ocr.json"
        sidecar_path.write_text("{}", encoding="utf-8")
        sidecars[sidecar_path.relative_to(tmp_path).as_posix()] = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    report = {"ocr_evidence_mode": "single_ocr", "sidecar_sha256": sidecars, "pages": [{"page": 1, "sidecar": "pages/page_001.ocr.json", "candidates": [{
        "page": 1, "source": "primary", "block": 3, "key": "9", "confidence": "review",
        "local_role": "footnote_start", "text": huge,
    }]}, {"page": 2, "sidecar": "pages/page_002.ocr.json", "candidates": []}], "cross_page_windows": [{"pages": [1, 2], "left_candidates": [{
        "page": 1, "source": "primary", "block": 3,
    }], "right_candidates": []}], "decisions": []}
    packet_path, packet_sha, _ = write_review_packet(tmp_path, report)
    files = _write_unit_contexts(
        tmp_path, report, review_packet=packet_path, review_packet_sha256=packet_sha,
    )
    contexts = {name: json.loads((tmp_path / relative).read_text(encoding="utf-8")) for name, relative in files.items()}
    assert contexts["chapter.part1.md"]["source_units"] == ["chapter.part1.md"]
    assert contexts["chapter.part2.md"]["source_units"] == ["chapter.part2.md"]
    assert contexts["chapter.part1.md"]["cross_page_windows"][0]["left_candidate_refs"] == ["1:primary:3"]
    assert set(contexts["chapter.part1.md"]["candidates"]) == {"1:primary:3"}
    assert all(len((tmp_path / relative).read_bytes()) < 10000 for relative in files.values())
    from pdf2epub.refine.footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
    from pdf2epub.refine.footnote_handoff import load_footnote_unit_contexts
    from pdf2epub.workflow_contracts import sha256_file
    evidence_hash = json.loads((tmp_path / packet_path).read_text(encoding="utf-8"))["preparation_evidence_sha256"]
    context_hashes = {name: sha256_file(tmp_path / relative) for name, relative in files.items()}
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "preparation_evidence_sha256": evidence_hash,
        "review_packet": packet_path,
        "review_packet_sha256": packet_sha,
        "unit_context_files": files,
        "unit_context_sha256": context_hashes,
    }
    (tmp_path / "footnote_candidates.json").write_text(json.dumps(report), encoding="utf-8")
    (tmp_path / "footnote_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    loaded = load_footnote_unit_contexts(tmp_path)
    assert set(loaded) == {"chapter.part1.md", "chapter.part2.md"}
    manifest["unit_context_files"].pop("chapter.part2.md")
    manifest["unit_context_sha256"].pop("chapter.part2.md")
    (tmp_path / "footnote_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="inventory differs from current refined mapping"):
        load_footnote_unit_contexts(tmp_path)


def test_review_packet_resolves_secondary_page_one_without_matching_page_ten():
    from pdf2epub.refine.footnote_review_packet import build_review_packet

    report = {
        "ocr_evidence_mode": "two_ocr",
        "review_pages": [1, 10],
        "pages": [{"page": 1}, {"page": 10}],
        "secondary_sidecar_sha256": {
            "ocr_secondary/page_001.ocr.json": "a" * 64,
            "ocr_secondary/page_0010.ocr.json": "b" * 64,
        },
    }
    packet = build_review_packet(report)
    pages = {item["page"]: item for item in packet["visual_pages"]}
    assert pages[1]["secondary_sidecar"] == "ocr_secondary/page_001.ocr.json"
    assert pages[10]["secondary_sidecar"] == "ocr_secondary/page_0010.ocr.json"


@pytest.mark.parametrize("inventory", [
    {"ocr_secondary/page_001.ocr.json": "a", "ocr_secondary/page_0001.ocr.json": "b"},
    {"pages/../ocr_secondary/page_001.ocr.json": "a"},
])
def test_exact_sidecar_resolver_rejects_aliases_and_source_forgery(inventory):
    from pdf2epub.refine.footnote_page_evidence import resolve_page_sidecar_path

    with pytest.raises(ValueError):
        resolve_page_sidecar_path(inventory, 1, "ocr_secondary")


def test_non_review_page_27_is_kept_in_scoped_unit_evidence(tmp_path, monkeypatch):
    import hashlib

    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "file": "chapter.md", "page_range": [27, 27],
    }]}), encoding="utf-8")
    (tmp_path / "pages").mkdir()
    sidecar_hashes = {}
    page_reports = []
    for page in range(1, 28):
        sidecar = tmp_path / "pages" / f"page_{page:03d}.ocr.json"
        sidecar.write_text("{}", encoding="utf-8")
        sidecar_hashes[sidecar.relative_to(tmp_path).as_posix()] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        page_reports.append({
            "page": page, "sidecar": sidecar.relative_to(tmp_path).as_posix(),
            "secondary_sidecar": f"ocr_secondary/page_{page:03d}.ocr.json",
            "secondary_sidecar_sha256": "stale-secondary-hash", "candidates": [],
        })
    report = {
        "ocr_evidence_mode": "single_ocr",
        "review_pages": [],
        "sidecar_sha256": sidecar_hashes,
        "pages": page_reports,
        "cross_page_windows": [], "continuation_reviews": [], "decisions": [],
    }
    packet_path, packet_sha, _ = write_review_packet(tmp_path, report)
    files = _write_unit_contexts(tmp_path, report, review_packet=packet_path, review_packet_sha256=packet_sha)
    context = json.loads((tmp_path / files["chapter.md"]).read_text(encoding="utf-8"))
    assert context["pages"][0]["page"] == 27
    assert context["pages"][0]["candidate_refs"] == []
    assert "secondary_sidecar" not in context["pages"][0]
    assert "secondary_sidecar_sha256" not in context["pages"][0]
    from pdf2epub.refine import footnote_handoff
    from pdf2epub.refine.footnote_page_evidence import page_sidecar_index
    packet = json.loads((tmp_path / packet_path).read_text(encoding="utf-8"))
    units = footnote_handoff._load_refined_units(tmp_path)
    index = page_sidecar_index(tmp_path, report)
    def fail_io(*_args, **_kwargs):
        raise AssertionError("canonical projector performed I/O")
    monkeypatch.setattr(footnote_handoff, "_load_refined_units", fail_io)
    monkeypatch.setattr(footnote_handoff, "page_sidecar_index", fail_io)
    projected = footnote_handoff._project_unit_contexts(report, packet, units, index, packet_path)
    assert projected["chapter.md"] == context


@pytest.mark.parametrize("fault", ["missing_secondary", "changed_secondary"])
def test_two_ocr_page_index_requires_matching_current_secondary_sidecars(tmp_path, fault):
    import hashlib
    from pdf2epub.refine.footnote_page_evidence import page_sidecar_index

    primary = tmp_path / "pages" / "page_001.ocr.json"
    secondary = tmp_path / "ocr_secondary" / "page_001.ocr.json"
    primary.parent.mkdir()
    secondary.parent.mkdir()
    primary.write_text("primary", encoding="utf-8")
    secondary.write_text("secondary", encoding="utf-8")
    report = {
        "ocr_evidence_mode": "two_ocr",
        "sidecar_sha256": {"pages/page_001.ocr.json": hashlib.sha256(primary.read_bytes()).hexdigest()},
        "secondary_sidecar_sha256": {"ocr_secondary/page_001.ocr.json": hashlib.sha256(secondary.read_bytes()).hexdigest()},
        "pages": [],
    }
    if fault == "missing_secondary":
        secondary.unlink()
    else:
        secondary.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="sidecar missing or changed"):
        page_sidecar_index(tmp_path, report)


def test_two_ocr_page_index_rejects_orphan_secondary_page(tmp_path):
    import hashlib
    from pdf2epub.refine.footnote_page_evidence import page_sidecar_index

    primary = tmp_path / "pages/page_001.ocr.json"
    secondary = tmp_path / "ocr_secondary/page_0010.ocr.json"
    primary.parent.mkdir()
    secondary.parent.mkdir()
    primary.write_text("p", encoding="utf-8")
    secondary.write_text("s", encoding="utf-8")
    report = {
        "ocr_evidence_mode": "two_ocr", "pages": [],
        "sidecar_sha256": {"pages/page_001.ocr.json": hashlib.sha256(primary.read_bytes()).hexdigest()},
        "secondary_sidecar_sha256": {"ocr_secondary/page_0010.ocr.json": hashlib.sha256(secondary.read_bytes()).hexdigest()},
    }
    with pytest.raises(ValueError, match="inventories differ"):
        page_sidecar_index(tmp_path, report)


def test_legacy_native_evidence_uses_single_source_and_ignores_old_secondary_inventory(tmp_path):
    import hashlib
    from pdf2epub.refine.footnote_page_evidence import page_sidecar_index

    sidecar = tmp_path / "pages/page_001.ocr.json"
    sidecar.parent.mkdir()
    sidecar.write_text("legacy", encoding="utf-8")
    (tmp_path / "pages/ocr_progress.json").write_text(json.dumps({"mode": "native_text"}), encoding="utf-8")
    report = {
        "ocr_evidence_mode": "single_ocr",
        "source_kind": "native_text",
        "sidecar_sha256": {"pages/page_001.ocr.json": hashlib.sha256(sidecar.read_bytes()).hexdigest()},
        "secondary_sidecar_sha256": {"ocr_secondary/page_001.ocr.json": "stale"},
        "pages": [{"page": 1}],
    }
    index = page_sidecar_index(tmp_path, report)
    assert set(index[1]) == {"primary"}


def test_malformed_refined_page_range_is_rejected_before_expansion(tmp_path):
    import hashlib

    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "file": "chapter.md", "page_range": [1, 1_000_000_000],
    }]}), encoding="utf-8")
    sidecar = tmp_path / "pages/page_001.ocr.json"
    sidecar.parent.mkdir()
    sidecar.write_text("{}", encoding="utf-8")
    report = {
        "ocr_evidence_mode": "single_ocr",
        "sidecar_sha256": {"pages/page_001.ocr.json": hashlib.sha256(sidecar.read_bytes()).hexdigest()},
        "pages": [{"page": 1, "sidecar": "pages/page_001.ocr.json", "candidates": []}],
    }
    packet_path, packet_hash, _ = write_review_packet(tmp_path, report)
    with pytest.raises(ValueError, match="invalid refined page range"):
        _write_unit_contexts(
            tmp_path, report, review_packet=packet_path,
            review_packet_sha256=packet_hash,
        )


def test_page_evidence_change_keeps_unrelated_unit_projection_bytes_stable(tmp_path):
    import hashlib
    from pdf2epub.refine.footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
    from pdf2epub.refine.footnote_handoff import load_footnote_unit_contexts
    from pdf2epub.workflow_contracts import sha256_file

    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "tree_progress.json").write_text(json.dumps({"units": [
        {"file": "first.md", "page_range": [1, 1]},
        {"file": "unrelated.md", "page_range": [3, 3]},
    ]}), encoding="utf-8")
    (tmp_path / "pages").mkdir()
    sidecars = {}
    for page in (1, 3):
        path = tmp_path / "pages" / f"page_{page:03d}.ocr.json"
        path.write_text(f"page {page}", encoding="utf-8")
        sidecars[path.relative_to(tmp_path).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "ocr_evidence_mode": "single_ocr", "source_kind": "visual_ocr", "footnote_mode": "footnotes",
        "review_pages": [], "sidecar_sha256": sidecars,
        "pages": [{"page": page, "sidecar": f"pages/page_{page:03d}.ocr.json", "candidates": []} for page in (1, 3)],
        "cross_page_windows": [], "continuation_reviews": [], "decisions": [],
    }
    def refresh():
        packet_path, packet_sha, _ = write_review_packet(tmp_path, report)
        files = _write_unit_contexts(tmp_path, report, review_packet=packet_path, review_packet_sha256=packet_sha)
        (tmp_path / "footnote_candidates.json").write_text(json.dumps(report), encoding="utf-8")
        hashes = {name: sha256_file(tmp_path / relative) for name, relative in files.items()}
        manifest = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "preparation_evidence_sha256": json.loads((tmp_path / packet_path).read_text(encoding="utf-8"))["preparation_evidence_sha256"],
            "review_packet": packet_path, "review_packet_sha256": packet_sha,
            "unit_context_files": files, "unit_context_sha256": hashes,
        }
        (tmp_path / "footnote_subagent_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return files, manifest
    files, _ = refresh()
    unrelated_path = tmp_path / files["unrelated.md"]
    original_bytes = unrelated_path.read_bytes()
    first_sidecar = tmp_path / "pages/page_001.ocr.json"
    first_sidecar.write_text("updated page one evidence", encoding="utf-8")
    report["sidecar_sha256"]["pages/page_001.ocr.json"] = hashlib.sha256(first_sidecar.read_bytes()).hexdigest()
    files, manifest = refresh()
    assert (tmp_path / files["unrelated.md"]).read_bytes() == original_bytes
    load_footnote_unit_contexts(tmp_path)
    locally_refreshed = (tmp_path / files["unrelated.md"]).read_bytes()
    report["footnote_mode"] = "disabled"
    report["source_kind"] = "native_text"
    files, _ = refresh()
    assert (tmp_path / files["unrelated.md"]).read_bytes() != locally_refreshed
    load_footnote_unit_contexts(tmp_path)
