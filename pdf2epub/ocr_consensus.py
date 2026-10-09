"""Local OCR-consensus contracts for selective visual review.

The primary OCR output remains the workflow source.  A configured secondary
OCR backend is used only to find pages that deserve visual Subagent review;
it never silently replaces the primary text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Dict, Mapping, Optional

from pdf2epub.workflow_contracts import atomic_write_text, sha256_file


OCR_CONSENSUS_SCHEMA_VERSION = 4
DEFAULT_MAX_EDIT_RATIO = 0.08
DEFAULT_MIN_CHANGED_CHARS = 2
# A one-line omission is exactly the failure mode this consensus gate is
# meant to catch.  Keep the default strict; users may relax it explicitly for
# unusual OCR layouts through ``ocr.consensus.max_line_delta``.
DEFAULT_MAX_LINE_DELTA = 0
DEFAULT_COMMON_MISS_SAMPLE_EVERY = 20
DEFAULT_COMMON_MISS_DENSITY_RATIO = 0.30
DEFAULT_COMMON_MISS_MIN_NEIGHBOR_CHARS = 800
DEFAULT_COMMON_MISS_MIN_PAGE_CHARS = 120


def secondary_page_dir(output_dir: Path) -> Path:
    return Path(output_dir) / "ocr_secondary"


def consensus_dir(output_dir: Path) -> Path:
    return Path(output_dir) / "ocr_consensus"


def consensus_manifest_path(output_dir: Path) -> Path:
    return Path(output_dir) / "ocr_consensus.json"


def _ocr_config(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _config_bool(value: Any, default: bool) -> bool:
    """Read a YAML boolean without treating arbitrary strings as truthy."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "on", "1"}:
            return True
        if normalized in {"false", "no", "off", "0"}:
            return False
    return default


def secondary_ocr_enabled(config: Mapping[str, Any]) -> bool:
    """Return whether the optional secondary OCR pass is enabled.

    ``ocr.secondary_backend`` remains a backwards-compatible shorthand. The
    structured ``ocr.secondary.enabled`` switch takes precedence whenever the
    structured block is present.
    """
    ocr = _ocr_config(config)
    secondary = ocr.get("secondary")
    if isinstance(secondary, Mapping):
        backend = str(secondary.get("backend") or "").strip()
        if "enabled" in secondary:
            return _config_bool(secondary.get("enabled"), False)
        return bool(backend)
    return bool(str(ocr.get("secondary_backend") or "").strip())


def secondary_backend_name(config: Mapping[str, Any]) -> Optional[str]:
    ocr = _ocr_config(config)
    secondary = ocr.get("secondary")
    if isinstance(secondary, Mapping):
        value = secondary.get("backend")
    else:
        value = ocr.get("secondary_backend")
    if not secondary_ocr_enabled(config):
        return None
    value = str(value or "").strip().lower()
    return value or None


def ocr_evidence_mode(config: Mapping[str, Any] | None) -> str:
    """Return the configured evidence mode used by layout decisions.

    The existing ``ocr.secondary.enabled`` switch is the single source of
    truth: disabled means one OCR result is used, enabled means every layout
    gate must have a current two-OCR consensus checkpoint.
    """
    return "two_ocr" if config is not None and secondary_ocr_enabled(config) else "single_ocr"


