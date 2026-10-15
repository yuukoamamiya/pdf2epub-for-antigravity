"""Guard the dependency direction of the footnote refinement modules."""

from __future__ import annotations

import ast
from pathlib import Path


REFINE_DIR = Path(__file__).parents[1] / "pdf2epub" / "refine"


def _module_imports(module_name: str) -> set[str]:
    """Return absolute imports for one local refine module without importing it."""
    path = REFINE_DIR / f"{module_name.rsplit('.', 1)[-1]}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = module_name.split(".")
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            package_parts = module_parts[:-node.level]
            if node.module:
                target = ".".join([*package_parts, node.module])
                imports.add(target)
            else:
                imports.update(
                    ".".join([*package_parts, alias.name])
                    for alias in node.names
                )
        elif node.module:
            imports.add(node.module)
    return imports


def test_low_level_footnote_modules_do_not_depend_on_coordinators():
    forbidden = {
        "pdf2epub.refine.footnote_prepare",
        "pdf2epub.refine.footnote_apply",
        "pdf2epub.refine.footnote_handoff",
        "pdf2epub.refine.footnote_validation",
    }
    for module_name in (
        "pdf2epub.refine.footnote_options",
        "pdf2epub.refine.footnote_contracts",
        "pdf2epub.refine.footnote_signals",
        "pdf2epub.refine.footnote_layout",
        "pdf2epub.refine.footnote_matching",
        "pdf2epub.refine.footnote_status",
        "pdf2epub.refine.footnote_continuation_regions",
    ):
        assert not (_module_imports(module_name) & forbidden), module_name


def test_validation_does_not_depend_on_candidate_preparation_coordinator():
    assert "pdf2epub.refine.footnote_prepare" not in _module_imports(
        "pdf2epub.refine.footnote_validation"
    )


def test_region_scanner_uses_the_geometry_region_module():
    imports = _module_imports("pdf2epub.refine.footnote_page_scan")
    assert "pdf2epub.refine.footnote_continuation_regions" in imports
    assert "pdf2epub.refine.footnote_continuation" in imports


def test_old_compatibility_exports_are_removed():
    from pdf2epub.refine import footnote_candidates, footnote_prepare

    removed_names = {
        "_candidate_for_block",
        "_candidate_block_indexes",
        "_window_for_page",
        "_candidate_signature",
        "_aggregate_secondary_only_candidates",
        "_candidates_match",
        "_marker_before_block",
        "_candidate_for_native_block",
        "_native_marker_before_block",
        "load_footnote_unit_contexts",
        "prepare_footnote_subagent",
        "validate_footnote_decisions",
    }
    for name in removed_names:
        assert not hasattr(footnote_candidates, name)
        assert not hasattr(footnote_prepare, name)

    assert not set(footnote_prepare.__all__) & {
        "load_footnote_unit_contexts",
        "prepare_footnote_subagent",
        "validate_footnote_decisions",
    }
