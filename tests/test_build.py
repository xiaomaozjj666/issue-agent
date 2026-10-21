import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app import build as build_module
from app.build import calculate_build_id, get_build_id


def test_build_id_is_stable_and_tracks_runtime_and_web_changes(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    static_dir = app_dir / "static"
    static_dir.mkdir(parents=True)
    source = app_dir / "main.py"
    asset = static_dir / "app.js"
    ignored = static_dir / "note.txt"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    asset.write_text("window.ready = true;\n", encoding="utf-8")
    ignored.write_text("ignored\n", encoding="utf-8")

    first = calculate_build_id(app_dir)
    assert first == calculate_build_id(app_dir)

    ignored.write_text("still ignored\n", encoding="utf-8")
    assert calculate_build_id(app_dir) == first

    source.write_text("VERSION = 2\n", encoding="utf-8")
    after_source_change = calculate_build_id(app_dir)
    assert after_source_change != first

    asset.write_text("window.ready = false;\n", encoding="utf-8")
    assert calculate_build_id(app_dir) != after_source_change


@pytest.fixture(autouse=True)
def _reset_dynamic_cache() -> Iterator[None]:
    """每个测试前后清空 get_build_id 的动态缓存，避免跨测试污染。"""
    yield
    build_module._cache_fingerprint = None
    build_module._cache_build_id = None


def test_get_build_id_returns_cached_value_when_files_unchanged() -> None:
    """文件未变化时 get_build_id 走指纹缓存，返回稳定值。"""
    first = get_build_id()
    assert get_build_id() == first


def test_get_build_id_refreshes_when_static_asset_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """静态资源内容变化后，get_build_id 无需进程重启即返回新版本号。

    H 缺陷回归：BUILD_ID 进程级缓存导致编辑前端文件后浏览器
    持续命中 immutable 旧缓存（scroll-follow 修复不生效的根因）。
    """
    app_dir = tmp_path / "app"
    static_dir = app_dir / "static" / "js"
    static_dir.mkdir(parents=True)
    asset = static_dir / "scroll-follow.js"
    asset.write_text("window.v = 1;\n", encoding="utf-8")

    monkeypatch.setattr(build_module, "_APP_DIR", app_dir)
    build_module._cache_fingerprint = None
    build_module._cache_build_id = None
    before = get_build_id()
    asset.write_text("window.v = 2;\n", encoding="utf-8")
    after = get_build_id()
    assert after != before, "静态资源内容变化后 get_build_id 必须返回新值"
    assert get_build_id() == after


def test_monitor_state_flags_recently_modified_files_as_fresh(tmp_path: Path) -> None:
    """刚写过的文件必须被判为 fresh，否则指纹缓存会漏掉同 tick 的等长改写。"""
    app_dir = tmp_path / "app"
    static_dir = app_dir / "static"
    static_dir.mkdir(parents=True)
    asset = static_dir / "app.js"
    asset.write_text("window.v = 1;\n", encoding="utf-8")

    _paths, _fingerprint, fresh = build_module._monitor_state(app_dir)
    assert fresh is True, "新建文件的 mtime 就在当下，应判为 fresh"

    old_ns = time.time_ns() - 60 * 10**9
    os.utime(asset, ns=(old_ns, old_ns))
    _paths, _fingerprint, fresh = build_module._monitor_state(app_dir)
    assert fresh is False, "把 mtime 推回一分钟后不应再判为 fresh"


def test_monitor_state_skips_unreadable_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """某个子目录不可读时跳过它，而不是让整个构建身份计算抛错。"""
    app_dir = tmp_path / "app"
    (app_dir / "static").mkdir(parents=True)
    (app_dir / "static" / "ok.js").write_text("ok\n", encoding="utf-8")
    (app_dir / "locked").mkdir()
    real_scandir = os.scandir

    def fake_scandir(path: Any) -> Any:
        if Path(str(path)).name == "locked":
            raise PermissionError("denied")
        return real_scandir(path)

    monkeypatch.setattr(build_module.os, "scandir", fake_scandir)
    paths, _fingerprint, _fresh = build_module._monitor_state(app_dir)
    assert [p.name for p in paths] == ["ok.js"]


def test_monitor_state_skips_file_whose_stat_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单个文件的 stat 失败只跳过该文件（删到一半的构建产物等）。"""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    good = app_dir / "ok.py"
    bad = app_dir / "bad.py"
    good.write_text("ok\n", encoding="utf-8")
    bad.write_text("bad\n", encoding="utf-8")

    class _Entry:
        def __init__(self, path: Path, *, broken: bool) -> None:
            self.name = path.name
            self.path = str(path)
            self._path = path
            self._broken = broken

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            return False

        def is_file(self, follow_symlinks: bool = True) -> bool:
            return True

        def stat(self) -> os.stat_result:
            if self._broken:
                raise OSError("stat failed")
            return self._path.stat()

    class _Scandir:
        def __init__(self, entries: list[_Entry]) -> None:
            self._entries = entries

        def __iter__(self):
            return iter(self._entries)

        def __enter__(self) -> "_Scandir":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(
        build_module.os,
        "scandir",
        lambda _p: _Scandir([_Entry(good, broken=False), _Entry(bad, broken=True)]),
    )
    paths, _fingerprint, _fresh = build_module._monitor_state(app_dir)
    assert [p.name for p in paths] == ["ok.py"]


def test_calculate_build_id_skips_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件在扫描后、读取前消失时只跳过它，不影响其它文件参与计算。"""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    keep = app_dir / "keep.py"
    gone = app_dir / "gone.py"
    keep.write_text("keep\n", encoding="utf-8")
    gone.write_text("gone\n", encoding="utf-8")
    paths, _fingerprint, _fresh = build_module._monitor_state(app_dir)
    real_read = Path.read_bytes

    def fake_read(self: Path) -> bytes:
        if self.name == "gone.py":
            raise OSError("vanished")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read)
    digest = build_module._hash_paths(paths, app_dir)
    assert digest == build_module._hash_paths([keep], app_dir), "跳过读不到的文件后应与只算剩余文件一致"
