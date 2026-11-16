import json

import pytest

from pdf2epub.refine.unit_mapping import load_pdf_continuation_files


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        "invalid mapping shape",
        {"units": {}},
        {"units": [{"part_files": "part-a.md"}]},
        {"units": [{"part_files": ["part-a.md", 3]}]},
    ],
)
def test_malformed_refined_mapping_is_reported(tmp_path, payload):
    progress = tmp_path / "ocr_markdown" / "tree_progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="refined unit mapping"):
        load_pdf_continuation_files(tmp_path, tmp_path / "ocr_markdown")
