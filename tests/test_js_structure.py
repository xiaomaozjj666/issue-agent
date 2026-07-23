"""前端 JS 结构约束。

C1 痛点：``app.js`` 是单个巨型 IIFE（约 4000 行、74 个顶层函数），
维护困难。完全拆分需大规模重构闭包变量，风险高于收益。本测试采取
务实策略：固化当前规模为上限，防止继续膨胀，并通过模块边界注释
标识未来可拆分的边界。

约束：
1. ``app.js`` 顶层函数数量不得超过当前基线 +5 的容差
2. ``app.js`` 总行数不得超过当前基线 +100 的容差
3. ``core.js`` 必须暴露 ``enumLabel`` 到 IA 命名空间（C1 去耦成果）
4. ``app.js`` 不得重新引入 ``window.`` 裸全局导出（C12 约束延续）
"""

from __future__ import annotations

import re
from pathlib import Path

_JS_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
_APP_JS = _JS_DIR / "app.js"
_CORE_JS = _JS_DIR / "core.js"

# 基线值：当前 app.js 的顶层函数数量与总行数。
# 新增功能应优先考虑是否可拆分到独立模块文件，而非继续向 app.js 堆叠。
_APP_JS_MAX_FUNCTIONS = 80   # 当前 74，容差 +6
_APP_JS_MAX_LINES = 4100     # 当前 ~3971，容差 +129


def _count_top_level_functions(text: str) -> int:
    """统计 IIFE 内的顶层 function 声明数量（2 空格缩进）。"""
    return len(re.findall(r"(?m)^  function \w+", text))


def test_app_js_function_count_within_budget() -> None:
    """app.js 顶层函数数量不得超过预算，防止 IIFE 继续膨胀。"""
    text = _APP_JS.read_text(encoding="utf-8")
    count = _count_top_level_functions(text)
    assert count <= _APP_JS_MAX_FUNCTIONS, (
        f"app.js 顶层函数数量 {count} 超过预算 {_APP_JS_MAX_FUNCTIONS}。"
        "新增功能应考虑拆分到独立模块文件（如 charts.js / files-tracker.js），"
        "而非继续向 app.js 堆叠。若确需新增，请上调 _APP_JS_MAX_FUNCTIONS 并说明理由。"
    )


def test_app_js_line_count_within_budget() -> None:
    """app.js 总行数不得超过预算，防止单文件过大。"""
    text = _APP_JS.read_text(encoding="utf-8")
    lines = text.count("\n") + 1
    assert lines <= _APP_JS_MAX_LINES, (
        f"app.js 总行数 {lines} 超过预算 {_APP_JS_MAX_LINES}。"
        "新增功能应考虑拆分到独立模块文件。"
    )


def test_core_js_exposes_enum_label() -> None:
    """core.js 必须暴露 enumLabel 到 IA 命名空间（C1 去耦成果）。"""
    text = _CORE_JS.read_text(encoding="utf-8")
    assert "function enumLabel(" in text, "core.js 缺少 enumLabel 函数定义"
    assert "enumLabel," in text, "core.js 的 IA 命名空间未导出 enumLabel"


def test_app_js_has_no_bare_window_globals() -> None:
    """app.js 不得重新引入 window. 裸全局导出（C12 约束延续）。

    允许 ``window.IssueAgent`` 和 ``window.addEventListener`` 等标准 API，
    以及 HTML inline onerror 设置的 CDN 降级标志（``__echartsFailed`` 等），
    禁止 ``window.myFunc = ...`` 形式的自定义全局函数导出。
    """
    text = _APP_JS.read_text(encoding="utf-8")
    # 匹配 window.xxx = 形式的自定义全局导出，排除标准 API 和 CDN 降级标志
    bare_exports = re.findall(r"window\.(\w+)\s*=", text)
    # __echartsFailed/__markedFailed/__domPurifyFailed/__hljsFailed 由 HTML onerror 设置
    allowed = {
        "IssueAgent", "addEventListener", "removeEventListener",
        "__echartsFailed", "__markedFailed", "__domPurifyFailed", "__hljsFailed",
    }
    forbidden = [name for name in bare_exports if name not in allowed]
    assert not forbidden, (
        f"app.js 引入了禁止的 window 裸全局导出：{forbidden}。"
        "应通过 IA 命名空间暴露公共接口。"
    )
