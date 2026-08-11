# GitHub Issue Agent

一个默认只读的 GitHub Issue 分析智能体：抓取 Issue、自主探索仓库源码、调用 OpenAI 兼容模型，输出带行级证据的结构化根因分析与修复建议报告。适合需要自动化定位 Issue 根因、生成修复补丁的开发者、开源维护者与团队。

## 功能特性

- 🔍 **有界工具调用循环** — 模型在受限循环中自主探索：目录树浏览、全仓库代码搜索、按行号读取源码、Git 提交历史、分支列表、指定提交下的文件快照；支持并行执行独立只读工具并自动去重重复调用
- 🛡️ **证据审计** — 确定性交叉核验模型结论与实际读取的文件与行号，无有效证据支撑的根因强制 `confidence: low`
- 🧭 **独立评审** — 独立的评审 Agent 基于原始源码证据挑战根因、备选假设、修复与测试建议，输出再次校验，评审服务不可用时安全降级
- 📊 **结构化报告** — JSON 输出：摘要、根因（完整因果链）、代码证据（路径 + 行号 + 理由 + 强度）、置信度、修复建议、统一 diff 补丁、建议测试、风险，以及备选假设、影响面、复现路径等增强字段
- 💬 **交互式聊天** — 调查后继续追问，可调用工具，支持 `/regenerate` 重新生成回答
- 🗂️ **会话工作区** — SQLite 持久化，可搜索、归档、恢复、取消、删除；导出 / 导入完整会话 JSON 用于跨实例备份迁移
- ⚡ **并发安全** — 会话版本号乐观锁，防止多 worker / 多进程静默覆盖
- 🔁 **熔断器** — LLM 供应商连续失败后快速失败，应用在故障期间仍能响应
- 🚦 **限流** — 按 API key 的滑动窗口限流（未配置时按客户端 IP 兜底），健康检查与静态资源豁免
- ⚙️ **批量分析** — 一次提交多个 Issue 到进程内异步任务队列后台分析，轮询进度
- 💾 **会话导出 / 导入** — 任意会话可下载为 JSON，导入后生成全新会话继续使用
- 🖥️ **双接口** — FastAPI REST API + Rich 终端 CLI + 内嵌 Web UI（带图表）
- 🐳 **Docker 支持** — 现成 Dockerfile，非 root 用户运行，含健康检查

## 安全模型

- 仅接受 `https://github.com/{owner}/{repo}/issues/{number}` 形式的 URL
- 默认使用只读 GitHub REST API 端点，绝不执行仓库代码
- 仓库写入只发生在 `POST /session/{session_id}/apply-fix`，且同时满足：`WRITE_MODE=true`、存在校验通过的存储提案、请求带显式 `confirm=true`
- 创建 PR 前会基于仓库默认分支重新校验提案；写入中途失败会回滚临时分支
- 将 Issue 文本与仓库内容视为不可信提示数据，注入其中的指令不会被遵循
- 限制候选文件数、模型上下文与输出 token（默认 8000）
- 提供带行号的源码片段，删除未知路径、格式错误或超出所给范围的证据
- 独立评审输出再次经证据校验，评审不可用时安全降级为调查报告

## 技术栈

| 层 | 技术 |
|---|---|
| 语言 | Python 3.11+ |
| Web 框架 | FastAPI + Uvicorn |
| LLM 接入 | OpenAI Python SDK（OpenAI 兼容接口，默认 DeepSeek） |
| HTTP 客户端 | httpx（连接池 + 指数退避重试） |
| 存储 | aiosqlite（SQLite，异步连接池 + 乐观锁） |
| 配置 | pydantic-settings（环境变量 / .env，frozen 单例） |
| 前端 | 原生 JavaScript + Primer CSS（无构建步骤）+ ECharts 图表 + DOMPurify/Marked 渲染 |
| CLI | Rich |
| 测试 | pytest + pytest-asyncio + pytest-cov + Playwright (E2E) |
| 质量 | ruff（lint）+ mypy（类型检查）+ pip-audit（依赖漏洞扫描） |

## 快速开始

### 本地安装

需要 Python 3.11 或更高版本。

**Windows（PowerShell）**

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

**Linux / macOS（bash）**

```bash
python -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env`，至少设置：

```dotenv
OPENAI_API_KEY=<your-key>
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-pro
```

私有仓库或需要更高 GitHub 限流时，设置 `GITHUB_TOKEN=<your-token>`。建议使用仅授予 contents 与 issues 只读权限的 fine-grained token；只有开启 `WRITE_MODE` 时才需要 contents 与 pull-requests 的写权限。

