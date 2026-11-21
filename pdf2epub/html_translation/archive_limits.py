"""Resource budgets for untrusted EPUB ZIP archives."""
from __future__ import annotations

from dataclasses import dataclass
import zipfile


@dataclass(frozen=True)
class EPUBArchiveLimits:
    """Maximum declared and actually consumed EPUB archive resources."""

    max_members: int = 100_000
    max_file_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024


DEFAULT_EPUB_ARCHIVE_LIMITS = EPUBArchiveLimits()
_CHUNK_SIZE = 64 * 1024


class EPUBArchiveLimitError(ValueError):
    """An EPUB archive exceeds a configured expanded-resource budget."""


def validate_archive_limits(
    archive: zipfile.ZipFile,
    limits: EPUBArchiveLimits = DEFAULT_EPUB_ARCHIVE_LIMITS,
) -> None:
    """Reject archives whose central directory exceeds configured budgets."""
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise EPUBArchiveLimitError(
            f"EPUB member count exceeds limit ({len(infos)} > {limits.max_members})"
        )
    total = 0
    for info in infos:
        if info.file_size < 0 or info.file_size > limits.max_file_bytes:
            raise EPUBArchiveLimitError(
                f"EPUB member expanded size exceeds limit: {info.filename!r} "
                f"({info.file_size} > {limits.max_file_bytes} bytes)"
            )
        total += info.file_size
        if total > limits.max_total_bytes:
            raise EPUBArchiveLimitError(
                f"EPUB total expanded size exceeds limit "
                f"({total} > {limits.max_total_bytes} bytes)"
            )


def read_member_bounded(
    archive: zipfile.ZipFile,
    member: str,
    limits: EPUBArchiveLimits = DEFAULT_EPUB_ARCHIVE_LIMITS,
) -> bytes:
    """Read a single member incrementally, enforcing its actual expanded size."""
    info = archive.getinfo(member)
    if info.file_size > limits.max_file_bytes:
        raise EPUBArchiveLimitError(
            f"EPUB member expanded size exceeds limit: {member!r} "
            f"({info.file_size} > {limits.max_file_bytes} bytes)"
        )
    chunks = []
    size = 0
    with archive.open(info) as stream:
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
            if size > limits.max_file_bytes:
                raise EPUBArchiveLimitError(
                    f"EPUB member actual expanded size exceeds limit: {member!r}"
                )
            chunks.append(chunk)
    return b"".join(chunks)
