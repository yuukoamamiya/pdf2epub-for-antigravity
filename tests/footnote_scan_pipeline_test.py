from copy import deepcopy

from pdf2epub.refine import footnote_page_scan as scan
from pdf2epub.refine.footnote_options import FootnotePrepareOptions


def test_shared_candidate_enhancement_keeps_regions_before_orphans_before_scoring(
    monkeypatch,
):
    calls = []
    candidates = [{"order": 2, "block": 2}, {"order": 1, "block": 1}]
    regions = [{"kind": "continuous"}]
    orphans = [{"kind": "orphan"}]

    def expand_continuous(_page, _sidecar, current, **kwargs):
        calls.append(("continuous", kwargs))
        return current, regions

    def expand_orphan(_page, _sidecar, current, **kwargs):
        calls.append(("orphan", kwargs))
        return current, orphans

    def annotate(_sidecar, current, all_regions, previous_context):
        calls.append(("score", all_regions, previous_context))
        return {"mode": "gated"}

    monkeypatch.setattr(scan, "expand_continuous_region_candidates", expand_continuous)
    monkeypatch.setattr(scan, "expand_orphan_continuation_candidates", expand_orphan)
    monkeypatch.setattr(scan, "annotate_cross_page_continuations", annotate)

    sidecar = {"page_number": 4, "blocks": [{"text": "kept"}]}
    original_sidecar = deepcopy(sidecar)
    result = scan._enhance_candidates(
        4,
        sidecar,
        candidates,
        options=FootnotePrepareOptions(auto_accept=True),
        native_text_source=False,
        auto_accept=False,
        page_markdown="",
        layout_profile={},
        previous_page_footnote_evidence=True,
        previous_page_context={"page": 3},
        excluded_block_indexes={5},
    )

    assert [call[0] for call in calls] == ["continuous", "orphan", "score"]
    assert calls[0][1]["native_text_source"] is False
    assert calls[0][1]["page_markdown"] == ""
    assert calls[0][1]["auto_accept"] is False
    assert calls[1][1]["native_text_source"] is False
    assert calls[1][1]["page_markdown"] == ""
    assert calls[1][1]["auto_accept"] is False
    assert calls[2][1] == [*regions, *orphans]
    assert result.continuous_regions == regions
    assert result.orphan_regions == orphans
    assert result.candidates == [{"order": 1, "block": 1}, {"order": 2, "block": 2}]
    assert sidecar == original_sidecar


def test_shared_candidate_enhancement_preserves_native_source_policy(monkeypatch):
    captured = []

    def passthrough(_page, _sidecar, current, **kwargs):
        captured.append(kwargs)
        return current, []

    monkeypatch.setattr(scan, "expand_continuous_region_candidates", passthrough)
    monkeypatch.setattr(scan, "expand_orphan_continuation_candidates", passthrough)
    monkeypatch.setattr(scan, "annotate_cross_page_continuations", lambda *_: {})

    scan._enhance_candidates(
        1,
        {},
        [],
        options=FootnotePrepareOptions(auto_accept=True),
        native_text_source=True,
        auto_accept=True,
        page_markdown="native markdown",
        layout_profile={},
        previous_page_footnote_evidence=False,
        previous_page_context=None,
        excluded_block_indexes=set(),
    )

    assert len(captured) == 2
    assert all(item["native_text_source"] is True for item in captured)
    assert all(item["page_markdown"] == "native markdown" for item in captured)
    assert all(item["auto_accept"] is True for item in captured)


