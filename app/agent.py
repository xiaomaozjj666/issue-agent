"""Core investigation agent: orchestrates LLM tool-calling loop and report generation.

Architecture:
- ``IssueAgent`` owns the OpenAI client lifecycle and delegates report
  generation to ``ReportGenerator`` and evidence review to ``ReviewerAgent``.
- The streaming interface (``investigate_stream``) yields ``AgentEvent`` objects
  consumed by the SSE endpoint; the non-streaming ``investigate`` is a thin
  wrapper for CLI and backward compatibility.
- File pre-loading of issue-referenced paths runs concurrently via
  ``asyncio.gather`` to reduce wall-clock latency.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from time import monotonic

import httpx
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage

from app.circuit_breaker import CircuitBreaker
from app.config import Settings
from app.errors import ModelResponseError
from app.events import (
    AgentEvent,
    done_event,
    phase_event,
    report_event,
    review_event,
    start_event,
    thinking_event,
    tool_call_event,
    tool_result_event,
)
from app.evidence import EvidenceValidator
from app.github import GitHubClient, extract_referenced_paths, parse_issue_url, select_candidate_paths
from app.i18n import (
    get_chat_system_prompt,
    get_review_unavailable_message,
    get_system_prompt,
    t,
)
from app.models import AnalysisReport, ChatResponse, IssueData, ReviewAudit
from app.provider import (
    chat_request_options,
    create_openai_client,
    iter_deltas,
    record_model_request,
    record_model_usage,
)
from app.report_generator import ReportGenerator
from app.reviewer import ReviewerAgent
from app.sessions import Session
from app.tools import ToolExecutor, get_tool_definitions, parse_tool_call

logger = logging.getLogger(__name__)

__all__ = ["IssueAgent", "ModelResponseError"]


class IssueAgent:
    """GitHub issue investigation agent.

    Manages the OpenAI client lifecycle and coordinates the multi-phase
    investigation: fetch → preload → explore → verify → report → review.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI | None = None,
        github_client: GitHubClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = client
        self._owns_client = client is None
        self._github_client = github_client
        self._circuit_breaker = circuit_breaker
        # 报告生成委托给独立的 ReportGenerator，避免 IssueAgent 单类过大。
        # client 延迟创建（_get_client），所以这里先用 None，首次生成报告时再绑定。
        self._report_generator: ReportGenerator | None = None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None
        self._report_generator = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = create_openai_client(self.settings)
            self._owns_client = True
        return self._client

    def _get_report_generator(self) -> ReportGenerator:
        """延迟创建 ReportGenerator，确保 client 已就绪。"""
        if self._report_generator is None:
            self._report_generator = ReportGenerator(
                self.settings, self._get_client(), circuit_breaker=self._circuit_breaker
            )
        return self._report_generator

    # ── streaming investigation ─────────────────────────────────────

    async def investigate_stream(
        self, issue_url: str, *, session: Session | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        """Investigate an issue, yielding AgentEvent objects for real-time streaming.

        Phases: fetching → preloading → exploring → verifying → reviewing → done.
        """
        owner, repo, number = parse_issue_url(issue_url)
        investigation_start = monotonic()
        logger.info("Investigating issue %s/%s#%d", owner, repo, number)
        if session is not None:
            session.metrics = {
                "model_calls": 0,
                "model_retries": 0,
                "tool_calls": 0,
                "duplicate_tool_calls": 0,
                "review_calls": 0,
                "files_read": 0,
            }
        yield phase_event("fetching", "Fetching issue and repository tree")

        github_context = self._github_client or GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
            timeout=self.settings.github_timeout,
            max_retries=self.settings.github_max_retries,
            cache_ttl=self.settings.github_cache_ttl_seconds,
            cache_max_entries=self.settings.github_cache_max_entries,
            cache_max_bytes=self.settings.github_cache_max_bytes,
            max_tree_entries=self.settings.github_max_tree_entries,
        )
        async with github_context as github:
            github_hits_before = int(getattr(github, "cache_hits", 0))
            github_misses_before = int(getattr(github, "cache_misses", 0))
            fetch_started = monotonic()
            issue = await github.get_issue(owner, repo, number)
            tree = await github.get_tree(issue)
            if session is not None:
                session.issue = issue
                session.tree = tree
                session.metrics["fetch_ms"] = round((monotonic() - fetch_started) * 1000)
            yield start_event(issue.title, len(tree))
            logger.info("Fetched issue: %r (%d comments, %d files)", issue.title, len(issue.comments), len(tree))

            executor = self._build_executor(github, issue, tree)
            referenced_paths = extract_referenced_paths(" ".join([issue.title, issue.body, *issue.comments]), tree)
            messages = self._build_initial_messages(issue, tree, referenced_paths)

            # 预读 issue 文本中明确引用的文件路径，确保关键源码进入 file_cache。
            # 并行读取减少网络等待；受 max_files 限制避免过度消耗上下文预算。
            if referenced_paths:
                yield phase_event(
                    "preloading",
                    f"Pre-loading {len(referenced_paths)} issue-referenced file(s)",
                )
                preload_paths = referenced_paths[: self.settings.max_candidate_files]

                async def _preload_one(path: str) -> tuple[str, str | None]:
                    try:
                        result = await executor.execute("read_file", {"path": path})
                        return path, result
                    except Exception as error:
                        logger.warning("Pre-read of issue-referenced path %s failed: %s", path, error)
                        return path, None

                preload_results = await asyncio.gather(*[_preload_one(p) for p in preload_paths])
                for path, result in preload_results:
                    if result is not None:
                        yield tool_call_event("read_file", {"path": path, "auto": "issue-referenced"}, 0)
                        yield tool_result_event("read_file", result)
                        if session is not None:
                            session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1

            yield phase_event("exploring", "Investigating candidate files")

            tools = get_tool_definitions(self.settings)
            client = self._get_client()
            duplicate_tool_rounds = 0
            exploration_started = monotonic()
            for iteration in range(self.settings.max_agent_iterations):
                # L3：调查总时长上限检测，在每轮迭代前检查，超时即抛出
                # 避免模型反复调用工具陷入死循环，耗尽 API 额度
                if monotonic() - investigation_start > self.settings.investigation_timeout:
                    raise TimeoutError(
                        f"Investigation exceeded {self.settings.investigation_timeout:.0f}s total timeout"
                    )
                record_model_request(session.metrics if session is not None else None, "exploration")
                if self._circuit_breaker is not None:
                    response = await self._circuit_breaker.call(
                        client.chat.completions.create,
                        **chat_request_options(self.settings),
                        messages=messages,  # type: ignore[arg-type]
                        tools=tools,  # type: ignore[arg-type]
                        max_tokens=self.settings.max_exploration_tokens,
                    )
                else:
                    response = await client.chat.completions.create(
                        **chat_request_options(self.settings),
                        messages=messages,  # type: ignore[arg-type]
                        tools=tools,  # type: ignore[arg-type]
                        max_tokens=self.settings.max_exploration_tokens,
                    )
                record_model_usage(session.metrics if session is not None else None, response, "exploration")

                if not response.choices:
                    raise ModelResponseError("The model returned no choices")

                msg = response.choices[0].message
                content = msg.content or ""
                messages.append(_serialize_message(msg))

                if content and not msg.tool_calls:
                    yield thinking_event(content)

                if not msg.tool_calls:
                    logger.info("Agent finished exploration after %d iterations", iteration + 1)
                    break

                parsed_calls = [(tc, *parse_tool_call(tc)) for tc in msg.tool_calls]
                for _tc, name, args in parsed_calls:
                    logger.info("Tool call %d: %s(%s)", iteration + 1, name, args)
                    yield tool_call_event(name, args, iteration + 1)
                    if session is not None:
                        session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1

                tool_started = monotonic()
                results, duplicate_count = await executor.execute_many(
                    [(name, args) for _tc, name, args in parsed_calls],
                    timeout=self.settings.tool_timeout,
                )
                if session is not None:
                    session.metrics["tool_ms"] = int(session.metrics.get("tool_ms", 0)) + round(
                        (monotonic() - tool_started) * 1000
                    )
                if session is not None and duplicate_count:
                    session.metrics["duplicate_tool_calls"] = int(
                        session.metrics.get("duplicate_tool_calls", 0)
                    ) + duplicate_count

                for (tc, name, _args), result in zip(parsed_calls, results, strict=True):
                    if session is not None:
                        # 同步 files_read 快照，让前端实时显示已读文件数（而非等到报告阶段）
                        session.metrics["files_read"] = len(executor.files_read)
                        if executor.pr_proposal is not None:
                            session.pending_pr = executor.pr_proposal
                    yield tool_result_event(name, result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

                if duplicate_count == len(parsed_calls):
                    duplicate_tool_rounds += 1
                    if duplicate_tool_rounds >= self.settings.max_duplicate_tool_rounds:
                        logger.info(
                            "Stopping exploration after %d exact-repeat tool rounds",
                            duplicate_tool_rounds,
                        )
                        break
                else:
                    duplicate_tool_rounds = 0
            else:
                logger.warning("Agent reached max iterations (%d)", self.settings.max_agent_iterations)

            if session is not None:
                session.metrics["exploration_ms"] = round((monotonic() - exploration_started) * 1000)
            yield phase_event("verifying", "Validating evidence and preparing the report")
            # 流式生成报告：实时推送 reasoning 思考过程到前端，避免长时间黑盒等待
            report: AnalysisReport | None = None
            report_started = monotonic()
            async for item in self._get_report_generator().generate_stream(
                messages,
                executor,
                session.metrics if session is not None else None,
            ):
                if isinstance(item, AgentEvent):
                    yield item
                else:
                    report = item
            if report is None:
                raise ModelResponseError("Report generation did not produce a result")
            if session is not None:
                session.metrics["report_ms"] = round((monotonic() - report_started) * 1000)
            if self.settings.independent_review:
                yield phase_event("reviewing", "Running independent evidence review")
                if session is not None:
                    session.metrics["review_calls"] = int(session.metrics.get("review_calls", 0)) + 1
                review_started = monotonic()
                try:
                    outcome = await ReviewerAgent(self.settings, client, circuit_breaker=self._circuit_breaker).review(
                        issue=issue,
                        report=report,
                        file_cache=executor.file_cache,
                        files_read=executor.files_read,
                        line_counts=executor.line_counts,
                        metrics=session.metrics if session is not None else None,
                    )
                    report = outcome.report
                except Exception:
                    logger.exception("Independent review failed; preserving deterministically validated report")
                    message = get_review_unavailable_message(self.settings.language)
                    report.review_audit = ReviewAudit(status="unavailable", summary=message)
                    if message not in report.risks:
                        report.risks.append(message)
                if session is not None:
                    session.metrics["review_ms"] = round((monotonic() - review_started) * 1000)
                yield review_event(
                    report.review_audit.status,
                    report.review_audit.summary,
                    report.review_audit.findings,
                )
            if session is not None:
                session.file_cache = executor.file_cache
                session.files_read = executor.files_read
                session.report = report
                session.metrics["files_read"] = len(executor.files_read)
                session.metrics["github_cache_hits"] = int(getattr(github, "cache_hits", 0)) - github_hits_before
                session.metrics["github_cache_misses"] = (
                    int(getattr(github, "cache_misses", 0)) - github_misses_before
                )

            yield report_event(report.model_dump())

            # 结构化完成日志：便于运维监控和性能分析
            elapsed_ms = round((monotonic() - investigation_start) * 1000)
            logger.info(
                "Investigation complete: issue=%s/%s#%d elapsed_ms=%d "
                "model_calls=%s tool_calls=%s files_read=%s confidence=%s",
                owner,
                repo,
                number,
                elapsed_ms,
                session.metrics.get("model_calls", 0) if session else "n/a",
                session.metrics.get("tool_calls", 0) if session else "n/a",
                session.metrics.get("files_read", 0) if session else "n/a",
                report.confidence,
            )

            yield done_event()

    # ── non‑streaming wrappers (backward‑compatible) ─────────────────

    async def investigate(self, issue_url: str, *, session: Session | None = None) -> AnalysisReport:
        # 不全程持锁：与 /stream 端点一致，investigate_stream 内部修改 session 状态
        # 时不持有 session.lock。全程持锁会阻塞 PATCH/DELETE 等操作长达数分钟。
        return await self._investigate_from_stream(issue_url, session=session)

    async def _investigate_from_stream(self, issue_url: str, *, session: Session | None = None) -> AnalysisReport:
        report: AnalysisReport | None = None
        async for event in self.investigate_stream(issue_url, session=session):
            if event.type == "report" and event.data:
                report = AnalysisReport(**event.data)
        if report is None:
            raise ModelResponseError("Investigation did not produce a report")
        return report

    # ── chat ─────────────────────────────────────────────────────────

    async def chat(self, session: Session, message: str) -> ChatResponse:
        async with session.lock:
            return await self._chat(session, message)

    async def chat_stream(self, session: Session, message: str) -> AsyncGenerator[dict, None]:
        """Stream chat reply token-by-token via SSE-friendly events.

        Yields dicts with shapes:
            {"type": "delta", "content": str}        # incremental content chunk
            {"type": "tool_call", "name": str}       # tool invocation notification
            {"type": "done", "reply": str, "tools_used": list[str]}
            {"type": "error", "message": str}

        Lock is NOT held across the LLM stream — only across state mutations
        (message append / metrics update) inside ``_chat_stream``. This prevents
        a slow SSE consumer from blocking concurrent operations on the same
        session (e.g. cancellation requests). Errors are surfaced as ``error``
        events so the SSE consumer can render them inline without aborting.
        """
        try:
            async for event in self._chat_stream(session, message):
                yield event
        except Exception as exc:  # noqa: BLE001 — surfaced to client via SSE
            logger.exception("chat_stream failed for session %s", session.session_id)
            yield {"type": "error", "message": _friendly_chat_error(exc)}

    async def _chat_prepare(self, session: Session, message: str) -> tuple[GitHubClient, ToolExecutor, list[dict]]:
        """Chat 共享初始化：校验 session、构建 executor 与 messages。

        返回 (github, executor, messages)。调用方负责 ``async with github`` 管理
        client 生命周期，消除 _chat / _chat_stream 的重复初始化逻辑。
        """
        if session.issue is None:
            raise ValueError("Session not initialized")

        github = self._github_client or GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
            timeout=self.settings.github_timeout,
            max_retries=self.settings.github_max_retries,
            cache_ttl=self.settings.github_cache_ttl_seconds,
            cache_max_entries=self.settings.github_cache_max_entries,
            cache_max_bytes=self.settings.github_cache_max_bytes,
            max_tree_entries=self.settings.github_max_tree_entries,
        )
        try:
            executor = self._build_executor(
                github,
                session.issue,
                session.tree,
                file_cache=session.file_cache,
                files_read=session.files_read,
            )
            investigation_context = _build_investigation_context(session)
            session.messages.append({"role": "user", "content": message})
            messages = [
                {"role": "system", "content": get_chat_system_prompt()},
                {"role": "system", "content": investigation_context},
                *session.messages,
            ]
        except Exception:
            # _chat_prepare 抛异常时，新建的 GitHubClient 未进入 async with，
            # 必须手动关闭避免 httpx 连接池泄漏
            if self._github_client is None:
                await github.aclose()
            raise
        return github, executor, messages

    def _chat_finalize(self, executor: ToolExecutor, session: Session) -> None:
        """Chat 共享收尾：回写 file_cache/files_read 并裁剪历史消息。"""
        session.file_cache = executor.file_cache
        session.files_read = executor.files_read
        history_budget = self.settings.max_total_context_chars - sum(
            len(content) for content in session.file_cache.values()
        )
        _trim_session_messages(session.messages, max(history_budget, 0))

    async def _chat_stream(self, session: Session, message: str) -> AsyncGenerator[dict, None]:
        tools = get_tool_definitions(self.settings)
        github, executor, messages = await self._chat_prepare(session, message)
        async with github:
          try:
            for _ in range(self.settings.max_agent_iterations):
                record_model_request(session.metrics, "chat")
                collected_content_parts: list[str] = []
                # 工具调用 chunk 按 index 累积：{index: {"id": ..., "name": ..., "arguments": "..."}}
                tool_call_buffers: dict[int, dict[str, str]] = {}

                stream = await self._call_llm_stream(
                    messages,
                    tools=tools,
                    max_tokens=self.settings.max_chat_tokens,
                )
                usage_response = None

                def capture_usage(chunk) -> None:
                    nonlocal usage_response
                    if getattr(chunk, "usage", None) is not None:
                        usage_response = chunk

                async for delta in iter_deltas(stream, on_chunk=capture_usage):
                    # 只透传最终回复内容，不展示 reasoning_content（思考过程）
                    if delta.content:
                        collected_content_parts.append(delta.content)
                        yield {"type": "delta", "content": delta.content}
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index if tc.index is not None else 0
                            entry = tool_call_buffers.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                entry["id"] = tc.id
                            fn = getattr(tc, "function", None)
                            if fn is not None and fn.name:
                                entry["name"] = fn.name
                            if fn is not None and fn.arguments:
                                entry["arguments"] += fn.arguments
                if usage_response is not None:
                    record_model_usage(session.metrics, usage_response, "chat")

                collected_content = "".join(collected_content_parts)
                # LLM 返回空 stream（零 chunk 或全部空 content）且无 tool 调用：
                # 回滚已追加的 user 消息（避免 dangling user 无 assistant 回复污染下次上下文），
                # 保留 file_cache 等已读取数据，直接报错让用户重试
                if not collected_content and not tool_call_buffers:
                    if session.messages and session.messages[-1].get("role") == "user":
                        session.messages.pop()
                    yield {
                        "type": "error",
                        "message": "Model returned an empty response. Please try rephrasing your question.",
                    }
                    self._chat_finalize(executor, session)
                    return
                serialized: dict = {"role": "assistant", "content": collected_content}
                ordered_tool_calls = [tool_call_buffers[i] for i in sorted(tool_call_buffers)]
                if ordered_tool_calls:
                    # 某些代理/网关返回空 tool_call id，生成合成 id 以保留工具调用意图
                    for idx, entry in enumerate(ordered_tool_calls):
                        if not entry["id"]:
                            entry["id"] = f"call_{idx}"
                    serialized["tool_calls"] = [
                        {
                            "id": entry["id"],
                            "type": "function",
                            "function": {"name": entry["name"], "arguments": entry["arguments"]},
                        }
                        for entry in ordered_tool_calls
                    ]
                messages.append(serialized)
                session.messages.append(serialized)

                valid_tool_calls = serialized.get("tool_calls", [])
                if not valid_tool_calls:
                    # 没有工具调用：回复完成
                    self._chat_finalize(executor, session)
                    yield {
                        "type": "done",
                        "reply": collected_content,
                        "tools_used": executor.tools_used,
                    }
                    return

                # 有工具调用：先发出调用事件，再批量执行独立只读工具。
                parsed_calls: list[tuple[dict[str, str], str, dict]] = []
                for entry in ordered_tool_calls:
                    name = entry["name"]
                    try:
                        args = json.loads(entry["arguments"]) if entry["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "tool_call", "name": name, "args": args}
                    parsed_calls.append((entry, name, args))

                results, duplicate_count = await executor.execute_many(
                    [(name, args) for _entry, name, args in parsed_calls],
                    timeout=self.settings.tool_timeout,
                )
                if duplicate_count:
                    session.metrics["duplicate_tool_calls"] = int(
                        session.metrics.get("duplicate_tool_calls", 0)
                    ) + duplicate_count

                for (entry, name, _args), result in zip(parsed_calls, results, strict=True):
                    session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1
                    session.metrics["files_read"] = len(executor.files_read)
                    if executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    # 工具结果事件：让前端展示调用参数与结果摘要
                    yield {"type": "tool_result", "name": name, "preview": result[:1200]}
                    tool_msg = {"role": "tool", "tool_call_id": entry["id"], "content": result}
                    messages.append(tool_msg)
                    session.messages.append(tool_msg)

            # 达到迭代上限：追加一条 assistant 消息收尾，
            # 避免上下文以未完成的 tool_call 结尾导致下次 API 报错
            depth_reply = t("depth_limit")
            session.messages.append({"role": "assistant", "content": depth_reply})
            self._chat_finalize(executor, session)
            yield {
                "type": "done",
                "reply": depth_reply,
                "tools_used": executor.tools_used,
            }
          except Exception:
            # 异常路径也必须 finalize：回写 file_cache/files_read，保留 chat 期间已读文件
            self._chat_finalize(executor, session)
            raise

    async def _chat(self, session: Session, message: str) -> ChatResponse:
        tools = get_tool_definitions(self.settings)
        github, executor, messages = await self._chat_prepare(session, message)
        async with github:
          try:
            for _ in range(self.settings.max_agent_iterations):
                record_model_request(session.metrics, "chat")
                response = await self._call_llm(messages, tools=tools, max_tokens=self.settings.max_chat_tokens)
                record_model_usage(session.metrics, response, "chat")
                if not response.choices:
                    raise ModelResponseError("The model returned no choices")
                msg = response.choices[0].message
                serialized = _serialize_message(msg)
                messages.append(serialized)
                session.messages.append(serialized)

                if not msg.tool_calls:
                    self._chat_finalize(executor, session)
                    return ChatResponse(
                        session_id=session.session_id,
                        reply=msg.content or "",
                        tools_used=executor.tools_used,
                    )

                parsed_calls = [(tc, *parse_tool_call(tc)) for tc in msg.tool_calls]
                results, duplicate_count = await executor.execute_many(
                    [(name, args) for _tc, name, args in parsed_calls],
                    timeout=self.settings.tool_timeout,
                )
                if duplicate_count:
                    session.metrics["duplicate_tool_calls"] = int(
                        session.metrics.get("duplicate_tool_calls", 0)
                    ) + duplicate_count
                for (tc, _name, _args), result in zip(parsed_calls, results, strict=True):
                    session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1
                    session.metrics["files_read"] = len(executor.files_read)
                    if executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result}
                    messages.append(tool_msg)
                    session.messages.append(tool_msg)
          except Exception:
            self._chat_finalize(executor, session)
            raise

        depth_reply = t("depth_limit")
        session.messages.append({"role": "assistant", "content": depth_reply})
        self._chat_finalize(executor, session)
        return ChatResponse(
            session_id=session.session_id,
            reply=depth_reply,
            tools_used=executor.tools_used,
        )

    # ── report generation (thin wrappers for backward compatibility) ──
    # 实际逻辑已抽到 app.report_generator.ReportGenerator；
    # 这里保留方法名以兼容现有测试和调用方（CLI、tests/test_agent.py）。

    async def _generate_report(
        self, messages: list[dict], executor: ToolExecutor, metrics: dict | None = None
    ) -> AnalysisReport:
        return await self._get_report_generator().generate(messages, executor, metrics)

    def _build_report_messages(self, messages: list[dict], executor: ToolExecutor) -> list[dict]:
        return self._get_report_generator().build_report_messages(messages, executor)

    # ── helpers ──────────────────────────────────────────────────────

    def _build_initial_messages(
        self,
        issue: IssueData,
        tree: list[str],
        referenced_paths: list[str] | None = None,
    ) -> list[dict]:
        candidate_paths = select_candidate_paths(tree, issue, self.settings.max_planning_paths)
        # issue 文本中明确引用的路径优先列入候选列表前部，便于 LLM 第一时间定位
        if referenced_paths:
            seen = set(referenced_paths)
            remaining = [p for p in candidate_paths if p not in seen]
            candidate_paths = list(referenced_paths) + remaining
            candidate_paths = candidate_paths[: self.settings.max_planning_paths]
        tree_preview = candidate_paths if candidate_paths else tree[: self.settings.max_planning_paths]
        issue_context = json.dumps(
            {"title": issue.title, "body": issue.body[:5000], "labels": issue.labels, "comments": issue.comments[:10]},
            ensure_ascii=False,
            indent=2,
        )
        referenced_hint = ""
        if referenced_paths:
            referenced_hint = (
                "\n\n=== Issue 显式引用的文件路径（必须 read_file 验证后再下结论）===\n"
                + "\n".join(referenced_paths)
                + "\n这些路径已被 issue 文本直接提及。任何关于它们的根因断言必须基于实际读取的代码内容，"
                "禁止基于文件名或路径推测。若路径不存在或读取失败，应在报告中明确标注。"
            )
        return [
            {"role": "system", "content": get_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Investigate this GitHub issue:\n{issue_context}\n\n"
                    f"Repository file tree ({len(tree)} files total, showing {len(tree_preview)} most relevant):\n"
                    + "\n".join(tree_preview)
                    + referenced_hint
                ),
            },
        ]

    @staticmethod
    def _has_valid_lines(lines: str | None, line_count: int) -> bool:
        return EvidenceValidator.has_valid_lines(lines, line_count)

    async def _call_llm(self, messages: list[dict], *, tools: list | None = None, max_tokens: int | None = None):
        client = self._get_client()
        kwargs: dict = {
            **chat_request_options(self.settings),
            "messages": messages,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._circuit_breaker is not None:
            return await self._circuit_breaker.call(client.chat.completions.create, **kwargs)
        return await client.chat.completions.create(**kwargs)

    async def _call_llm_stream(
        self,
        messages: list[dict],
        *,
        tools: list | None = None,
        max_tokens: int | None = None,
    ):
        """Stream chat completion chunks from the model.

        Returns an async iterable of ``ChatCompletionChunk``. The caller is
        responsible for accumulating ``delta.content`` and ``delta.tool_calls``
        fragments; reasoning_content is intentionally ignored (not surfaced to
        the end user).
        """
        client = self._get_client()
        kwargs: dict = {
            **chat_request_options(self.settings),
            "messages": messages,
            "max_tokens": max_tokens or self.settings.max_output_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self._circuit_breaker is not None:
            return await self._circuit_breaker.call(client.chat.completions.create, **kwargs)
        return await client.chat.completions.create(**kwargs)

    def _build_executor(self, github: GitHubClient, issue: IssueData, tree: list[str], **kwargs) -> ToolExecutor:
        return ToolExecutor(
            github,
            self.settings,
            issue,
            tree,
            max_files=self.settings.max_candidate_files,
            max_file_chars=self.settings.max_file_chars,
            max_total_context_chars=self.settings.max_total_context_chars,
            **kwargs,
        )


# ── module‑level helpers ────────────────────────────────────────────


def _serialize_message(message: ChatCompletionMessage) -> dict:
    """Serialize an OpenAI ChatCompletionMessage into a plain dict for context storage."""
    msg: dict = {"role": "assistant", "content": message.content or ""}
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},  # type: ignore[union-attr]
            }
            for tc in message.tool_calls
        ]
    return msg


