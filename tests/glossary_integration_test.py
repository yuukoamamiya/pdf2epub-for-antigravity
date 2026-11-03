"""Language-pair integration coverage for glossary consumers."""
import json
from pathlib import Path

import yaml

from pdf2epub.glossary import (
    build_metadata_glossary_context,
    build_unit_glossary_contexts,
    load_glossary,
)


def _write_multilingual_glossary(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "metadata": {
                    "id": "integration",
                    "name": "Integration fixture",
                    "domain": "test",
                    "version": "1",
                },
                "entries": [
                    {
                        "id": "concept-1",
                        "terms": {
                            "en": {"preferred": "concept", "policy": "fixed"},
                            "fr": {"preferred": "notion", "policy": "fixed"},
                            "de": {"preferred": "Begriff", "policy": "fixed"},
                            "zh-Hans": {"preferred": "概念", "policy": "fixed"},
                        },
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_unit_and_metadata_contexts_use_configured_language_pair(tmp_path: Path):
    source_table = tmp_path / "multilingual.yaml"
    _write_multilingual_glossary(source_table)
    normalized = load_glossary(source_table)
    assert normalized["schema_version"] == 2

    output = tmp_path / "output"
    units = tmp_path / "units"
    units.mkdir()
    (units / "chapter.md").write_text("La notion est importante.", encoding="utf-8")
    context_files = {"domain_glossary_001": source_table}

    unit_contexts = build_unit_glossary_contexts(
        output,
        units,
        context_files,
        source_language="French",
        target_language="German",
    )
    unit_data = json.loads(unit_contexts["chapter.md"].read_text(encoding="utf-8"))
    assert unit_data["entries"][0]["source"] == "notion"
    assert unit_data["entries"][0]["target"] == "Begriff"

    metadata_path = build_metadata_glossary_context(
        output,
        context_files,
        "The idea appears in the metadata.",
        source_language="French",
        target_language="German",
    )
    # The requested source language is French, so an English-only metadata
    # string must not accidentally receive an English-to-Chinese projection.
    assert metadata_path is None

    metadata_path = build_metadata_glossary_context(
        output,
        context_files,
        "La notion apparaît dans les métadonnées.",
        source_language="French",
        target_language="German",
    )
    metadata_data = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata_data["entries"][0]["source"] == "notion"
    assert metadata_data["entries"][0]["target"] == "Begriff"


def test_metadata_handoff_uses_sparse_projected_context_and_keeps_snapshots_audit_only(
    tmp_path: Path, monkeypatch
):
    from pdf2epub.html_translation import metadata_preparation

    output = tmp_path / "output"
    glossary_dir = output / "translation_glossaries"
    glossary_dir.mkdir(parents=True)
    domain_path = glossary_dir / "domain.yaml"
    reference_path = glossary_dir / "reference.yaml"
    _write_multilingual_glossary(domain_path)
    _write_multilingual_glossary(reference_path)
    source_table = load_glossary(reference_path)
    source_table["metadata"]["id"] = "reference"
    source_table["metadata"]["name"] = "Reference"
    source_table["entries"][0]["terms"]["en"] = {
        "preferred": "notion",
        "aliases": ["concept"],
        "policy": "preferred",
    }
    source_table["entries"].append(
        {
            "id": "reference-only",
            "terms": {
                "fr": {"preferred": "référence", "policy": "preferred"},
                "de": {"preferred": "Hinweis", "policy": "preferred"},
            },
        }
    )
    reference_path.write_text(
        yaml.safe_dump(source_table, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    class _FakeTOCExtractor:
        def __init__(self, _parser):
            pass

        def get_flat_toc(self):
            return []

    monkeypatch.setattr(metadata_preparation, "resolve_subagent_model", lambda *_args: "test")
    monkeypatch.setattr(
        "pdf2epub.html_translation.toc_extractor.TOCExtractor", _FakeTOCExtractor
    )
    preparer = metadata_preparation.MetadataTranslationPreparer(
        metadata_preparation.MetadataPreparationInput(
            output_dir=output,
            parser=object(),
            config={"translation": {"target_language": "German"}},
            metadata={"description": "La notion est ici, avec une référence."},
            source_language="French",
            book_title="La notion et référence",
        )
    )
    source_path = preparer.prepare(
        target_language="German",
        context_files={
            "domain_glossary_001": domain_path,
            "reference_glossary_001": reference_path,
        },
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    operational = source["translation_context"]["files"]
    assert set(operational) == {"metadata_glossary"}
    projected = json.loads((output / operational["metadata_glossary"]).read_text(encoding="utf-8"))
    assert {entry["kind"] for entry in projected["entries"]} == {"domain", "reference"}
    domain_entry = next(entry for entry in projected["entries"] if entry["kind"] == "domain")
    reference_entry = next(entry for entry in projected["entries"] if entry["kind"] == "reference")
    assert (domain_entry["source"], domain_entry["target"]) == ("notion", "Begriff")
    assert (reference_entry["source"], reference_entry["target"]) == ("référence", "Hinweis")
    assert set(source["translation_audit_context"]["files"]) == {
        "domain_glossary_001",
        "reference_glossary_001",
    }
