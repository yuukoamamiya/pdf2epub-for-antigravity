"""Compatibility entry point for the removed provider-backed translator.

The old V2 command used to own online and batch translation.  Translation is
now performed by an Antigravity workspace Subagent, so this module deliberately
contains no provider imports or model execution code.  Keeping this tiny
forwarder avoids a surprising import failure for integrations that still
reference the old command symbol while ensuring they use the supported local
handoff workflow.
"""

from __future__ import annotations


def translate_v2_command(args) -> int:
    """Prepare the supported local Subagent translation hand-off."""
    from ..cli import translate_command

    return translate_command(args)


__all__ = ["translate_v2_command"]
