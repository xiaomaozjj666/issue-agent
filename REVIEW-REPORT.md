# GitHub Issue Agent — 全量审查与测试报告

审查日期：2026-02（本地环境，Windows / Python 3.14.6 / venv）
审查范围：`app/`（全部 26 个模块）、`tests/`（21 个文件）、`e2e/`、CI 配置、Dockerfile、启动脚本、Wiki 文档

---

## 一、测试执行结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| Lint | `ruff check app/ tests/` | ✅ All checks passed |
| 类型检查 | `mypy app/` | ✅ 26 files, no issues |
| 单元/集成测试 | `pytest -v --cov=app` | ✅ **250/250 通过**，覆盖率 **80.34%**（门槛 75%） |
| 浏览器 E2E | `npx playwright test` | ✅ **8/8 通过**（桌面 7 + 移动 1，Chromium） |
| 依赖漏洞 | `pip-audit --strict` | ⚠️ 未发现已知 CVE；因本地可编辑安装包 `github-issue-agent` 不在 PyPI 上而 exit 1（CI 已 `continue-on-error`） |

注：本地跑在 Python 3.14；CI 矩阵覆盖 3.11/3.12/3.13，单测在三个版本上通过。

---

## 二、审查发现

### 🔴 Critical

**1. `app/main.py:440`（`/stream`）与 `app/main.py:619`（`/chat/stream`）— 15 秒 SSE 心跳会取消进行中的调查/聊天步骤，静默截断主流程**

```python
event = await asyncio.wait_for(event_iter.__anext__(), timeout=15.0)
```

`wait_for` 超时后会**取消** `__anext__()` 协程：`CancelledError` 被注入异步生成器当前挂起点（如 `execute_many` 的工具执行、`get_issue` 的网络等待、报告阶段等待模型首 token）。生成器内部未捕获该取消 → 生成器直接终结（finally 执行，后续步骤全部丢弃），`wait_for` 对外抛 `TimeoutError` → 心跳继续 → 下一次 `__anext__()` 立刻抛 `StopAsyncIteration` → 流结束，会话被标记为 **completed 但没有报告**（`/stream`）或回复被截断（`/chat/stream`）。

**已用最小脚本实测复现**（Python 3.14）：
- 现状：`await asyncio.sleep(10)` 在 0.5s 超时后立即被取消（"gen: finally ran" 提前打印），下一次 `__anext__()` 直接 `StopAsyncIteration`。
- 触发条件极其常见：任何单步超过 15s 的操作，包括 `search_code`/`get_file_history`（GitHub API 经常 5–20s+）、批量工具执行（`tool_timeout` 默认 60s）、DeepSeek thinking 模式报告阶段首个 delta（常见 30–120s）。
- 影响：主功能（调查→报告）在慢网络/慢模型下静默产出"完成但无报告"的会话，且无任何错误事件；前端无法感知。

**修复方案（已实测验证）**：用 `asyncio.shield` 保护 `__anext__` 任务，超时只发心跳不取消：

```python
event_task = asyncio.create_task(event_iter.__anext__())
while True:
    try:
        event = await asyncio.wait_for(asyncio.shield(event_task), timeout=15.0)
    except TimeoutError:
        yield ": keepalive\n\n"   # event_task 继续运行
        continue
    except StopAsyncIteration:
        break
    # ... 处理 event；请求取消时需 event_task.cancel()
```

实测：shield 方案下 10s 操作期间持续输出 19 次心跳后正常拿到结果。两个端点都需要改。

---

### 🟡 Medium

**2. `app/github.py:352-357` — 所有 403 都被当作限流处理**

`RATE_LIMIT_STATUSES = {403, 429}`，`_get()` 对任何 403 都抛 `GitHubRateLimitError`。GitHub 的 403 也可能是权限不足（token 无访问权、私有仓库未授权）。后果：权限错误被误报为 "GitHub API rate limit hit"，用户/调用方被误导，且错误映射为 429 而非 502。
修复：仅当响应头含 `X-RateLimit-Remaining: 0`（或消息明确为 rate limit）时按限流处理，其余 403 走 `GitHubError`。

