# 评审指南

本文件定义本仓库的评审优先级与可跳过范围。

## 最可能失效的地方

- **证据校验的边界判定**（`app/evidence.py`）：`parse_line_range` 只接受 `L12`、`L12-L20` 这类格式，任何放宽或改写都会直接改变哪些引用被保留；`_score_content_alignment` 在无行号、引用为空、标识符无交集时分别落到 `unknown` / `weak`，`validate` 再按引用条数和对齐结果给置信度封顶。这里的启发式一旦调整，可能悄悄放过无依据结论，或把有效证据误降级。
- **写路径的三重闸门与回滚**（`app/services.py` 的 `apply_fix`、`app/tools.py` 的 `validate_pr_proposal`、`app/github.py` 的写方法）：仓库写入必须同时满足 `WRITE_MODE=true`、存在校验过的提案、请求带 `confirm=true`；提案还要经过分支名、受保护路径、单文件与总字节数校验。失败回滚时 `GitHubPRCreatedError` 是特例——PR 已创建却删分支会连带关闭 PR，改错这一分支等于生产事故。
- **补丁验证的进程与平台语义**（`app/patch_validation.py`）：命令通过 `asyncio.create_subprocess_shell` 执行，超时后 Windows 走 `taskkill /T /F`、POSIX 走 `killpg`，`CREATE_NEW_PROCESS_GROUP` 用 `getattr` 兼容非 Windows；输出由 `_read_limited` 截断，`_overall_status` 按「补丁前失败、补丁后通过、回归全过」的真值表判定。任何一处偏差都会把未验证的补丁标成通过，或留下孤儿进程与 worktree。
- **会话并发与持久化**（`app/db.py` 的 `ConnectionPool`、`app/sessions.py` 的版本乐观锁、`app/sse.py` 的心跳）：连接池耗尽会抛 `RuntimeError`；`save` 用 `WHERE session_id=? AND version=?` 做乐观锁并在失败时抛 `SessionConflictError`；SSE 心跳必须用 shield 保护在途步骤，不能让保活超时取消调查。心跳、取消、落库三条路径交叉时最容易出现状态卡死或静默丢报告。
- **构建身份缓存**（`app/build.py`）：`get_build_id` 依赖 `(mtime_ns, size)` 指纹加新鲜窗口，Windows 时间戳粒度不足时同 tick 等长写入会撞指纹；`os.scandir` 遍历、目录不跟随符号链接、文件跟随符号链接这些细节都影响启动器判断是否重启。
- **GitHub 输入与路径拼接**（`app/github.py`）：`parse_issue_url` 只接受 `https://github.com/...` 或 `www.github.com` 的固定路径；`_repo_segment` 与各处 `quote` 决定 owner/repo/path 如何进入 REST URL；大仓库树走递归分页，缓存键与 TTL 决定一次调查内读到的是否是同一快照。改动这些位置要同时考虑越权访问、路径编码和限流回退。

## 重点看

- `app/evidence.py` 与 `app/report_generator.py`：证据保留、强度降级、置信度封顶，以及报告解析失败后的多级重试与 thinking 降级。
- `app/services.py` 的 `apply_fix`、`app/tools.py` 的 `validate_pr_proposal` / `_is_protected_write_path`、`app/github.py` 的分支与文件写入方法。
- `app/patch_validation.py` 全文件：命令执行、超时终止、输出截断、worktree 清理与状态真值表。
- `app/db.py`、`app/sessions.py`、`app/sse.py`：连接池、版本乐观锁、取消语义与心跳保活。
- `app/build.py` 与 `start-issue-agent.ps1`、`打开 Issue Agent.cmd`：构建身份、端口回退和过期进程替换。

## 可以跳过

- `uv.lock`、`package-lock.json`：锁文件；确认版本变化与 `pyproject.toml` / `package.json` 一致即可，不逐行审。
- `app/static/vendor/*.min.js`：第三方压缩产物（echarts、highlight、marked、purify），只看是否更新版本与来源。
- `docs/screenshots/*.png`：文档截图快照，不影响运行逻辑。
- `.venv/`、`node_modules/`、`__pycache__/`、`.mypy_cache/`、`.ruff_cache/`、`test-results/`、`build/`、`data/`、`github_issue_agent.egg-info/`：本地生成物，已被忽略，不应出现在 diff 中。
- `CHANGELOG.md` 中纯依赖升级条目：核对版本号与锁文件一致即可。

## 本地验证

评审基线使用与 CI 同一组命令（CI 另带 `--cov-report=xml`、`--junitxml` 与覆盖率门槛）：

```bash
ruff check app/ tests/ scripts/ evals/
ruff format --check app/ tests/ scripts/ evals/
mypy app/ tests/ scripts/ evals/
pytest -v --cov=app --cov-report=term-missing
```

涉及前端交互或布局时再运行 `npm ci && npx playwright install chromium && npm run test:e2e`；涉及 Dockerfile 或启动路径时运行 `docker build -t issue-agent .`。

## 评审规则

- 每条发现必须带 `file:line` 证据，不接受凭命名或猜测。
- 标严重度：Important / Nit / Pre-existing；区分本次引入与既有问题。
- 先报真实 bug，再报可读性；最多 3 条 nit。
- 已提过的发现不重复；第 3 轮起只审新增 diff，并请作者重新考虑方案。
- 每条发现必须被处理：修复，或在回复中说明为何不修。
- 安全类发现未解决前不得合并。
