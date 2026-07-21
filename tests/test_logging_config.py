import json
import logging

from app.logging_config import setup_logging


def test_setup_logging_preserves_host_handlers_and_is_idempotent() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    host_handler = logging.NullHandler()
    root.addHandler(host_handler)
    try:
        setup_logging(level="WARNING")
        setup_logging(level="INFO")

        issue_agent_handlers = [handler for handler in root.handlers if getattr(handler, "_issue_agent_handler", False)]
        assert host_handler in root.handlers
        assert len(issue_agent_handlers) == 1
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_json_formatter_emits_valid_json_with_fields() -> None:
    """The JSON formatter produces a single-line JSON object with ts/level/logger/msg."""
    from app.logging_config import _JSONFormatter

    formatter = _JSONFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="test message %s",
        args=("formatted",),
        exc_info=None,
    )

    output = formatter.format(record)
    payload = json.loads(output)

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.test"
    assert payload["msg"] == "test message formatted"
    assert "ts" in payload


def test_json_formatter_includes_exc_info_when_present() -> None:
    """Exception info is serialized into the exc_info field."""
    from app.logging_config import _JSONFormatter

    formatter = _JSONFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    output = formatter.format(record)
    payload = json.loads(output)

    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]
