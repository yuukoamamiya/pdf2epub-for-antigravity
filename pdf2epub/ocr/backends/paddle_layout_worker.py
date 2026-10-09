"""GPU-only PaddleOCR layout-detection subprocess.

This worker is deliberately separate from the secondary OCR worker.  Layout
detection supplies page-region evidence; it does not produce text and must not
be treated as a second OCR opinion.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.metadata
import io
import json
import os
import platform
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any


_worker_directory = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == _worker_directory:
    sys.path.pop(0)


_model = None
_paddle = None
_settings_cache: dict[str, Any] = {}


def _settings(config: Any) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    ocr = config.get("ocr", {})
    layout = ocr.get("layout", {}) if isinstance(ocr, Mapping) else {}
    return dict(layout) if isinstance(layout, Mapping) else {}


def _distribution_version(*names: str) -> str | None:
    for name in names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            return json.loads(value)
        except (TypeError, UnicodeError, json.JSONDecodeError):
            return value
    if hasattr(value, "tolist"):
        try:
            return value.tolist()
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _json_safe(value: Any) -> Any:
    value = _json_value(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _result_mapping(value: Any) -> Mapping[str, Any] | None:
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


def _iter_results(raw: Any) -> list[Any]:
    if isinstance(raw, Mapping):
        return [raw]
    if _result_mapping(raw) is not None:
        return [raw]
    try:
        return list(raw or [])
    except TypeError as exc:
        raise RuntimeError("PP-DocLayout predict() did not return an iterable") from exc


def _normalise_boxes(raw: Any) -> list[dict[str, Any]]:
    boxes: list[dict[str, Any]] = []
    for item in _iter_results(raw):
        mapping = _result_mapping(item)
        if mapping is None:
            continue
        # PaddleOCR 3.x wraps the actual result in {"res": {...}}.
        nested = mapping.get("res")
        if isinstance(nested, Mapping):
            mapping = nested
        raw_boxes = mapping.get("boxes", [])
        if not isinstance(raw_boxes, (list, tuple)):
            continue
        for raw_box in raw_boxes:
            if not isinstance(raw_box, Mapping):
                continue
            coordinate = raw_box.get("coordinate")
            if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 4:
                continue
            try:
                bbox = [float(value) for value in coordinate]
                score = float(raw_box.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                continue
            boxes.append(
                {
                    "label": str(raw_box.get("label") or ""),
                    "score": score,
                    "bbox": bbox,
                    "cls_id": raw_box.get("cls_id"),
                }
            )
    boxes.sort(key=lambda box: (box["bbox"][1], box["bbox"][0]))
    return boxes


def _initialise(config: Any) -> dict[str, Any]:
    global _model, _paddle, _settings_cache
    settings = _settings(config)
    _settings_cache = settings
    model_source = str(settings.get("model_source") or "").strip()
    if model_source:
        os.environ["PADDLE_PDX_MODEL_SOURCE"] = model_source

    requested = str(settings.get("device") or "gpu:0").strip().lower()
    if requested == "gpu":
        requested = "gpu:0"
    if not requested.startswith("gpu:"):
        raise RuntimeError(
            "PP-DocLayout-L requires a GPU device such as gpu:0; "
            f"received {requested!r}"
        )

    with contextlib.redirect_stdout(sys.stderr):
        import paddle
        from paddleocr import LayoutDetection

        _paddle = paddle
        compiled_with_cuda = bool(paddle.is_compiled_with_cuda())
        device_count = int(paddle.device.cuda.device_count()) if compiled_with_cuda else 0
        if not compiled_with_cuda:
            raise RuntimeError("installed PaddlePaddle is not compiled with CUDA")
        if device_count <= 0:
            raise RuntimeError("PaddlePaddle reports no CUDA GPU devices")
        paddle.set_device(requested)
        actual = str(paddle.get_device()).strip().lower()
        if actual != requested:
            raise RuntimeError(
                f"Paddle refused requested device {requested}; active device is {actual}"
            )

        kwargs: dict[str, Any] = {
            "model_name": settings.get("model_name") or "PP-DocLayout-L",
            "device": requested,
        }
        if settings.get("model_dir"):
            kwargs["model_dir"] = settings["model_dir"]
        for key in (
            "engine",
            "engine_config",
            "enable_hpi",
            "use_tensorrt",
            "precision",
            "img_size",
            "threshold",
            "layout_nms",
            "layout_unclip_ratio",
            "layout_merge_bboxes_mode",
        ):
            if key in settings and settings[key] is not None:
                kwargs[key] = settings[key]
        _model = LayoutDetection(**kwargs)

        diagnostics = {
            "status": "ready",
            "backend": "pp_doclayout",
            "model_name": kwargs["model_name"],
            "worker_mode": "subprocess",
            "python": platform.python_version(),
            "executable": sys.executable,
            "platform": platform.platform(),
            "paddle_version": getattr(paddle, "__version__", None),
            "paddleocr_version": getattr(sys.modules.get("paddleocr"), "__version__", None),
            "paddlepaddle_distribution": _distribution_version(
                "paddlepaddle-gpu", "paddlepaddle"
            ),
            "paddleocr_distribution": _distribution_version("paddleocr"),
            "device_requested": requested,
            "device_actual": actual,
            "compiled_with_cuda": compiled_with_cuda,
            "cuda_device_count": device_count,
        }
    return diagnostics


def _predict(image_base64: Any) -> dict[str, Any]:
    if _model is None:
        raise RuntimeError("PP-DocLayout model has not been initialized")
    if not isinstance(image_base64, str) or not image_base64:
        raise RuntimeError("predict requires a non-empty image_base64 string")
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
        from PIL import Image
        import numpy as np

        with Image.open(io.BytesIO(image_bytes)) as image:
            rgb_image = np.asarray(image.convert("RGB"))
    except Exception as exc:
        raise RuntimeError("PP-DocLayout worker could not decode the PNG image") from exc

    predict_kwargs: dict[str, Any] = {"batch_size": 1}
    if _settings_cache.get("layout_nms") is not None:
        predict_kwargs["layout_nms"] = _settings_cache["layout_nms"]
    with contextlib.redirect_stdout(sys.stderr):
        raw = _model.predict(rgb_image, **predict_kwargs)
    height, width = rgb_image.shape[:2]
    return {
        "page_box": [0, 0, int(width), int(height)],
        "boxes": _normalise_boxes(raw),
    }


def _response(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise RuntimeError("request must be a JSON object")
            operation = request.get("op")
            if operation == "init":
                _response({"ok": True, "diagnostics": _initialise(request.get("config"))})
            elif operation == "predict":
                _response({"ok": True, "result": _predict(request.get("image_base64"))})
            elif operation == "shutdown":
                _response({"ok": True})
                return 0
            else:
                raise RuntimeError(f"unknown worker operation: {operation!r}")
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr, end="")
            _response({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
