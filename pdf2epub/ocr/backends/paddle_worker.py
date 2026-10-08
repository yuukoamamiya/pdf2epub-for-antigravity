"""GPU-only PaddleOCR subprocess for the page OCR adapter.

This file intentionally has no imports from the main pdf2epub package. It is
started by the isolated Paddle Python environment and speaks JSON Lines over
stdin/stdout; diagnostic output is kept on stderr so stdout stays a protocol.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.metadata
import io
import json
import platform
import sys
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any


# When this file is launched by path, Python puts its directory first on
# sys.path. That directory also contains the project's ``paddle.py`` adapter,
# which would shadow the third-party Paddle package. Remove only that script
# directory before importing the isolated runtime.
_worker_directory = Path(__file__).resolve().parent
if sys.path and Path(sys.path[0]).resolve() == _worker_directory:
    sys.path.pop(0)


_ocr = None
_paddle = None


def _settings(config: Any) -> dict[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    ocr = config.get("ocr", {})
    backends = ocr.get("backends", {}) if isinstance(ocr, Mapping) else {}
    paddle = backends.get("paddle", {}) if isinstance(backends, Mapping) else {}
    return dict(paddle) if isinstance(paddle, Mapping) else {}


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
    if isinstance(value, Mapping):
        return dict(value)
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


def _normalise_result(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        raw_items = [raw]
    else:
        try:
            raw_items = list(raw or [])
        except TypeError as exc:
            raise RuntimeError("PaddleOCR predict() did not return an iterable") from exc

    results: list[dict[str, Any]] = []
    for item in raw_items:
        mapping = _result_mapping(item)
        if mapping is None:
            continue
        # These are the only fields consumed by the parent layout adapter.
        # Keeping the response narrow avoids serializing model internals or
        # accidentally putting image data into the protocol.
        result: dict[str, Any] = {}
        for key in ("rec_texts", "rec_boxes", "dt_polys"):
            if key in mapping:
                result[key] = _json_safe(mapping[key])
        if result:
            results.append(result)
    return results


def _initialise(config: Any) -> dict[str, Any]:
    global _ocr, _paddle
    settings = _settings(config)
    requested = str(settings.get("device") or "gpu:0").strip().lower()
    if requested == "gpu":
        requested = "gpu:0"
    if not requested.startswith("gpu:"):
        raise RuntimeError(
            f"Paddle secondary OCR requires a GPU device such as gpu:0; received {requested!r}"
        )

    # All Paddle imports happen in this child process, never in the main OCR
    # interpreter. Import and initialization stdout is redirected to stderr so
    # it cannot corrupt the JSON-lines protocol.
    with contextlib.redirect_stdout(sys.stderr):
        import paddle
        from paddleocr import PaddleOCR

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
                f"Paddle refused the requested GPU device {requested}; active device is {actual}"
            )

        kwargs: dict[str, Any] = {"device": requested}
        for key in (
            "lang",
            "engine",
            "engine_config",
            "use_doc_orientation_classify",
            "use_doc_unwarping",
            "use_textline_orientation",
            "use_angle_cls",
        ):
            if key in settings:
                kwargs[key] = settings[key]
        kwargs.setdefault("use_doc_orientation_classify", True)
        kwargs.setdefault("use_doc_unwarping", True)
        kwargs.setdefault("use_textline_orientation", True)
        _ocr = PaddleOCR(**kwargs)

        diagnostics = {
            "status": "ready",
            "backend": "paddle",
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


def _predict(image_base64: Any) -> list[dict[str, Any]]:
    if _ocr is None:
        raise RuntimeError("Paddle GPU worker has not been initialized")
    if not isinstance(image_base64, str) or not image_base64:
        raise RuntimeError("predict requires a non-empty image_base64 string")
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("predict received invalid base64 image data") from exc
    try:
        from PIL import Image
        import numpy as np

        with Image.open(io.BytesIO(image_bytes)) as image:
            rgb_image = np.asarray(image.convert("RGB"))
    except Exception as exc:
        raise RuntimeError("Paddle GPU worker could not decode the PNG image") from exc
    with contextlib.redirect_stdout(sys.stderr):
        raw = _ocr.predict(rgb_image)
        return _normalise_result(raw)


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
