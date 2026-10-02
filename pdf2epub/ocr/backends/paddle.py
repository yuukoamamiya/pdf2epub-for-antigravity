"""Optional local PaddleOCR adapter used for OCR consensus checks."""

from __future__ import annotations

import io
import json
from typing import Any, Dict, Iterable, List, Mapping, Tuple


def _backend_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    ocr = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    backends = ocr.get("backends", {}) if isinstance(ocr, Mapping) else {}
    settings = backends.get("paddle", {}) if isinstance(backends, Mapping) else {}
    return dict(settings) if isinstance(settings, Mapping) else {}


def init_client(config: Mapping[str, Any]):
    """Create a PaddleOCR client lazily so Paddle stays optional."""
    try:
        from paddleocr import PaddleOCR
    except (ImportError, OSError) as exc:  # pragma: no cover - optional dependency/platform specific
        raise RuntimeError(
            "PaddleOCR is configured as a secondary OCR backend but its optional "
            "dependencies could not be imported. Install the locked local OCR "
            "dependencies with `uv sync --extra ocr-local` and check the platform "
            "runtime/DLL requirements."
        ) from exc

    settings = _backend_config(config)
    kwargs: Dict[str, Any] = {}
    for key in (
        "lang",
        "device",
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_textline_orientation",
        "use_angle_cls",
    ):
        if key in settings:
            kwargs[key] = settings[key]
    try:
        return PaddleOCR(**kwargs)
    except TypeError:
        # PaddleOCR 2.x accepts a smaller constructor surface than 3.x.
        fallback = {key: kwargs[key] for key in ("lang", "use_angle_cls") if key in kwargs}
        return PaddleOCR(**fallback)


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
        points = list(box)
        xs = [float(point[0]) for point in points if len(point) >= 2]
        ys = [float(point[1]) for point in points if len(point) >= 2]
        return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def _extract_new_api(result: Any) -> List[Tuple[Any, str]]:
    mapping = _mapping_result(result)
    if not mapping:
        return []
    texts = mapping.get("rec_texts") or mapping.get("texts") or []
    boxes = mapping.get("rec_boxes") or mapping.get("dt_polys") or []
    rows = []
    for index, text in enumerate(texts):
        value = str(text or "").strip()
        if not value:
            continue
        box = boxes[index] if index < len(boxes) else []
        rows.append((box, value))
    return rows


def _extract_legacy_api(result: Any) -> List[Tuple[Any, str]]:
    rows: List[Tuple[Any, str]] = []
    if not isinstance(result, Iterable) or isinstance(result, (str, bytes, Mapping)):
        return rows
    for page in result:
        if not isinstance(page, Iterable) or isinstance(page, (str, bytes, Mapping)):
            continue
        for item in page:
            try:
                box, payload = item
                text = payload[0]
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            value = str(text or "").strip()
            if value:
                rows.append((box, value))
    return rows


def process_page(
    *,
    client: Any,
    img_bytes: bytes,
    page_num: int,
    config: Mapping[str, Any],
    base_output_dir=None,
) -> Dict[str, Any]:
    """Run PaddleOCR and return text in stable visual reading order."""
    del page_num, base_output_dir, config
    try:
        from PIL import Image
        import numpy as np
    except ImportError as exc:  # pragma: no cover - project dependencies provide these
        raise RuntimeError("Pillow and NumPy are required for PaddleOCR") from exc

    image = np.asarray(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    if hasattr(client, "predict"):
        raw = client.predict(image)
        rows: List[Tuple[Any, str]] = []
        for item in raw or []:
            rows.extend(_extract_new_api(item))
    else:
        raw = client.ocr(image, cls=True)
        rows = _extract_legacy_api(raw)
    rows.sort(key=lambda item: _box_position(item[0]))
    return {
        "text": "\n".join(text for _box, text in rows),
        "illustrations": [],
        "blocks": [
            {"text": text, "box": box}
            for box, text in rows
        ],
    }


__all__ = ["init_client", "process_page"]
