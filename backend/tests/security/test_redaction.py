"""Log redaction — the escape route the vault does not cover."""
from __future__ import annotations

import logging

import pytest

from app.security.redaction import RedactionFilter, install_redaction, scrub

# ── scrubbing ───────────────────────────────────────────────────────────────


def test_addresses_are_removed():
    assert scrub("failed sending to priya@corp.com") == "failed sending to [email]"


def test_phones_are_removed():
    assert "98765" not in scrub("called +91 98765 43210")


def test_surrounding_text_survives():
    """A log line that redacts everything tells you nothing about the incident."""
    out = scrub("ACTION_TIMEOUT clicking Send for priya@corp.com after 20s")
    assert out.startswith("ACTION_TIMEOUT clicking Send for")
    assert out.endswith("after 20s")


def test_ordinary_numbers_survive():
    assert scrub("step 12 of 40, 2026-08-20") == "step 12 of 40, 2026-08-20"


def test_empty_is_safe():
    assert scrub("") == ""


# ── the logging filter ──────────────────────────────────────────────────────


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.INFO)
    return caplog


def test_the_message_is_scrubbed(captured):
    logger = logging.getLogger("test.msg")
    logger.addFilter(RedactionFilter())
    logger.info("bounced: priya@corp.com")

    assert "priya@corp.com" not in captured.text
    assert "[email]" in captured.text


def test_formatting_args_are_scrubbed(captured):
    """`logger.info("sending to %s", addr)` is the COMMON case, and it never touches msg."""
    logger = logging.getLogger("test.args")
    logger.addFilter(RedactionFilter())
    logger.info("sending to %s", "priya@corp.com")

    assert "priya@corp.com" not in captured.text
    assert "[email]" in captured.text


def test_dict_args_are_scrubbed(captured):
    logger = logging.getLogger("test.dict")
    logger.addFilter(RedactionFilter())
    logger.info("recipient %(who)s", {"who": "priya@corp.com"})

    assert "priya@corp.com" not in captured.text


def test_non_string_args_pass_through(captured):
    logger = logging.getLogger("test.nonstr")
    logger.addFilter(RedactionFilter())
    logger.info("step %d of %d", 3, 40)

    assert "step 3 of 40" in captured.text


def test_records_are_redacted_never_dropped(captured):
    """Losing a log line because it held an address would hide the incident you most want."""
    logger = logging.getLogger("test.keep")
    logger.addFilter(RedactionFilter())
    logger.warning("STUCK after mailing priya@corp.com")

    assert "STUCK after mailing" in captured.text
    assert len(captured.records) == 1


# ── installation ────────────────────────────────────────────────────────────


def test_install_is_idempotent():
    logger = logging.getLogger("test.install")
    logger.filters.clear()

    first = install_redaction(logger)
    second = install_redaction(logger)

    assert first is second
    assert sum(isinstance(f, RedactionFilter) for f in logger.filters) == 1


def test_install_also_covers_handlers():
    """A logger-level filter does not see records propagating up from children."""
    logger = logging.getLogger("test.handlers")
    logger.filters.clear()
    handler = logging.StreamHandler()
    handler.filters.clear()
    logger.addHandler(handler)

    install_redaction(logger)

    assert any(isinstance(f, RedactionFilter) for f in handler.filters)
    logger.removeHandler(handler)
