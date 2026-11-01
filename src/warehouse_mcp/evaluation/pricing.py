"""Token cost estimate for one agent run, from list prices.

The Messages API reports tokens, not dollars, so cost is computed here. Prices mirror the
table used by the sibling filings-copilot eval; verify them against
https://www.anthropic.com/pricing before trusting cost gates on a new model.
"""

from __future__ import annotations

# USD per million tokens: (input, output, cache read). Cache writes bill at
# CACHE_WRITE_MULTIPLIER x input. Unknown models get no estimate rather than a wrong one.
PRICE_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25


def estimate_cost_usd(model: str, *, input_tokens: int, output_tokens: int,
                      cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float | None:
    """Cost in USD; `input_tokens` excludes cached tokens, as the API reports it."""
    if model not in PRICE_PER_MTOK:
        return None
    price_in, price_out, price_cache_read = PRICE_PER_MTOK[model]
    return (input_tokens * price_in
            + cache_write_tokens * price_in * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * price_cache_read
            + output_tokens * price_out) / 1e6