### Windows 一键启动

完成本地安装后，双击项目根目录下的 `打开 Issue Agent.cmd`。启动器会检查环境、启动服务并自动打开浏览器；默认使用端口 `9123`，被占用时依次回退 `9124 → 9125`（会先通过 `/health` 确认端口上确实运行的是本应用），实际地址以启动器终端输出为准。使用期间请保持启动器窗口开启，`Ctrl+C` 停止；若服务已在运行，则只打开已有页面。代码更新后启动器会对比构建标识并自动替换过期的本地进程；强制干净重启可执行 `.\start-issue-agent.ps1 -Restart`。

### Docker

```bash
docker build -t issue-agent .
docker run -p 8000:8000 --env-file .env -v issue-agent-data:/app/data issue-agent
```

容器内固定监听 `8000`，可映射到任意宿主端口（`-p HOST:8000`）。

### CLI 使用

```bash
# 单次分析
issue-agent analyze https://github.com/owner/repo/issues/123

# 保存生成的补丁
issue-agent analyze https://github.com/owner/repo/issues/123 --save-patch fix.patch

# 交互式聊天模式
issue-agent chat https://github.com/owner/repo/issues/123
```

聊天模式内：`/save <file>` 保存补丁，`/quit` 或 `/exit` 退出。

### 启动 API 服务

```bash
python -m uvicorn app.main:app --port 9123 --reload
```

浏览器打开 `http://127.0.0.1:9123/docs` 查看 Swagger UI，访问 `http://127.0.0.1:9123/` 使用内嵌 Web UI。

## 配置

所有配置通过环境变量或 `.env` 文件提供（pydantic-settings）。请勿把 `.env` 提交到仓库（已被 `.gitignore` 与 `.dockerignore` 排除）。以下为常用配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 必填 | LLM 供应商 API key |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 地址 |
| `OPENAI_MODEL` | `deepseek-v4-pro` | 调查模型名 |
| `OPENAI_THINKING` | `enabled` | 思考模式（`enabled` / `disabled`） |
| `OPENAI_REASONING_EFFORT` | `high` | 思考强度（`high` / `max`） |
| `OPENAI_TIMEOUT` | `180` | 单次请求超时（秒），思考模式常需 60–120s |
| `GITHUB_TOKEN` | 可选 | GitHub 令牌，提高限流额度并访问私有仓库 |
| `GITHUB_MAX_FILE_BYTES` | `512000` | 跳过大于此字节数的文件 |
| `GITHUB_TIMEOUT` | `30` | GitHub API 请求超时（秒） |
| `GITHUB_CACHE_TTL_SECONDS` | `300` | 仓库树 / 源码缓存有效期，`0` 关闭 |
| `MAX_CANDIDATE_FILES` | `12` | 单次调查最多读取的不同源码文件数 |
| `MAX_PLANNING_PATHS` | `80` | 初始提示中展示的候选路径数 |
| `MAX_FILE_CHARS` | `16000` | 单个文件最多保留字符数 |
| `MAX_TOTAL_CONTEXT_CHARS` | `80000` | 保留的源码 + 对话总字符上限 |
| `MAX_EXPLORATION_TOKENS` | `2000` | 每轮规划输出 token 预算 |
| `MAX_OUTPUT_TOKENS` | `8000` | 每次模型响应最大 token 数 |
| `MAX_AGENT_ITERATIONS` | `15` | 工具调用循环最大迭代次数 |
| `MAX_PARALLEL_TOOL_CALLS` | `4` | 并行执行的最大只读工具数 |
| `MAX_DUPLICATE_TOOL_ROUNDS` | `2` | 连续重复工具轮次上限，触发即停止探索 |
| `TOOL_TIMEOUT` | `60` | 单工具执行超时（秒） |
| `INVESTIGATION_TIMEOUT` | `600` | 单次调查最大墙钟时间（秒） |
| `INDEPENDENT_REVIEW` | `true` | 是否运行独立评审 |
| `REVIEW_MODEL` | 同调查模型 | 独立评审模型（可选） |
| `LANGUAGE` | `zh` | 响应语言（`zh` / `en`） |
| `API_KEY` | 可选 | 设置后在 `X-API-Key` 请求头校验，否则全站开放 |
| `WRITE_MODE` | `false` | 是否允许校验过的 PR 提案与确认后的写操作 |
| `SESSION_DB_PATH` | `data/sessions.db` | SQLite 路径，`:memory:` 用于测试 |
| `SESSION_RETENTION_DAYS` | `30` | 启动时自动清理早于该天数的终态会话 |
| `MAX_PR_FILES` | `20` | 单个 PR 提案最多文件数 |
| `MAX_PR_TOTAL_BYTES` | `1000000` | 提案内容总字节上限 |
| `MAX_SESSION_IMPORT_BYTES` | `5242880` | 导入会话 JSON 大小上限 |
| `BATCH_MAX_CONCURRENT` | `2` | 批量分析并发 worker 数 |
| `BATCH_MAX_QUEUE_SIZE` | `100` | 批量待处理队列容量 |
| `CIRCUIT_BREAKER_THRESHOLD` | `5` | 触发熔断的连续失败次数 |
| `CIRCUIT_BREAKER_RECOVERY` | `30` | 熔断后探活等待秒数 |
| `RATE_LIMIT_REQUESTS` | `30` | 每个限流窗口每个 key 的最大请求数 |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | 滑动限流窗口长度（秒） |

