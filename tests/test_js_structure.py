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
_SCROLL_FOLLOW_JS = _JS_DIR / "scroll-follow.js"
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"
_CSS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "primer.css"

# 基线值：当前 app.js 的顶层函数数量与总行数。
# 新增功能应优先考虑是否可拆分到独立模块文件，而非继续向 app.js 堆叠。
# 2026-08-24 上调行数预算：续跑功能（startAnalysisStream / resumeAnalysis /
# addResumePrompt）与 analyze / restoreSession 共享闭包状态（currentStream、
# sessionId、navigationStack 等），拆分需大规模暴露闭包变量，风险高于收益，
# 故按测试授权条款上调并说明（净增 3 个函数为续跑相关，函数总数 75 <= 80 仍合规）。
_APP_JS_MAX_FUNCTIONS = 80   # 当前 75，容差 +5
_APP_JS_MAX_LINES = 4300     # 当前 4234，容差 +66（续跑功能所致，见上方说明）


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


# ── 滚动跟随模块（ScrollFollow）结构约束 ─────────────────────────────


def test_scroll_follow_module_exports_api() -> None:
    """scroll-follow.js 必须存在并通过 IA.ScrollFollow 暴露 notify/reset 接口。"""
    assert _SCROLL_FOLLOW_JS.exists(), "缺少 app/static/js/scroll-follow.js 滚动跟随模块"
    text = _SCROLL_FOLLOW_JS.read_text(encoding="utf-8")
    assert "IA.ScrollFollow" in text, "scroll-follow.js 未暴露 IA.ScrollFollow 命名空间"
    assert "notify" in text and "reset" in text, "IA.ScrollFollow 缺少 notify/reset 方法"
    # pinned 状态机核心逻辑标记：贴底跟随 + 未读徽标 + 跳底按钮
    assert "pinned" in text, "缺少 pinned 跟随状态"
    assert "unread" in text, "缺少未读消息计数"
    assert "jump-latest-btn" in text, "未绑定回到底部按钮"
    # 无 window 裸全局导出
    bare = re.findall(r"window\.(\w+)\s*=", text)
    assert all(name in {"IssueAgent", "addEventListener", "removeEventListener"} for name in bare)
    # 后台标签页健壮性：rAF 被暂停时必须有定时器兜底与 visibilitychange 补滚
    assert "scheduleFollowFallback" in text, "缺少 scheduleFollowFallback 定时器兜底（后台标签页 rAF 暂停时跟随失效）"
    assert "visibilitychange" in text, "缺少 visibilitychange 监听（切回标签页时应立即补滚贴底）"


def test_index_html_includes_jump_button_and_module() -> None:
    """index.html 必须包含跳底按钮，且 scroll-follow.js 在 app.js 之前加载。"""
    html = (_TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="jump-latest-btn"' in html, "index.html 缺少 #jump-latest-btn 按钮"
    assert "jump-latest-badge" in html, "index.html 缺少未读徽标元素"
    assert "jump_to_latest" in html, "按钮缺少 data-i18n 国际化绑定"
    follow_pos = html.find("/static/js/scroll-follow.js")
    app_pos = html.find("/static/js/app.js")
    core_pos = html.find("/static/js/core.js")
    assert core_pos != -1 and follow_pos != -1 and app_pos != -1
    assert core_pos < follow_pos < app_pos, "scroll-follow.js 必须在 core.js 之后、app.js 之前加载"


def test_app_js_delegates_scroll_following() -> None:
    """app.js 的滚动跟随必须委托给 ScrollFollow 模块并保留降级路径。"""
    text = _APP_JS.read_text(encoding="utf-8")
    assert "IA.ScrollFollow" in text, "app.js 未接入 IA.ScrollFollow 模块"
    assert "IA.ScrollFollow.reset()" in text, "resetWorkspace 未调用 ScrollFollow.reset"
    # fillToolCard 展开工具结果后必须触发滚动跟随（此前缺失导致进度不可见）
    fill_start = text.index("function fillToolCard")
    fill_end = text.index("const ISSUE_URL_PATTERN", fill_start)
    fill_block = text[fill_start:fill_end]
    assert "scrollToBottomIfNear" in fill_block, "fillToolCard 展开后未触发滚动跟随"


def test_css_has_jump_latest_button_styles() -> None:
    """primer.css 必须包含跳底按钮的悬浮样式与未读徽标样式。"""
    css = _CSS_PATH.read_text(encoding="utf-8")
    assert "#jump-latest-btn" in css, "缺少 #jump-latest-btn 样式"
    assert "#jump-latest-btn.visible" in css, "缺少按钮可见态样式"
    assert ".jump-latest-badge" in css, "缺少未读徽标样式"
    assert ".jump-latest-label" in css, "缺少窄屏隐藏文案的响应式规则"


def test_charts_have_stagger_animation_and_reduced_motion_fallback() -> None:
    """charts.js 必须提供 stagger 入场动画并尊重 prefers-reduced-motion。

    顶尖监控面板（Grafana/Datadog）的标配：条形依次生长而非齐刷刷弹现；
    无障碍要求减弱动效偏好下全部动画禁用。
    """
    text = (_JS_DIR / "charts.js").read_text(encoding="utf-8")
    assert "withAnim" in text, "缺少 withAnim 动画配置工厂"
    assert "animationDelay" in text, "缺少 stagger 延迟动画"
    assert "prefersReducedMotion" in text, "缺少 prefers-reduced-motion 检测"
    # 所有入场 setOption 都必须经 withAnim 包装（禁止退回裸 setOption 丢动画配置）。
    # 例外：ResizeObserver 的增量更新（animationDurationUpdate）不带入场动画语义。
    bare_setoption = [m for m in re.findall(r"setOption\(\{([^}]*)", text)
                      if "animationDurationUpdate" not in m]
    assert not bare_setoption, f"发现 {len(bare_setoption)} 处未包装 withAnim 的 setOption"


def test_charts_have_hover_focus_and_tooltip_dots() -> None:
    """条形/热图系列必须有 hover 聚焦淡出（emphasis.focus）与 tooltip 色点。"""
    text = (_JS_DIR / "charts.js").read_text(encoding="utf-8")
    assert 'focus: "self"' in text, "缺少 emphasis.focus: self 聚焦配置"
    assert 'blurScope: "coordinateSystem"' in text, "缺少 blurScope 坐标系级淡出"
    assert "tooltipDot" in text, "缺少 tooltip 色点工厂"


def test_risk_matrix_marker_uses_ripple_effect() -> None:
    """风险矩阵定位标记必须用 effectScatter 涟漪，且减弱动效下降级普通散点。"""
    text = (_JS_DIR / "charts.js").read_text(encoding="utf-8")
    assert "effectScatter" in text, "风险矩阵标记未使用 effectScatter 涟漪"
    assert "rippleEffect" in text, "缺少 rippleEffect 涟漪配置"
    degraded = 'prefersReducedMotion() ? "scatter" : "effectScatter"' in text
    assert degraded, "effectScatter 必须在 prefers-reduced-motion 下降级为普通散点"
