from pathlib import Path

from app.build import calculate_build_id


def test_build_id_is_stable_and_tracks_web_asset_changes(tmp_path: Path) -> None:
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

    asset.write_text("window.ready = false;\n", encoding="utf-8")
    assert calculate_build_id(app_dir) != first
