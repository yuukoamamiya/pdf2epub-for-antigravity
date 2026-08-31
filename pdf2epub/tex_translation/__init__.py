"""Local TeX materialization and compile validation for Subagent output.

The supported workflow is exposed by the CLI: ``translate-arxiv`` prepares a
workspace hand-off and ``translate-arxiv-validate`` compiles the Subagent-edited
project locally.  The removed in-process translation pipeline is not imported
here, so normal CLI use cannot initialize a provider-backed translator.
"""

__all__: list[str] = []
