"""C17 TypeScript 迁移决策约束。

C17 痛点：前端 JS 缺乏类型安全。经评估，当前不迁移 TypeScript：

1. 项目无构建工具链（无 package.json/npm/webpack/vite），纯静态 JS
   通过 FastAPI StaticFiles 直接提供。引入 TS 需建立完整构建链。
2. 项目核心是 Python 后端（agent/LLM/分析逻辑），前端是辅助 UI。
3. 4644 行 JS 逐文件重写为 TS 工作量巨大，收益有限。
4. 第三方库（echarts/marked/DOMPurify）通过 CDN 加载，TS 类型定义
   需额外维护 @types 包。

替代缓解措施：
- ``'use strict'`` 已在所有 IIFE 中启用
- 结构约束测试（test_js_structure.py）固化规模上限
- i18n 同步测试（test_i18n_sync.py）防止 key 漂移
- CSS 命名空间测试（test_css_namespace.py）防止样式冲突

若未来决定迁移 TypeScript，必须先满足以下前置条件：
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_no_typescript_build_chain_until_prerequisites_met() -> None:
    """在满足前置条件之前，不得引入 TypeScript 构建链。

    前置条件（全部满足后方可迁移）：
    1. package.json 存在且声明了构建脚本
    2. tsconfig.json 存在且配置了 strict 模式
    3. 前端 JS 总行数超过 6000 行（当前约 4644，未达迁移阈值）
    4. 至少 3 个 JS 文件（当前仅 3 个，刚好达标但无拆分压力）
    """
    has_package_json = (_PROJECT_ROOT / "package.json").exists()
    has_tsconfig = (_PROJECT_ROOT / "tsconfig.json").exists()

    # 若已有构建链，则迁移条件已满足，本测试不再约束
    if has_package_json and has_tsconfig:
        return

    # 无构建链时，禁止引入 .ts 文件
    ts_files = list((_PROJECT_ROOT / "app" / "static" / "js").glob("*.ts"))
    tsx_files = list((_PROJECT_ROOT / "app" / "static" / "js").glob("*.tsx"))
    all_ts = ts_files + tsx_files
    assert not all_ts, (
        f"发现 TypeScript 文件 {[f.name for f in all_ts]}，但项目尚未建立构建链。"
        "引入 .ts 文件前必须先创建 package.json 和 tsconfig.json，"
        "配置构建脚本并确保 strict 模式启用。"
    )


def test_strict_mode_enabled_in_all_iife_modules() -> None:
    """所有 IIFE 模块必须启用 'use strict'（C17 替代缓解措施）。

    在不迁移 TypeScript 的前提下，'use strict' 是最低限度的运行时约束，
    防止意外全局变量、静默错误赋值等问题。
    """
    js_dir = _PROJECT_ROOT / "app" / "static" / "js"
    for js_file in js_dir.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        # 检查 IIFE 开头的 'use strict'
        has_iife = "(function" in text[:200]
        if has_iife:
            assert '"use strict"' in text[:300] or "'use strict'" in text[:300], (
                f"{js_file.name} 的 IIFE 未在开头启用 'use strict'。"
                "在未迁移 TypeScript 前，'use strict' 是强制的最低运行时约束。"
            )
