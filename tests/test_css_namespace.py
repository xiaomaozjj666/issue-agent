"""CSS 命名空间一致性约束。

C5 痛点：``primer.css`` 单文件 302 个 class，若新增类不带领域前缀，
容易与未来引入的第三方 CSS 冲突。本测试将"无前缀的独立类"固化为
显式白名单，未来新增的无前缀类必须先在此声明并说明理由，否则测试失败。

领域前缀指 ``-`` 分隔的第一段，如 ``report-`` / ``hero-`` / ``diff-``。
白名单中的类要么是第三方库约定（``hljs``），要么是项目内部稳定约定
（``badge`` / ``msg`` / ``toast``），重命名风险高于收益。
"""

from __future__ import annotations

import re
from pathlib import Path

_CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "primer.css"

# 允许的无前缀独立类（选择器以 .name 开头且 name 不含 -）
_BARE_CLASS_WHITELIST: set[str] = {
    "hljs",   # highlight.js 库约定，不可改名
    "badge",  # 项目稳定约定的通用徽章，与 .review-chip 共用样式
    "msg",    # 对话消息气泡，历史稳定命名
    "toast",  # 全局浮层提示，历史稳定命名
}

# 匹配选择器行首的 .name 形式（name 不含 -），捕获独立泛型类
_BARE_CLASS_RE = re.compile(r"(?m)^\.([a-zA-Z][a-zA-Z0-9_]*)\s*[,){: ]")


def test_no_new_bare_classes_outside_whitelist() -> None:
    """所有无前缀独立类必须在白名单中，防止命名空间漂移。"""
    text = _CSS.read_text(encoding="utf-8")
    found = set(_BARE_CLASS_RE.findall(text))
    unexpected = found - _BARE_CLASS_WHITELIST
    assert not unexpected, (
        f"发现未在白名单中声明的无前缀独立类：{sorted(unexpected)}。"
        "新增类应使用领域前缀（如 report-/hero-/diff-），"
        "或显式加入 _BARE_CLASS_WHITELIST 并说明理由。"
    )


def test_no_external_imports_in_stylesheet() -> None:
    """primer.css 不得引入外部 @import，避免第三方 CSS 污染命名空间。"""
    text = _CSS.read_text(encoding="utf-8")
    imports = re.findall(r"@import\b", text)
    assert not imports, f"primer.css 含 {len(imports)} 处 @import，禁止引入外部样式"