完整配置项详见 [`wiki/Configuration.md`](wiki/Configuration.md)。

## 项目结构

```
app/
  main.py              FastAPI 入口：路由、SSE 流、限流 / 认证中间件
  agent.py             IssueAgent：多阶段调查（获取 → 预读 → 探索 → 验证 → 报告 → 评审）
  tools.py             工具定义与执行器（只读工具 + 可选的 create_pull_request 提案）
  github.py            GitHub REST 客户端（重试、连接池、缓存、路径 / 树分析）
  provider.py          OpenAI 兼容 provider 的请求选项与流式解析
  report_generator.py  结构化报告生成（多级重试 + 思考降级）
  reviewer.py          独立评审 Agent
  evidence.py          确定性证据校验
  retry.py             报告 / 评审共用的重试与思考降级策略
  sessions.py / db.py  会话管理与 SQLite 持久化（连接池 + 乐观锁）
  task_queue.py        进程内异步批量任务队列
  services.py          会话状态、PR 应用 / 回滚等服务逻辑
  cli.py               Rich 终端 CLI
  static/              前端 JS / CSS（无构建步骤）
  templates/           Web 页面模板
tests/                 pytest 单元 / 集成测试
e2e/                   Playwright 端到端测试
wiki/                  项目文档
```

## API 摘要

| 端点 | 说明 |
|---|---|
| `POST /analyze` | 非流式分析，返回 `AnalysisReport` |
| `POST /stream` | SSE 流式调查：`session` → 进度事件 → `report` / `cancelled` / `error` |
| `POST /chat` | 阻塞式聊天 |
| `POST /chat/stream` | SSE 逐 token 流式聊天（Web UI 使用） |
| `GET /health` | 健康检查 |
| `GET /i18n?lang=zh\|en` | 前端语言包 |
| `GET /sessions` | 会话列表，支持 `q` 搜索 |
| `GET /session/{id}` | 会话详情（消息、报告、事件、指标、版本） |
| `PATCH /session/{id}` | 重命名 / 归档 / 恢复 |
| `DELETE /session/{id}` | 永久删除 |
| `POST /session/{id}/cancel` | 请求取消进行中的调查 |
| `GET /session/{id}/report` | 返回已生成的报告 |
| `GET /session/{id}/proposal` | 安全的 PR 提案预览（不含文件内容） |
| `GET /session/{id}/export` | 导出完整会话 JSON |
| `POST /session/import` | 导入会话 JSON 到新会话 |
| `POST /batch` / `GET /batch/{batch_id}` | 批量提交与进度轮询 |
| `POST /session/{session_id}/apply-fix` | 写模式：创建 PR（需 `WRITE_MODE=true` 与 `confirm=true`） |

## 测试

```bash
ruff check .
mypy app/
pytest -v --cov=app --cov-report=term-missing
npm install
npx playwright install chromium
npm run test:e2e
```

Playwright 套件在独立本地服务上验证桌面 / 移动端布局、无障碍标签、报告导航、源码链接、XSS 转义、输入清空与网络故障恢复；CI 在每次 push / PR 上运行同一套件（Python 3.11–3.13 + Docker 构建 + 浏览器回归）。

## 已知限制

- GitHub 代码搜索的鉴权、索引与限流行为比仓库树访问更严格；agent 保留确定性文件名选择作为回退
- SQLite 乐观锁可防止跨 worker 覆盖，但高吞吐分布式部署仍建议使用专用事务数据库与后台任务系统
- 取消是协作式的，在下一个模型 / 工具事件边界生效；在途的 provider 请求可能先完成

## License

[MIT](LICENSE)
