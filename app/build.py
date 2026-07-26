"""Build identity shared by the launcher, health check, and web assets.

The id is captured when the Python process starts. If source files change on
disk, the launcher can distinguish the running process from the checked-out
code and restart one coherent build instead of mixing new assets with stale
imported modules.
"""

from __future__ import annotations

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


def get_build_id() -> str:
    """返回当前运行进程启动时的不变构建身份。"""
    return BUILD_ID
