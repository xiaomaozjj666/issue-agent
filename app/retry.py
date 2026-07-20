"""多级重试策略：报告生成和审查共用同一套 thinking 降级机制。

策略（参考 DeepSeek TUI、Claude Code 的 reasoning_effort 处理）：
- 第 1 次：沿用配置的 thinking 模式（reasoning_effort=high）
- 中间几次：保留 thinking，附加错误反馈让模型纠正格式
- 最后一次：降级 thinking disabled，全部 token 预算给 content 保底
- 业务层自控重试，SDK max_retries=0 避免叠加

把"是否最后一次"和"thinking 覆盖"的判断集中在这里，
agent.py 和 reviewer.py 不再各自重复同一套 if/else。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.provider import ThinkingMode, chat_request_options


@dataclass(frozen=True)
class AttemptPlan:
    """单次重试的执行参数。"""

    is_last: bool
    options: dict
    messages: list[dict]


def build_attempt_plan(
    settings: Settings,
    *,
    base_messages: list[dict],
    last_raw_content: str,
    last_failure_reason: str,
    attempt: int,
    total_attempts: int,
    temperature: float | None = None,
    model: str | None = None,
    retry_feedback_builder=None,
) -> AttemptPlan:
    """构造第 attempt 次重试的 options 和 messages。

    Args:
        settings: 配置（提供 max_report_retries、thinking 默认值等）
        base_messages: 首次调用的消息列表
        last_raw_content: 上一次失败时模型的原始输出
        last_failure_reason: 上一次失败的校验错误描述
        attempt: 当前是第几次（0-based）
        total_attempts: 总尝试次数（>=1）
        temperature: 覆盖默认 temperature；None 表示用 provider 默认
        model: 覆盖模型名（reviewer 用 review_model）；None 表示用配置默认
        retry_feedback_builder: 可选的回调，签名 (last_raw_content, last_failure_reason) -> str，
            用于构造重试反馈消息内容。None 时表示首次调用无需反馈。

    Returns:
        AttemptPlan: 包含 is_last、options、messages
    """
    is_last = attempt == total_attempts - 1
    thinking_override: ThinkingMode | None = "disabled" if is_last else None
    attempt_options = chat_request_options(
        settings,
        model=model,
        temperature=temperature,
        thinking=thinking_override,
    )

    if attempt == 0 or retry_feedback_builder is None:
        return AttemptPlan(is_last=is_last, options=attempt_options, messages=list(base_messages))

    feedback_content = retry_feedback_builder(last_raw_content, last_failure_reason)
    retry_messages = list(base_messages)
    retry_messages.append({"role": "user", "content": feedback_content})
    return AttemptPlan(is_last=is_last, options=attempt_options, messages=retry_messages)
