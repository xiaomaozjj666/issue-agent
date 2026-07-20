# Architecture

## 系统总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Layer                              │
│   Browser (Primer UI)  │  CLI (rich)  │  REST API consumers     │
└────────────┬──────────────────┬───────────────────┬─────────────┘
             │                  │                   │
┌────────────▼──────────────────▼───────────────────▼─────────────┐
│                     FastAPI Application (main.py)                │
│  Routing · DI · SSE Streaming · Auth Middleware · Error Mapping  │
└────────────┬──────────────────┬───────────────────┬─────────────┘
             │                  │                   │
┌────────────▼──────┐ ┌────────▼────────┐ ┌───────▼──────────────┐
│   IssueAgent      │ │ SessionManager  │ │   Service Layer      │
│   (agent.py)      │ │ (sessions.py)   │ │   (services.py)      │
│                   │ │                 │ │                      │
│ • Tool loop       │ │ • MemoryStore   │ │ • Event recording    │
│ • Report gen      │ │ • SqliteStore   │ │ • Report formatting  │
│ • Reviewer        │ │ • OCC version   │ │ • PR apply/rollback  │
│ • Evidence audit  │ │ • Purge         │ │ • Session lifecycle  │
└───┬───────┬───────┘ └────────┬────────┘ └──────────────────────┘
    │       │                  │
┌───▼───┐ ┌─▼──────────┐ ┌────▼────┐
│Tools  │ │ReportGen   │ │ SQLite  │
│(tools │ │(report_    │ │ (db.py) │
│ .py)  │ │generator)  │ │         │
└───┬───┘ └────────────┘ └─────────┘
    │
┌───▼───────────────────────────────────────────┐
│           GitHubClient (github.py)             │
│  REST API · Retry · Connection Pool · Tree    │
└───────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| **入口 & 路由** | `main.py` | HTTP 端点定义、DI 容器、SSE 流组装、错误映射 |
| **调查引擎** | `agent.py` | 工具调用循环编排、并行文件预加载、流式事件生成 |
| **工具执行** | `tools.py` | 沙盒化工具执行（read_file / list_directory / search_code / grep_content / create_pr） |
| **报告生成** | `report_generator.py` | 从调查上下文构造报告 prompt、多级重试、流式 reasoning |
| **独立评审** | `reviewer.py` | 挑战根因假设、验证替代解释、评审结果归一化 |
| **证据审计** | `evidence.py` | 确定性验证：行号范围、文件路径、置信度降级 |
| **GitHub 客户端** | `github.py` | REST API 封装、指数退避重试、连接池、仓库树分析 |
| **会话管理** | `sessions.py` | Session 数据模型、Memory/SQLite 双后端、OCC 并发控制 |
| **数据库** | `db.py` | Schema DDL、迁移、性能索引 |
| **服务层** | `services.py` | 无状态业务逻辑（事件记录、报告格式化、PR 应用） |
| **配置** | `config.py` | pydantic-settings，frozen 实例，环境变量驱动 |
| **国际化** | `i18n.py` | 中英文 system prompt、工具策略、前端字符串 |
| **事件协议** | `events.py` | SSE 事件类型定义（AgentEvent dataclass） |
| **LLM 适配** | `provider.py` | DeepSeek thinking mode 参数适配 |
| **重试策略** | `retry.py` | 报告生成多级重试计划（thinking → feedback → fallback） |
| **认证** | `auth.py` | X-API-Key 中间件，constant-time 比较 |
| **日志** | `logging_config.py` | 结构化 JSON / console 双格式日志 |
| **CLI** | `cli.py` | 命令行入口（analyze / chat），rich 渲染 |
| **构建标识** | `build.py` | 基于前端资源哈希的 BUILD_ID（缓存破坏） |

## 核心数据流

### 调查流程 (POST /stream)

