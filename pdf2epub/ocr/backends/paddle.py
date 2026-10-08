"""Optional local PaddleOCR adapter used for OCR consensus checks."""

from __future__ import annotations

import io
import html
import importlib.metadata
import base64
import json
from numbers import Real
from pathlib import Path
import platform
import queue
import re
import subprocess
import sys
import threading
from collections import deque
from typing import Any, Dict, List, Mapping, Tuple


def _backend_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    ocr = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    backends = ocr.get("backends", {}) if isinstance(ocr, Mapping) else {}
    settings = backends.get("paddle", {}) if isinstance(backends, Mapping) else {}
    return dict(settings) if isinstance(settings, Mapping) else {}


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _worker_python(settings: Mapping[str, Any]) -> Path:
    configured = settings.get("python_executable") or ".venv-paddle/Scripts/python.exe"
    path = Path(str(configured)).expanduser()
    if not path.is_absolute():
        path = _repository_root() / path
    return path.resolve()


def _timeout(settings: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return max(1.0, float(settings.get(key, default)))
    except (TypeError, ValueError):
        return default


class PaddleWorkerClient:
    """Proxy for one GPU PaddleOCR subprocess.

    The parent process never imports Paddle or PaddleOCR. A worker is started
    for one OCR run, keeps the model in its own interpreter while pages are
    processed, and is explicitly shut down by the caller afterwards.
    """

    def __init__(self, config: Mapping[str, Any]):
        settings = _backend_config(config)
        self._settings = settings
        self._request_timeout = _timeout(settings, "request_timeout", 300.0)
        self._startup_timeout = _timeout(settings, "worker_startup_timeout", 300.0)
        self._lock = threading.Lock()
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._stderr_lines = deque(maxlen=80)
        self._closed = False

        executable = _worker_python(settings)
        if not executable.is_file():
            raise RuntimeError(
                "Paddle GPU worker Python executable was not found: "
                f"{executable}. Create the isolated .venv-paddle environment "
                "with the pinned Paddle GPU wheel first."
            )
        worker_script = Path(__file__).with_name("paddle_worker.py").resolve()
        try:
            self._process = subprocess.Popen(
                [str(executable), "-u", str(worker_script)],
                cwd=str(_repository_root()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not start the isolated Paddle GPU worker with {executable}: {exc}"
            ) from exc

        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, name="paddle-worker-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="paddle-worker-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            response = self._request(
                {"op": "init", "config": {"ocr": {"backends": {"paddle": settings}}}},
                timeout=self._startup_timeout,
            )
        except Exception:
            self._terminate_process()
            raise
        diagnostics = response.get("diagnostics")
        if not isinstance(diagnostics, Mapping) or diagnostics.get("status") != "ready":
            self._terminate_process()
            raise RuntimeError(
                "Paddle GPU worker did not report a ready CUDA runtime: "
                f"{diagnostics or response}"
            )
        self.diagnostics = dict(diagnostics)

    def _drain_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._responses.put(None)
            return
        try:
            for line in stream:
                self._responses.put(line)
        finally:
            self._responses.put(None)

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for line in stream:
            self._stderr_lines.append(line.rstrip())

    def _stderr_tail(self) -> str:
        return "\n".join(line for line in self._stderr_lines if line)[-4000:]

    def _request(self, payload: Mapping[str, Any], *, timeout: float) -> Dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Paddle GPU worker has already been closed")
            if self._process.stdin is None:
                raise RuntimeError("Paddle GPU worker stdin is unavailable")
            try:
                self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                detail = self._stderr_tail()
                raise RuntimeError(
                    f"Paddle GPU worker stopped while sending a request: {exc}. {detail}"
                ) from exc

            try:
                line = self._responses.get(timeout=timeout)
            except queue.Empty as exc:
                self._terminate_process()
                detail = self._stderr_tail()
                raise TimeoutError(
                    f"Paddle GPU worker did not respond within {timeout:.1f}s. {detail}"
                ) from exc
            if line is None:
                detail = self._stderr_tail()
                raise RuntimeError(
                    "Paddle GPU worker exited before returning a response. "
                    f"{detail}"
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._terminate_process()
                raise RuntimeError(
                    "Paddle GPU worker emitted a non-JSON response. "
                    f"{line.strip()[:500]}"
                ) from exc
            if not isinstance(response, dict):
                raise RuntimeError("Paddle GPU worker response was not a JSON object")
            if not response.get("ok"):
                detail = str(response.get("error") or "unknown worker error")
                stderr = self._stderr_tail()
                if stderr:
                    detail = f"{detail}; worker stderr: {stderr}"
                raise RuntimeError(detail)
            return response

    def predict(self, image: Any) -> List[Mapping[str, Any]]:
        """Send one RGB image to the worker and return JSON-safe OCR rows."""
        if isinstance(image, (bytes, bytearray)):
            image_bytes = bytes(image)
        else:
            try:
                from PIL import Image

                image_buffer = io.BytesIO()
                Image.fromarray(image).save(image_buffer, format="PNG")
                image_bytes = image_buffer.getvalue()
            except Exception as exc:
                raise RuntimeError("Could not encode an image for the Paddle GPU worker") from exc
        response = self._request(
            {
                "op": "predict",
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            },
            timeout=self._request_timeout,
        )
        result = response.get("result", [])
        if not isinstance(result, list):
            raise RuntimeError("Paddle GPU worker returned an invalid OCR result")
        return result

    def _terminate_process(self) -> None:
        self._closed = True
        process = getattr(self, "_process", None)
        if process is None:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def close(self) -> None:
        """Ask the worker to exit and reclaim the child process."""
        if self._closed:
            return
        try:
            self._request({"op": "shutdown"}, timeout=min(5.0, self._request_timeout))
        except Exception:
            pass
        self._terminate_process()


def init_client(config: Mapping[str, Any]) -> PaddleWorkerClient:
    """Start an isolated, GPU-only PaddleOCR worker for the current OCR run."""
    return PaddleWorkerClient(config)


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
    # Keep Paddle out of the main interpreter. The child worker reports its
    # imported module versions during preflight.
    diagnostics.update(_module_version_diagnostics(import_modules=False))
    return diagnostics


def preflight(config: Mapping[str, Any]) -> Tuple[Dict[str, Any], Any | None]:
    """Start the isolated worker and validate its CUDA runtime."""
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
                    "Install the pinned Paddle GPU worker environment and ensure "
                    "ocr.backends.paddle.device is gpu:0; CPU fallback is disabled."
                ),
            }
        )
        return diagnostics, None
    return dict(client.diagnostics), client


def close_client(client: Any) -> None:
    """Close a worker proxy without requiring callers to know its type."""
    close = getattr(client, "close", None)
    if callable(close):
        close()


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
            "predict() API; reinstall the project's pinned .venv-paddle environment."
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
    "PaddleWorkerClient",
    "close_client",
    "init_client",
    "preflight",
    "process_page",
    "runtime_diagnostics",
]