**3. 启用 `API_KEY` 后 Web UI 完全不可用（`app/auth.py` + 前端）**

- 前端所有 `fetch`（`core.js` 的 `apiJson`、`app.js`、`enhancements.js`）都不携带 `X-API-Key`，UI 也没有 API key 输入入口；
- `SKIP_PATHS` 只豁免 `/health`、`/`、`/favicon.ico`、`/static`，`/i18n` 也要求认证 → 语言热切换同样 401。
- 后果：配置 `API_KEY`（README 建议生产环境）后，浏览器界面所有请求 401，仅 CLI/curl 可用。
- 建议：UI 增加 API key 输入（存 sessionStorage）并在请求头注入；或将 `/i18n` 加入豁免。

**4. `app/agent.py` `_chat` / `_chat_stream` — LLM 调用失败后遗留 dangling user 消息**

`_chat_prepare` 先把用户消息追加进 `session.messages`；若 `_call_llm*` 抛异常（网络/超时），只有 `_chat_stream` 的空响应分支会回滚该消息，异常路径不回滚。下次用户提问会出现连续两条 user 消息，污染上下文（对工具调用轮次的 API 兼容性也有风险）。
建议：在失败路径 pop 掉未配对的 user 消息，或改为"成功后才落库"。

**5. `app/services.py:252-272` `apply_fix` — 极端回滚场景可能删掉已建 PR 的分支**

若 `create_pull_request` 已成功但响应解析抛 `GitHubError`（如 `html_url` 校验失败），`branch_created=True` 触发分支回滚 → 已创建的 PR 失去 head 分支被 GitHub 自动关闭。实际触发概率低（GitHub 返回的 html_url 几乎总是合法），但属于数据破坏性边界。建议回滚前先确认 PR 未创建成功（如记录 PR 编号后再决定是否回滚）。

### 🔵 Low

**6. `app/db.py:76` — `get_db(":memory:")` 每次连接都是独立内存库**：连接池 size=5 时 5 条连接互不可见。当前 `SessionManager` 把 `:memory:` 映射为 `MemoryStore`，未走此路径，但这是未来代码/测试的坑。建议为内存库加 `shared_cache` URI 或文档标注。

**7. `app/sessions.py:366` — `SqliteStore.list` 搜索对 `issue_json` 做 `LIKE '%...%'` 全表扫描**：会话量大时慢；只有 `status, updated_at` 索引。可接受（LIMIT 兜底），量大时可考虑 FTS5。

**8. `app/agent.py:478-479` — `_chat_stream` 内 `try`/`for` 使用 2 空格缩进**：合法且 ruff 通过，但与全库 4 空格不一致，建议统一。

**9. `app/main.py` `StreamRequest.message` 字段从未被使用**：`/stream` 端点只做调查，`message` 是死字段。要么实现（流式追问）要么删除。

**10. `app/task_queue.py` — 无效 URL 提交时不校验**：`submit()` 接受任意字符串，URL 解析错误要等 worker 异步执行时才暴露为 failed。建议提交时同步 `parse_issue_url`。

**11. `tests/test_fixes_coverage.py:52-57` 文档与实现漂移**：docstring 声称 "pre-SELECT + merge was removed"，但 `sessions.py:211-222` 的 merge 仍在（测试本身仍通过，无行为问题）。

**12. `pip-audit --strict` 本地必然 exit 1**：可编辑安装的本地包不在 PyPI 导致 "could not be audited"。CI 已 `continue-on-error`；本地可忽略或改用 `pip freeze` 输出审计。

### 🔵 Low（补充）

**13. `.env.example` 未覆盖全部可调配置**：`RATE_LIMIT_*`、`BATCH_MAX_HISTORY`、`GITHUB_CACHE_*`、`GITHUB_MAX_TREE_ENTRIES`、`TOOL_TIMEOUT`、`INVESTIGATION_TIMEOUT`、`MAX_EXPLORATION_TOKENS`、`MAX_PARALLEL_TOOL_CALLS`、`MAX_DUPLICATE_TOOL_ROUNDS`、`REVIEW_MODEL` 等字段未写入 `.env.example`，新用户不易发现这些开关。建议补齐。

