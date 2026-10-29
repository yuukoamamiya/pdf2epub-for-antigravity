"""Input-file lookup independent of workflow policy."""
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

def resolve_input_path(path: Optional[Union[str, Path]]) -> Optional[Path]:
    """
    Resolve an input file path, searching in input/ directory if not found directly.

    Args:
        path: Path string or Path object

    Returns:
        Resolved Path object or original Path if not found
    """
    if path is None:
        return None
    p = Path(path)
    if p.exists():
        return p
    # Check under input/ directory
    input_p = Path("input") / p
    if input_p.exists():
        return input_p
    input_name = Path("input") / p.name
    if input_name.exists():
        return input_name
    return p


def resolve_book_input_path(
    explicit_path: Optional[Union[str, Path]] = None,
    *,
    config_value: Optional[Union[str, Path]] = None,
    config_path: Optional[Union[str, Path]] = None,
    output_dir: Optional[Union[str, Path]] = None,
    input_dir: Optional[Union[str, Path]] = None,
    extensions: Iterable[str] = (),
    output_names: Sequence[str] = (),
) -> Path:
    """Resolve book input files using one consistent CLI/config priority.

    Priority is explicit CLI path, configured path, a unique file in ``input/``
    and finally a standard file in the output directory. Relative configured
    paths are resolved relative to the config file first, while explicit CLI
    paths retain the current-working-directory behavior for compatibility.
    """
    cwd = Path.cwd()
    config_parent = Path(config_path).expanduser().resolve().parent if config_path else cwd
    normalized_extensions = {
        ext.lower() if str(ext).startswith(".") else f".{str(ext).lower()}"
        for ext in extensions
    }

    def candidates_for(value, roots):
        if value is None:
            return []
        path = Path(value).expanduser()
        if path.is_absolute():
            return [path]
        candidates = [root / path for root in roots]
        candidates.append(cwd / "input" / path)
        candidates.append(cwd / "input" / path.name)
        return candidates

    def first_existing(candidates):
        seen = set()
        for candidate in candidates:
            candidate = candidate.resolve() if not candidate.is_absolute() else candidate
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                return candidate
        return None

    explicit_candidates = candidates_for(explicit_path, [cwd, config_parent])
    resolved = first_existing(explicit_candidates)
    if explicit_path is not None:
        return resolved or Path(explicit_candidates[0] if explicit_candidates else explicit_path)

    configured_candidates = candidates_for(config_value, [config_parent, cwd])
    resolved = first_existing(configured_candidates)
    if config_value is not None:
        return resolved or Path(configured_candidates[0] if configured_candidates else config_value)

    search_dirs = []
    if input_dir is not None:
        search_dirs.append(Path(input_dir))
    else:
        search_dirs.extend([config_parent / "input", cwd / "input"])
    unique_files = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for candidate in directory.iterdir():
            if not candidate.is_file():
                continue
            if normalized_extensions and candidate.suffix.lower() not in normalized_extensions:
                continue
            unique_files[str(candidate.resolve()).lower()] = candidate
    if len(unique_files) == 1:
        return next(iter(unique_files.values()))
    if len(unique_files) > 1:
        names = ", ".join(sorted(path.name for path in unique_files.values()))
        raise ValueError(f"Multiple input files found; specify one explicitly: {names}")

    if output_dir:
        output_root = Path(output_dir)
        for name in output_names:
            candidate = output_root / name
            if candidate.is_file():
                return candidate
        if output_names:
            return output_root / output_names[0]
    return Path(output_names[0]) if output_names else Path()
