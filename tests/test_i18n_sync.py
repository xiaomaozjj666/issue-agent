"""前后端 i18n 字典同步校验。

C3 痛点：``app/i18n.py::_FRONTEND_STRINGS``（权威源，注入 #i18n-payload）
与 ``app/static/js/core.js::I18N_DEFAULTS``（英文降级兜底）两套字典容易漂移。
本测试强制约束：

1. 前端 JS 中所有 ``t("key")`` / ``translate("key")`` 调用、HTML 模板中所有
   ``data-i18n*="key"`` 属性引用的 key，必须同时存在于
   ``_FRONTEND_STRINGS["zh"]`` 与 ``_FRONTEND_STRINGS["en"]``。
2. ``I18N_DEFAULTS`` 中的每个 key 必须存在于 ``_FRONTEND_STRINGS["en"]``，
   保证降级兜底字典始终是权威源的有效子集。
3. zh / en 两个语言包的 key 集合必须一致，避免某一语言漏翻。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.i18n import _FRONTEND_STRINGS

_STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"

# t("key") / t('key') / translate("key") / translate('key')
# 要求字符串字面量后紧跟 , 或 )，排除 t("prefix_" + var) 这类动态拼接
_KEY_CALL_RE = re.compile(r"\b(?:t|translate)\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*(?:,|\))")
# data-i18n="key" / data-i18n-placeholder="key" / data-i18n-title / data-i18n-aria-label
_DATA_I18N_RE = re.compile(r'data-i18n(?:-[a-z]+)?\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"')


def _collect_js_call_keys() -> set[str]:
    keys: set[str] = set()
    for js_file in (_STATIC_DIR / "js").glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        keys.update(_KEY_CALL_RE.findall(text))
    return keys


def _collect_html_attr_keys() -> set[str]:
    keys: set[str] = set()
    for html_file in _TEMPLATES_DIR.glob("*.html"):
        text = html_file.read_text(encoding="utf-8")
        keys.update(_DATA_I18N_RE.findall(text))
    return keys


def _collect_defaults_keys() -> set[str]:
    """从 core.js 解析 I18N_DEFAULTS 对象的字面量 key。"""
    core_js = (_STATIC_DIR / "js" / "core.js").read_text(encoding="utf-8")
    start = core_js.index("const I18N_DEFAULTS = {")
    # 找到匹配的闭合大括号：从 start 起按花括号深度扫描
    depth = 0
    end = -1
    for idx in range(start, len(core_js)):
        ch = core_js[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    assert end > start, "无法定位 I18N_DEFAULTS 对象的闭合大括号"
    block = core_js[start:end]
    # 匹配行首的 key: 形式（兼容 "key": 与 key: 两种写法），排除注释行
    matched = re.findall(
        r'^\s{2,}(?:"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))\s*:',
        block,
        re.MULTILINE,
    )
    return {a or b for a, b in matched}


def test_zh_and_en_frontend_strings_have_identical_key_sets() -> None:
    """zh 与 en 字典的 key 集合必须一致，避免某一语言漏翻。"""
    zh_keys = set(_FRONTEND_STRINGS["zh"].keys())
    en_keys = set(_FRONTEND_STRINGS["en"].keys())
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    assert not only_zh, f"zh 字典多出 en 缺失的 key：{sorted(only_zh)}"
    assert not only_en, f"en 字典多出 zh 缺失的 key：{sorted(only_en)}"


def test_all_frontend_used_keys_exist_in_both_languages() -> None:
    """前端 JS 调用与 HTML 属性引用的每个 key 必须同时存在于 zh 与 en。"""
    used = _collect_js_call_keys() | _collect_html_attr_keys()
    zh_keys = set(_FRONTEND_STRINGS["zh"].keys())
    en_keys = set(_FRONTEND_STRINGS["en"].keys())
    missing_zh = used - zh_keys
    missing_en = used - en_keys
    assert not missing_zh, f"前端使用但 zh 字典缺失的 key：{sorted(missing_zh)}"
    assert not missing_en, f"前端使用但 en 字典缺失的 key：{sorted(missing_en)}"


def test_i18n_defaults_are_subset_of_frontend_strings_en() -> None:
    """core.js I18N_DEFAULTS 必须是 _FRONTEND_STRINGS['en'] 的子集。

    降级兜底字典若包含权威源没有的 key，会在 payload 加载失败时显示
    与正常状态不一致的文案；若权威源新增 key 而 defaults 未跟进，则
    payload 失败时该 key 会回退为原始 key 字符串。
    """
    defaults = _collect_defaults_keys()
    en_keys = set(_FRONTEND_STRINGS["en"].keys())
    extra = defaults - en_keys
    assert not extra, f"I18N_DEFAULTS 含 en 字典不存在的 key：{sorted(extra)}"


# enumLabel(prefix, value) 在前端动态构造 `${prefix}_${value}` 的 i18n key。
# 这里显式枚举各前缀的合法取值域，确保所有组合在 zh/en 字典中都存在，
# 避免 enumLabel 回退为原始 value 字符串（用户看到裸英文枚举值）。
_ENUM_DOMAINS: dict[str, set[str]] = {
    "phase": {
        "starting", "investigating", "reviewing", "reporting",
        "chatting", "completed", "failed", "interrupted", "cancelled",
    },
    "status": {"running", "completed", "failed", "cancelled"},
    "review_status": {"approved", "revised", "rejected", "unavailable"},
    "confidence": {"high", "medium", "low"},
    "risk_severity": {"high", "medium", "low"},
}


def test_enum_label_dynamic_keys_exist_in_both_languages() -> None:
    """enumLabel(prefix, value) 构造的所有动态 key 必须在 zh/en 字典中存在。"""
    zh_keys = set(_FRONTEND_STRINGS["zh"].keys())
    en_keys = set(_FRONTEND_STRINGS["en"].keys())
    missing_zh: set[str] = set()
    missing_en: set[str] = set()
    for prefix, values in _ENUM_DOMAINS.items():
        for value in values:
            key = f"{prefix}_{value}"
            if key not in zh_keys:
                missing_zh.add(key)
            if key not in en_keys:
                missing_en.add(key)
    assert not missing_zh, f"enumLabel 动态 key 在 zh 字典缺失：{sorted(missing_zh)}"
    assert not missing_en, f"enumLabel 动态 key 在 en 字典缺失：{sorted(missing_en)}"