---

## 前端审查（app/templates + app/static/js 逐点核查）

审查方式：对全部 7 个前端文件（index.html、core.js、session-runtime.js、charts.js、motion.js、enhancements.js、app.js 共约 7,900 行）做安全与正确性核查。曾派子代理深度复核（运行 45+ 分钟未产出即中止），以下结论来自主代理的逐点检查，覆盖全部 `innerHTML` 注入点、fetch/SSE 链路、URL 构造与状态机关键路径。

### 安全结论：未发现 XSS

- **Markdown 渲染**（app.js:1106-1138）：`marked + DOMPurify` 双白名单（标签/属性均受限），`afterSanitizeAttributes` 钩子强制外链 `rel="noopener noreferrer nofollow"`；marked/DOMPurify 任一加载失败自动降级为 `escapeHtml` 纯文本（含 `window.__markedFailed` / `__domPurifyFailed` 检测与 CDN 失败横幅）。
- **全部 `innerHTML` 站点逐一核查**：工具卡片（app.js:1321）、时间线/历史摘要（session-runtime.js:78/131）、报告章节（app.js:2006-2008 经 `pushSection` + escapeHtml）、图表 tooltip（charts.js 全部 formatter 均 `IA.escapeHtml`）、图表弹窗（app.js:2800）、CDN 通知（app.js:4091）、骨架屏（静态模板）——凡插入不可信数据处均经 `escapeHtml`/`escapeAttr`/`safeClass`。
- **URL 构造**：GitHub blob 链接（core.js:600-619）逐段 `encodeURIComponent`；issue URL 校验 `ISSUE_URL_PATTERN` 白名单；下载文件名（enhancements.js:313）为固定前缀 + session id。
- **localStorage** 仅存主题/设置/折叠状态，无密钥；前端不持有任何凭据。
- **SSE 解析**（core.js:399-433）兼容 `\n\n`/`\r\n\r\n`/`\r\r`，非 JSON 行跳过，无注入路径。

### 正确性/健壮性观察

- 会话切换有完整的竞态防护（`restoreRequestId`、切换前 `cancelCurrentStream`、取消轮询时校验 `sessionId` 归属，session-runtime.js:214-256）。
- 图表生命周期管理（dispose/resize/主题切换）有 `IntersectionObserver` 懒加载与 rafId 清理（motion.js:31-99），尊重 `prefers-reduced-motion`。
- 无事件监听器泄漏迹象：图表/弹窗关闭时显式 `dispose()` + 移除 keydown 监听。
- 唯一跨前后端的设计缺口是 **API_KEY 认证与 UI 的脱节**（见发现 #3）：前端无 key 输入、所有请求不带 `X-API-Key`，配置 `API_KEY` 后 Web UI 整体不可用。

### 💡 观察（非问题）

- `app.js` 达 4,126 行（有 `test_js_structure` 行数/函数数预算测试护航，且模块已拆 core/charts/motion/session-runtime/enhancements 五份）；如继续增长可考虑进一步拆分。
- e2e（`e2e/ui.spec.js`）全部走 `page.route` mock 后端，是纯 UI 回归测试，不覆盖真实网络/模型路径——与本次发现的 15s 心跳 bug 未被任何测试捕获一致。

### 💡 观察（非问题）

- **会话 ID 熵**：`uuid4().hex[:12]` = 48 bit；未配置 `API_KEY` 时任何能访问端口的人可枚举/操作会话。作为本地工具可接受，但建议文档明示"必须配合 API_KEY 或网络隔离使用"。
- **`/chat`（非流式）全程持有 `session.lock`**（`agent.py:368-369`）：单会话聊天串行化是有意设计（有测试锁定），但与 `/stream`"不全程持锁"的注释意图不一致，建议补注释。
- **metrics 合并方向**：`update_metrics`（不递增 version）与 `save()`（乐观锁）并发时，极端情况下 `save()` 会用内存旧值覆盖 DB 新值；单进程部署下实际由同一 session 对象累积，风险低。
- 依赖层面 `openai`/`fastapi`/`httpx` 等版本均较新且 `pip-audit` 无已知 CVE；`requirements` 均带上下限约束，锁得较紧。

