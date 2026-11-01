"""Deterministic grading of an answer against a ground-truth value. No LLM judge.

Graded text is the `ANSWER:` line the system prompt asks for, so numbers quoted along the
way (row counts, intermediate totals) cannot pass a case by accident. Leak checks
(`none_of`) read the whole answer instead, because a leaked name anywhere is a failure.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# The last `ANSWER: ...` line, tolerating markdown emphasis, quotes and list markers.
_ANSWER_LINE = re.compile(r"^[\s>*_`#-]*answer\s*[:\uff1a][\s*_`]*(?P<value>.*?)[\s*_`]*$",
                          re.IGNORECASE | re.MULTILINE)
_NUMBER = re.compile(
    r"(?<![\w.])(?P<num>-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?)(?!\.?\w)"
    r"\s*(?P<pct>%|percent\b)?", re.IGNORECASE)
_MIN_ROUNDED_SIGNIFICANT_DIGITS = 2


@dataclass(frozen=True)
class AnswerNumber:
    value: float
    decimals: int
    percent: bool

    @property
    def significant_digits(self) -> int:
        digits = f"{abs(self.value):.{self.decimals}f}".replace(".", "").lstrip("0")
        return len(digits)


def extract_answer(text: str) -> str | None:
    """The value of the final `ANSWER:` line, or None when the agent omitted it."""
    matches = list(_ANSWER_LINE.finditer(text))
    return matches[-1].group("value").strip() if matches else None


def extract_numbers(text: str) -> list[AnswerNumber]:
    found = []
    for m in _NUMBER.finditer(text.replace("\u2212", "-")):
        found.append(AnswerNumber(float(m.group("num").replace(",", "")),
                                  len(m.group("dec") or ""), bool(m.group("pct"))))
    return found


def number_matches(expected: float, found: AnswerNumber, *, rel_tol: float,
                   abs_tol: float = 0.0, percent: bool = False) -> bool:
    """Within tolerance, or `expected` correctly rounded to the precision the agent showed.

    `percent=True` means `expected` is a fraction (0.81) that may be stated as 81% or 0.81.
    Rounding credit needs two significant digits, so "2" does not pass for 1.86.
    """
    # (value on the expected's scale, divisor applied to the stated figure)
    candidates = [(found.value / 100, 100)] if found.percent else [(found.value, 1)]
    if percent and not found.percent:
        candidates.append((found.value / 100, 100))  # "81" or "81.0" without the % sign
    for value, scale in candidates:
        if math.isclose(value, expected, rel_tol=rel_tol, abs_tol=abs_tol):
            return True
        half_step = 0.5 * 10 ** -found.decimals / scale
        if (found.significant_digits >= _MIN_ROUNDED_SIGNIFICANT_DIGITS
                and abs(value - expected) <= half_step * (1 + 1e-9)):
            return True
    return False


def normalise(text: str) -> str:
    """Case-, whitespace- and underscore-insensitive form used for name matching."""
    return " ".join(text.casefold().replace("_", " ").replace("\u2019", "'").split())


def contains_value(haystack: str, value: str) -> bool:
    """`value` appears as a whole token sequence, so 'enwiki' does not match 'enwikinews'."""
    pattern = r"(?<![\w])" + re.escape(normalise(value)) + r"(?![\w])"
    return re.search(pattern, normalise(haystack)) is not None
