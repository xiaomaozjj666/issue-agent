"""报告生成器：从 IssueAgent 抽离的报告生成业务逻辑。

包含：
- _build_report_messages: 构造最终报告生成的消息上下文（系统提示 + issue + 调查账本 + 已读源码 + 输出指令）
- _generate_report_stream: 流式生成报告，实时 yield reasoning_event
- _generate_report: 非流式兼容包装器（供 CLI 和测试使用）
- _append_report_retry_feedback: 重试时附加错误反馈

参考业界实践（Claude Code、DeepSeek TUI、OpenAI o3 reasoning_effort）：
- 流式输出 reasoning_content，避免长时间黑盒等待
- 多级重试：第 1 次 thinking enabled + reasoning_effort high，
  中间次 thinking enabled + 错误反馈，最后一次 thinking disabled 保底
- 业务层自控重试，SDK max_retries=0 避免叠加
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.config import Settings
from app.errors import ModelResponseError
from app.events import AgentEvent, reasoning_event
from app.evidence import EvidenceValidator
from app.i18n import get_final_output_prompt, get_report_phase_instruction, get_report_retry_prompt
from app.json_utils import extract_json
from app.models import AnalysisReport
from app.retry import build_attempt_plan
from app.tools import ToolExecutor

logger = logging.getLogger(__name__)


class ReportGenerator:
    """封装报告生成的所有逻辑，从 IssueAgent 解耦。

    IssueAgent 持有一个 ReportGenerator 实例，委托报告生成调用；
    旧的 _generate_report / _build_report_messages 等方法保留为 IssueAgent 上的薄包装，
    供现有测试和调用方继续使用，无需改动。
    """

    def __init__(self, settings: Settings, client: AsyncOpenAI) -> None:
        self._settings = settings
        self._client = client

    async def generate_stream(
        self,
        messages: list[dict],
        executor: ToolExecutor,
    ) -> AsyncGenerator[AgentEvent | AnalysisReport, None]:
        """流式生成报告，实时 yield reasoning_event 让用户看到思考过程。"""
        base_messages = self._build_report_messages(messages, executor)

        last_raw_content = ""
        last_failure_reason = ""
        total_attempts = max(self._settings.max_report_retries, 1)
        for attempt in range(total_attempts):
            plan = build_attempt_plan(
                self._settings,
                base_messages=base_messages,
                last_raw_content=last_raw_content,
                last_failure_reason=last_failure_reason,
                attempt=attempt,
                total_attempts=total_attempts,
                retry_feedback_builder=_build_retry_feedback,
            )

            # 流式调用：实时接收 reasoning_content 和 content 增量
            stream = await self._client.chat.completions.create(  # type: ignore[call-overload]
                **plan.options,
                messages=plan.messages,
                response_format={"type": "json_object"},
                max_tokens=self._settings.max_output_tokens,
                stream=True,
            )

            content_parts: list[str] = []
            has_choices = False
            async for chunk in stream:
                if not chunk.choices:
                    continue
                has_choices = True
                delta = chunk.choices[0].delta

                # 实时推送 reasoning_content 到前端（DeepSeek thinking 模式）
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    getattr(delta, "reasoning", None), "content", None
                )
                if reasoning:
                    yield reasoning_event(reasoning)

                if delta.content:
                    content_parts.append(delta.content)

            if not has_choices:
                last_raw_content = ""
                last_failure_reason = "The model returned no choices"
                logger.warning("Report generation returned no choices on attempt %d", attempt + 1)
                if not plan.is_last:
                    continue
                raise ModelResponseError("The model returned no choices")

            content = "".join(content_parts)
            if not content:
                last_raw_content = ""
                last_failure_reason = "The model returned an empty response"
                logger.warning("Report generation returned empty content on attempt %d", attempt + 1)
                if not plan.is_last:
                    continue
                raise ModelResponseError("The model returned an empty response")

            json_text = extract_json(content)
            try:
                report = AnalysisReport.model_validate_json(json_text)
            except ValidationError as error:
                last_raw_content = content
                last_failure_reason = str(error)
                logger.warning("Report validation failed on attempt %d: %s", attempt + 1, error)
                if not plan.is_last:
                    continue
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
            yield report
            return

        raise ModelResponseError("The model returned an invalid analysis report")

    async def generate(self, messages: list[dict], executor: ToolExecutor) -> AnalysisReport:
        """非流式包装器：消费所有 reasoning 事件并返回最终报告。

        供 CLI 和不需要实时 reasoning 的调用方使用。
        """
        report: AnalysisReport | None = None
        async for item in self.generate_stream(messages, executor):
            if isinstance(item, AnalysisReport):
                report = item
        if report is None:
            raise ModelResponseError("Report generation did not produce a result")
        return report

    def build_report_messages(self, messages: list[dict], executor: ToolExecutor) -> list[dict]:
        """对外暴露的消息构造方法，供 IssueAgent._build_report_messages 委托调用。"""
        return self._build_report_messages(messages, executor)

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
            self._settings.max_total_context_chars
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
            ledger_budget = min(self._settings.max_investigation_ledger_chars, budget // 3)
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
                numbered = "\n".join(f"L{number}: {line}" for number, line in enumerate(content.splitlines(), 1))
                block = f"\n--- {path} ---\n{numbered}\n"
                if len(block) > budget:
                    block = block[:budget]
                user_parts.append(block)
                budget -= len(block)
                if budget <= 0:
                    break

        user_parts.append("")
        user_parts.append(final_output_prompt)
        # 在最终输出指令之后追加"报告生成阶段"强约束，避免模型在 thinking 模式下
        # 把已读源码中的 L1/L500 行号前缀误当作工具调用参数输出
        user_parts.append("")
        user_parts.append(get_report_phase_instruction())

        final_messages: list[dict] = []
        if system_prompt:
            final_messages.append({"role": "system", "content": system_prompt})
        final_messages.append({"role": "user", "content": "\n".join(user_parts)})
        return final_messages


def _build_retry_feedback(last_raw_content: str, last_failure_reason: str) -> str:
    """构造报告生成重试时的 user 消息内容。"""
    return get_report_retry_prompt(
        previous_output=last_raw_content[:1500],
        validation_error=last_failure_reason[:800],
    )
