"""Redaction for logs and error text — the last line of the PII defence.

The vault stops PII reaching the *model*. This stops it reaching a *log file*, which is a
different escape route with its own failure mode: an exception message carrying the address
that caused it, a debug line dumping a raw element, a stack trace with an argument inlined.

Deliberately **pattern-based and vault-free**, unlike the tokenizer. Logs do not need to be
reversible, they need to be safe, and a scrubber that required a session vault could not be
installed on the root logger at process start — which is exactly where it has to live to
catch the lines nobody thought about.

Names are not scrubbed here. Doing so needs the session's learned-name list, and a log
scrubber that mangles every capitalised word would make logs unreadable while adding little:
the deterministic classes are what actually identify a person.
"""
from __future__ import annotations

import logging

from app.security.patterns import EMAIL_RE, find_phones

EMAIL_PLACEHOLDER = "[email]"
PHONE_PLACEHOLDER = "[phone]"


def scrub(text: str) -> str:
    """Remove addresses and phone numbers from free text."""
    if not text:
        return text
    scrubbed = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    for phone in find_phones(scrubbed):
        scrubbed = scrubbed.replace(phone, PHONE_PLACEHOLDER)
    return scrubbed


class RedactionFilter(logging.Filter):
    """Scrubs every log record on its way out.

    Applied to the message, the formatting args, and any exception text. Args matter more
    than they look: `logger.info("sending to %s", address)` keeps the address out of
    `record.msg` entirely and puts it in `record.args`, so a filter that only cleaned the
    message would miss the most common way an address gets logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: scrub(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    scrub(value) if isinstance(value, str) else value for value in record.args
                )

        # Never drop a record — this filter redacts, it does not censor. Losing a log line
        # because it happened to contain an address would hide the incident you most want.
        return True


def install_redaction(logger: logging.Logger | None = None) -> RedactionFilter:
    """Install the filter at the root, so nothing has to opt in.

    Called from the composition root at startup. Opt-in redaction protects the loggers
    someone remembered; this protects the ones they did not.
    """
    target = logger or logging.getLogger()
    for existing in target.filters:
        if isinstance(existing, RedactionFilter):
            return existing

    redaction = RedactionFilter()
    target.addFilter(redaction)
    # A filter on a logger does not apply to records from its children, only to records
    # logged directly on it. Handlers see everything that propagates up, so the filter goes
    # on the handlers too — this is the difference between working and only appearing to.
    for handler in target.handlers:
        handler.addFilter(redaction)
    return redaction
