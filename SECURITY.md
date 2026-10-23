# 安全策略

GitHub Issue Agent 是一个默认只读的 Issue 分析智能体：抓取 Issue、只读探索仓库源码、调用 OpenAI 兼容模型生成带行级证据的报告，并在显式开启写模式后按确认创建 PR。本文件说明如何私密报告**本仓库自身**的安全问题。

## 支持的版本

版本号见 `pyproject.toml`（当前为 0.6.0）；仓库未发布 tag / release，安全修复进入 `main` 最新提交。

| 版本 | 支持状态 |
|---|---|
| 0.6.x（当前） | ✅ 接收安全报告并在 `main` 修复 |
| 更早的历史提交 | ❌ 不单独维护，请使用 `main` 最新代码 |

## 私密报告漏洞

**请勿在公开 Issue 中粘贴漏洞细节、利用代码、GitHub token、LLM API Key 或私有仓库内容。**

请通过 GitHub 的私密漏洞报告渠道提交：

1. 打开 [Security Advisories](https://github.com/xiaomaozjj666/issue-agent/security/advisories) 页面；
2. 点击 **Report a vulnerability**；
3. 填写复现步骤与影响范围。

也可直接访问：<https://github.com/xiaomaozjj666/issue-agent/security/advisories/new>

若页面上看不到该入口（仓库尚未开启私密报告），请先开一个**不含漏洞细节**的 Issue 说明需要私密渠道，维护者开启后会与你联系。

报告中建议包含：

- 受影响的版本 / 提交号、Python 版本与部署方式（本地 uvicorn / Docker）；
- 相关开关的状态（如 `API_KEY`、`WRITE_MODE` 是否开启，`GITHUB_TOKEN` 的权限范围——**不要贴 token 原文**）；
- 复现步骤或最小复现；
- 影响说明，例如鉴权绕过、路径穿越、会话数据泄露、PR 写入越权，或提示注入导致越权工具调用；
- 如有可能，附上修复建议。

会话导出、报告与日志中可能含 Issue 原文和私有仓库源码片段，提交前请先脱敏。在修复发布前，请勿公开细节。

## 范围说明

- 覆盖本仓库的服务端、Web UI、CLI、Docker 镜像，以及调查 / 评审 / 证据校验流水线。
- 设计上把 Issue 文本与仓库内容视为不可信提示数据，不执行被分析仓库的代码；仓库写入只在 `WRITE_MODE=true`、存在校验通过的提案且请求带 `confirm=true` 时发生。部署方应最小化 `GITHUB_TOKEN` 权限并设置 `API_KEY`。
- 如果你能证明模型输出可以绕过证据校验、越权触发写操作或泄露不应返回的数据，属于本仓库的安全问题。
- 不覆盖：被分析仓库自身的漏洞、LLM 供应商服务，以及模型回答的事实正确性。
