# Development Guide

## 环境搭建

### 前置要求

- Python 3.11+（推荐 3.12）
- Node.js LTS（仅 E2E 测试需要）
- Git

### 安装

```bash
git clone https://github.com/xiaomaozjj666/issue-agent.git
cd issue-agent

# 创建虚拟环境
python -m venv .venv

# 安装（含开发依赖）
# Linux/macOS:
./.venv/bin/pip install -e ".[dev]"
# Windows:
.venv\Scripts\pip install -e ".[dev]"

# 配置
cp .env.example .env
# 编辑 .env 设置 OPENAI_API_KEY
```

### 启动开发服务器

```bash
python -m uvicorn app.main:app --port 8000 --reload
```

打开 `http://127.0.0.1:8000` 使用 Web UI，或 `http://127.0.0.1:8000/docs` 查看 Swagger。

---

## 项目结构

```
issue-agent/
├── app/                    # 应用源码
│   ├── __init__.py         # 包导出
│   ├── main.py             # FastAPI 入口 & 路由
│   ├── agent.py            # 调查引擎（工具循环）
│   ├── tools.py            # 工具定义 & 执行器
│   ├── report_generator.py # 报告生成
│   ├── reviewer.py         # 独立评审
│   ├── evidence.py         # 证据审计
│   ├── github.py           # GitHub API 客户端
│   ├── sessions.py         # 会话管理
│   ├── services.py         # 服务层
│   ├── config.py           # 配置
│   ├── models.py           # Pydantic 数据模型
│   ├── events.py           # SSE 事件类型
│   ├── i18n.py             # 国际化 & Prompt
│   ├── provider.py         # LLM 参数适配
│   ├── retry.py            # 重试策略
│   ├── auth.py             # 认证中间件
│   ├── db.py               # 数据库 Schema
│   ├── cli.py              # CLI 入口
│   ├── build.py            # 构建标识
│   ├── logging_config.py   # 日志配置
│   ├── json_utils.py       # JSON 提取工具
│   ├── errors.py           # 自定义异常
│   ├── templates/          # Jinja2 HTML 模板
│   └── static/             # 前端资源 (JS/CSS)
├── tests/                  # 测试
│   ├── conftest.py         # 共享 fixtures
│   ├── test_agent.py       # Agent 单元测试
│   ├── test_main.py        # API 集成测试
│   ├── test_tools.py       # 工具执行测试
│   ├── test_sessions.py    # 会话管理测试
│   ├── test_github.py      # GitHub 客户端测试
│   ├── test_reviewer.py    # 评审测试
│   ├── test_auth.py        # 认证测试
│   └── ...
├── wiki/                   # 项目文档
├── .github/workflows/      # CI 配置
├── pyproject.toml          # 项目元数据 & 工具配置
├── Dockerfile              # 容器构建
└── .env.example            # 配置模板
```

---

## 测试

### 运行全部检查

```bash
# Lint
ruff check app/ tests/

# 类型检查
mypy app/

# 单元测试 + 覆盖率
pytest -v --cov=app --cov-report=term-missing

# E2E 浏览器测试
npm install
npx playwright install chromium
npm run test:e2e
```

### 测试约定

- 异步测试使用 `pytest-asyncio`（`asyncio_mode = "auto"`，无需手动标记）
- API 测试使用 `app.dependency_overrides` 注入 mock SessionManager
- 测试结束后清理 overrides: `app.dependency_overrides.clear()`
- 覆盖率要求: ≥ 75%（`pyproject.toml` 中配置）

### 测试 DI 模式

```python
from app.main import app, get_session_manager
from app.sessions import SessionManager

def test_example():
    manager = SessionManager()  # 内存模式
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        response = TestClient(app).post("/chat", json={...})
    finally:
        app.dependency_overrides.clear()
```

---

## 代码规范

### Lint 规则 (ruff)

```toml
[tool.ruff]
line-length = 120
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "UP", "B", "C4", "SIM"]
```

- `E/F/W`: pyflakes + pycodestyle
- `I`: import 排序 (isort)
- `UP`: pyupgrade（现代语法）
- `B`: bugbear（常见 bug 模式）
- `C4`: comprehensions
- `SIM`: simplify

### 类型检查 (mypy)

```toml
[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true
warn_unused_configs = true
```

### 代码风格要求

1. **Docstring**: 所有模块、类、public 方法必须有 docstring
2. **类型标注**: 所有函数参数和返回值必须有类型标注
3. **模块 docstring 格式**: 简述职责 + 关键设计决策
4. **DI 模式**: 使用 `Annotated[T, Depends(...)]` 类型别名，避免 B008
5. **配置**: 所有可调参数通过 `Settings` 暴露，不硬编码

---

## 添加新工具

1. 在 `app/tools.py` 的 `_READ_ONLY_TOOLS` 列表添加工具 schema：

```python
{
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "工具描述",
        "parameters": {
            "type": "object",
            "properties": {
                "param": {"type": "string", "description": "参数说明"}
            },
            "required": ["param"],
        },
    },
}
```

2. 在 `ToolExecutor` 类中添加处理方法：

```python
async def _tool_my_tool(self, arguments: dict[str, Any]) -> str:
    """执行 my_tool 并返回结果字符串。"""
    param = arguments.get("param", "")
    # 实现逻辑...
    return result
```

3. 分发自动完成（`getattr(self, f"_tool_{name}")`），无需修改其他代码。

4. 添加测试到 `tests/test_tools.py`。

---

## 添加新端点

1. 在 `app/models.py` 定义 Request/Response 模型
2. 在 `app/main.py` 添加路由，使用 `SessionMgr` 依赖注入
3. 业务逻辑放在 `app/services.py`（保持 main.py 仅处理 HTTP 关注点）
4. 添加测试到 `tests/test_main.py`

```python
@app.post("/my-endpoint", response_model=MyResponse)
async def my_endpoint(request: MyRequest, session_mgr: SessionMgr) -> MyResponse:
    # HTTP 关注点: 参数验证、状态码
    # 业务逻辑委托给 services 层
    ...
```

---

## Git 工作流

- 主分支: `main`（受保护，CI 通过后方可合并）
- Commit 格式: [Conventional Commits](https://www.conventionalcommits.org/)
  - `feat:` 新功能
  - `fix:` 修复
  - `refactor:` 重构
  - `perf:` 性能优化
  - `test:` 测试
  - `docs:` 文档
- 提交前确保: `ruff check` + `mypy` + `pytest` 全部通过

---

## 调试技巧

### 查看 LLM 交互

设置 `LOG_LEVEL=DEBUG` 可看到完整的工具调用和模型响应日志。

### 内存模式（无持久化）

```dotenv
SESSION_DB_PATH=:memory:
```

每次重启清空所有会话，适合开发调试。

### 禁用评审（加速迭代）

```dotenv
INDEPENDENT_REVIEW=false
```

### 减少迭代次数（快速测试）

```dotenv
MAX_AGENT_ITERATIONS=3
MAX_CANDIDATE_FILES=3
```
