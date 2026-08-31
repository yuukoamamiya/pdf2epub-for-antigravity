"""Legacy processor internals.

The supported CLI does not expose in-process Markdown processors.  OCR
polishing and translation are handed to an Antigravity workspace Subagent and
validated locally.  The old processor modules remain importable only for
isolated migration/tests; they are intentionally not re-exported as a public
execution API.
"""

__all__: list[str] = []
