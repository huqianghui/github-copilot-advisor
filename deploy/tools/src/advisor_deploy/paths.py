"""Portable link checks, including Windows junctions on Python 3.11."""

from pathlib import Path
import stat


def is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def has_link(path: Path) -> bool:
    return any(is_link(item) for item in (path, *path.parents))