---

## 三、做得好的地方（正面确认）

- **安全设计扎实**：URL 白名单（仅 https://github.com）、路径穿越防护（`..`/反斜杠/绝对路径拒绝）、GitHub 搜索 scope 防覆盖、`WRITE_MODE` + `confirm=true` 双重确认 + 提案二次校验 + 分支回滚、prompt 注入规则写入系统提示、`_UNTRUSTED_CONTENT_RULES` 注入到所有 agent 提示。
- **前端 XSS 防线到位**：报告/消息 Markdown 走 `marked + DOMPurify`（标签/属性白名单 + `afterSanitizeAttributes` 钩子强制 `rel="noopener noreferrer nofollow"`），库加载失败降级纯文本；工具卡片、时间线、历史卡片全部 `escapeHtml`；session-runtime.js 无裸注入。
- **并发正确性**：会话乐观锁（version）+ 冲突 409、取消/中断路径的 `CancelledError` 传播链处理、`recover_stale` 兜底、流式端点不全程持锁、连接池借用跟踪。
- **工程纪律**：`.env` 未入库、ruff/mypy 全绿、CI 矩阵完整、e2e 使用 mock 路由隔离后端、Docker 非 root + HEALTHCHECK、报告回填脚本带备份+校验且幂等、日志 JSON 格式支持。
- **测试质量高**：250 个单测覆盖熔断器状态机、重试降级、证据过滤、并发冲突、缓存边界、回滚等关键路径；覆盖率 80.3% 且有 fail_under 门槛。

---

## 四、修复优先级建议

1. 🔴 **立即**：修复 `/stream` 与 `/chat/stream` 的 15s 心跳取消 bug（shield 方案，两处）。
2. 🟡 尽快：403 限流误判；API_KEY 与 Web UI 的认证缺口（二选一：UI 加 key 输入 或 文档明示 API-only）。
3. 🟡 排期：chat 失败路径的消息回滚、apply_fix 回滚边界。
4. 🔵 顺手：缩进统一、死字段清理、`.env.example` 补全、文档漂移修正。

## 五、总体结论

代码库整体质量高：安全设计系统性强（写入三重确认、不可信数据隔离、前端双白名单渲染），并发模型经得起推敲（乐观锁 + 取消链 + 熔断/限流），工程配套完整（CI 三版本矩阵、覆盖率门槛、e2e、文档、Docker 非 root）。250 个单测 + 8 个 e2e 全部通过，ruff/mypy 全绿。

**唯一必须立即处理的问题**是 15s SSE 心跳会取消进行中的生成器步骤（🔴，实测复现），它会在慢网络/慢模型的常规场景下静默截断调查并把会话标记为"已完成但无报告"。修复方案（`asyncio.shield` 保活）已实测验证有效。

## 六、修复记录（2026-02 全量修复后验证）

以下问题已全部修复，并新增对应回归测试：

