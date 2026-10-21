"""Build identity shared by the launcher, health check, and web assets.

BUILD_ID captures the state when the Python process starts. If source files
change on disk, the launcher can distinguish the running process from the
checked-out code and restart one coherent build instead of mixing new assets
with stale imported modules.

get_build_id() is request-time dynamic: it re-hashes contents whenever the
watched files' (mtime, size) fingerprint changes, so editing a static asset
invalidates browser caches without a service restart.

实现说明（2026-10-21 实测，改前请先读）：

* 目录遍历必须用 ``os.scandir`` 而不是 ``Path.rglob``：同一台机器上
  ``rglob + stat`` 走 53 个文件要 **20.4 ms**，``scandir`` 只要 **1.34 ms**
  （15 倍），而两者产出的指纹逐字相同。缓存命中路径的成本全在这个遍历上，
  所以遍历一变慢，缓存就等于没有。
* 指纹只用 ``(mtime_ns, size)`` **不足以**判定"没变"：Windows 的文件时间戳
  有毫秒级粒度，同一 tick 内两次等长写入会得到相同指纹（实测会让
  ``test_get_build_id_refreshes_when_static_asset_changes`` 约 30% 概率失败）。
  因此引入"新鲜窗口"：只要有文件的 mtime 落在 ``_FRESH_WINDOW_NS`` 内，
  本次一律重算，不信任指纹缓存。
"""

from __future__ import annotations

import os
import threading
import time
from hashlib import sha256
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_WATCH_SUFFIXES = frozenset({".css", ".html", ".js", ".py"})

# 文件 mtime 落在这个窗口内 ⇒ 指纹不可信（时间戳粒度可能让两次写入撞车）
_FRESH_WINDOW_NS = 2_000_000_000


def _monitor_state(base: Path) -> tuple[list[Path], str, bool]:
    """一次遍历同时拿到受监控文件列表、指纹、以及"是否有刚改过的文件"。

    用 ``os.scandir`` 递归（``entry.stat()`` 复用目录扫描时拿到的元数据），
    比 ``Path.rglob`` 快一个数量级。

    Args:
        base: 应用目录。

    Returns:
        ``(按路径排序的文件列表, 指纹字符串, 是否含新鲜文件)``。
    """
    now_ns = time.time_ns()
    fresh = False
    found: list[Path] = []
    parts: list[str] = []
    stack: list[Path] = [base]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                if entry.name == "__pycache__":
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    # 文件跟随符号链接（与旧实现 Path.is_file() 一致），
                    # 目录不跟随，避免软链成环导致无限递归。
                    if not entry.is_file(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                if os.path.splitext(entry.name)[1] not in _WATCH_SUFFIXES:
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                path = Path(entry.path)
                found.append(path)
                parts.append(f"{path.relative_to(base).as_posix()}:{stat.st_mtime_ns}:{stat.st_size}")
                if now_ns - stat.st_mtime_ns < _FRESH_WINDOW_NS:
                    fresh = True
    found.sort()
    parts.sort()
    return found, "|".join(parts), fresh


def _hash_paths(paths: list[Path], base: Path) -> str:
    """按统一顺序对给定文件的内容算短版本号。"""
    digest = sha256()
    for path in paths:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(path.relative_to(base).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def calculate_build_id(app_dir: Path | None = None) -> str:
    """基于应用运行时代码和 Web 资源的内容计算短版本号。

    Args:
        app_dir: 可选的应用目录路径，默认使用模块内 _APP_DIR。
    """
    base = app_dir or _APP_DIR
    paths, _fingerprint, _fresh = _monitor_state(base)
    return _hash_paths(paths, base)


BUILD_ID = calculate_build_id()

_cache_lock = threading.Lock()
_cache_fingerprint: str | None = None
_cache_build_id: str | None = None


def get_build_id() -> str:
    """返回当前磁盘内容的构建身份，文件变更后自动失效缓存。

    与 BUILD_ID（进程启动快照）不同：编辑静态资源后无需重启服务，
    页面模板与静态缓存校验即拿到新版本号，浏览器缓存随之刷新。
    """
    global _cache_fingerprint, _cache_build_id
    paths, fingerprint, fresh = _monitor_state(_APP_DIR)
    # 有文件刚被改过 ⇒ 指纹可能与上一次撞车（时间戳粒度），必须重算
    if not fresh:
        with _cache_lock:
            if _cache_build_id is not None and fingerprint == _cache_fingerprint:
                return _cache_build_id
    build_id = _hash_paths(paths, _APP_DIR)
    with _cache_lock:
        _cache_fingerprint = fingerprint
        _cache_build_id = build_id
    return build_id
