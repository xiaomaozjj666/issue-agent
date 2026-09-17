"""C17 TypeScript 迁移决策约束。

C17 痛点：前端 JS 缺乏类型安全。经评估，当前**不**迁移 TypeScript。

2026-09 复核后的现状（本测试用可计算断言固化，避免文档再次过期）：
- 前端已有 ``package.json``，但**只有 Playwright 测试依赖，没有任何构建脚本**
  （``scripts`` 里没有 build/bundle/tsc 之类），资源仍由 FastAPI StaticFiles 直接提供；
- 无 ``tsconfig.json``；
- 前端 JS 约 8.1k 行 / 11 个文件（旧文档写的「4644 行 / 3 个文件」已过期）；
- 第三方库（echarts / marked / DOMPurify / highlight）**本地自带**在
  ``app/static/vendor/``，不再通过 CDN 加载（旧文档写的 CDN 加载同样已过期）。

替代缓解措施：
- ``'use strict'`` 已在所有 IIFE 中启用（``test_strict_mode_enabled_in_all_iife_modules``）
- 结构约束测试（test_js_structure.py）固化规模上限
- i18n 同步测试（test_i18n_sync.py）防止 key 漂移
- CSS 命名空间测试（test_css_namespace.py）防止样式冲突

迁移前置条件（全部满足即可重新评估；本测试在条件满足时会失败提醒，而不是继续沿用旧结论）：
1. ``package.json`` 声明了构建脚本；
2. ``tsconfig.json`` 存在并启用 strict；
3. 前端规模达到需要类型约束的量级（当前已超过旧文档记录的两倍，属「已达标」）；
4. 模块拆分到足以按文件渐进迁移的粒度（当前 11 个模块，具备渐进条件）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_JS_DIR = _PROJECT_ROOT / "app" / "static" / "js"

# 记录基线：规模增长到该量级时应当重新评估迁移（而不是无限期沿用「暂不迁移」）
_LINE_BASELINE = 7_000
_FILE_BASELINE = 10
_BUILD_SCRIPT_HINTS = ("build", "bundle", "tsc", "vite", "webpack", "rollup", "esbuild")


class _Facts(TypedDict):
    """前端形态事实：精确类型让断言可以做数值比较（此前是 dict[str, object]）。"""

    files: int
    lines: int
    has_package_json: bool
    has_build_script: bool
    has_tsconfig: bool
    vendor_local: bool


def _frontend_facts() -> _Facts:
    """用当前文件系统事实描述前端形态（供断言，不写死快照）。"""
    js_files = sorted(_JS_DIR.glob("*.js"))
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in js_files)

    package_json = _PROJECT_ROOT / "package.json"
    scripts: dict[str, str] = {}
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except json.JSONDecodeError:
            scripts = {}

    return {
        "files": len(js_files),
        "lines": lines,
        "has_package_json": package_json.exists(),
        "has_build_script": any(hint in name.lower() for name in scripts for hint in _BUILD_SCRIPT_HINTS),
        "has_tsconfig": (_PROJECT_ROOT / "tsconfig.json").exists(),
        "vendor_local": (_PROJECT_ROOT / "app" / "static" / "vendor").is_dir(),
    }


def test_frontend_facts_are_recorded_not_assumed() -> None:
    """前端形态必须与 docstring 记录一致：文档写死的旧快照会让结论悄悄过期。"""
    facts = _frontend_facts()

    assert facts["has_package_json"] is True, "package.json 消失：迁移决策依据需重新核对"
    assert facts["has_build_script"] is False, (
        "package.json 已声明构建脚本 —— 构建链已就绪，请重新做 TypeScript 迁移决策，"
        "并同步更新本测试与其 docstring（旧结论是「无构建工具链」）。"
    )
    assert facts["has_tsconfig"] is False
    assert facts["vendor_local"] is True, "第三方库改回 CDN 会推翻 docstring 里的记录"
    assert facts["files"] >= _FILE_BASELINE, f"前端模块数 {facts['files']} 低于基线 {_FILE_BASELINE}"
    assert facts["lines"] >= _LINE_BASELINE, f"前端行数 {facts['lines']} 低于基线 {_LINE_BASELINE}"


def test_no_typescript_build_chain_until_prerequisites_met() -> None:
    """在没有构建链的前提下，不得引入 TypeScript 源文件。"""
    facts = _frontend_facts()
    if facts["has_package_json"] and facts["has_tsconfig"] and facts["has_build_script"]:
        return  # 前置条件已满足：本测试不再约束（迁移决策由人重新评估）

    ts_files = list(_JS_DIR.glob("*.ts")) + list(_JS_DIR.glob("*.tsx"))
    assert not ts_files, (
        f"发现 TypeScript 文件 {[f.name for f in ts_files]}，但项目尚未建立构建链。"
        "引入 .ts 前必须先声明构建脚本、创建 tsconfig.json（strict），"
        "并确认规模确实需要类型约束。"
    )


def test_strict_mode_enabled_in_all_iife_modules() -> None:
    """所有 IIFE 模块必须启用 'use strict'（C17 替代缓解措施）。

    在不迁移 TypeScript 的前提下，'use strict' 是最低限度的运行时约束，
    防止意外全局变量、静默错误赋值等问题。
    """
    for js_file in _JS_DIR.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        has_iife = "(function" in text[:200]
        if has_iife:
            assert '"use strict"' in text[:300] or "'use strict'" in text[:300], (
                f"{js_file.name} 的 IIFE 未在开头启用 'use strict'。"
                "在未迁移 TypeScript 前，'use strict' 是强制的最低运行时约束。"
            )