| # | 问题 | 修复 | 回归测试 |
|---|---|---|---|
| 1 🔴 | 15s 心跳取消生成器步骤（main.py 两处） | 新增 `_iter_events_with_heartbeat`（shield 任务保活，超时只发心跳；请求取消时才取消步骤） | `tests/test_heartbeat.py`（5 个用例：慢步骤保活、快速透传、空生成器、消费者取消传播、多步不截断） |
| 2 🟡 | 403 一律误判为限流 | 仅 429 或带限流头（`X-RateLimit-Remaining: 0` / `Retry-After` / `X-RateLimit-Reset`）的 403 视为限流，其余按权限错误走通用 4xx | `test_403_without_rate_limit_headers_is_not_rate_limit_error`、`test_429_always_raises_rate_limit_error` |
| 3 🟡 | API_KEY 配置后 Web UI 不可用 | 前端新增 API 密钥支持：`core.js` 的 `authHeaders()` 统一注入 `X-API-Key`（apiJson 自动 + 4 处直接 fetch 手工合并），设置面板新增 API key 输入（存 localStorage，不进入请求覆盖），zh/en i18n 补齐 | e2e `sends the stored API key with every API request when present` |
| 4 🟡 | chat 失败路径遗留 dangling user 消息 | `_chat` / `_chat_stream` 记录 `turn_start`，`except BaseException`（含取消）时回滚本次尝试追加的全部消息 | `test_chat_rolls_back_messages_on_model_failure`、`test_chat_stream_rolls_back_messages_on_model_failure` |
| 5 🟡 | apply_fix 回滚可能删掉已建 PR 的分支 | 新增 `GitHubPRCreatedError`：PR 已创建但响应校验失败时抛专用异常，`services.apply_fix` 对该异常跳过分支回滚 | `test_apply_fix_does_not_delete_branch_when_pr_was_created`、`test_apply_fix_rolls_back_branch_on_generic_failure` |
| 6 🔵 | `StreamRequest.message` 死字段 | 已删除（pydantic 对多余字段默认忽略，不破坏客户端兼容） | — |
| 7 🔵 | task_queue 提交时 URL 不校验 | `submit()` 同步 `parse_issue_url`，非法 URL 立即 422 | `test_submit_rejects_invalid_url_immediately` |
| 8 🔵 | `_chat_stream`/`_chat` 2 空格缩进 | `ruff format app/agent.py` 统一 4 空格 | — |
| 9 🔵 | `ConnectionPool(":memory:")` 每连接独立库 | 构造时直接拒绝并提示使用 MemoryStore；`get_db` 文档标注 | — |
| 10 🔵 | `.env.example` 缺配置项 | 补齐 `GITHUB_CACHE_*`、`GITHUB_MAX_TREE_ENTRIES`、`INVESTIGATION_TIMEOUT`、`TOOL_TIMEOUT`、`MAX_EXPLORATION_TOKENS`、`MAX_PARALLEL_TOOL_CALLS`、`MAX_DUPLICATE_TOOL_ROUNDS`、`BATCH_MAX_HISTORY`、`RATE_LIMIT_*` | — |
| 11 💡 | 测试文档与实现漂移 | `test_fixes_coverage.py` docstring 修正为与实际 merge 语义一致 | — |
| 12 💡 | 会话 ID 熵仅 48 bit（`uuid4().hex[:12]`） | 提升至 64 bit（`[:16]`，sessions.py 两处）；`task_queue._new_id` 同步 `token_hex(6)`→`token_hex(8)`；测试同步 | `test_sessions.py` 长度断言更新为 16 |
| 13 🔵 | `SqliteStore.list` 对 `issue_json` 整列 LIKE 扫描 | 改为只搜 `issue_url`/`display_title`/`json_extract(issue_json,'$.title')`，语义与 MemoryStore 对齐，避免扫描 body/comments 大文本 | 现有搜索测试覆盖（`query="parser"`） |
| 14 💡 | `pip-audit --strict` 因本地可编辑包恒 exit 1 | CI 改为 `pip-audit --skip-editable`（非 strict）：真实 CVE 仍失败阻断，跳过项仅提示；本地验证 exit 0 | 本地实测通过 |
| 15 🔵 | `chat` 新会话路径状态写入不持锁 | 与 `/stream` 一致：状态切换持锁、调查执行不持锁 | 现有 chat 测试覆盖 |
| 16 💡 | 两个观察项补充说明 | `agent.chat` 持锁为有意设计（串行化，有测试锁定）已加注释；`update_metrics` 与 `save()` 的合并语义已加注释 | — |

**修复后全量验证（第二轮）**：
- ruff ✅ / mypy ✅（26 文件）
- pytest ✅ **262/262 通过**，覆盖率 81.25%
- pip-audit ✅ `No known vulnerabilities found`（exit 0）
- e2e：9 个用例结果见上

**保留未改（有意为之，均有注释说明）**：
- 无——第一、二轮修复已覆盖审查报告全部发现与观察项（含"保留未改"的两项：LIKE 搜索已优化、pip-audit 已可判定）。