def validate_ocr_config(config: Mapping[str, Any]) -> None:
    """Validate the independent OCR and PP-DocLayout configuration gates."""
    ocr = _ocr_config(config)
    secondary = ocr.get("secondary")
    if (
        isinstance(secondary, Mapping)
        and secondary_ocr_enabled(config)
        and not str(secondary.get("backend") or "").strip()
    ):
        raise ValueError(
            "ocr.secondary.enabled is true but ocr.secondary.backend is missing"
        )

    layout = ocr.get("layout")
    if not isinstance(layout, Mapping):
        return
    if not _config_bool(layout.get("enabled"), False):
        return

    backend = str(layout.get("backend") or "pp_doclayout").strip().lower()
    if backend != "pp_doclayout":
        raise ValueError(
            "ocr.layout.backend must be 'pp_doclayout' when PP-DocLayout is enabled"
        )
    model_name = str(layout.get("model_name") or "PP-DocLayout-L").strip()
    if not model_name:
        raise ValueError("ocr.layout.model_name must not be empty")
    device = str(layout.get("device") or "gpu:0").strip().lower()
    if device == "gpu":
        device = "gpu:0"
    if not device.startswith("gpu:"):
        raise ValueError(
            "ocr.layout.device must be a GPU device such as gpu:0; CPU fallback is disabled"
        )
    try:
        dpi = int(layout.get("dpi", 192))
    except (TypeError, ValueError) as exc:
        raise ValueError("ocr.layout.dpi must be an integer between 72 and 600") from exc
    if not 72 <= dpi <= 600:
        raise ValueError("ocr.layout.dpi must be an integer between 72 and 600")


def consensus_settings(config: Mapping[str, Any]) -> Dict[str, Any]:
    ocr = _ocr_config(config)
    value = ocr.get("consensus", {})
    return dict(value) if isinstance(value, Mapping) else {}


def common_miss_settings(config: Mapping[str, Any]) -> Dict[str, Any]:
    value = consensus_settings(config).get("common_miss", {})
    return dict(value) if isinstance(value, Mapping) else {}


