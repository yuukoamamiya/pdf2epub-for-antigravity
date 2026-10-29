"""Keep production consumers on narrow planning and audit service APIs."""
import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _imports(path: str):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]


def test_production_modules_do_not_import_private_batching_helpers():
    consumers = (
        "pdf2epub/markdown_handoff.py",
        "pdf2epub/markdown_subagent_validation.py",
        "pdf2epub/page_furniture_repair.py",
        "pdf2epub/html_translation/builder.py",
        "pdf2epub/commands/markdown.py",
    )
    private_names = {"_batch_queue", "_batching_config", "_markdown_files", "_recommended_batches"}
    for path in consumers:
        for node in _imports(path):
            if node.module and node.module.endswith("subagent_runtime"):
                assert not (private_names & {alias.name for alias in node.names}), path


def test_html_translation_validator_owns_special_role_audit_rule():
    imports = _imports("pdf2epub/html_translation/translation_validation.py")
    assert any(
        node.module == "pdf2epub.special_role_audit"
        and any(alias.name == "validate_special_role_markers" for alias in node.names)
        for node in imports
    )
    builder = (ROOT / "pdf2epub/html_translation/builder.py").read_text(encoding="utf-8")
    assert "validate_special_role_markers" not in builder
    assert "HTMLTranslationValidator" in builder


def test_validation_checkpoint_service_does_not_import_the_validator():
    imports = _imports("pdf2epub/markdown_validation_checkpoint.py")
    assert not any(
        node.module and node.module.endswith("markdown_subagent_validation")
        for node in imports
    )


def test_production_code_imports_common_helpers_from_their_owner_modules():
    for path in (ROOT / "pdf2epub").rglob("*.py"):
        if path.name == "common.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, ast.ImportFrom)
            and node.module == "pdf2epub.utils.common"
            for node in ast.walk(tree)
        ), path.relative_to(ROOT)


def test_path_helpers_do_not_load_workflow_policy():
    imports = _imports("pdf2epub/utils/paths.py")
    assert not any(
        node.module == "pdf2epub.pipeline_policy" for node in imports
    )


def test_common_module_keeps_explicit_legacy_reexports():
    from pdf2epub.utils import common, input_paths

    assert common.resolve_book_input_path is input_paths.resolve_book_input_path
    assert common.resolve_input_path is input_paths.resolve_input_path


def test_markdown_audit_uses_public_role_and_heading_services():
    imports = _imports("pdf2epub/markdown_validation_audit.py")
    assert any(
        node.module == "special_role_audit"
        and any(alias.name == "validate_special_role_markers" for alias in node.names)
        for node in imports
    )
    assert not any(
        node.module == "markdown_validation"
        and any(alias.name.startswith("_") for alias in node.names)
        for node in imports
    )


def test_internal_import_graph_is_acyclic_and_business_layers_do_not_import_commands():
    modules = {}
    for path in (ROOT / "pdf2epub").rglob("*.py"):
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        edges = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges.update(alias.name for alias in node.names if alias.name.startswith("pdf2epub."))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package = module.split(".")[:-1]
                    if path.name == "__init__.py":
                        package = module.split(".")
                    base = package[: len(package) - node.level + 1]
                    prefix = ".".join(base + ([node.module] if node.module else []))
                else:
                    prefix = node.module or ""
                if prefix == "pdf2epub" or prefix.startswith("pdf2epub."):
                    edges.add(prefix)
                for alias in node.names:
                    candidate = f"{prefix}.{alias.name}" if prefix else alias.name
                    if candidate in {"pdf2epub", "pdf2epub.cli"} or candidate.startswith("pdf2epub."):
                        edges.add(candidate)
        modules[module] = {edge for edge in edges if edge in modules or (ROOT / (edge.replace(".", "/") + ".py")).exists() or (ROOT / edge.replace(".", "/") / "__init__.py").exists()}

    visiting, visited = set(), set()

    def visit(module):
        assert module not in visiting, f"internal import cycle includes {module}"
        if module in visited:
            return
        visiting.add(module)
        for dependency in modules.get(module, ()):
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in modules:
        visit(module)

    for module, dependencies in modules.items():
        if module in {"pdf2epub.cli"} or module.startswith("pdf2epub.commands"):
            continue
        assert not any(dep == "pdf2epub.cli" or dep.startswith("pdf2epub.commands") for dep in dependencies), module


def test_markdown_handoff_planning_returns_inventory_and_batch_plan(tmp_path):
    from pdf2epub.markdown_handoff_planning import plan_markdown_handoff

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "chapter.md").write_text("Heading\n\nBody", encoding="utf-8")
    plan = plan_markdown_handoff(source_dir, "translate", declared_files=["chapter.md"])
    assert [path.name for path in plan.sources] == ["chapter.md"]
    assert plan.file_stats["chapter.md"]["nonempty_line_count"] == 2
    assert plan.source_sha256["chapter.md"]
    assert plan.recommended_batches == [["chapter.md"]]


