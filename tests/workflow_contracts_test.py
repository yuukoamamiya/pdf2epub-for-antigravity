from pathlib import Path

from pdf2epub.workflow_contracts import (
    is_reusable_checkpoint,
    load_json_object,
    sha256_file,
    validated_checkpoint_data,
)


def test_checkpoint_requires_nonempty_target_and_matching_source_hash(tmp_path: Path):
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("source", encoding="utf-8")
    target.write_text("translation", encoding="utf-8")
    source_hash = sha256_file(source)

    assert is_reusable_checkpoint(
        target, "unit", source_hash, {"unit"}, {"unit": source_hash}
    )
    assert not is_reusable_checkpoint(
        target, "unit", "changed", {"unit"}, {"unit": source_hash}
    )
    assert not is_reusable_checkpoint(
        target, "unit", source_hash, set(), {"unit": source_hash}
    )


def test_checkpoint_rejects_empty_target(tmp_path: Path):
    source = tmp_path / "source.txt"
    target = tmp_path / "target.txt"
    source.write_text("source", encoding="utf-8")
    target.write_text("\n", encoding="utf-8")
    source_hash = sha256_file(source)

    assert not is_reusable_checkpoint(
        target, "unit", source_hash, {"unit"}, {"unit": source_hash}
    )


def test_validation_report_normalization_and_invalid_json(tmp_path: Path):
    report = {
        "valid_files": ["one.txt", "", None],
        "source_sha256": {"one.txt": "abc", "": "ignored", "two.txt": None},
    }
    valid, hashes = validated_checkpoint_data(report)
    assert valid == {"one.txt"}
    assert hashes == {"one.txt": "abc"}

    missing = tmp_path / "missing.json"
    assert load_json_object(missing) == {}
