import os
import subprocess
import sys

from pdf2epub.utils.encoding import configure_utf8_stdio


class _Stream:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def test_configure_utf8_stdio_sets_utf8_without_crashing(monkeypatch):
    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    configure_utf8_stdio()

    assert os.environ["PYTHONIOENCODING"] == "utf-8"
    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_configure_utf8_stdio_overrides_inherited_gbk(monkeypatch):
    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")

    configure_utf8_stdio()

    assert os.environ["PYTHONIOENCODING"] == "utf-8"


def _gbk_environment():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "gbk"
    return env


def test_cli_import_configures_captured_streams_and_nested_python(tmp_path):
    marker = "äöüß 中文"
    child_code = f"import os; print(os.environ['PYTHONIOENCODING']); print({marker!r})"
    code = f"""
import os, subprocess, sys
import pdf2epub.cli
print({marker!r})
print({marker!r}, file=sys.stderr)
sys.stdout.flush()
child = subprocess.run(
    [sys.executable, '-c', {child_code!r}],
    check=True, capture_output=True,
)
sys.stdout.buffer.write(child.stdout)
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=_gbk_environment(),
        capture_output=True,
        check=True,
    )

    assert result.stdout.decode("utf-8").splitlines() == [marker, "utf-8", marker]
    assert result.stderr.decode("utf-8").strip() == marker


def test_cli_import_writes_utf8_to_redirected_output_files(tmp_path):
    marker = "äöüß 中文"
    code = f"import pdf2epub.cli; print({marker!r}); import sys; print({marker!r}, file=sys.stderr)"
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"

    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        subprocess.run(
            [sys.executable, "-c", code],
            env=_gbk_environment(),
            stdout=stdout,
            stderr=stderr,
            check=True,
        )

    newline = os.linesep.encode("ascii")
    assert stdout_path.read_bytes() == marker.encode("utf-8") + newline
    assert stderr_path.read_bytes() == marker.encode("utf-8") + newline


def test_configure_utf8_stdio_tolerates_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())
    configure_utf8_stdio()


def test_configure_utf8_stdio_tolerates_closed_and_failing_streams(monkeypatch):
    class BrokenStream:
        def __init__(self, error):
            self.error = error

        def reconfigure(self, **kwargs):
            raise self.error

    monkeypatch.setattr(sys, "stdout", BrokenStream(ValueError("closed")))
    monkeypatch.setattr(sys, "stderr", BrokenStream(OSError("unsupported")))
    configure_utf8_stdio()
