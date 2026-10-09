"""Parent-process proxy for the isolated PP-DocLayout Paddle worker."""

from __future__ import annotations

import base64
import io
import json
import platform
import queue
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Mapping


def _layout_config(config: Mapping[str, Any]) -> dict[str, Any]:
    ocr = config.get("ocr", {}) if isinstance(config, Mapping) else {}
    settings = ocr.get("layout", {}) if isinstance(ocr, Mapping) else {}
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


class PaddleLayoutWorkerClient:
    """One GPU-only worker kept alive for a layout-detection batch."""

    def __init__(self, config: Mapping[str, Any]):
        settings = _layout_config(config)
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
                "PP-DocLayout worker Python executable was not found: "
                f"{executable}. Reuse the isolated .venv-paddle environment."
            )
        worker_script = Path(__file__).with_name("paddle_layout_worker.py").resolve()
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
                f"Could not start PP-DocLayout worker with {executable}: {exc}"
            ) from exc

        self._stdout_thread = threading.Thread(
            target=self._drain_stdout, name="paddle-layout-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="paddle-layout-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            response = self._request(
                {"op": "init", "config": {"ocr": {"layout": settings}}},
                timeout=self._startup_timeout,
            )
        except Exception:
            self._terminate_process()
            raise
        diagnostics = response.get("diagnostics")
        if not isinstance(diagnostics, Mapping) or diagnostics.get("status") != "ready":
            self._terminate_process()
            raise RuntimeError(
                "PP-DocLayout worker did not report a ready CUDA runtime: "
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

    def _request(self, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("PP-DocLayout worker has already been closed")
            if self._process.stdin is None:
                raise RuntimeError("PP-DocLayout worker stdin is unavailable")
            try:
                self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise RuntimeError(
                    "PP-DocLayout worker stopped while sending a request: "
                    f"{exc}. {self._stderr_tail()}"
                ) from exc
            try:
                line = self._responses.get(timeout=timeout)
            except queue.Empty as exc:
                self._terminate_process()
                raise TimeoutError(
                    f"PP-DocLayout worker did not respond within {timeout:.1f}s. "
                    f"{self._stderr_tail()}"
                ) from exc
            if line is None:
                raise RuntimeError(
                    "PP-DocLayout worker exited before returning a response. "
                    f"{self._stderr_tail()}"
                )
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._terminate_process()
                raise RuntimeError(
                    "PP-DocLayout worker emitted a non-JSON response: "
                    f"{line.strip()[:500]}"
                ) from exc
            if not isinstance(response, dict):
                raise RuntimeError("PP-DocLayout worker response was not an object")
            if not response.get("ok"):
                detail = str(response.get("error") or "unknown worker error")
                stderr = self._stderr_tail()
                if stderr:
                    detail = f"{detail}; worker stderr: {stderr}"
                raise RuntimeError(detail)
            return response

    def predict(self, image: Any) -> dict[str, Any]:
        if isinstance(image, (bytes, bytearray)):
            image_bytes = bytes(image)
        else:
            try:
                from PIL import Image

                image_buffer = io.BytesIO()
                Image.fromarray(image).save(image_buffer, format="PNG")
                image_bytes = image_buffer.getvalue()
            except Exception as exc:
                raise RuntimeError("Could not encode an image for PP-DocLayout") from exc
        response = self._request(
            {
                "op": "predict",
                "image_base64": base64.b64encode(image_bytes).decode("ascii"),
            },
            timeout=self._request_timeout,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("PP-DocLayout worker returned an invalid result")
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
        if self._closed:
            return
        try:
            self._request({"op": "shutdown"}, timeout=min(5.0, self._request_timeout))
        except Exception:
            pass
        self._terminate_process()


def init_client(config: Mapping[str, Any]) -> PaddleLayoutWorkerClient:
    return PaddleLayoutWorkerClient(config)


def close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def preflight(config: Mapping[str, Any]):
    diagnostics = {
        "backend": "pp_doclayout",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "executable": sys.executable,
    }
    try:
        client = init_client(config)
    except Exception as exc:
        diagnostics.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
        return diagnostics, None
    return dict(client.diagnostics), client


__all__ = ["PaddleLayoutWorkerClient", "close_client", "init_client", "preflight"]
