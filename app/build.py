"""Build identity for cache-busting static assets.

每次请求都基于 static 和 templates 目录下关键文件的内容计算版本号。
开发时改完文件刷新即可生效，无需重启服务；生产环境也始终准确。
关键文件仅 JS/CSS/HTML（约 10 个），每次计算 < 1ms，无需缓存。
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_WATCH_SUFFIXES = frozenset({".css", ".html", ".js"})


def calculate_build_id(app_dir: Path | None = None) -> str:
    """基于 static 和 templates 目录下文件内容计算短版本号。

    每次调用都重新计算（不缓存），确保文件修改后立即生效。
    仅扫描 JS/CSS/HTML 文件，忽略 .txt/.py/.md 等。

    Args:
        app_dir: 可选的应用目录路径，默认使用模块内 _APP_DIR。
    """
    base = app_dir or _APP_DIR
    watch_dirs = (base / "static", base / "templates")
    digest = sha256()
    for watch_dir in watch_dirs:
        if not watch_dir.is_dir():
            continue
        for path in sorted(watch_dir.rglob("*")):
            if not path.is_file() or path.suffix not in _WATCH_SUFFIXES:
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


def get_build_id() -> str:
    """获取构建 ID（每次调用都重新计算，保证文件修改后立即生效）。"""
    return calculate_build_id()


# 向后兼容：保留 BUILD_ID 模块级属性，通过 __getattr__ 延迟计算
# 注意：每次访问 BUILD_ID 都会重新计算，不再缓存
def __getattr__(name: str) -> str:
    if name == "BUILD_ID":
        return calculate_build_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
