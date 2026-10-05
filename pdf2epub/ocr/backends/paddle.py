"""Optional local PaddleOCR adapter used for OCR consensus checks."""

from __future__ import annotations

import io
import html
import importlib.metadata
import json
from numbers import Real
import platform
import re
import sys
from typing import Any, Dict, List, Mapping, Tuple


def _backend_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    ocr = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    backends = ocr.get("backends", {}) if isinstance(ocr, Mapping) else {}
    settings = backends.get("paddle", {}) if isinstance(backends, Mapping) else {}
    return dict(settings) if isinstance(settings, Mapping) else {}


def init_client(config: Mapping[str, Any]):
    """Create the project's single supported local PaddleOCR client.

    The optional backend deliberately targets one PaddleOCR 3.7.0 /
    PaddlePaddle 3.3.1 API/runtime pair.  The project's semantic embedding
    dependency is optional so PaddleX/ModelScope does not accidentally load a
    separate PyTorch runtime into the OCR process.
    """
    try:
        from paddleocr import PaddleOCR
    except (ImportError, OSError) as exc:  # pragma: no cover - optional dependency/platform specific
        raise RuntimeError(
            "PaddleOCR is configured as a secondary OCR backend but its optional "
            "PaddleOCR 3.7.0/PaddlePaddle 3.3.1 dependencies could not be imported. "
            "Install the locked local OCR "
            "dependencies with `uv sync --extra ocr-local` and check the platform "
            "runtime/DLL requirements."
        ) from exc

    settings = _backend_config(config)
    kwargs: Dict[str, Any] = {}
    for key in (
        "lang",
        "device",
        "engine",
        "engine_config",
        "enable_mkldnn",
        "mkldnn_cache_capacity",
        "cpu_threads",
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_textline_orientation",
        "use_angle_cls",
    ):
        if key in settings:
            kwargs[key] = settings[key]

    # MKLDNN/OneDNN is the known failure mode on the Windows CPU wheels.  It
    # remains opt-in so users can explicitly enable it after verifying their
    # local Paddle runtime.  GPU configurations are unaffected by this CPU
    # safeguard.
    # Do not let Paddle auto-select a GPU for an auxiliary verification pass;
    # this keeps the optional backend deterministic on the supported Windows
    # CPU setup.
    kwargs.setdefault("device", "cpu")
    device = str(kwargs["device"] or "cpu").strip().lower()
    if device.startswith("cpu") and "enable_mkldnn" not in kwargs:
        kwargs["enable_mkldnn"] = False
    try:
        return PaddleOCR(**kwargs)
    except Exception as exc:  # pragma: no cover - optional dependency/platform specific
        raise RuntimeError(
            "PaddleOCR 3.7.0 initialization failed. Check the installed "
            "PaddleOCR/PaddlePaddle pair and the local CPU settings "
            "(enable_mkldnn=false is the safe Windows default)."
        ) from exc


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _distribution_diagnostics() -> Dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": sys.executable,
        "paddlepaddle_distribution": _distribution_version("paddlepaddle"),
        "paddleocr_distribution": _distribution_version("paddleocr"),
        "protobuf_distribution": _distribution_version("protobuf"),
    }


def _module_version_diagnostics(*, import_modules: bool) -> Dict[str, Any]:
    diagnostics: Dict[str, Any] = {}
    for module_name, key in (
        ("paddle", "paddle_version"),
        ("paddleocr", "paddleocr_version"),
        ("google.protobuf", "protobuf_runtime_version"),
    ):
        if not import_modules and module_name not in sys.modules:
            diagnostics[key] = None
            continue
        try:
            module = __import__(module_name, fromlist=["__version__"])
            value = getattr(module, "__version__", None)
        except Exception as exc:  # pragma: no cover - platform/runtime specific
            value = f"unavailable: {type(exc).__name__}: {exc}"
        diagnostics[key] = value
    return diagnostics


def runtime_diagnostics() -> Dict[str, Any]:
    """Collect safe version/runtime details for local Paddle failures."""
    diagnostics = _distribution_diagnostics()
    diagnostics.update(_module_version_diagnostics(import_modules=True))
    return diagnostics


