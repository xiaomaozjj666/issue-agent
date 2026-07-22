"""Stable build identity for detecting stale local server processes."""

from __future__ import annotations

from functools import lru_cache
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


@lru_cache(maxsize=1)
def _cached_build_id() -> str:
    """延迟计算并缓存 BUILD_ID，避免模块加载时同步遍历文件系统。"""
    return calculate_build_id()


def get_build_id() -> str:
    """获取构建 ID（首次调用时计算，后续从缓存返回）。"""
    return _cached_build_id()


# 向后兼容：保留 BUILD_ID 模块级属性，但通过 __getattr__ 延迟计算
def __getattr__(name: str) -> str:
    if name == "BUILD_ID":
        return _cached_build_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
