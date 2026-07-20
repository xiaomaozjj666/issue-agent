"""Logging configuration: console and JSON-line formatters.

Supports two output formats controlled by ``LOG_FORMAT`` env var:
- ``console`` (default): human-readable single-line format.
- ``json``: structured JSON lines for log aggregators (Loki, ELK, etc.).
"""

import logging
import os

_ISSUE_AGENT_HANDLER = "_issue_agent_handler"


def setup_logging(*, level: int | str | None = None) -> None:
    """Configure application logging.

    In production set ``LOG_LEVEL=INFO`` or ``LOG_LEVEL=WARNING``.
    Set ``LOG_FORMAT=json`` to emit JSON lines for log aggregators.
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO").upper()

    fmt = os.getenv("LOG_FORMAT", "console").lower()
    if fmt == "json":
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s | %(message)s", datefmt="%H:%M:%S")
        )

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        if getattr(existing, _ISSUE_AGENT_HANDLER, False):
            root.removeHandler(existing)
            existing.close()
    setattr(handler, _ISSUE_AGENT_HANDLER, True)
    root.addHandler(handler)


class _JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines with exception info when present."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)
