"""Console encoding helpers for cross-platform CLI output."""

from __future__ import annotations

import sys
import os


def configure_utf8_stdio() -> None:
    """Make CLI stdout/stderr able to print international filenames.

    ``reconfigure`` is unavailable on a few test and embedding streams, so
    this helper deliberately treats those streams as already configured.
    """
    # Child processes launched by the CLI inherit this setting.  It cannot
    # change the parent PowerShell process, but it prevents nested Python tools
    # from falling back to the Windows active code page.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # A closed or application-owned stream should not prevent the CLI
            # from starting.
            continue
