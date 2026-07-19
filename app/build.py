"""Stable build identity for detecting stale local server processes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_BUILD_SUFFIXES = frozenset({".css", ".html", ".js", ".py"})


def calculate_build_id(app_dir: Path = _APP_DIR) -> str:
    """Hash application source and bundled web assets into a short build id."""
    digest = sha256()
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _BUILD_SUFFIXES:
            continue
        digest.update(path.relative_to(app_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


BUILD_ID = calculate_build_id()
