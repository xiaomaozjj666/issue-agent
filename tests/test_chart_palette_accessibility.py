"""图表调色板的双重约束：必须「基于 GitHub 配色」且「色盲下仍可区分」。

背景
----
风险矩阵的标记色按严重度取
``{critical: danger, high: riskMarkerHigh, medium: warning, low: success, unknown: muted}``，
这五种颜色会**同时出现在同一张图里**。审查（2026-09）用二色觉模拟实测发现：直接使用
GitHub 语义色（red / orange / yellow）时，``danger/riskMarkerHigh`` 在红色盲下的 Lab
色差仅 3.6（暗色）/ 4.5（浅色）——「严重」与「高」对红绿色盲用户几乎同色。

本测试同时固化两条约束：

1. **取值必须来自 GitHub 官方色板**：候选为 ``@primer/primitives`` 11.10.0
   （2026-09-16 取包核对）的官方语义色与官方色阶档位；``_PRIMER_VALUES`` 逐条登记了取值
   对应的 token 名，调色板里出现任何非官方颜色都会失败。
2. **色盲下仍可区分**：五色两两在正常视觉 + 绿色盲 + 红色盲下的 Lab 色差（ΔE76）下限，
   以及「标记 vs 所在格底色 / 页面背景」对比度 ≥ 3:1；四个风险等级的格底色两两可区分。

官方取值下的最优解（实测）：暗色最差 ΔE 13.7、浅色 12.8（原为 3.6 / 4.5）。做法是
critical 用官方 ``danger``、low 用官方 ``success``，high/medium 取官方 orange / yellow
色阶中明度差更大的档位（``orange/5``+``yellow/9``、``orange/6``+``yellow/6``）——
不引入任何非 GitHub 颜色。

注：Primer 同一色系 0-2 档的明度几乎相同（暗色 L*≈10-11、浅色 L*≈92-93），因此无法在
「只用官方取值」的前提下做出单调明度阶梯；格底色的可区分性改用两两 ΔE 下限保证。
"""

from __future__ import annotations

import itertools
import pathlib
import re

import pytest

_CHARTS_JS = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "charts.js"

# 阈值取「实测值 - 余量」：暗色 13.7、浅色 12.8
_MIN_DELTA_E = {"PALETTE_DARK": 13.0, "PALETTE_LIGHT": 12.0}
_MIN_CONTRAST = 3.0
_MIN_CELL_DELTA_E = 15.0

_PAGE_BG = {"PALETTE_DARK": "#0d1117", "PALETTE_LIGHT": "#ffffff"}
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
_CELL_KEYS = ("riskLow", "riskMedium", "riskHigh", "riskCritical")

