"""Local EPUB preparation, validation, and rebuilding utilities.

Translation is performed by an Antigravity workspace Subagent.  This package
does not expose an in-process translation client.
"""

from .compressor import HTMLCompressor
from .builder import HTMLEpubBuilder, HTMLEpubPipeline, build_html_epub

__all__ = [
    # Compression
    "HTMLCompressor",
    # Building
    "HTMLEpubBuilder",
    "HTMLEpubPipeline",
    "build_html_epub",
]
