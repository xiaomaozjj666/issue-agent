from pathlib import Path

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
def _reset_dynamic_cache() -> None:
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
