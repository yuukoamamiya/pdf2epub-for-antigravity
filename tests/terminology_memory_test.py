import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pdf2epub.terminology_memory import MemoryAuthority, prepare_memory, publish_memory
from pdf2epub.subagent_dispatch import build_assignment_sha256


def sha(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def assignment(worker_id, filename):
    value = {"task": "translate", "worker_id": worker_id, "assigned_files": [filename], "batch_ids": [worker_id]}
    digest = build_assignment_sha256(**value)
    return digest, value


def authority(tmp_path, *, book_entities_path=None, book_entities_sha256=None):
    workspace = tmp_path / "work"
    source_dir, target_dir = workspace / "source", workspace / "target"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    (source_dir / "part1.md").write_text("existing phrase", encoding="utf-8")
    (target_dir / "part1.md").write_text("旧译文中没有新术语", encoding="utf-8")
    (source_dir / "part2.md").write_text("social form and class relation", encoding="utf-8")
    (target_dir / "part2.md").write_text("社会形式以及阶级关系", encoding="utf-8")
    a1, c1 = assignment("w1", "part1.md")
    a2, c2 = assignment("w2", "part2.md")
    inventory = {
        "unit-1": {
            "source_file": "part1.md", "target_file": "part1.md",
            "source_sha256": hashlib.sha256((source_dir / "part1.md").read_bytes()).hexdigest(),
            "target_sha256": hashlib.sha256((target_dir / "part1.md").read_bytes()).hexdigest(),
            "chapter_id": "chapter-1", "scope": {"kind": "chapter", "id": "chapter-1"},
            "effective_projection_sha256": sha("old-projection"),
        }
    }
    return MemoryAuthority(
        workspace=workspace,
        source_dir=source_dir,
        target_dir=target_dir,
        assignments={a1: c1, a2: c2},
        book_id="book-1",
        source_language="English",
        target_language="Chinese",
        chapter_map={"part1.md": "chapter-1", "part2.md": "chapter-2"},
        unit_inventory=inventory,
        book_entities_path=book_entities_path,
        book_entities_sha256=book_entities_sha256,
    )


def candidate(auth, term="social form", target="社会形式", cid="c1"):
    source_text = (auth.source_dir / "part2.md").read_bytes()
    target_text = (auth.target_dir / "part2.md").read_bytes()
    assignment_sha, _ = assignment("w2", "part2.md")
    return {
        "candidate_id": cid,
        "source": term,
        "target": target,
        "source_file": "part2.md",
        "target_file": "part2.md",
        "source_evidence": term,
        "target_evidence": target,
        "source_sha256": hashlib.sha256(source_text).hexdigest(),
        "target_sha256": hashlib.sha256(target_text).hexdigest(),
        "assignment_sha256": assignment_sha,
        "scope": {"kind": "chapter", "id": "chapter-2"},
        "language_pair": {"source": auth.source_language, "target": auth.target_language},
    }


def affected_part1(auth):
    return {
        "unit_id": "unit-1", **dict(auth.unit_inventory["unit-1"]),
    }


def prepare(tmp_path, auth, candidates=None, affected=(), book_entities=()):
    return prepare_memory(
        tmp_path / "memory", candidates or [candidate(auth)], authority=auth,
        affected_targets=affected, book_entities=book_entities,
    )


def review_for(packet):
    return {
        "prepared_packet_sha256": packet["prepared_packet_sha256"],
        "candidates": [{"candidate_id": row["candidate_id"], "decision": "approve"} for row in packet["candidates"]],
        "affected_targets": [
            {**{key: row[key] for key in ("unit_id", "source_sha256", "target_sha256", "scope")}, "decision": "consistent"}
            for row in packet["affected_targets"]
        ],
    }


def publish(root, packet, review, auth, parent):
    return publish_memory(
        root, packet, review, authority=auth, current_snapshot_sha256=parent,
        lease_state="none",
        operator_attestation={
            "no_active_sessions": True, "checked_at": "2026-11-04T00:00:00Z",
        "book_id": auth.book_id, "authority_sha256": auth.authority_sha256,
            "evidence_sha256": sha("attestation"),
        },
    )


def test_authority_checks_membership_scope_live_files_and_evidence(tmp_path):
    auth = authority(tmp_path)
    bad = candidate(auth)
    bad["source_evidence"] = "fabricated evidence"
    with pytest.raises(ValueError, match="source evidence"):
        prepare(tmp_path, auth, [bad])
    bad = candidate(auth)
    bad["scope"] = {"kind": "chapter", "id": "chapter-elsewhere"}
    with pytest.raises(ValueError, match="scope"):
        prepare(tmp_path, auth, [bad])
    bad = candidate(auth)
    bad["target_sha256"] = sha("stale target")
    with pytest.raises(ValueError, match="target_sha256 is stale"):
        prepare(tmp_path, auth, [bad])
    bad = candidate(auth)
    bad["source_file"] = "../outside.md"
    with pytest.raises(ValueError, match="must belong to its referenced assignment"):
        prepare(tmp_path, auth, [bad])


def test_packet_tamper_after_prepare_is_rejected_even_with_old_review(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    packet["candidates"][0]["target"] = "未经审阅的新译法"
    with pytest.raises(ValueError, match="content hash mismatch|target evidence"):
        publish(root, packet, review, auth, None)
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


def test_prepare_review_publish_and_persisted_consistency_evidence(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    digest = publish(root, packet, review, auth, None)
    snapshot = json.loads((root / "snapshots" / f"{digest}.json").read_text(encoding="utf-8"))
    assert snapshot["review"] == review
    assert snapshot["entries"][0]["target"] == "社会形式"
    assert json.loads((root / "current.json").read_text(encoding="utf-8"))["snapshot_sha256"] == digest


def test_book_entity_original_alias_and_variant_conflicts_block_prepare(tmp_path):
    auth = authority(tmp_path)
    for entity in (
        {"original": "social form", "suggested_translation": "社会构型"},
        {"original": "other", "suggested_translation": "别的", "aliases": ["social form"]},
        {"original": "other", "suggested_translation": "别的", "variants": ["social form"]},
    ):
        from pdf2epub.terminology_memory import _check_book_entity_conflicts
        with pytest.raises(ValueError, match="book entity"):
            _check_book_entity_conflicts([candidate(auth)], [entity])


def test_live_book_entity_file_is_bound_and_changes_block_publish(tmp_path):
    auth0 = authority(tmp_path)
    entity_path = auth0.workspace / "translation_entities.json"
    document = {"terms": [{"original": "other", "suggested_translation": "其他"}]}
    raw = json.dumps(document, ensure_ascii=False).encode()
    entity_path.write_bytes(raw)
    auth = replace(auth0, book_entities_path=entity_path, book_entities_sha256=hashlib.sha256(raw).hexdigest())
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    entity_path.write_text(json.dumps({"terms": [{"original": "social form", "suggested_translation": "社会构型"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="book entity file changed|live binding"):
        publish(root, packet, review, auth, None)
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


@pytest.mark.parametrize("malformation", ["list", "row", "id", "decision"])
def test_malformed_review_fails_as_value_error_without_artifacts(tmp_path, malformation):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    if malformation == "list":
        review = []
    elif malformation == "row":
        review["candidates"] = [None]
    elif malformation == "id":
        review["candidates"][0]["candidate_id"] = []
    else:
        review["candidates"][0]["decision"] = []
    with pytest.raises(ValueError):
        publish(root, packet, review, auth, None)
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


def test_two_generations_cumulatively_keep_parent_and_replay_is_idempotent(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet1 = prepare(tmp_path, auth)
    review1 = review_for(packet1)
    first = publish(root, packet1, review1, auth, None)
    replay = publish(root, packet1, review1, auth, None)
    assert replay == first
    packet2 = prepare(tmp_path, auth, [candidate(auth, "class relation", "阶级关系", "c2")])
    review2 = review_for(packet2)
    second = publish(root, packet2, review2, auth, first)
    snapshot = json.loads((root / "snapshots" / f"{second}.json").read_text(encoding="utf-8"))
    assert {row["candidate_id"] for row in snapshot["entries"]} == {"c1", "c2"}
    assert publish(root, packet2, review2, auth, first) == second


def test_same_form_conflicts_across_overlapping_scope_block_publish(tmp_path):
    auth = authority(tmp_path)
    target_path = auth.target_dir / "part2.md"
    target_path.write_text("社会形式以及社会构型", encoding="utf-8")
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth, [
        candidate(auth, "social form", "社会形式", "c1"),
        candidate(auth, "social form", "社会构型", "c2"),
    ])
    with pytest.raises(ValueError, match="candidate_id is duplicated|terminology form conflict"):
        publish(root, packet, review_for(packet), auth, None)


def test_source_forms_are_not_confused_with_target_forms(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth, [
        candidate(auth, "social form", "阶级关系", "c1"),
        candidate(auth, "class relation", "社会形式", "c2"),
    ])
    digest = publish(root, packet, review_for(packet), auth, None)
    assert digest


def test_nonempty_affected_prior_wave_can_request_retranslation(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth, affected=[affected_part1(auth)])
    assert packet["candidates"][0]["source_file"] == "part2.md"
    assert packet["affected_targets"][0]["source_file"] == "part1.md"
    review = review_for(packet)
    review["affected_targets"][0]["decision"] = "retranslate_required"
    digest = publish(root, packet, review, auth, None)
    snapshot = json.loads((root / "snapshots" / f"{digest}.json").read_text(encoding="utf-8"))
    assert snapshot["affected_targets"][0]["unit_id"] == "unit-1"
    assert snapshot["review"]["affected_targets"][0]["decision"] == "retranslate_required"


def test_candidate_cannot_use_another_assignment_through_union(tmp_path):
    auth = authority(tmp_path)
    bad = candidate(auth)
    bad["source_file"] = "part1.md"
    bad["target_file"] = "part1.md"
    bad["scope"] = {"kind": "chapter", "id": "chapter-1"}
    with pytest.raises(ValueError, match="must belong to its referenced assignment"):
        prepare(tmp_path, auth, [bad])


def test_source_and_target_files_must_follow_one_to_one_mapping(tmp_path):
    auth = authority(tmp_path)
    bad = candidate(auth)
    bad["target_file"] = "part1.md"
    with pytest.raises(ValueError, match="must match source_file"):
        prepare(tmp_path, auth, [bad])


def test_lease_operator_barrier_and_cas_failure_create_no_orphan_snapshot(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    base = {"authority": auth, "current_snapshot_sha256": None, "lease_state": "none"}
    with pytest.raises(ValueError, match="operator no-active-session"):
        publish_memory(root, packet, review, **base)
    with pytest.raises(ValueError, match="compare-and-swap"):
        publish(root, packet, review, auth, "a" * 64)
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


def test_active_or_unconfirmed_expired_session_blocks_without_mutation(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    review = review_for(packet)
    base = {"authority": auth, "current_snapshot_sha256": None}
    with pytest.raises(ValueError, match="active dispatch"):
        publish_memory(root, packet, review, lease_state="active", **base)
    with pytest.raises(ValueError, match="session-stop evidence"):
        publish_memory(root, packet, review, lease_state="expired", lease_session_id="s1", **base)
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


def test_competing_pointer_lock_fails_without_creating_snapshot(tmp_path):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    (root / "current.lock").write_text("held", encoding="utf-8")
    with pytest.raises(ValueError, match="locked"):
        publish(root, packet, review_for(packet), auth, None)
    assert not list((root / "snapshots").glob("*.json"))


def test_live_target_change_at_lock_acquisition_is_rechecked_before_snapshot(tmp_path, monkeypatch):
    import pdf2epub.terminology_memory as memory

    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    original_open = memory.os.open
    changed = False

    def racing_open(path, flags, mode=0o777):
        nonlocal changed
        descriptor = original_open(path, flags, mode)
        if Path(path) == root / "current.lock" and not changed:
            changed = True
            (auth.target_dir / "part2.md").write_text("社会形式以及阶级关系。", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(memory.os, "open", racing_open)
    with pytest.raises(ValueError, match="target_sha256 is stale"):
        publish(root, packet, review_for(packet), auth, None)
    assert changed
    assert not (root / "current.json").exists()
    assert not list((root / "snapshots").glob("*.json"))


def test_parent_digest_and_snapshot_root_confinement(tmp_path):
    from pdf2epub.terminology_memory import _load_parent, _current_parent

    auth = authority(tmp_path)
    root = tmp_path / "memory"
    root.mkdir()
    (root / "current.json").write_text(json.dumps({"snapshot_sha256": "../../outside", "path": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid digest"):
        _current_parent(root)
    (root / "current.json").unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / f"{'a' * 64}.json").write_text("{}", encoding="utf-8")
    try:
        (root / "snapshots").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ValueError, match="escapes memory root"):
        _load_parent(root, "a" * 64, auth)


def test_first_publish_rejects_snapshots_escape_before_creating_snapshot(tmp_path, monkeypatch):
    auth = authority(tmp_path)
    root = tmp_path / "memory"
    packet = prepare(tmp_path, auth)
    external = tmp_path / "external-store"
    external.mkdir()
    original_resolve = Path.resolve

    def escaped_resolve(path, *args, **kwargs):
        if path.name == "snapshots":
            return external
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", escaped_resolve)
    with pytest.raises(ValueError, match="snapshots directory escapes memory root"):
        publish(root, packet, review_for(packet), auth, None)
    assert not (root / "current.json").exists()
    assert not (external / "snapshots").exists()
    assert not list(external.glob("*.json"))