def secondary_config_hash(config: Mapping[str, Any]) -> str:
    ocr = _ocr_config(config)
    backend = secondary_backend_name(config)
    backends = ocr.get("backends", {})
    backend_settings = backends.get(backend, {}) if isinstance(backends, Mapping) else {}
    payload = {
        "backend": backend,
        "backend_settings": backend_settings,
        "consensus": consensus_settings(config),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _comparable_text(text: str) -> str:
    """Normalize layout syntax without hiding meaningful characters."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    # Chandra materializes semantic footnotes as Markdown while a secondary
    # OCR pass may only recover the visible numeric definition marker.
    # Compare their visible content, not the backend-specific delimiters.
    value = re.sub(r"\[\^([^\]]+)\]\s*:", r"\1 ", value)
    value = re.sub(r"\[\^([^\]]+)\]", r"\1", value)
    lines = []
    for line in value.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*[-+*]\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"[`*_]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _changed_char_count(primary: str, secondary: str) -> int:
    matcher = SequenceMatcher(None, primary, secondary, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed += max(i2 - i1, j2 - j1)
    return changed


def compare_ocr_texts(
    primary_text: str,
    secondary_text: str,
    config: Mapping[str, Any],
    *,
    primary_backend: Optional[str] = None,
    secondary_backend: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare two OCR outputs and classify whether visual review is needed.

    Chandra and Paddle do not emit equivalent line or numeric-marker streams:
    Chandra is a semantic document OCR while Paddle is a line detector.  Those
    fields remain useful diagnostics, but they must not independently block the
    page-level text correction gate for that pair.  Callers that do not provide
    backend names retain the legacy strict comparison for compatibility.
    """
    primary = _comparable_text(primary_text)
    secondary = _comparable_text(secondary_text)
    settings = consensus_settings(config)
    max_edit_ratio = float(settings.get("max_edit_ratio", DEFAULT_MAX_EDIT_RATIO))
    min_changed_chars = int(settings.get("min_changed_chars", DEFAULT_MIN_CHANGED_CHARS))
    max_line_delta = int(settings.get("max_line_delta", DEFAULT_MAX_LINE_DELTA))
    max_edit_ratio = max(0.0, min(1.0, max_edit_ratio))
    min_changed_chars = max(1, min_changed_chars)
    max_line_delta = max(0, max_line_delta)
    heterogeneous_layout_pair = {
        str(primary_backend or "").strip().lower(),
        str(secondary_backend or "").strip().lower(),
    } == {"chandra", "paddle"}

    if not primary or not secondary:
        reasons = ["one OCR result is empty"]
        status = "review_required"
        ratio = 1.0 if primary != secondary else 0.0
    else:
        matcher_ratio = SequenceMatcher(None, primary, secondary, autojunk=False).ratio()
        ratio = 1.0 - matcher_ratio
        reasons = []
        changed_chars = _changed_char_count(primary, secondary)
        primary_lines = len([line for line in str(primary_text).splitlines() if line.strip()])
        secondary_lines = len([line for line in str(secondary_text).splitlines() if line.strip()])
        line_counts_differ = abs(primary_lines - secondary_lines) > max_line_delta
        if line_counts_differ and not heterogeneous_layout_pair:
            reasons.append("non-empty line counts differ")
        primary_numeric_markers = re.findall(r"\d+(?:[./:-]\d+)*", primary)
        secondary_numeric_markers = re.findall(r"\d+(?:[./:-]\d+)*", secondary)
        numeric_markers_differ = primary_numeric_markers != secondary_numeric_markers
        if numeric_markers_differ and not heterogeneous_layout_pair:
            reasons.append("numeric markers differ")
        if changed_chars >= min_changed_chars and ratio > max_edit_ratio:
            reasons.append("OCR text differs above the configured threshold")
        status = "review_required" if reasons else "agree"

    return {
        "status": status,
        "reasons": reasons,
        "normalized_edit_ratio": round(ratio, 6),
        "changed_char_count": _changed_char_count(primary, secondary),
        "primary_normalized_chars": len(primary),
        "secondary_normalized_chars": len(secondary),
        "primary_nonempty_line_count": len(
            [line for line in str(primary_text).splitlines() if line.strip()]
        ),
        "secondary_nonempty_line_count": len(
            [line for line in str(secondary_text).splitlines() if line.strip()]
        ),
        "diagnostics": {
            "line_counts_differ": (
                False
                if not primary and not secondary
                else (
                    abs(
                        len([line for line in str(primary_text).splitlines() if line.strip()])
                        - len([line for line in str(secondary_text).splitlines() if line.strip()])
                    )
                    > max_line_delta
                )
            ),
            "numeric_markers_differ": (
                bool(primary and secondary)
                and re.findall(r"\d+(?:[./:-]\d+)*", primary)
                != re.findall(r"\d+(?:[./:-]\d+)*", secondary)
            ),
            "heterogeneous_layout_pair": heterogeneous_layout_pair,
        },
    }


def _layout_block_text(block: Mapping[str, Any]) -> str:
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _layout_summary(sidecar: Mapping[str, Any]) -> Dict[str, Any]:
    blocks = sidecar.get("blocks", []) if isinstance(sidecar, Mapping) else []
    if not isinstance(blocks, list):
        blocks = []
    footnote_blocks = []
    footnote_keys = []
    footnote_y_values = []
    footnote_y_bins: set[int] = set()
    labels: Dict[str, int] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        label = str(block.get("label") or "").strip().casefold()
        labels[label] = labels.get(label, 0) + 1
        if label != "footnote":
            continue
        text = _layout_block_text(block)
        footnote_blocks.append(block)
        bbox = block.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                y0, y1 = float(bbox[1]), float(bbox[3])
                footnote_y_values.extend([y0, y1])
                first_bin = max(0, min(19, int(y0 // 50)))
                last_bin = max(0, min(19, int(max(y0, y1 - 1) // 50)))
                footnote_y_bins.update(range(first_bin, last_bin + 1))
            except (TypeError, ValueError):
                pass
        match = re.match(r"^\s*(?:\[\^)?(\d{1,4})", text)
        if match:
            footnote_keys.append(match.group(1))
    return {
        "block_count": len(blocks),
        "labels": labels,
        "footnote_block_count": len(footnote_blocks),
        "footnote_keys": footnote_keys,
        "has_footnote": bool(footnote_blocks),
        "footnote_y_range": (
            [min(footnote_y_values), max(footnote_y_values)]
            if footnote_y_values
            else None
        ),
        "footnote_y_bins": sorted(footnote_y_bins),
    }


def compare_ocr_layouts(
    primary_sidecar: Optional[Mapping[str, Any]],
    secondary_sidecar: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compare the small layout contract relevant to footnote triage.

    Backend layout labels are evidence rather than semantic truth. A label or
    note-key disagreement is therefore evidence for visual review, while
    agreement does not claim that either OCR is correct.
    """
    if not isinstance(primary_sidecar, Mapping) or not isinstance(secondary_sidecar, Mapping):
        return {
            "status": "not_available",
            "reasons": ["layout sidecar missing"],
        }
    primary = _layout_summary(primary_sidecar)
    secondary = _layout_summary(secondary_sidecar)
    reasons: list[str] = []
    if primary["has_footnote"] != secondary["has_footnote"]:
        reasons.append("footnote label presence differs")
    if primary["footnote_keys"] != secondary["footnote_keys"]:
        reasons.append("footnote numeric keys differ")
    if primary["footnote_y_bins"] != secondary["footnote_y_bins"]:
        reasons.append("footnote vertical coverage differs")
    primary_range = primary.get("footnote_y_range")
    secondary_range = secondary.get("footnote_y_range")
    if primary_range and secondary_range and max(
        abs(primary_range[0] - secondary_range[0]),
        abs(primary_range[1] - secondary_range[1]),
    ) > 100:
        reasons.append("footnote vertical range differs")
    return {
        "status": "review_required" if reasons else "agree",
        "reasons": reasons,
        "primary": primary,
        "secondary": secondary,
    }


def common_ocr_risk_reasons(
    page_texts: Mapping[str, Mapping[str, str]],
    config: Mapping[str, Any],
) -> Dict[str, list[str]]:
    """Find pages both OCR engines could have missed in the same way.

    There is no textual ground truth for a common OCR error, so this is a
    conservative triage layer rather than a correctness proof. It combines a
    deterministic sample with an internal-page density outlier check. Any hit
    is sent to the visual reviewer, while ordinary pages remain auto-accepted.
    """
    settings = common_miss_settings(config)
    try:
        sample_every = max(
            0, int(settings.get("sample_every", DEFAULT_COMMON_MISS_SAMPLE_EVERY))
        )
    except (TypeError, ValueError):
        sample_every = DEFAULT_COMMON_MISS_SAMPLE_EVERY
    try:
        density_ratio = float(
            settings.get("density_ratio", DEFAULT_COMMON_MISS_DENSITY_RATIO)
        )
    except (TypeError, ValueError):
        density_ratio = DEFAULT_COMMON_MISS_DENSITY_RATIO
    try:
        min_neighbor_chars = max(
            1,
            int(
                settings.get(
                    "min_neighbor_chars", DEFAULT_COMMON_MISS_MIN_NEIGHBOR_CHARS
                )
            ),
        )
    except (TypeError, ValueError):
        min_neighbor_chars = DEFAULT_COMMON_MISS_MIN_NEIGHBOR_CHARS
    try:
        min_page_chars = max(
            0,
            int(settings.get("min_page_chars", DEFAULT_COMMON_MISS_MIN_PAGE_CHARS)),
        )
    except (TypeError, ValueError):
        min_page_chars = DEFAULT_COMMON_MISS_MIN_PAGE_CHARS
    density_ratio = max(0.05, min(0.9, density_ratio))

    names = sorted(str(name) for name in page_texts)
    metrics: Dict[str, tuple[int, int]] = {}
    for name in names:
        values = page_texts.get(name, {})
        primary = _comparable_text(str(values.get("primary") or ""))
        secondary = _comparable_text(str(values.get("secondary") or ""))
        metrics[name] = (
            int(round((len(primary) + len(secondary)) / 2)),
            int(
                round(
                    (
                        len([line for line in str(values.get("primary") or "").splitlines() if line.strip()])
                        + len([line for line in str(values.get("secondary") or "").splitlines() if line.strip()])
                    )
                    / 2
                )
            ),
        )

    risks: Dict[str, list[str]] = {}
    for index, name in enumerate(names):
        reasons: list[str] = []
        page_number_match = re.search(r"(?:^|[_-])(\d+)(?:\.[^.]+)?$", Path(name).stem)
        page_number = int(page_number_match.group(1)) if page_number_match else 0
        if sample_every and page_number and page_number % sample_every == 0:
            reasons.append(
                f"deterministic common-OCR sample (every {sample_every} pages)"
            )

        if 0 < index < len(names) - 1:
            current_chars, current_lines = metrics[name]
            previous_chars, previous_lines = metrics[names[index - 1]]
            next_chars, next_lines = metrics[names[index + 1]]
            neighbor_chars = [previous_chars, next_chars]
            neighbor_lines = [previous_lines, next_lines]
            if (
                current_chars >= min_page_chars
                and min(neighbor_chars) >= min_neighbor_chars
                and current_chars < median(neighbor_chars) * density_ratio
                and current_lines < median(neighbor_lines) * density_ratio
            ):
                reasons.append(
                    "internal page text density is anomalously low compared with both neighbors"
                )
        if reasons:
            risks[name] = reasons
    return risks


def write_page_consensus(
    output_dir: Path,
    *,
    source_name: str,
    primary_text: str,
    secondary_text: str,
    primary_backend: str,
    secondary_backend: str,
    config: Mapping[str, Any],
    primary_layout: Optional[Mapping[str, Any]] = None,
    secondary_layout: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist one secondary OCR output and its comparison record."""
    output_dir = Path(output_dir)
    secondary_dir = secondary_page_dir(output_dir)
    record_dir = consensus_dir(output_dir)
    secondary_dir.mkdir(parents=True, exist_ok=True)
    record_dir.mkdir(parents=True, exist_ok=True)
    secondary_path = secondary_dir / source_name
    atomic_write_text(secondary_path, str(secondary_text or ""))
    comparison = compare_ocr_texts(
        primary_text,
        secondary_text,
        config,
        primary_backend=primary_backend,
        secondary_backend=secondary_backend,
    )
    layout_comparison = compare_ocr_layouts(primary_layout, secondary_layout)
    record = {
        "schema_version": OCR_CONSENSUS_SCHEMA_VERSION,
        "source_file": source_name,
        "primary_backend": primary_backend,
        "secondary_backend": secondary_backend,
        "primary_sha256": hashlib.sha256(str(primary_text).encode("utf-8")).hexdigest(),
        "secondary_sha256": hashlib.sha256(str(secondary_text).encode("utf-8")).hexdigest(),
        "secondary_file": f"ocr_secondary/{source_name}",
        "secondary_layout_file": f"ocr_secondary/{Path(source_name).stem}.ocr.json",
        "comparison": comparison,
        "layout_comparison": layout_comparison,
        # Layout disagreement is consumed by footnote/illustration stages as
        # evidence.  It must not turn a page into an OCR text-correction task:
        # Chandra and Paddle intentionally use different block granularity.
        "layout_review": layout_comparison.get("status") == "review_required",
        "action": "visual_review" if comparison["status"] == "review_required" else "auto_accept",
    }
    atomic_write_text(
        record_dir / f"{Path(source_name).stem}.json",
        json.dumps(record, ensure_ascii=False, indent=2),
    )
    return record


def rebuild_ocr_consensus(
    output_dir: Path,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Recompute consensus from existing primary/secondary OCR artifacts.

    This is deliberately offline.  It is used after changing comparison
    policy so a completed secondary OCR batch does not need to be run again.
    The function still requires the complete page and sidecar set and writes
    the same freshness-bound manifest as the online OCR path.
    """
    output_dir = Path(output_dir)
    secondary_backend = secondary_backend_name(config)
    if not secondary_backend:
        raise ValueError(
            "ocr-consensus-rebuild requires ocr.secondary.enabled and a backend"
        )

    pages_dir = output_dir / "pages"
    secondary_dir = secondary_page_dir(output_dir)
    try:
        progress = json.loads(
            (pages_dir / "ocr_progress.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("pages/ocr_progress.json is missing or invalid") from exc
    if not isinstance(progress, Mapping):
        raise ValueError("pages/ocr_progress.json must contain an object")

    try:
        total_pages = int(progress.get("total_pages") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("ocr_progress.json has an invalid total_pages value") from exc
    if total_pages <= 0:
        raise ValueError("ocr_progress.json has no positive total_pages value")

    primary_backend = str(progress.get("backend") or "").strip().lower()
    source_sha256 = str(progress.get("source_sha256") or "").strip()
    if not primary_backend or not source_sha256:
        raise ValueError(
            "ocr_progress.json must contain the current primary backend and source hash"
        )

    source_names = [f"page_{number:03d}.md" for number in range(1, total_pages + 1)]
    consensus_dir(output_dir).mkdir(parents=True, exist_ok=True)
    records: Dict[str, Any] = {}
    failed_pages: list[int] = []
    page_texts: Dict[str, Dict[str, str]] = {}

    def _load_sidecar(path: Path) -> Optional[Mapping[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, Mapping) else None

    for page_number, name in enumerate(source_names, 1):
        primary_path = pages_dir / name
        secondary_path = secondary_dir / name
        primary_sidecar_path = pages_dir / f"{Path(name).stem}.ocr.json"
        secondary_sidecar_path = secondary_dir / f"{Path(name).stem}.ocr.json"
        try:
            primary_text = primary_path.read_text(encoding="utf-8")
            secondary_text = secondary_path.read_text(encoding="utf-8")
            primary_sidecar = _load_sidecar(primary_sidecar_path)
            secondary_sidecar = _load_sidecar(secondary_sidecar_path)
            if primary_sidecar is None or secondary_sidecar is None:
                raise ValueError("primary or secondary layout sidecar is missing or invalid")
            record = write_page_consensus(
                output_dir,
                source_name=name,
                primary_text=primary_text,
                secondary_text=secondary_text,
                primary_backend=primary_backend,
                secondary_backend=secondary_backend,
                config=config,
                primary_layout=primary_sidecar,
                secondary_layout=secondary_sidecar,
            )
            record["primary_source_sha256"] = sha256_file(primary_path)
            record["primary_layout_sha256"] = sha256_file(primary_sidecar_path)
            record["secondary_layout_sha256"] = sha256_file(secondary_sidecar_path)
            atomic_write_text(
                consensus_dir(output_dir) / f"{Path(name).stem}.json",
                json.dumps(record, ensure_ascii=False, indent=2),
            )
            records[name] = record
            page_texts[name] = {
                "primary": primary_text,
                "secondary": secondary_text,
            }
        except (OSError, UnicodeError, ValueError) as exc:
            logger_name = f"{name}: {exc}"
            # Keep the manifest useful to the caller without introducing a
            # logging dependency into this low-level module.
            records[name] = {"source_file": name, "error": logger_name}
            failed_pages.append(page_number)

    manifest = {
        "schema_version": OCR_CONSENSUS_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "primary_backend": primary_backend,
        "secondary_backend": secondary_backend,
        "config_sha256": secondary_config_hash(config),
        "files": source_names,
        "records": records,
        "failed_pages": sorted(failed_pages),
        "complete": not failed_pages and len(records) == len(source_names),
        "rebuild_mode": "offline_existing_artifacts",
    }
    if manifest["complete"]:
        common_risks = common_ocr_risk_reasons(page_texts, config)
        for name, reasons in common_risks.items():
            record = records.get(name)
            if not isinstance(record, dict):
                continue
            record["common_miss_risks"] = reasons
            record["action"] = "visual_review"
            record["comparison"]["status"] = "review_required"
            record["comparison"].setdefault("reasons", []).extend(reasons)
            atomic_write_text(
                consensus_dir(output_dir) / f"{Path(name).stem}.json",
                json.dumps(record, ensure_ascii=False, indent=2),
            )
    atomic_write_text(
        consensus_manifest_path(output_dir),
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    review_pages = sorted(
        name
        for name, record in records.items()
        if isinstance(record, Mapping) and record.get("action") == "visual_review"
    )
    return {
        "enabled": True,
        "secondary_backend": secondary_backend,
        "review_pages": review_pages,
        "failed_pages": sorted(failed_pages),
        "manifest": manifest,
    }


def load_consensus_manifest(output_dir: Path) -> Dict[str, Any]:
    try:
        value = json.loads(consensus_manifest_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _primary_backend_from_progress(output_dir: Path) -> str:
    try:
        value = json.loads(
            (Path(output_dir) / "pages" / "ocr_progress.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return str(value.get("backend") or "").strip().lower()


def consensus_is_current(
    output_dir: Path,
    config: Mapping[str, Any],
    *,
    source_sha256: Optional[str] = None,
    primary_backend: Optional[str] = None,
) -> bool:
    """Check that every comparison belongs to the current primary OCR set."""
    backend = secondary_backend_name(config)
    if not backend:
        return False
    manifest = load_consensus_manifest(output_dir)
    pages_dir = Path(output_dir) / "pages"
    source_names = sorted(path.name for path in pages_dir.glob("*.md") if path.is_file())
    if not source_names:
        return False
    if manifest.get("schema_version") != OCR_CONSENSUS_SCHEMA_VERSION:
        return False
    if manifest.get("secondary_backend") != backend:
        return False
    if manifest.get("config_sha256") != secondary_config_hash(config):
        return False
    if source_sha256 is not None and manifest.get("source_sha256") != source_sha256:
        return False
    if primary_backend is not None and manifest.get("primary_backend") != primary_backend:
        return False
    if primary_backend is None:
        current_primary = _primary_backend_from_progress(output_dir)
        if current_primary and manifest.get("primary_backend") != current_primary:
            return False
    if manifest.get("files") != source_names:
        return False
    records = manifest.get("records", {})
    if not isinstance(records, Mapping):
        return False
    for name in source_names:
        source = pages_dir / name
        record = records.get(name)
        secondary = secondary_page_dir(output_dir) / name
        secondary_sidecar = secondary.with_suffix(".ocr.json")
        primary_sidecar = pages_dir / f"{Path(name).stem}.ocr.json"
        if not isinstance(record, Mapping) or not secondary.is_file() or not secondary_sidecar.is_file():
            return False
        if record.get("primary_source_sha256") != sha256_file(source):
            return False
        if record.get("secondary_sha256") != sha256_file(secondary):
            return False
        if record.get("secondary_layout_sha256") != sha256_file(secondary_sidecar):
            return False
        if primary_sidecar.is_file() and record.get("primary_layout_sha256") != sha256_file(primary_sidecar):
            return False
    return True


def review_required_files(output_dir: Path) -> list[str]:
    manifest = load_consensus_manifest(output_dir)
    records = manifest.get("records", {})
    if not isinstance(records, Mapping):
        return []
    return sorted(
        name
        for name, record in records.items()
        if isinstance(record, Mapping)
        and record.get("action") == "visual_review"
    )


def auto_accepted_files(output_dir: Path) -> list[str]:
    manifest = load_consensus_manifest(output_dir)
    files = manifest.get("files", [])
    if not isinstance(files, list):
        return []
    review = set(review_required_files(output_dir))
    return sorted(str(name) for name in files if str(name) not in review)


__all__ = [
    "OCR_CONSENSUS_SCHEMA_VERSION",
    "auto_accepted_files",
    "compare_ocr_layouts",
    "compare_ocr_texts",
    "common_miss_settings",
    "common_ocr_risk_reasons",
    "consensus_dir",
    "consensus_is_current",
    "consensus_manifest_path",
    "rebuild_ocr_consensus",
    "review_required_files",
    "ocr_evidence_mode",
    "secondary_ocr_enabled",
    "secondary_backend_name",
    "validate_ocr_config",
    "secondary_config_hash",
    "secondary_page_dir",
    "write_page_consensus",
]
