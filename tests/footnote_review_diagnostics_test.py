import json
import hashlib
from pathlib import Path

import pytest

from pdf2epub.commands.footnote_diagnostics import footnote_review_summary_command
from pdf2epub.refine.footnote_review_diagnostics import ArtifactError, build_footnote_review_summary


def put(path: Path, name: str, value):
    (path / name).write_text(json.dumps(value), encoding="utf-8")


def sample(path: Path):
    put(path, "pdf_text_probe.json", {"page_count": 8})
    put(path, "footnote_candidates.json", {
        "review_pages": [2, 3, 4, 5],
        "pages": [
            {"page": 2, "candidates": [{"page": 2, "block": 1, "confidence": "high", "review_reason": "boundary"}],
             "audit_candidates": [{"page": 2, "source": "primary", "block": 1, "confidence": "review", "review_reason": "boundary", "marker_label": "†"}],
             "footnote_regions": [{"region_id": "r1"}]},
            {"page": 3, "secondary_only_candidates": [{"page": 3, "block": 2, "source": "secondary", "confidence": "review", "review_reason": "continuation"}]},
        ],
        "continuation_reviews": [{"pages": [3, 4]}],
        "cross_page_windows": [{"pages": [3, 4], "page_images": ["p3.png", "p4.png"]}],
        "visual_evidence": {"files": {"2": "p2.png", "3": "p3.png", "4": "p4.png"}},
    })
    put(path, "footnote_visual_audit_manifest.json", {
        "tasks": [
            {"region_id": "r1", "page": 2, "reasons": ["boundary", "overlap"], "blocks": [{"page": 2, "block": 1}] , "visual_file": "p2.png", "region_visual_file": "crop1.png"},
            {"region_id": "r2", "page": 3, "reasons": ["boundary"], "blocks": [{"page": 3, "source": "secondary", "block": 2}], "visual_file": "p3.png"},
            {"task_id": "gap", "kind": "continuation", "pages": [3, 4], "visual_files": {"3": "p3.png", "4": "p4.png"}},
        ],
    })


def test_counts_distinguish_pages_regions_and_deduplicates_flat_nested_decisions(tmp_path):
    sample(tmp_path)
    decisions = {
        "decisions": [
            {"page": 2, "block": 1, "role": "body"},
            {"page": 3, "block": 2, "source": "secondary", "role": "footnote_start"},
            {"page": 9, "block": 99, "role": "body"},
            {"page": 2, "role": "body"},
        ],
        "region_audits": [{"region_id": "r1", "disposition": "body_region", "decisions": [
            {"page": 2, "block": 1, "role": "body"},
            {"page": 2, "block": 1, "role": "footnote_start"},
        ]}],
    }
    put(tmp_path, "footnote_decisions.json", decisions)
    result = build_footnote_review_summary(tmp_path)
    assert result["counts"]["pdf_total_pages"] == 8
    assert result["counts"]["actual_review_region_pages"] == 2
    assert result["counts"]["review_pages_including_context"] == 4
    assert result["counts"]["regions"] == 2
    assert result["counts"]["review_candidate_blocks"] == 2
    assert result["region_reasons_multi_select"] == {"boundary": 2, "overlap": 1}
    assert result["region_reason_combinations"] == {"boundary + overlap": 1, "boundary": 1}
    assert result["candidate_review_reason_distribution"] == {"boundary": 1, "continuation": 1}
    assert result["decision_diagnostics"]["duplicate_entries"] == 1
    assert result["decision_diagnostics"]["conflicting_addresses"] == 1
    assert result["decision_diagnostics"]["unknown_addresses"] == 1
    assert result["decision_diagnostics"]["invalid_entries"] == 1
    assert result["decision_diagnostics"]["missing_decision_addresses"] == 0
    assert result["counts"]["unique_supporting_page_images"] == 3
    assert result["reviewer_region_disposition_distribution"] == {"body_region": 1}
    assert result["region_dispositions_by_region_reason"]["boundary"] == {"body_region": 1}
    assert result["diagnostic_only"] and result["production_validated"] is False


