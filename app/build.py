"""Build identity shared by the launcher, health check, and web assets.

BUILD_ID captures the state when the Python process starts. If source files
change on disk, the launcher can distinguish the running process from the
checked-out code and restart one coherent build instead of mixing new assets
with stale imported modules.

get_build_id() is request-time dynamic: it re-hashes contents whenever the
watched files' (mtime, size) fingerprint changes, so editing a static asset
invalidates browser caches without a service restart.
"""

from __future__ import annotations

import threading
from hashlib import sha256
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_WATCH_SUFFIXES = frozenset({".css", ".html", ".js", ".py"})


def calculate_build_id(app_dir: Path | None = None) -> str:
    """基于应用运行时代码和 Web 资源的内容计算短版本号。

    Args:
        app_dir: 可选的应用目录路径，默认使用模块内 _APP_DIR。
    """
    base = app_dir or _APP_DIR
    digest = sha256()
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in _WATCH_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


BUILD_ID = calculate_build_id()

_cache_lock = threading.Lock()
_cache_fingerprint: str | None = None
_cache_build_id: str | None = None


def _fingerprint(app_dir: Path) -> str:
    """轻量指纹：全部受监控文件的 (mtime_ns, size) 拼接。

    读取 stat 远快于逐文件 hash 内容，用作内容重算的缓存键。
    """
    parts: list[str] = []
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file() or path.suffix not in _WATCH_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        parts.append(f"{path.relative_to(app_dir).as_posix()}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


def get_build_id() -> str:
    """返回当前磁盘内容的构建身份，文件变更后自动失效缓存。

    与 BUILD_ID（进程启动快照）不同：编辑静态资源后无需重启服务，
    页面模板与静态缓存校验即拿到新版本号，浏览器缓存随之刷新。
    """
    global _cache_fingerprint, _cache_build_id
    fingerprint = _fingerprint(_APP_DIR)
    with _cache_lock:
        if _cache_build_id is not None and fingerprint == _cache_fingerprint:
            return _cache_build_id
    build_id = calculate_build_id(_APP_DIR)
    with _cache_lock:
        _cache_fingerprint = fingerprint
        _cache_build_id = build_id
    return build_id
