"""json_utils.extract_json 全分支覆盖测试。"""


from app.json_utils import extract_json


def test_extract_json_returns_bare_json_unchanged() -> None:
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_strips_surrounding_whitespace() -> None:
    assert extract_json('  \n{"a": 1}\n  ') == '{"a": 1}'


def test_extract_json_from_markdown_fence() -> None:
    text = 'Here is the result:\n```json\n{"a": 1}\n```\nThanks.'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_from_plain_fence() -> None:
    text = '```\n{"a": 1}\n```'
    assert extract_json(text) == '{"a": 1}'


def test_extract_json_from_noisy_text_between_braces() -> None:
    text = 'prefix {"a": 1, "b": [1,2]} suffix'
    assert extract_json(text) == '{"a": 1, "b": [1,2]}'


def test_extract_json_returns_original_when_no_braces() -> None:
    text = "the model returned no json at all"
    assert extract_json(text) == text


def test_extract_json_single_brace_returns_original() -> None:
    """只有起始 { 没有结束 }：无法截取，原样返回让上层抛解析异常。"""
    text = '{"unfinished'
    assert extract_json(text) == text