def test_markdown_handoff_planning_rejects_unsafe_or_missing_inventory(tmp_path):
    import pytest
    from pdf2epub.markdown_handoff_planning import plan_markdown_handoff

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    with pytest.raises(ValueError, match="Invalid declared"):
        plan_markdown_handoff(source_dir, "translate", declared_files=["../outside.md"])
    with pytest.raises(ValueError, match="missing"):
        plan_markdown_handoff(source_dir, "translate", declared_files=["missing.md"])


def test_compressed_cache_service_receipt_tracks_input_and_active_files(tmp_path):
    import json
    from pdf2epub.html_translation.compressed_cache import CompressedExtractionCache

    output = tmp_path / "output"
    units = output / "compressed_units"
    units.mkdir(parents=True)
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"epub")
    (units / "part1.md").write_text("active", encoding="utf-8")
    (units / "unused.md").write_text("inactive", encoding="utf-8")
    (units / "unit.mapping.json").write_text(
        json.dumps({"translation_parts": ["part1.md"]}), encoding="utf-8"
    )
    cache = CompressedExtractionCache(output, epub, units)
    cache.write(max_nonempty_lines=350)
    assert cache.is_current(max_nonempty_lines=350)
    assert set(cache.files()) == {"unit.mapping.json", "part1.md"}
    assert not cache.is_current(max_nonempty_lines=351)
    epub.write_bytes(b"changed")
    assert not cache.is_current(max_nonempty_lines=350)


def test_compressed_cache_service_fails_closed_for_corrupt_json_shapes(tmp_path):
    import json
    from pdf2epub.html_translation.compressed_cache import CompressedExtractionCache

    output = tmp_path / "output"
    units = output / "compressed_units"
    units.mkdir(parents=True)
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"epub")
    (units / "unit.md").write_text("active", encoding="utf-8")
    mapping = units / "unit.mapping.json"
    cache = CompressedExtractionCache(output, epub, units)
    for invalid_json in ("null", "[]", '"text"', "{"):
        mapping.write_text(invalid_json, encoding="utf-8")
        assert not cache.is_current(max_nonempty_lines=350)
        try:
            cache.write(max_nonempty_lines=350)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid mapping must not produce a receipt")
        assert not cache.path.exists()

    mapping.write_text(json.dumps({"translation_parts": ["missing.md"]}), encoding="utf-8")
    assert not cache.is_current(max_nonempty_lines=350)
    try:
        cache.write(max_nonempty_lines=350)
    except ValueError:
        pass
    else:
        raise AssertionError("missing active file must not produce a receipt")
    assert not cache.path.exists()


def test_compressed_cache_service_rejects_escaping_active_paths(tmp_path):
    import json
    from pdf2epub.html_translation.compressed_cache import CompressedExtractionCache

    output = tmp_path / "output"
    units = output / "compressed_units"
    units.mkdir(parents=True)
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"epub")
    outside = tmp_path / "outside.md"
    outside.write_text("must not be read", encoding="utf-8")
    mapping = units / "unit.mapping.json"
    cache = CompressedExtractionCache(output, epub, units)
    for unsafe in ("../outside.md", str(outside.resolve())):
        mapping.write_text(json.dumps({"translation_parts": [unsafe]}), encoding="utf-8")
        assert not cache.is_current(max_nonempty_lines=350)
        try:
            cache.files()
        except ValueError:
            pass
        else:
            raise AssertionError("escaping active path must be rejected")
        assert not cache.path.exists()


def test_compressed_cache_service_rejects_non_object_receipts_and_deleted_parts(tmp_path):
    import json
    from pdf2epub.html_translation.compressed_cache import CompressedExtractionCache

    output = tmp_path / "output"
    units = output / "compressed_units"
    units.mkdir(parents=True)
    epub = tmp_path / "book.epub"
    epub.write_bytes(b"epub")
    active = units / "part1.md"
    active.write_text("active", encoding="utf-8")
    (units / "unit.mapping.json").write_text(
        json.dumps({"translation_parts": ["part1.md"]}), encoding="utf-8"
    )
    cache = CompressedExtractionCache(output, epub, units)
    for invalid_receipt in ("[]", "null", '"text"'):
        cache.path.write_text(invalid_receipt, encoding="utf-8")
        assert not cache.is_current(max_nonempty_lines=350)
    cache.write(max_nonempty_lines=350)
    assert cache.is_current(max_nonempty_lines=350)
    active.unlink()
    assert not cache.is_current(max_nonempty_lines=350)


def test_extracted_services_do_not_import_their_orchestrators():
    for path, forbidden in (
        ("pdf2epub/markdown_handoff_planning.py", "markdown_handoff"),
        ("pdf2epub/html_translation/compressed_cache.py", "html_translation.builder"),
    ):
        for node in _imports(path):
            assert not (node.module and node.module.endswith(forbidden)), path
