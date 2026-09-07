"""Logging setup with hard redaction of secrets.

Rules.md §3: credentials and file contents must never appear in logs, at any level.
A filter is used rather than developer discipline because discipline is not a control.
"""

from __future__ import annotations

import logging
import re
from logging.config import dictConfig

from app.core.config import settings

# Patterns that must never survive into a log record.
_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # AWS access key ids and secret keys
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY_ID]"),
    (re.compile(r"(?i)(aws_secret_access_key\W{0,3})([A-Za-z0-9/+=]{40})"), r"\1[REDACTED]"),
    # Bearer tokens / JWTs
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}"), "[REDACTED_JWT]"),
    # Generic key=value secrets. The leading [A-Za-z0-9_-]* is load-bearing: `\b` does
    # not match between `_` and `A`, so a prefixed name like FILESHARE_VIRUSTOTAL_API_KEY
    # would otherwise slip straight through.
    (
        re.compile(
            r"(?i)([A-Za-z0-9_\-]*(?:api[_-]?key|secret|password|passwd|token|authorization))"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',;)]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    # Postgres/Redis connection strings with inline credentials
    (re.compile(r"(?i)\b([a-z+]+://)([^:/\s]+):([^@/\s]+)@"), r"\1\2:[REDACTED]@"),
]


class RedactingFilter(logging.Filter):
    """Scrubs known secret shapes from the formatted message and its args."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not break logging
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text: str) -> str:
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def configure_logging() -> None:
    _filtered_logger = {
        "handlers": ["console"],
        "level": settings.log_level,
        "propagate": False,
    }
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"redact": {"()": RedactingFilter}},
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)-8s %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%dT%H:%M:%S%z",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "filters": ["redact"],
                }
            },
            "root": {"handlers": ["console"], "level": settings.log_level},
            "loggers": {
                # Access logs echo query strings; keep the filter on them too.
                "uvicorn.access": _filtered_logger,
                "uvicorn.error": _filtered_logger,
                "celery": _filtered_logger,
                # botocore is extremely chatty at DEBUG and echoes signed URLs.
                "botocore": {"level": "WARNING"},
                "boto3": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
            },
        }
    )