```
Client ──POST /stream──▶ main.py
                           │
                    ┌──────▼──────┐
                    │ 创建 Session │
                    │ 发送 session │──▶ SSE: {"type":"session"}
                    │   event     │
                    └──────┬──────┘
                           │
                    ┌──────▼──────────────┐
                    │ IssueAgent           │
                    │ .investigate_stream()│
                    │                     │
                    │ 1. fetch issue      │──▶ SSE: phase("fetching")
                    │ 2. build tree index │
                    │ 3. preload files    │──▶ asyncio.gather 并行
                    │ 4. tool loop ×N     │──▶ SSE: tool_call / tool_result
                    │ 5. generate report  │──▶ SSE: thinking / report
                    │ 6. evidence audit   │
                    │ 7. reviewer         │──▶ SSE: review
                    └──────┬──────────────┘
                           │
                    ┌──────▼──────┐
                    │ done event  │──▶ SSE: {"type":"done"}
                    │ save session│
                    └─────────────┘
```

### 工具调用循环

```
┌─────────────────────────────────────────────┐
│          Bounded Tool Loop (max 15 iter)     │
│                                             │
│  ┌─────────┐    ┌──────────┐    ┌───────┐  │
│  │ LLM API │───▶│tool_calls│───▶│Execute│  │
│  │  Call   │    │ in resp  │    │ Tools │  │
│  └────▲────┘    └──────────┘    └───┬───┘  │
│       │                             │      │
│       └───── tool results ◀─────────┘      │
│                                             │
│  Exit: no tool_calls → proceed to report    │
└─────────────────────────────────────────────┘
```

## 关键设计决策

### 1. 依赖注入 (DI)

`SessionManager` 通过 FastAPI `Depends` 注入，生命周期绑定到 `app.state`：

```python
def get_session_manager(request: Request) -> SessionManager:
    return request.app.state.session_manager

SessionMgr = Annotated[SessionManager, Depends(get_session_manager)]
```

- lifespan 启动时创建，关闭时 close
- 测试通过 `app.dependency_overrides` 注入 mock

### 2. 工具分发 (Tool Dispatch)

`ToolExecutor.execute` 使用方法名约定 `_tool_<name>` + `getattr` 分发：

```python
handler = getattr(self, f"_tool_{name}", None)
```

新增工具只需：定义 `_tool_xxx` 方法 + 在 `_READ_ONLY_TOOLS` 列表注册 schema。

### 3. DB 写入优化

SSE 流式期间，`record_agent_event` 仅追加事件到内存列表，**不逐事件持久化**。
Session 完整状态仅在关键节点（phase / report / done）写入 SQLite，将每次调查的 DB 写入从 30-50 次降至 ~5 次。

### 4. GitHub API 容错

- 5xx / 网络错误：指数退避重试（0.5s → 1s → 2s），最多 3 次
- 429 Rate Limit：立即抛出 `GitHubRateLimitError`，不重试
- 连接池：`httpx.Limits(max_connections=20, max_keepalive_connections=10)`

### 5. 证据审计 & 置信度

`EvidenceValidator` 是纯确定性逻辑（无 LLM 调用）：
- 验证 evidence 中的文件路径是否在 `files_read` 中
- 验证行号范围 `L12-L45` 是否在实际读取的文件行数内
- 无有效证据 → 强制 `confidence = "low"`

### 6. 独立评审

`ReviewerAgent` 使用独立 LLM 调用（可配置不同模型）：
- 挑战根因的因果链完整性
- 检查是否遗漏替代假设
- 验证修复建议与证据的一致性
- 输出 `approved` / `revised` / `rejected`
- 失败时安全降级为 investigator 原始报告

### 7. 会话并发控制

- 进程内：per-session `asyncio.Lock` 序列化同 session 请求
- 跨进程：SQLite OCC（optimistic concurrency control）via `version` 字段
- 更新时 `WHERE version = ?`，不匹配则抛 `SessionConflictError`

## 安全模型

```
              ┌─────────────────────────────┐
              │     Default: READ-ONLY      │
              │                             │
              │  • GitHub REST (read)       │
              │  • No code execution        │
              │  • Bounded context          │
              └──────────────┬──────────────┘
                             │
              WRITE_MODE=true (opt-in)
                             │
              ┌──────────────▼──────────────┐
              │     Write Path              │
              │                             │
              │  1. Agent stores proposal   │
              │  2. User confirms (POST)    │
              │  3. Revalidate proposal     │
              │  4. Create branch + PR      │
              │  5. Rollback on failure     │
              └─────────────────────────────┘
```

- Issue 文本和仓库内容视为**不可信 prompt 数据**
- 文件路径规范化 + 白名单验证
- PR proposal 大小限制（20 files / 1MB）
- 禁止目标 default branch
