"""JSON 解析工具：从 LLM 响应文本中提取 JSON 内容。

DeepSeek 等模型偶尔会把 JSON 包在 markdown 代码块里，或前后带额外文本。
这里提供统一的提取逻辑，供 agent.py（报告生成）和 reviewer.py（审查）复用。
"""

import re

_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def extract_json(content: str) -> str:
    """从可能含 markdown 包裹或前后噪声的文本中提取 JSON 字符串。

    优先级：
    1. 文本本身就是裸 JSON（首尾是 { 和 }）
    2. ```json ... ``` 代码块
    3. 第一个 { 到最后一个 } 之间的子串
    4. 原样返回（让上层抛 JSON 解析异常）
    """
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    match = _JSON_BLOCK.search(content)
    if match:
        return match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    return stripped[start : end + 1] if start != -1 and end > start else stripped
