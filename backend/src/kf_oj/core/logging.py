import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ProductionFormatter(logging.Formatter):
    """Closed event vocabulary; arbitrary messages/arguments/tracebacks are private data."""

    EVENTS = {
        ("kf_oj.api", "Platform API started"): "api_started",
        ("kf_oj.api", "Platform API stopped"): "api_stopped",
        ("kf_oj.outbox", "outbox broker unavailable"): "outbox_broker_unavailable",
        ("kf_oj.outbox", "outbox database unavailable"): "outbox_database_unavailable",
    }
    LOGGERS = {"kf_oj.api", "kf_oj.outbox", "uvicorn", "uvicorn.error", "uvicorn.access"}
    LEVELS = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Do not call getMessage/formatException: library errors can contain
        # connection URIs, query parameters, source or diagnostic body bytes.
        event = "infrastructure_event"
        if isinstance(record.msg, str) and not record.args:
            event = self.EVENTS.get((record.name, record.msg), event)
        return json.dumps(
            {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": self.LEVELS.get(record.levelno, "UNKNOWN"),
                "logger": record.name if record.name in self.LOGGERS else "infrastructure",
                "message": event,
                "exception_present": record.exc_info is not None,
            },
            ensure_ascii=False,
        )


def configure_logging(level: str, *, production: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(ProductionFormatter() if production else JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    if production:
        # Uvicorn installs its own handlers before lifespan, which otherwise
        # bypass the root formatter when reporting an unhandled application error.
        for name in ("uvicorn", "uvicorn.error"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
        access = logging.getLogger("uvicorn.access")
        access.handlers.clear()
        access.propagate = False
