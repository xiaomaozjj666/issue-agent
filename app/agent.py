import json
import logging
import re
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import Settings
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
from app.github import GitHubClient, parse_issue_url, select_candidate_paths
from app.i18n import (
    get_chat_system_prompt,
    get_final_output_prompt,
    get_review_unavailable_message,
    get_system_prompt,
    t,
)
from app.models import AnalysisReport, ChatResponse, IssueData, ReviewAudit
from app.provider import chat_request_options
from app.reviewer import ReviewerAgent
from app.sessions import Session
from app.tools import ToolExecutor, get_tool_definitions, parse_tool_call

logger = logging.getLogger(__name__)

class ModelResponseError(RuntimeError):
    pass


class IssueAgent:
    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.settings = settings
        self._client: AsyncOpenAI | None = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.close()
        self._client = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openai_api_key,
                base_url=self.settings.openai_base_url,
                timeout=self.settings.openai_timeout,
                max_retries=self.settings.openai_max_retries,
            )
            self._owns_client = True
        return self._client

    # ── streaming investigation ─────────────────────────────────────

    async def investigate_stream(
        self, issue_url: str, *, session: Session | None = None
    ) -> AsyncGenerator[AgentEvent, None]:
        """Investigate an issue, yielding AgentEvent objects for real-time streaming."""
        owner, repo, number = parse_issue_url(issue_url)
        logger.info("Investigating issue %s/%s#%d", owner, repo, number)
        if session is not None:
            session.metrics = {"model_calls": 0, "tool_calls": 0, "review_calls": 0, "files_read": 0}
        yield phase_event("fetching", "Fetching issue and repository tree")

        async with GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
        ) as github:
            issue = await github.get_issue(owner, repo, number)
            tree = await github.get_tree(issue)
            if session is not None:
                session.issue = issue
                session.tree = tree
            yield start_event(issue.title, len(tree))
            logger.info("Fetched issue: %r (%d comments, %d files)", issue.title, len(issue.comments), len(tree))

            executor = self._build_executor(github, issue, tree)
            messages = self._build_initial_messages(issue, tree)
            yield phase_event("exploring", "Investigating candidate files")

            tools = get_tool_definitions(self.settings)
            client = self._get_client()
            for iteration in range(self.settings.max_agent_iterations):
                if session is not None:
                    session.metrics["model_calls"] = int(session.metrics.get("model_calls", 0)) + 1
                response = await client.chat.completions.create(
                    **chat_request_options(self.settings),
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools,  # type: ignore[arg-type]
                    max_tokens=self.settings.max_output_tokens,
                )

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

                for tc in msg.tool_calls:
                    name, args = parse_tool_call(tc)
                    logger.info("Tool call %d: %s(%s)", iteration + 1, name, args)
                    yield tool_call_event(name, args, iteration + 1)
                    if session is not None:
                        session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1
                    result = await executor.execute(name, args)
                    if session is not None and executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    yield tool_result_event(name, result)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            else:
                logger.warning("Agent reached max iterations (%d)", self.settings.max_agent_iterations)

            yield phase_event("verifying", "Validating evidence and preparing the report")
            if session is not None:
                session.metrics["model_calls"] = int(session.metrics.get("model_calls", 0)) + 1
            report = await self._generate_report(messages, executor)
            if self.settings.independent_review:
                yield phase_event("reviewing", "Running independent evidence review")
                if session is not None:
                    session.metrics["model_calls"] = int(session.metrics.get("model_calls", 0)) + 1
                    session.metrics["review_calls"] = int(session.metrics.get("review_calls", 0)) + 1
                try:
                    outcome = await ReviewerAgent(self.settings, client).review(
                        issue=issue,
                        report=report,
                        file_cache=executor.file_cache,
                        files_read=executor.files_read,
                        line_counts=executor.line_counts,
                    )
                    report = outcome.report
                except Exception:
                    logger.exception("Independent review failed; preserving deterministically validated report")
                    message = get_review_unavailable_message(self.settings.language)
                    report.review_audit = ReviewAudit(status="unavailable", summary=message)
                    if message not in report.risks:
                        report.risks.append(message)
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

            yield report_event(report.model_dump())

            yield done_event()

    # ── non‑streaming wrappers (backward‑compatible) ─────────────────

    async def investigate(self, issue_url: str, *, session: Session | None = None) -> AnalysisReport:
        if session is not None:
            async with session.lock:
                return await self._investigate_from_stream(issue_url, session=session)
        return await self._investigate_from_stream(issue_url)

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

    async def _chat(self, session: Session, message: str) -> ChatResponse:
        if session.issue is None:
            raise ValueError("Session not initialized")

        tools = get_tool_definitions(self.settings)
        async with GitHubClient(
            self.settings.github_token,
            max_file_bytes=self.settings.github_max_file_bytes,
        ) as github:
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

            for _ in range(self.settings.max_agent_iterations):
                session.metrics["model_calls"] = int(session.metrics.get("model_calls", 0)) + 1
                response = await self._call_llm(messages, tools=tools, max_tokens=self.settings.max_chat_tokens)
                if not response.choices:
                    raise ModelResponseError("The model returned no choices")
                msg = response.choices[0].message
                serialized = _serialize_message(msg)
                messages.append(serialized)
                session.messages.append(serialized)

                if not msg.tool_calls:
                    session.file_cache = executor.file_cache
                    session.files_read = executor.files_read
                    history_budget = self.settings.max_total_context_chars - sum(
                        len(content) for content in session.file_cache.values()
                    )
                    _trim_session_messages(session.messages, max(history_budget, 0))
                    return ChatResponse(
                        session_id=session.session_id,
                        reply=msg.content or "",
                        tools_used=executor.tools_used,
                    )

                for tc in msg.tool_calls:
                    name, args = parse_tool_call(tc)
                    result = await executor.execute(name, args)
                    session.metrics["tool_calls"] = int(session.metrics.get("tool_calls", 0)) + 1
                    if executor.pr_proposal is not None:
                        session.pending_pr = executor.pr_proposal
                    tool_msg = {"role": "tool", "tool_call_id": tc.id, "content": result}
                    messages.append(tool_msg)
                    session.messages.append(tool_msg)

        session.file_cache = executor.file_cache
        session.files_read = executor.files_read
        history_budget = self.settings.max_total_context_chars - sum(
            len(content) for content in session.file_cache.values()
        )
        _trim_session_messages(session.messages, max(history_budget, 0))
        return ChatResponse(
            session_id=session.session_id,
            reply=t("depth_limit"),
            tools_used=executor.tools_used,
        )

    # ── report generation ────────────────────────────────────────────

    async def _generate_report(self, messages: list[dict], executor: ToolExecutor) -> AnalysisReport:
        client = self._get_client()
        final_messages = self._build_report_messages(messages, executor)

        response = await client.chat.completions.create(  # type: ignore[call-overload]
            **chat_request_options(self.settings),
            messages=final_messages,
            response_format={"type": "json_object"},
            max_tokens=self.settings.max_output_tokens,
        )

        if not response.choices:
            raise ModelResponseError("The model returned no choices")

        content = response.choices[0].message.content
        if not content:
            raise ModelResponseError("The model returned an empty response")

        json_text = _extract_json(content)
        try:
            report = AnalysisReport.model_validate_json(json_text)
        except ValidationError as error:
            logger.warning("Report validation failed: %s", error)
            raise ModelResponseError("The model returned an invalid analysis report") from error

        report = EvidenceValidator().validate(
            report,
            files_read=executor.files_read,
            line_counts=executor.line_counts,
        )

        logger.info(
            "Analysis complete: confidence=%s, evidence=%d, files=%d",
            report.confidence,
            report.evidence_audit.valid_references,
            len(report.files_examined),
        )
        return report

    # ── helpers ──────────────────────────────────────────────────────

    def _build_initial_messages(self, issue: IssueData, tree: list[str]) -> list[dict]:
        candidate_paths = select_candidate_paths(tree, issue, self.settings.max_planning_paths)
        tree_preview = candidate_paths if candidate_paths else tree[: self.settings.max_planning_paths]
        issue_context = json.dumps(
            {"title": issue.title, "body": issue.body[:5000], "labels": issue.labels, "comments": issue.comments[:10]},
            ensure_ascii=False,
            indent=2,
        )
        return [
            {"role": "system", "content": get_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Investigate this GitHub issue:\n{issue_context}\n\n"
                    f"Repository file tree ({len(tree)} files total, showing {len(tree_preview)} most relevant):\n"
                    + "\n".join(tree_preview)
                ),
            },
        ]

    def _build_report_messages(self, messages: list[dict], executor: ToolExecutor) -> list[dict]:
        # 用有界调查事实和已读源码替代重复的原始工具 transcript，控制上下文同时保留关键结论。
        system_prompt = ""
        issue_context = ""
        for message in messages:
            role = message.get("role")
            if role == "system" and not system_prompt:
                system_prompt = str(message.get("content", ""))
            elif role == "user" and not issue_context:
                issue_context = str(message.get("content", ""))
            if system_prompt and issue_context:
                break

        final_output_prompt = get_final_output_prompt()
        # 预留输出预算和分隔符开销，剩余空间用于附加已读文件内容
        budget = max(
            self.settings.max_total_context_chars
            - len(system_prompt)
            - len(issue_context)
            - len(final_output_prompt)
            - 256,
            0,
        )

        user_parts: list[str] = []
        if issue_context:
            user_parts.append(issue_context)

        ledger_text = "\n".join(executor.investigation_ledger)
        if ledger_text and budget > 0:
            ledger_budget = min(self.settings.max_investigation_ledger_chars, budget // 4)
            if ledger_budget > 0:
                limited_ledger = ledger_text[:ledger_budget]
                user_parts.append("")
                user_parts.append("Investigation ledger (bounded tool findings):")
                user_parts.append(limited_ledger)
                budget -= len(limited_ledger)

        if executor.files_read:
            user_parts.append("")
            user_parts.append("Source files examined (with line numbers):")
            for path in executor.files_read:
                content = executor.file_cache.get(path)
                if not content or budget <= 0:
                    continue
                numbered = "\n".join(
                    f"L{number}: {line}" for number, line in enumerate(content.splitlines(), 1)
                )
                block = f"\n--- {path} ---\n{numbered}\n"
                if len(block) > budget:
                    block = block[:budget]
                user_parts.append(block)
                budget -= len(block)
                if budget <= 0:
                    break

        user_parts.append("")
        user_parts.append(final_output_prompt)

        final_messages: list[dict] = []
        if system_prompt:
            final_messages.append({"role": "system", "content": system_prompt})
        final_messages.append({"role": "user", "content": "\n".join(user_parts)})
        return final_messages

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


def _serialize_message(message) -> dict:
    msg: dict = {"role": "assistant", "content": message.content or ""}
    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    if message.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ]
    return msg


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
            turn = [
                message
                for message in turn
                if message.get("role") == "user"
                or (message.get("role") == "assistant" and not message.get("tool_calls"))
            ][-max_messages:]
            turn_size = sum(message_size(message) for message in turn)
        if not turn:
            break
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


_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


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


def _extract_json(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    block_match = _JSON_BLOCK.search(content)
    if block_match:
        return block_match.group(1).strip()
    brace_start = stripped.find("{")
    brace_end = stripped.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        return stripped[brace_start : brace_end + 1]
    return stripped
