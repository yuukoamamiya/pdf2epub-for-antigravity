"""Local EPUB preparation, validation, and rebuilding utilities.

Translation is performed by an Antigravity workspace Subagent. This package
does not expose an in-process translation client. Public compatibility names
are loaded lazily so focused validation and package services stay independent
of the full builder and archive implementation.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "HTMLCompressor",
    "HTMLEpubBuilder",
    "HTMLEpubPipeline",
    "NavigationUpdateError",
    "build_html_epub",
]

_EXPORTS = {
    "HTMLCompressor": (".compressor", "HTMLCompressor"),
    "HTMLEpubBuilder": (".builder", "HTMLEpubBuilder"),
    "HTMLEpubPipeline": (".builder", "HTMLEpubPipeline"),
    "NavigationUpdateError": (".build_contracts", "NavigationUpdateError"),
    "build_html_epub": (".builder", "build_html_epub"),
}


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
