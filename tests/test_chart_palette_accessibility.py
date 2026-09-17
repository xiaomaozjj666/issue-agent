"""图表调色板的可及性约束：风险矩阵/严重度标记必须在色盲下仍可区分。

背景：风险矩阵的标记色按严重度取
``{critical: danger, high: riskMarkerHigh, medium: warning, low: success, unknown: muted}``，
这五种颜色会**同时出现在同一张图里**。审查（2026-09）用二色觉模拟实测发现：
原取值在红色盲（protanopia）下 ``danger/riskMarkerHigh`` 色差仅 3.6（暗）/ 4.5（浅）——
即「严重」与「高」两个相邻等级对红绿色盲用户几乎同色。

本测试把该约束固化下来：
1. 五色两两之间，在正常视觉 + 绿色盲 + 红色盲下的 Lab 色差（ΔE76）不得低于阈值；
2. 每个颜色在自己的页面背景与风险矩阵格底色上，对比度不得低于 3:1。

色盲模拟采用 Viénot/Brettel 1999 的线性 RGB 变换矩阵（工业界常用近似），
不引入第三方依赖，因此结论在任何 Python 版本与平台上完全一致。
"""

from __future__ import annotations

import itertools
import pathlib
import re

import pytest

_CHARTS_JS = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "charts.js"

# 阈值取「实测值 - 余量」：当前暗色最差 15.6、浅色最差 27.1（ΔE76）
_MIN_DELTA_E = 14.0
_MIN_CONTRAST = 3.0

_PAGE_BG = {"PALETTE_DARK": "#0d1117", "PALETTE_LIGHT": "#ffffff"}
# 各严重度标记所在的矩阵格底色（标记必须在自己那格里看得见）
_CELL_BG = {
    "PALETTE_DARK": {
        "danger": "riskCritical",
        "riskMarkerHigh": "riskHigh",
        "warning": "riskMedium",
        "success": "riskLow",
    },
    "PALETTE_LIGHT": {
        "danger": "riskCritical",
        "riskMarkerHigh": "riskHigh",
        "warning": "riskMedium",
        "success": "riskLow",
    },
}
_MARKER_KEYS = ("danger", "riskMarkerHigh", "warning", "success", "muted")

_MATRICES = {
    "deuteranopia": ((0.625, 0.375, 0.0), (0.700, 0.300, 0.0), (0.0, 0.300, 0.700)),
    "protanopia": ((0.567, 0.433, 0.0), (0.558, 0.442, 0.0), (0.0, 0.242, 0.758)),
}


def _palette(name: str) -> dict[str, str]:
    source = _CHARTS_JS.read_text(encoding="utf-8")
    block = re.search(rf"const {name} = \{{(.*?)\n  \}};", source, re.S)
    assert block, f"{name} 未在 charts.js 中找到"
    result = dict(re.findall(r'(\w+):\s*"(#[0-9a-fA-F]{6})"', block.group(1)))
    assert result, f"{name} 未解析出颜色"
    return result


def _rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _simulate(rgb: tuple[float, float, float], kind: str) -> tuple[float, float, float]:
    linear = [_linear(c) for c in rgb]
    matrix = _MATRICES[kind]
    out = [sum(matrix[i][j] * linear[j] for j in range(3)) for i in range(3)]
    return tuple(max(0.0, min(1.0, _srgb(c))) for c in out)  # type: ignore[return-value]


def _lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_linear(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _delta_e(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sum((x - y) ** 2 for x, y in zip(_lab(a), _lab(b), strict=True)) ** 0.5


def _luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(_rgb(a)), _luminance(_rgb(b))), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_severity_markers_stay_distinguishable_under_color_blindness(theme: str) -> None:
    palette = _palette(theme)
    colors = {key: palette[key] for key in _MARKER_KEYS}

    worst = (999.0, "", "")
    for kind in ("normal", "deuteranopia", "protanopia"):
        simulated = {
            key: (_rgb(value) if kind == "normal" else _simulate(_rgb(value), kind)) for key, value in colors.items()
        }
        for left, right in itertools.combinations(sorted(simulated), 2):
            delta = _delta_e(simulated[left], simulated[right])
            if delta < worst[0]:
                worst = (delta, kind, f"{left}/{right}")

    assert worst[0] >= _MIN_DELTA_E, (
        f"{theme} 在 {worst[1]} 下 {worst[2]} 的色差仅 {worst[0]:.1f}（要求 ≥ {_MIN_DELTA_E}）。"
        "风险矩阵里这些颜色同时出现，红绿色盲用户将无法区分严重度，"
        "请调整 charts.js 调色板（可用紫/粉偏移破解暖色坍缩，或拉开明度差）。"
    )


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_severity_markers_are_visible_on_their_own_cell(theme: str) -> None:
    palette = _palette(theme)
    for key, cell_key in _CELL_BG[theme].items():
        assert _contrast(palette[key], palette[cell_key]) >= _MIN_CONTRAST, (
            f"{theme}: {key} 在 {cell_key} 格底色上的对比度不足 3:1"
        )
        assert _contrast(palette[key], _PAGE_BG[theme]) >= _MIN_CONTRAST, f"{theme}: {key} 在页面背景上的对比度不足 3:1"


# 格底色的明度阶梯与相邻等级区分度：色相在色盲下会坍缩，明度是最后一道冗余编码。
_CELL_ORDER = ("riskLow", "riskMedium", "riskHigh", "riskCritical")
_CELL_MIN_STEP = 2.0
_CELL_MIN_DELTA_E = {"PALETTE_DARK": 9.0, "PALETTE_LIGHT": 4.0}


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_risk_cells_form_a_monotonic_lightness_ladder(theme: str) -> None:
    palette = _palette(theme)
    lightness = [_lab(_rgb(palette[key]))[0] for key in _CELL_ORDER]

    if theme == "PALETTE_DARK":
        assert lightness == sorted(lightness), f"暗色主题的严重度色阶应逐级变亮：{lightness}"
    else:
        assert lightness == sorted(lightness, reverse=True), f"浅色主题的严重度色阶应逐级加深：{lightness}"

    steps = [abs(lightness[i + 1] - lightness[i]) for i in range(len(lightness) - 1)]
    assert min(steps) >= _CELL_MIN_STEP, (
        f"{theme} 相邻严重度的明度差过小（{steps}）——色相在色盲/灰度下不可靠，明度阶梯是读出等级的最后一道冗余编码。"
    )


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_adjacent_severity_cells_stay_distinguishable_under_color_blindness(theme: str) -> None:
    palette = _palette(theme)
    for left, right in itertools.pairwise(_CELL_ORDER):
        worst = min(
            _delta_e(_simulate(_rgb(palette[left]), kind), _simulate(_rgb(palette[right]), kind))
            for kind in ("deuteranopia", "protanopia")
        )
        assert worst >= _CELL_MIN_DELTA_E[theme], (
            f"{theme}: {left}/{right} 在色盲模拟下仅差 {worst:.1f}"
            f"（要求 ≥ {_CELL_MIN_DELTA_E[theme]}），相邻严重度的格底色将难以区分。"
        )
