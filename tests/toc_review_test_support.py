"""Build explicit modern TOC review artifacts for synthetic refine fixtures."""
import hashlib
import json
from pathlib import Path

from pdf2epub.refine.subagent_workflow import page_numbers
from pdf2epub.refine.toc_structure_review import build_structure_review


def install_toc_review_contract(output_dir: Path, pages_dir: Path | None = None) -> None:
    output_dir = Path(output_dir)
    pages_dir = Path(pages_dir or output_dir / "pages")
    tree_path = output_dir / "toc_tree.json"
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    outline_path = output_dir / "outline_toc_draft.json"
    if not outline_path.exists():
        outline_path.write_text(json.dumps({"schema_version": 1, "chapters": []}), encoding="utf-8")
    pdf_path = output_dir / "input_original.pdf"
    if not pdf_path.exists():
        pdf_path = output_dir / "input.pdf"
    pages = {}
    for page in page_numbers(pages_dir):
        pages[page] = (pages_dir / f"page_{page:03d}.md").read_text(encoding="utf-8").splitlines()
    manifest = build_structure_review(output_dir, pages_dir, "ocr", outline_path, pdf_path)
    candidates_doc = json.loads((output_dir / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    candidates = candidates_doc["candidates"]
    by_location = {
        (c["page"], c["line"]): c
        for c in candidates
        if c.get("kind") == "text_line" and type(c.get("line")) is int
    }
    used = set()
    decisions = [{"candidate_id": c["candidate_id"], "disposition": "body_text"} for c in candidates]
    ids = set()

    def visit(nodes):
        for node in nodes:
            page = node["start_page"]
            lines = pages[page]
            boundary = node.setdefault("boundary_info", {})
            line = boundary.get("start_line", 1)
            if type(line) is not int or line < 1 or line > len(lines) or not lines[line - 1].strip() or (page, line) in used:
                line = next((n for n, value in enumerate(lines, 1) if value.strip() and (page, n) not in used), 1)
            boundary["start_line"] = line
            used.add((page, line))
            excerpt = lines[line - 1].strip()
            candidate = by_location.get((page, line))
            if candidate:
                cid = candidate["candidate_id"]
                decisions[:] = [d for d in decisions if d["candidate_id"] != cid]
                decisions.append({"candidate_id": cid, "disposition": "toc_node", "node_id": cid, "binding_mode": "visible"})
            else:
                cid = "reviewer-" + hashlib.sha256(f"{page}:{line}:{excerpt}".encode()).hexdigest()[:20]
                decisions.append({"candidate_id": cid, "disposition": "toc_node", "node_id": cid,
                                  "binding_mode": "visible", "evidence": {"page": page, "line": line}, "excerpt": excerpt})
            if cid in ids:
                # The fixture described two nodes at one source location; give
                # the extra node a bounded invisible identity.
                node["binding_mode"] = "container_only"
                node_id = f"container-{page}-{line}-{len(ids)}"
                node["node_id"] = node_id
                decisions[-1]["node_id"] = node_id
                decisions[-1]["binding_mode"] = "container_only"
                decisions[-1]["reason"] = "synthetic structural container fixture"
            else:
                node["binding_mode"] = "visible"
                node["node_id"] = cid
            ids.add(node["node_id"])
            visit(node.get("children", []))

    visit(tree["chapters"])
    tree["schema_version"] = 2
    tree_path.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")
    evidence = "visual" if manifest["visual_evidence"] else "page_text"
    decision_doc = {"schema_version": 1, "source_fingerprint": manifest["source_fingerprint"],
                    "candidate_inventory_sha256": candidates_doc["inventory_sha256"],
                    "candidate_decisions": decisions,
                    "page_coverage": [{"page": p, "reviewed": True, "evidence": [evidence]} for p in pages]}
    (output_dir / "toc_structure_decisions.json").write_text(json.dumps(decision_doc), encoding="utf-8")
    prepare_manifest = {"schema_version": 1, "structure_review": manifest}
    (output_dir / "refine_subagent_manifest.json").write_text(json.dumps(prepare_manifest), encoding="utf-8")
