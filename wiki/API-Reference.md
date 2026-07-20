# API Reference

Base URL: `http://127.0.0.1:8000`

所有需要认证的端点需在请求头中携带 `X-API-Key: <your-api-key>`（仅当配置了 `API_KEY` 时生效）。

---

## 健康检查

### `GET /health`

无需认证。

**Response 200:**
```json
{
  "status": "ok",
  "app": "issue-agent",
  "build_id": "a1b2c3d4"
}
```

---

## 分析

### `POST /analyze`

一次性分析（非流式），阻塞直到完成。

**Request:**
```json
{
  "issue_url": "https://github.com/owner/repo/issues/123"
}
```

**Response 200:** `AnalysisReport`
```json
{
  "summary": "问题摘要",
  "root_cause": "根因分析（因果链）",
  "confidence": "high | medium | low",
  "evidence": [
    {
      "file": "src/parser.py",
      "lines": "L42-L58",
      "description": "空输入未做防御检查"
    }
  ],
  "proposed_changes": ["在 parse() 入口添加空值检查"],
  "patch": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ ...",
  "tests": ["test_parse_empty_input_raises_value_error"],
  "risks": ["可能影响下游调用方的异常处理逻辑"],
  "review": {
    "outcome": "approved | revised",
    "audit": { ... }
  }
}
```

**Errors:**
| Code | 条件 |
|------|------|
| 422 | issue_url 格式无效 |
| 429 | GitHub API rate limit |
| 502 | LLM 返回无效响应 / GitHub API 错误 |

---

### `POST /stream`

SSE 流式分析。返回 `text/event-stream`。

**Request:**
```json
{
  "issue_url": "https://github.com/owner/repo/issues/123"
}
```

或恢复已有 session：
```json
{
  "session_id": "a1b2c3d4e5f6"
}
```

**SSE 事件序列:**

| 事件类型 | 说明 | data 字段 |
|----------|------|-----------|
| `session` | 会话创建 | `{session_id}` |
| `start` | 调查开始 | `{title, file_count}` |
| `phase` | 阶段切换 | `{phase, label}` |
| `tool_call` | 工具调用 | `{tool, arguments}` |
| `tool_result` | 工具结果 | `{tool, result}` (truncated) |
| `thinking` | 模型推理 | `{content}` |
| `report` | 最终报告 | `AnalysisReport` |
| `review` | 评审结果 | `{outcome, audit}` |
| `done` | 完成 | `{}` |
| `cancelled` | 已取消 | `{}` |
| `error` | 错误 | `{message}` |

**SSE 格式:**
```
data: {"type": "phase", "data": {"phase": "exploring", "label": "Exploring repository"}}

data: {"type": "done", "data": {}}

```

---

## 对话

### `POST /chat`

新建会话（需要 `issue_url`）或继续已有会话（需要 `session_id`）。

**新建会话:**
```json
{
  "issue_url": "https://github.com/owner/repo/issues/123",
  "message": "分析一下这个 bug"
}
```

**继续会话:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "message": "问题出现在哪个函数？"
}
```

**Response 200:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "reply": "根据分析，问题出在 parse() 函数...",
  "tools_used": ["read_file", "grep_content"],
  "report": { ... }
}
```

**Errors:**
| Code | 条件 |
|------|------|
| 404 | session_id 不存在 |
| 409 | session 已归档 |
| 422 | 缺少 issue_url（新会话）/ 输入无效 |
| 429 | GitHub rate limit |
| 502 | LLM / GitHub 错误 |

---

## 会话管理

### `GET /sessions`

列出会话。

**Query Parameters:**
| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `q` | string | — | 搜索关键词（标题/URL） |
| `archived` | bool | `false` | `true` 返回已归档会话 |

**Response 200:**
```json
[
  {
    "session_id": "a1b2c3d4e5f6",
    "title": "Parser crashes on empty input",
    "issue_url": "https://github.com/acme/widget/issues/42",
    "status": "completed",
    "phase": "completed",
    "created_at": "2025-01-15T10:30:00Z",
    "updated_at": "2025-01-15T10:35:00Z",
    "archived": false
  }
]
```

---

### `GET /session/{session_id}`

获取会话完整详情。

**Response 200:**
```json
{
  "session_id": "a1b2c3d4e5f6",
  "issue_url": "https://github.com/acme/widget/issues/42",
  "title": "Parser crashes on empty input",
  "status": "completed",
  "phase": "completed",
  "messages": [{"role": "user", "content": "..."}],
  "report": { ... },
  "events": [{"type": "phase", "data": {...}, "created_at": "..."}],
  "metrics": {"model_calls": 5, "tool_calls": 8, "duration_ms": 45000},
  "version": 3,
  "created_at": "...",
  "updated_at": "..."
}
```

**Errors:** 404 if not found.

---

### `GET /session/{session_id}/report`

仅返回分析报告（调查完成后）。

**Response 200:** `AnalysisReport`

**Errors:** 404 if session not found or report not yet generated.

---

### `PATCH /session/{session_id}`

更新会话元数据。

**Request (任意组合):**
```json
{
  "display_title": "新标题",
  "archived": true
}
```

**Response 200:** 更新后的 `SessionSummary`

---

### `DELETE /session/{session_id}`

永久删除会话。

**Response 204:** No Content

---

### `POST /session/{session_id}/cancel`

请求取消正在运行的调查（协作式取消）。

**Response 200:**
```json
{
  "status": "cancel_requested"
}
```

---

## PR 提案 (Write Mode)

### `GET /session/{session_id}/proposal`

获取 PR 提案预览（不含文件内容）。

**Response 200:**
```json
{
  "branch": "fix/parser-bug",
  "title": "fix: guard empty parser input",
  "body": "Prevents the parser crash.",
  "files": ["src/parser.py"]
}
```

---

### `POST /session/{session_id}/apply-fix`

创建 Pull Request。需要 `WRITE_MODE=true`。

**Request:**
```json
{
  "confirm": true
}
```

**Response 200:**
```json
{
  "pr_url": "https://github.com/acme/widget/pull/43",
  "branch": "fix/parser-bug"
}
```

**Errors:**
| Code | 条件 |
|------|------|
| 400 | `confirm` 不为 `true` |
| 403 | `WRITE_MODE` 未启用 |
| 404 | session 或 proposal 不存在 |
| 409 | 提案验证失败（分支冲突等） |

---

## 认证

当配置了 `API_KEY` 环境变量时，以下端点需要 `X-API-Key` 请求头：

- 所有 `/session*` 端点
- `/analyze`、`/stream`、`/chat`

公开端点（无需认证）：
- `GET /health`
- `GET /` (Web UI)
- `GET /static/*`
- `GET /openapi.json`、`/docs`

认证失败：
| Code | 条件 |
|------|------|
| 401 | 缺少 X-API-Key 头 |
| 403 | Key 不匹配（constant-time 比较） |

---

## OpenAPI

交互式文档：`GET /docs`（Swagger UI）

OpenAPI JSON：`GET /openapi.json`