def test_secondary_scan_keeps_candidate_creation_review_gated(monkeypatch):
    candidate_calls = []
    enhancement_calls = []

    def candidate_for_block(*args, **kwargs):
        candidate_calls.append((args, kwargs))
        return {
            "order": 0,
            "block": 0,
            "source": "secondary",
            "disposition": "review_required",
        }

    monkeypatch.setattr(scan, "structure_excluded_block_indexes", lambda *_: set())
    monkeypatch.setattr(scan, "candidate_for_block", candidate_for_block)
    monkeypatch.setattr(
        scan,
        "expand_continuous_region_candidates",
        lambda _p, _s, cs, **kwargs: (enhancement_calls.append(kwargs) or cs, []),
    )
    monkeypatch.setattr(scan, "expand_orphan_continuation_candidates", lambda _p, _s, cs, **_: (cs, []))
    monkeypatch.setattr(scan, "annotate_cross_page_continuations", lambda *_: {})

    source = {"blocks": [{"text": "candidate"}]}
    original_source = deepcopy(source)
    result = scan._scan_secondary_candidates(
        1,
        source,
        options=FootnotePrepareOptions(auto_accept=True),
        page_markdown="primary page markdown must not reach candidate creation",
        structure_exclusion={},
        previous_page_footnote_evidence=False,
        previous_page_context=None,
    )

    assert candidate_calls[0][1]["source"] == "secondary"
    assert "page_markdown" not in candidate_calls[0][1]
    assert candidate_calls[0][1].get("auto_accept", False) is False
    assert enhancement_calls[0]["native_text_source"] is False
    assert enhancement_calls[0]["auto_accept"] is False
    assert enhancement_calls[0]["page_markdown"] == ""
    assert result.candidates[0]["disposition"] == "review_required"
    assert source == original_source


def test_primary_visual_scan_preserves_configured_auto_accept(monkeypatch):
    enhancement_calls = []

    monkeypatch.setattr(scan, "candidate_for_block", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scan,
        "expand_continuous_region_candidates",
        lambda _p, _s, cs, **kwargs: (enhancement_calls.append(kwargs) or cs, []),
    )
    monkeypatch.setattr(scan, "expand_orphan_continuation_candidates", lambda _p, _s, cs, **_: (cs, []))
    monkeypatch.setattr(scan, "annotate_cross_page_continuations", lambda *_: {})
    monkeypatch.setattr(scan, "bottom_detection_stats", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(scan, "suspected_missed_block_indexes", lambda *_args, **_kwargs: [])

    scan._scan_primary_candidates(
        1,
        {"blocks": []},
        options=FootnotePrepareOptions(auto_accept=True),
        native_text_source=False,
        page_markdown="primary visual markdown",
        layout_profile={},
        previous_page_footnote_evidence=False,
        previous_page_context=None,
        structure_excluded_blocks=set(),
        native_font_profile={},
    )

    assert enhancement_calls[0]["native_text_source"] is False
    assert enhancement_calls[0]["auto_accept"] is True
    assert enhancement_calls[0]["page_markdown"] == "primary visual markdown"


def test_primary_native_scan_keeps_native_source_and_configured_auto_accept(monkeypatch):
    enhancement_calls = []

    monkeypatch.setattr(scan, "native_body_font_excluded_blocks", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(scan, "candidate_for_native_block", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scan,
        "expand_continuous_region_candidates",
        lambda _p, _s, cs, **kwargs: (enhancement_calls.append(kwargs) or cs, []),
    )
    monkeypatch.setattr(scan, "expand_orphan_continuation_candidates", lambda _p, _s, cs, **_: (cs, []))
    monkeypatch.setattr(scan, "annotate_cross_page_continuations", lambda *_: {})
    monkeypatch.setattr(scan, "bottom_detection_stats", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(scan, "suspected_missed_block_indexes", lambda *_args, **_kwargs: [])

    scan._scan_primary_candidates(
        1,
        {"blocks": []},
        options=FootnotePrepareOptions(auto_accept=True),
        native_text_source=True,
        page_markdown="primary native markdown",
        layout_profile={},
        previous_page_footnote_evidence=False,
        previous_page_context=None,
        structure_excluded_blocks=set(),
        native_font_profile={},
    )

    assert enhancement_calls[0]["native_text_source"] is True
    assert enhancement_calls[0]["auto_accept"] is True
    assert enhancement_calls[0]["page_markdown"] == "primary native markdown"
