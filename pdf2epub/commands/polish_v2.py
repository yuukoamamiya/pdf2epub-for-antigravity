"""Compatibility shim for the removed provider-backed polish command.

The supported entry point is ``pdf2epub polish``.  It creates a workspace
Subagent hand-off and never constructs an API client.
"""


def polish_v2_command(args):
    """Delegate old imports to the local-only CLI workflow."""
    from ..cli import polish_command

    return polish_command(args)
