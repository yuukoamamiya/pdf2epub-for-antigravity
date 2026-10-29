"""Small filesystem helpers without workflow policy."""
from pathlib import Path

def ensure_directory(directory_path: Path) -> None:
    """
    Ensure a directory exists, create it if it doesn't.

    Args:
        directory_path: Path to the directory
    """
    Path(directory_path).mkdir(parents=True, exist_ok=True)