def test_legacy_missing_fields_and_stale_hash_are_not_success(tmp_path):
    sample(tmp_path)
    put(tmp_path, "footnote_decision_validation.json", {"candidate_report_sha256": "old"})
    put(tmp_path, "pdf_text_probe.json", {"page_count": 477})
    (tmp_path / "footnote_contexts").mkdir()
    (tmp_path / "footnote_contexts" / "unit-a.json").write_text("{}", encoding="utf-8")
    put(tmp_path, "footnote_subagent_manifest.json", {"unit_context_files": {"unit-a": "footnote_contexts/unit-a.json", "unit-b": "footnote_contexts/unit-a.json"}})
    (tmp_path / "footnote_review_packet.json").write_text("{\"compact\":true}", encoding="utf-8")
    put(tmp_path, "footnote_subagent_manifest.json", {
        "unit_context_files": {"unit-a": "footnote_contexts/unit-a.json", "unit-b": "footnote_contexts/unit-a.json"},
        "review_packet": "footnote_review_packet.json",
    })
    result = build_footnote_review_summary(tmp_path)
    assert result["validation_freshness"] == "candidate_report_stale"
    assert result["counts"]["high_confidence_candidates"] == 1
    assert result["counts"]["handoff_files_bytes"] is not None
    assert result["cost_inventory"]["files"]["footnote_contexts/unit-a.json"]["bytes"] == 2
    assert result["cost_inventory"]["files"]["footnote_review_packet.json"]["category"] == "review_packet"
    assert result["counts"]["handoff_files_bytes"] == ((tmp_path / "footnote_subagent_manifest.json").stat().st_size
                                                       + (tmp_path / "footnote_visual_audit_manifest.json").stat().st_size + 2
                                                       + (tmp_path / "footnote_review_packet.json").stat().st_size)
    assert result["artifacts"]["footnote_candidates.json"]["sha256"]
    assert result["decision_diagnostics"]["missing_decision_addresses"] is None
    assert "footnote_decisions.json" in result["missing_artifacts"]
    assert result["production_validated"] is False


def test_canonical_flat_nested_agreement_and_special_marker_snapshot(tmp_path):
    sample(tmp_path)
    raw = {"decisions": [{"page": "2", "block": "1", "role": "body"}],
           "region_audits": [{"region_id": "r1", "disposition": "body_region", "decisions": [{"page": 2, "block": 1, "source": "primary", "role": "body", "confidence": "high", "reason": "review"}]}]}
    put(tmp_path, "footnote_decisions.json", raw)
    validation = {"candidate_report_sha256": hashlib.sha256((tmp_path / "footnote_candidates.json").read_bytes()).hexdigest(),
                  "decisions": [{"page": 2, "block": 1, "source": "primary", "role": "body", "marker_label": "†"}]}
    put(tmp_path, "footnote_decision_validation.json", validation)
    result = build_footnote_review_summary(tmp_path)
    assert result["decision_diagnostics"]["duplicate_entries"] == 1
    assert result["decision_diagnostics"]["conflicting_addresses"] == 0
    assert result["decision_snapshot"] == "matches_after_candidate_defaults"
    raw["decisions"][0]["marker_label"] = "‡"
    put(tmp_path, "footnote_decisions.json", raw)
    result = build_footnote_review_summary(tmp_path)
    assert result["decision_snapshot"] == "changed_since_validation"


def test_secondary_binding_changes_conflict_and_numeric_keys_canonicalize(tmp_path):
    sample(tmp_path)
    rows = [
        {"page": 3, "block": 2, "source": "secondary", "role": "footnote_start", "key": "①", "text": "first", "source_file": "p3.md", "primary_disposition": "absent"},
    ]
    put(tmp_path, "footnote_decisions.json", {"decisions": rows, "region_audits": [{"region_id": "r2", "disposition": "footnote_region", "decisions": [
        {"page": "3", "block": "2", "source": "secondary", "role": "footnote_start", "key": "1", "text": "second", "source_file": "p3.md", "primary_disposition": "absent"},
    ]}]})
    result = build_footnote_review_summary(tmp_path)
    assert result["decision_diagnostics"]["conflicting_addresses"] == 1
    assert result["decision_diagnostics"]["duplicate_entries"] == 0


def test_bad_candidate_collection_is_explicit_error(tmp_path):
    sample(tmp_path)
    report_path = tmp_path / "footnote_candidates.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["pages"][0]["audit_candidates"] = {"not": "an array"}
    put(tmp_path, report_path.name, report)
    with pytest.raises(ArtifactError, match="must be an array"):
        build_footnote_review_summary(tmp_path)


def test_null_region_audits_are_treated_as_empty(tmp_path):
    put(tmp_path, "footnote_candidates.json", {"pages": [], "review_pages": []})
    put(tmp_path, "footnote_visual_audit_manifest.json", {"tasks": []})
    put(tmp_path, "footnote_decisions.json", {"decisions": [], "region_audits": None})
    result = build_footnote_review_summary(tmp_path)
    assert result["decision_diagnostics"]["flat_entries"] == 0
    assert result["decision_diagnostics"]["nested_entries"] == 0
    assert result["decision_diagnostics"]["unmatched_region_decisions"] == 0


