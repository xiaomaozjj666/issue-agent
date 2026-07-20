# Configuration

所有配置通过环境变量或 `.env` 文件驱动，由 `pydantic-settings` 管理。Settings 实例创建后 **frozen**（不可变），防止运行时意外修改。

---

## LLM Provider

| 变量 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `OPENAI_API_KEY` | *(必填)* | — | LLM 提供商 API Key |
| `OPENAI_BASE_URL` | `https://api.deepseek.com` | — | OpenAI-compatible API 地址 |
| `OPENAI_MODEL` | `deepseek-v4-pro` | — | 调查模型名称 |
| `OPENAI_THINKING` | `enabled` | `enabled` / `disabled` | DeepSeek thinking mode |
| `OPENAI_REASONING_EFFORT` | `high` | `high` / `max` | 推理深度 |
| `OPENAI_TIMEOUT` | `60` | 1–300 | 单次 LLM 请求超时（秒） |
| `OPENAI_MAX_RETRIES` | `2` | 0–5 | SDK 层重试次数 |

> **注意**: DeepSeek thinking mode 下单次请求可能需要 60–120s，建议 timeout ≥ 120。

---

## GitHub Integration

| 变量 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `GITHUB_TOKEN` | *(可选)* | — | GitHub PAT（提高 rate limit / 访问私有仓库） |
| `GITHUB_TIMEOUT` | `30` | 1–120 | GitHub API HTTP 超时（秒） |
| `GITHUB_MAX_RETRIES` | `3` | 0–5 | 应用层指数退避重试次数（仅 5xx/网络错误） |
| `GITHUB_MAX_FILE_BYTES` | `512000` | 4096–2000000 | 跳过超过此大小的文件 |

**重试策略:**
- 5xx 响应 / 网络错误 → 指数退避重试（0.5s × 2^attempt）
- 429 Rate Limit → 立即抛出 `GitHubRateLimitError`，不重试
- 连接池: `max_connections=20`, `max_keepalive_connections=10`

**Token 权限建议:**
- 只读模式: `Contents: read` + `Issues: read`
- 写模式: 额外 `Contents: write` + `Pull requests: write`

---

## Agent Behaviour

| 变量 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `MAX_CANDIDATE_FILES` | `12` | 1–30 | 每次调查最大源文件数 |
| `MAX_PLANNING_PATHS` | `80` | 10–200 | 初始 prompt 中展示的最大路径数 |
| `MAX_FILE_CHARS` | `16000` | 1000–50000 | 单文件最大保留字符数 |
| `MAX_TOTAL_CONTEXT_CHARS` | `80000` | 5000–200000 | 源码 + 对话总上下文上限 |
| `MAX_OUTPUT_TOKENS` | `8000` | 500–16000 | 模型单次响应最大 token |
| `MAX_AGENT_ITERATIONS` | `15` | 3–40 | 工具调用循环最大迭代次数 |
| `MAX_INVESTIGATION_LEDGER_CHARS` | `12000` | 1000–50000 | 调查账本（搜索/历史/工具发现）保留上限 |
| `MAX_CHAT_TOKENS` | `2000` | 500–16000 | 聊天模式单次响应最大 token |

**上下文预算分配（报告生成阶段）:**
- 调查账本 (investigation ledger): `budget // 3`
- 已读源码 (files_read): 剩余预算
- System prompt + issue 信息: 固定开销

---

## Independent Review

| 变量 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `INDEPENDENT_REVIEW` | `true` | — | 是否运行独立评审 |
| `REVIEW_MODEL` | *(同调查模型)* | — | 评审使用的模型（可用更便宜的模型） |
| `REVIEW_MAX_TOKENS` | `8000` | 500–16000 | 评审响应最大 token |
| `MAX_REVIEW_CONTEXT_CHARS` | `32000` | 4000–100000 | 提供给评审的最大上下文 |
| `MAX_REPORT_RETRIES` | `3` | 1–5 | 报告生成重试次数（含首次） |

**重试策略 (报告 & 评审):**
1. 第 1 次: 原始 thinking 配置
2. 中间次: thinking enabled + 错误反馈
3. 最后一次: thinking disabled 保底

---

## Runtime Behaviour

| 变量 | 默认值 | 范围 | 说明 |
|------|--------|------|------|
| `LANGUAGE` | `zh` | `zh` / `en` | 响应语言 |
| `API_KEY` | *(可选)* | — | 设置后启用 X-API-Key 认证 |
| `WRITE_MODE` | `false` | — | 启用 PR 创建能力 |
| `SESSION_DB_PATH` | `data/sessions.db` | — | SQLite 路径（`:memory:` 为临时） |
| `SESSION_STALE_AFTER_SECONDS` | `1800` | 60–86400 | 心跳超时，标记中断会话 |
| `SESSION_RETENTION_DAYS` | `30` | 1–365 | 自动清理已完成会话的天数 |
| `MAX_PR_FILES` | `20` | 1–50 | PR 提案最大文件数 |
| `MAX_PR_TOTAL_BYTES` | `1000000` | 4096–10000000 | PR 提案最大总字节数 |

---

## Logging

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | 日志级别 (DEBUG / INFO / WARNING / ERROR) |
| `LOG_FORMAT` | `console` | `console`（人类可读）或 `json`（结构化 NDJSON） |

**JSON 日志格式:**
```json
{
  "timestamp": "2025-01-15T10:30:00.123Z",
  "level": "INFO",
  "logger": "app.agent",
  "message": "Investigation completed",
  "issue_url": "https://github.com/acme/widget/issues/42",
  "duration_ms": 45000,
  "tool_calls": 8,
  "model_calls": 5,
  "exc_info": null
}
```

---

## .env 示例

```dotenv
# LLM
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-pro
OPENAI_THINKING=enabled
OPENAI_TIMEOUT=180

# GitHub
GITHUB_TOKEN=ghp_your-token-here
GITHUB_TIMEOUT=30
GITHUB_MAX_RETRIES=3

# Agent
MAX_AGENT_ITERATIONS=15
MAX_CANDIDATE_FILES=12
LANGUAGE=zh

# Runtime
WRITE_MODE=false
SESSION_DB_PATH=data/sessions.db
SESSION_RETENTION_DAYS=30
LOG_FORMAT=console
```