def preflight(config: Mapping[str, Any]) -> Tuple[Dict[str, Any], Any | None]:
    """Import and initialize Paddle once before any secondary OCR workers run."""
    # Initialize first.  Importing PaddleOCR once for diagnostics and then
    # importing it again after a failed partial import can leave PaddleX in a
    # poisoned state (notably after a Windows DLL error).
    diagnostics = _distribution_diagnostics()
    try:
        client = init_client(config)
    except Exception as exc:  # pragma: no cover - optional dependency/platform specific
        diagnostics.update(_module_version_diagnostics(import_modules=False))
        diagnostics.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "recommendation": (
                    "Reinstall the locked ocr-local extra and keep the semantic "
                    "extra disabled, or set ocr.secondary.enabled to false."
                ),
            }
        )
        return diagnostics, None
    diagnostics.update(_module_version_diagnostics(import_modules=False))
    diagnostics["status"] = "ready"
    return diagnostics, client


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return json.loads(value)
        except (TypeError, UnicodeError, json.JSONDecodeError):
            return value
    return value


def _mapping_result(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    for attribute in ("json", "to_json"):
        candidate = getattr(value, attribute, None)
        if callable(candidate):
            candidate = candidate()
        candidate = _json_value(candidate)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _box_position(box: Any) -> Tuple[float, float]:
    try:
        if hasattr(box, "tolist"):
            box = box.tolist()
        points = list(box)
        # PaddleOCR 3.x exposes ``rec_boxes`` as [x1, y1, x2, y2], while
        # ``dt_polys`` is a list of [x, y] points.
        if len(points) >= 4 and all(
            isinstance(value, Real) for value in points[:4]
        ):
            return (float(points[1]), float(points[0]))
        xs = [float(point[0]) for point in points if len(point) >= 2]
        ys = [float(point[1]) for point in points if len(point) >= 2]
        return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def _mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    """Return the first present result field without truth-testing arrays."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _extract_new_api(result: Any) -> List[Tuple[Any, str]]:
    mapping = _mapping_result(result)
    if not mapping:
        return []
    texts = _mapping_value(mapping, "rec_texts", "texts")
    boxes = _mapping_value(mapping, "rec_boxes", "dt_polys")
    texts = [] if texts is None else texts
    boxes = [] if boxes is None else boxes
    rows = []
    for index, text in enumerate(texts):
        value = str(text or "").strip()
        if not value:
            continue
        box = boxes[index] if index < len(boxes) else []
        rows.append((box, value))
    return rows


_FOOTNOTE_KEY_RE = re.compile(
    r"^\s*(?:<sup>\s*)?(\d{1,4})(?:\s*</sup>)?(?=\s+|[.)\]:;,*\u2020\u2021]|$)"
)
_FOOTNOTE_BOTTOM_RATIO = 0.64
_PAGE_HEADER_RATIO = 0.08
_PAGE_FOOTER_RATIO = 0.90


def _footnote_definition_parts(text: str) -> Tuple[str, str] | None:
    """Return a conservative numeric footnote key and its visible body."""
    match = _FOOTNOTE_KEY_RE.match(str(text or ""))
    if not match:
        return None
    return match.group(1), str(text or "")[match.end() :].strip()


def _paddle_block_inner_html(label: str, text: str) -> str:
    """Render the shared Chandra-shaped semantic subset for Paddle output.

    Paddle does not preserve enough typography to identify inline references or
    ordinary superscripts reliably.  Only a numeric marker at the beginning of
    a geometry-labelled footnote block is materialized as a definition marker.
    """
    if label == "Footnote":
        parts = _footnote_definition_parts(text)
        if parts is not None:
            key, body = parts
            marker = f'<sup class="footnote-def">{html.escape(key)}</sup>'
            if body:
                marker += f" {html.escape(body)}"
            return f"<p>{marker}</p>"
    return f"<p>{html.escape(text)}</p>"


def _paddle_markdown(blocks: List[Dict[str, Any]]) -> str:
    """Use Chandra-compatible syntax for conservative footnote definitions."""
    lines: List[str] = []
    for block in blocks:
        text = str(block.get("text") or "")
        if block.get("label") == "Footnote":
            parts = _footnote_definition_parts(text)
            if parts is not None:
                key, body = parts
                text = f"[^{key}]: {body}".rstrip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _box_to_bbox(box: Any) -> List[int] | None:
    """Convert Paddle's rectangle or polygon into pixel coordinates."""
    try:
        if hasattr(box, "tolist"):
            box = box.tolist()
        points = list(box)
    except (TypeError, ValueError):
        return None

    if len(points) >= 4 and all(isinstance(value, Real) for value in points[:4]):
        values = [float(value) for value in points[:4]]
        x0, y0, x1, y1 = values
    else:
        coordinates = []
        for point in points:
            try:
                if hasattr(point, "tolist"):
                    point = point.tolist()
                if len(point) < 2:
                    continue
                coordinates.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not coordinates:
            return None
        x_values = [value[0] for value in coordinates]
        y_values = [value[1] for value in coordinates]
        x0, y0, x1, y1 = min(x_values), min(y_values), max(x_values), max(y_values)

    if not (x1 > x0 and y1 > y0):
        return None
    return [round(x0), round(y0), round(x1), round(y1)]


def _normalise_bbox(bbox: List[int] | None, width: int, height: int) -> List[int] | None:
    if bbox is None or width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = bbox
    return [
        max(0, min(1000, round(x0 * 1000 / width))),
        max(0, min(1000, round(y0 * 1000 / height))),
        max(0, min(1000, round(x1 * 1000 / width))),
        max(0, min(1000, round(y1 * 1000 / height))),
    ]


def _label_paddle_blocks(rows: List[Tuple[Any, str]], width: int, height: int) -> List[Dict[str, Any]]:
    """Build Chandra-shaped blocks with conservative local semantic labels.

    Paddle does not emit semantic layout labels.  ``Footnote`` is therefore
    only inferred from the block's own geometry and a visible numeric start;
    continuation lines are labelled as such only after that local signal.  The
    resulting HTML/Markdown uses the same explicit definition marker as
    Chandra, while ordinary superscripts and inline references remain plain
    text because Paddle cannot identify them safely.
    """
    blocks: List[Dict[str, Any]] = []
    in_footnote = False
    for order, (raw_box, text) in enumerate(rows):
        bbox_px = _box_to_bbox(raw_box)
        bbox = _normalise_bbox(bbox_px, width, height)
        if bbox_px is None or bbox is None:
            label = "Text"
        elif bbox[1] / 1000 >= _PAGE_FOOTER_RATIO:
            label = "Page-Footer"
            in_footnote = False
        elif bbox[3] / 1000 <= _PAGE_HEADER_RATIO:
            label = "Page-Header"
        else:
            bottom_block = bbox[3] / 1000 >= _FOOTNOTE_BOTTOM_RATIO
            starts_with_key = bool(_FOOTNOTE_KEY_RE.match(text))
            if bottom_block and (starts_with_key or in_footnote):
                label = "Footnote"
                in_footnote = True
            else:
                label = "Text"
                if not bottom_block:
                    in_footnote = False

        normalized = " ".join(str(text or "").split())
        inner_html = _paddle_block_inner_html(label, normalized)
        bbox_attribute = (
            f' data-bbox="{" ".join(str(value) for value in bbox)}"'
            if bbox is not None
            else ""
        )
        blocks.append(
            {
                "order": order,
                "label": label,
                "bbox": bbox,
                "bbox_px": bbox_px,
                "html": inner_html,
                "label_source": "paddle_geometry",
                "text": normalized,
                "_html_block": f'<div{bbox_attribute} data-label="{label}">{inner_html}</div>',
            }
        )
    return blocks


def process_page(
    *,
    client: Any,
    img_bytes: bytes,
    page_num: int,
    config: Mapping[str, Any],
    base_output_dir=None,
) -> Dict[str, Any]:
    """Run PaddleOCR and return text plus Chandra-shaped layout evidence."""
    del page_num, base_output_dir, config
    try:
        from PIL import Image
        import numpy as np
    except ImportError as exc:  # pragma: no cover - project dependencies provide these
        raise RuntimeError("Pillow and NumPy are required for PaddleOCR") from exc

    image = np.asarray(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    if not hasattr(client, "predict"):
        raise RuntimeError(
            "The installed PaddleOCR package does not expose the required 3.7 "
            "predict() API; reinstall the project's pinned ocr-local extra."
        )
    raw = client.predict(image)
    rows: List[Tuple[Any, str]] = []
    for item in raw or []:
        rows.extend(_extract_new_api(item))
    rows.sort(key=lambda item: _box_position(item[0]))
    height, width = image.shape[:2]
    blocks = _label_paddle_blocks(rows, width, height)
    html_blocks = [block.pop("_html_block") for block in blocks]
    return {
        "text": _paddle_markdown(blocks),
        "illustrations": [],
        "html": "".join(html_blocks),
        "blocks": blocks,
        "page_box": [0, 0, width, height],
        "model_input_size": [width, height],
    }


__all__ = [
    "init_client",
    "preflight",
    "process_page",
    "runtime_diagnostics",
]
