"""Pure logging vocabulary checks, not evidence of real request/daemon log privacy."""

import json
import logging

from kf_oj.core.logging import ProductionFormatter, configure_logging


def test_production_does_not_serialize_unapproved_message_arguments_or_exception_data():
    record = logging.LogRecord(
        "external_library",
        logging.ERROR,
        "private/path",
        5,
        "temporary fixture token=%s",
        ("fixture-only",),
        (ValueError, ValueError("fixture-only"), None),
    )
    value = json.loads(ProductionFormatter().format(record))
    assert value["message"] == "infrastructure_event"
    assert value["logger"] == "infrastructure"
    assert value["exception_present"] is True
    assert "fixture-only" not in json.dumps(value)
    assert "private/path" not in json.dumps(value)


def test_only_exact_lifecycle_message_without_arguments_is_retained():
    record = logging.LogRecord(
        "kf_oj.api", logging.INFO, "unused", 1, "Platform API started", (), None
    )
    assert json.loads(ProductionFormatter().format(record))["message"] == "api_started"
    record.args = ("fixture-only",)
    assert json.loads(ProductionFormatter().format(record))["message"] == "infrastructure_event"


def test_custom_logger_or_level_names_do_not_expand_the_output_vocabulary():
    record = logging.LogRecord("fixture-only", logging.ERROR, "unused", 1, {}, (), None)
    record.levelname = "fixture-only" * 1024
    value = ProductionFormatter().format(record)
    assert len(value) < 512
    assert "fixture-only" not in value


def test_uvicorn_error_handlers_cannot_bypass_production_formatter(capsys):
    loggers = [
        logging.getLogger(name) for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    original = [(logger, logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
    try:
        configure_logging("INFO", production=True)
        assert not logging.getLogger("uvicorn.access").hasHandlers()
        logging.getLogger("uvicorn.error").error("fixture-only %s", "temporary value")
        value = json.loads(capsys.readouterr().err)
        assert value["message"] == "infrastructure_event"
        assert "fixture-only" not in json.dumps(value)
    finally:
        for logger, handlers, level, propagate in original:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
