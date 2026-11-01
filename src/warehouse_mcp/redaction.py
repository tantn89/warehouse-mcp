"""Second net for free text: redact emails and phone numbers that people type into comments.

Column tags cover identifiers the model knows about. Free text (edit summaries, ticket
bodies) can hold anything, so values in columns tagged `free_text` are scanned on the way
out as well.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 8+ digits, optionally with a leading +, separated by spaces, dots, dashes or parentheses.
_PHONE = re.compile(r"(?<![\w])\+?\d(?:[\s().-]?\d){7,14}(?![\w])")


def redact(text: str, phones: bool) -> tuple[str, int]:
    """Return the text with emails (and phone numbers if asked) replaced, and the count.

    Emails are redacted in every string value: the pattern almost never fires on anything
    else. Phone numbers only in free-text columns: titles like "1990 1999" look like one.
    """
    text, found = _EMAIL.subn("[email]", text)
    if phones:
        text, more = _PHONE.subn("[phone]", text)
        found += more
    return text, found