# 允许出现的颜色：GitHub Primer 官方语义色 + 官方色阶档位（含 hover 用的相邻档）
_PRIMER_VALUES: dict[str, str] = {
    # 官方语义色（themes/dark.json、themes/light.json）
    "#f85149": "dark fgColor/danger",
    "#3fb950": "dark fgColor/success",
    "#f0f6fc": "dark fgColor/default",
    "#9198a1": "dark fgColor/muted",
    "#d1242f": "light fgColor/danger",
    "#1a7f37": "light fgColor/success",
    "#1f2328": "light fgColor/default",
    "#59636e": "light fgColor/muted",
    # 官方色阶（scales/dark.json）
    "#c46212": "dark orange/5",
    "#f0ca6a": "dark yellow/9",
    "#92a1b5": "dark gray/6",
    "#122117": "dark green/0",
    "#182f1f": "dark green/1",
    "#5a3702": "dark yellow/2",
    "#6d4403": "dark yellow/3",
    "#311708": "dark orange/0",
    "#43200a": "dark orange/1",
    "#3c0614": "dark red/0",
    "#58091a": "dark red/1",
    # 官方色阶（scales/light.json）
    "#a24610": "light orange/6",
    "#805900": "light yellow/6",
    "#647182": "light gray/5",
    "#caf7ca": "light green/0",
    "#9ceda0": "light green/1",
    "#ffec9e": "light yellow/0",
    "#ffd642": "light yellow/1",
    "#fecfaa": "light orange/1",
    "#fbaf74": "light orange/2",
    "#ffe2e0": "light red/0",
    "#fecdcd": "light red/1",
}

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
def test_palette_values_come_from_github_primer(theme: str) -> None:
    """调色板必须完全由 GitHub 官方色板取值构成（本项目的配色基调）。"""
    palette = _palette(theme)
    used = {key: palette[key] for key in _MARKER_KEYS + _CELL_KEYS}
    used.update({f"{key}Hover": palette[f"{key}Hover"] for key in _CELL_KEYS})
    unknown = {key: value for key, value in used.items() if value.lower() not in _PRIMER_VALUES}
    assert not unknown, (
        f"{theme} 出现非 GitHub 官方取值的颜色：{unknown}。"
        "请改用 @primer/primitives 的语义色或色阶档位（并在 _PRIMER_VALUES 登记 token 名）。"
    )


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

    assert worst[0] >= _MIN_DELTA_E[theme], (
        f"{theme} 在 {worst[1]} 下 {worst[2]} 的色差仅 {worst[0]:.1f}（要求 ≥ {_MIN_DELTA_E[theme]}）。"
        "风险矩阵里这些颜色同时出现，红绿色盲用户将无法区分严重度；"
        "可在 GitHub 官方色阶内换用明度差更大的档位（如 orange/5 + yellow/9）。"
    )


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_severity_markers_are_visible_on_their_own_cell(theme: str) -> None:
    palette = _palette(theme)
    for key, cell_key in _CELL_BG[theme].items():
        assert _contrast(palette[key], palette[cell_key]) >= _MIN_CONTRAST, (
            f"{theme}: {key} 在 {cell_key} 格底色上的对比度不足 3:1"
        )
        assert _contrast(palette[key], _PAGE_BG[theme]) >= _MIN_CONTRAST, f"{theme}: {key} 在页面背景上的对比度不足 3:1"


@pytest.mark.parametrize("theme", ["PALETTE_DARK", "PALETTE_LIGHT"])
def test_risk_cells_stay_distinguishable(theme: str) -> None:
    """四个风险等级的格底色必须两两可区分（Primer 各色系底色明度相近，故用 ΔE 而非明度阶梯）。"""
    palette = _palette(theme)
    worst = 999.0
    worst_pair = ("", "")
    for left, right in itertools.combinations(_CELL_KEYS, 2):
        delta = _delta_e(_rgb(palette[left]), _rgb(palette[right]))
        if delta < worst:
            worst, worst_pair = delta, (left, right)
    assert worst >= _MIN_CELL_DELTA_E, (
        f"{theme}: {worst_pair[0]}/{worst_pair[1]} 的格底色仅差 {worst:.1f}（要求 ≥ {_MIN_CELL_DELTA_E}），"
        "相邻风险等级的底纹将难以区分。"
    )


def test_severity_mapping_is_single_source_of_truth() -> None:
    """同一严重度在报告的任何图里必须是同一个颜色。

    历史缺陷：high 在风险矩阵用 riskMarkerHigh（橙）、在波及范围用 warning（黄）；
    low 一处 success（绿）一处 muted（灰）。用户不得不为每张图重新学一遍映射。
    这里直接解析 charts.js：``severityColor`` 必须委托给 ``riskMarkerColor``。
    """
    source = _CHARTS_JS.read_text(encoding="utf-8")

    severity_body = re.search(r"function severityColor\(severity, palette\) \{(.*?)\n  \}", source, re.S)
    assert severity_body, "未找到 severityColor 定义"
    assert "riskMarkerColor(severity, palette)" in severity_body.group(1), (
        "severityColor 必须委托给 riskMarkerColor，否则同一严重度会在不同图里显示成不同颜色。"
    )

    marker_body = re.search(r"function riskMarkerColor\(severity, palette\) \{(.*?)\n  \}", source, re.S)
    assert marker_body, "未找到 riskMarkerColor 定义"
    mapping = dict(re.findall(r"(\w+): palette\.(\w+)", marker_body.group(1)))
    assert mapping == {
        "critical": "danger",
        "high": "riskMarkerHigh",
        "medium": "warning",
        "low": "success",
    }, f"严重度配色映射被改动：{mapping} —— 请同步 _PRIMER_VALUES 与文档说明。"