def friendly_chat_error(exc: Exception) -> str:
    """Map SDK/network exceptions to user-friendly messages for SSE error events.

    Public alias so /stream and /chat/stream endpoints share the same mapping.
    """
    return _friendly_chat_error(exc)


def _friendly_chat_error(exc: Exception) -> str:
    """Map SDK/network exceptions to user-friendly messages for SSE error events."""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "API key is invalid. Please check the configuration."
    if status == 429:
        return "Model rate limit reached. Please wait a moment and retry."
    if status is not None and 500 <= status < 600:
        return f"Model service error (HTTP {status}). Please retry shortly."
    # 优先用异常类型匹配，避免依赖类名字符串（SDK 版本升级后类名可能变化）
    if isinstance(exc, asyncio.TimeoutError):
        return "Model response timed out. Please try again."
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "Unable to connect to the model service. Check your network."
    # openai SDK 的 APIConnectionError 是 httpx 网络异常的子类，上面已覆盖；
    # 但若 openai 抛出非 httpx 的连接异常，用类名兜底匹配
    name = type(exc).__name__
    if "Timeout" in name:
        return "Model response timed out. Please try again."
    if "Connection" in name or "APIConnection" in name:
        return "Unable to connect to the model service. Check your network."
    # 应用级异常（ValueError/KeyError 等内置异常）消息可读且无敏感信息，透传
    # 其他未知异常只返回类型名，避免泄漏 SDK 内部文本（URL/key 片段）
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError, RuntimeError)):
        return str(exc)[:500]
    return f"Internal error: {type(exc).__name__}"