def test_region_audit_mapping_and_list_deduplicate_consistently(tmp_path):
    sample(tmp_path)
    put(tmp_path, "footnote_decisions.json", {"region_audits": [
        {"region_id": "r1", "disposition": "body_region", "decisions": []},
        {"region_id": "r1", "disposition": "body_region", "decisions": []},
        {"region_id": "r2", "disposition": "footnote_region", "decisions": []},
    ]})
    listed = build_footnote_review_summary(tmp_path)
    put(tmp_path, "footnote_decisions.json", {"region_audits": {
        "r1": {"disposition": "body_region", "decisions": []},
        "r2": {"disposition": "footnote_region", "decisions": []},
    }})
    mapped = build_footnote_review_summary(tmp_path)
    for summary in (listed, mapped):
        assert summary["reviewer_region_disposition_distribution"] == {"body_region": 1, "footnote_region": 1}
        assert summary["decision_diagnostics"]["unmatched_region_decisions"] == 0
    assert listed["decision_diagnostics"]["duplicate_region_audits"] == 1
    put(tmp_path, "footnote_decisions.json", {"region_audits": [
        {"region_id": "r1", "disposition": "body_region", "decisions": []},
        {"region_id": "r1", "disposition": "footnote_region", "decisions": []},
    ]})
    conflict = build_footnote_review_summary(tmp_path)
    assert "r1" in conflict["decision_diagnostics"]["conflicting_region_ids"]
    assert conflict["reviewer_region_disposition_distribution"] == {}


def test_missing_vs_empty_image_collection_and_invalid_collections(tmp_path):
    sample(tmp_path)
    report_path = tmp_path / "footnote_candidates.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.pop("visual_evidence")
    report["review_pages"] = "bad"
    put(tmp_path, report_path.name, report)
    with pytest.raises(ArtifactError, match="review_pages"):
        build_footnote_review_summary(tmp_path)
    report["review_pages"] = [[3], 4]
    put(tmp_path, report_path.name, report)
    manifest_path = tmp_path / "footnote_visual_audit_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for task in manifest["tasks"]:
        task.pop("visual_file", None); task.pop("visual_files", None)
    put(tmp_path, manifest_path.name, manifest)
    invalid = build_footnote_review_summary(tmp_path)
    assert invalid["decision_diagnostics"]["invalid_candidate_entries"] == 1
    report["review_pages"] = []
    put(tmp_path, report_path.name, report)
    missing = build_footnote_review_summary(tmp_path)
    assert missing["counts"]["review_pages_including_context"] == 0
    assert missing["counts"]["unique_supporting_page_images"] is None
    report["visual_evidence"] = {"files": {}}
    put(tmp_path, report_path.name, report)
    empty = build_footnote_review_summary(tmp_path)
    assert empty["counts"]["unique_supporting_page_images"] == 0


def test_missing_required_or_malformed_json_fails_closed(tmp_path):
    with pytest.raises(ArtifactError, match="missing"):
        build_footnote_review_summary(tmp_path)
    (tmp_path / "footnote_candidates.json").write_text("{", encoding="utf-8")
    with pytest.raises(ArtifactError, match="invalid JSON"):
        build_footnote_review_summary(tmp_path)


def test_invalid_nested_entries_unknown_role_region_and_missing_review_addresses(tmp_path):
    sample(tmp_path)
    put(tmp_path, "footnote_decisions.json", {
        "decisions": ["bad", {"page": [], "block": 3}, {"page": 3, "block": 2, "source": "tertiary", "role": "body"},
                      {"page": 2, "block": 1, "role": "mystery"}],
        "region_audits": [
            {"region_id": "unknown", "disposition": "body_region", "decisions": [None]},
            {"region_id": "r1", "disposition": "body_region", "decisions": [{"page": 2, "block": 1, "role": "body", "reason": "annotation"}]},
        ],
    })
    result = build_footnote_review_summary(tmp_path)
    assert result["decision_diagnostics"]["invalid_entries"] >= 4
    assert result["decision_diagnostics"]["unknown_region_ids"] == 1
    assert result["decision_diagnostics"]["unknown_addresses"] == 0
    assert result["decision_diagnostics"]["missing_decision_addresses"] == 1
    assert result["decision_diagnostics"]["unmatched_region_decisions"] == 1


def test_cli_json_is_read_only_and_does_not_initialize_logs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text("title: Synthetic\n", encoding="utf-8")
    out = tmp_path / "output" / "Synthetic"
    out.mkdir(parents=True)
    sample(out)
    # Snapshot every file and directory before invoking the CLI.
    def inventory():
        return {str(p.relative_to(tmp_path)): ("dir" if p.is_dir() else p.read_bytes()) for p in tmp_path.rglob("*")}
    before = inventory()
    code = footnote_review_summary_command(type("Args", (), {"config": str(config), "json": True})())
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostic_only"] is True
    assert inventory() == before
    assert not (tmp_path / "logs").exists()


def test_cli_missing_artifacts_outputs_json_error_and_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("title: Empty\n", encoding="utf-8")
    code = footnote_review_summary_command(type("Args", (), {"config": "config.yaml", "json": True})())
    assert code != 0
    assert json.loads(capsys.readouterr().out)["available"] is False
