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

        issue_agent_handlers = [
            handler for handler in root.handlers if getattr(handler, "_issue_agent_handler", False)
        ]
        assert host_handler in root.handlers
        assert len(issue_agent_handlers) == 1
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