def _trim_session_messages(messages: list[dict], max_chars: int, max_messages: int = 50) -> None:
    def message_size(message: dict) -> int:
        size = len(str(message.get("content", "")))
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            size += len(str(function.get("arguments", "")))
        return size

    turns: list[list[dict]] = []
    for message in messages:
        if message.get("role") == "user" or not turns:
            turns.append([])
        turns[-1].append(message)

    retained: list[list[dict]] = []
    used = 0
    message_count = 0
    for turn in reversed(turns):
        turn_size = sum(message_size(message) for message in turn)
        if used + turn_size > max_chars or message_count + len(turn) > max_messages:
            if retained:
                break
            # 首个保留的 turn 超预算：剔除 tool_calls/tool 结果，只保留对话内容
            filtered = [
                message
                for message in turn
                if message.get("role") == "user"
                or (message.get("role") == "assistant" and not message.get("tool_calls"))
            ][-max_messages:]
            if not filtered:
                # 过滤后为空（整个 turn 全是 tool 调用）：跳过此 turn，继续看更早的 turn，
                # 避免因最近一轮全是工具调用就丢失全部历史上下文
                continue
            turn = filtered
            turn_size = sum(message_size(message) for message in turn)
        retained.append(turn)
        used += turn_size
        message_count += len(turn)
        if used >= max_chars:
            break

    selected = [message for turn in reversed(retained) for message in turn]
    total = sum(message_size(message) for message in selected)
    for message in selected:
        overflow = total - max_chars
        if overflow <= 0:
            break
        content = str(message.get("content", ""))
        removed = min(len(content), overflow)
        message["content"] = content[removed:]
        total -= removed
    messages[:] = selected


def _build_investigation_context(session: Session) -> str:
    if session.report is None:
        return t("no_investigation")
    report = session.report
    parts = [t("investigation_context_header"), ""]
    parts.append(f"Summary: {report.summary}")
    parts.append(f"Root Cause: {report.root_cause}")
    parts.append(f"Confidence: {report.confidence}")
    if report.evidence:
        parts.append("Evidence:")
        for ev in report.evidence:
            parts.append(f"  - {ev.path} {ev.lines or ''}: {ev.reason or ''}")
    if report.proposed_changes:
        parts.append("Proposed Changes:")
        for i, change in enumerate(report.proposed_changes, 1):
            parts.append(f"  {i}. {change}")
    if report.files_examined:
        parts.append(f"Files Examined: {', '.join(report.files_examined)}")
    parts.append("")
    parts.append(t("investigation_context_footer"))
    return "\n".join(parts)
