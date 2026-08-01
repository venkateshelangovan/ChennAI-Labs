"""
Structured logging setup.

Why this exists now, in Stage 1, rather than later: the observability
strategy (Stage 0, Section 17) depends on every later component — event
ingestion, the recommendation pipeline, the Mesh client — logging through
one consistently-configured logger, so log lines are greppable/parseable
from day one instead of retrofitted. Nothing here is recommendation- or
AI-specific; it's just "make every log line structured JSON with a
timestamp, level, and logger name."
"""

import logging
import sys
from datetime import datetime, timezone

from app.core.config import settings


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Anything passed via `logger.info(..., extra={"foo": "bar"})`
        # shows up as a top-level field, which is what lets us log things
        # like recommendation_id/latency_ms as structured data later.
        for key, value in record.__dict__.items():
            if key not in payload and key not in logging.LogRecord(
                "", 0, "", 0, "", (), None
            ).__dict__:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root.handlers.clear()
    root.addHandler(handler)
