from __future__ import annotations

DEFAULT_TIMEOUT_MS = 600000
# A bare number below this is almost always seconds typed where milliseconds are
# read. A value carrying a unit has already said what it means, so it is taken as
# written -- the guard exists only to catch the ambiguity, not to impose a floor.
MIN_BARE_TIMEOUT_MS = 1000
_TIMEOUT_SUFFIXES_MS = (("ms", 1), ("s", 1000), ("m", 60000))


def _plural(amount: int, noun: str) -> str:
    return f"{amount} {noun}" if amount == 1 else f"{amount} {noun}s"


def parse_timeout_ms(value: str) -> int:
    """Read a timeout in milliseconds, accepting an explicit ``ms``/``s``/``m`` unit.

    A bare number stays milliseconds for compatibility, but one small enough to be a
    seconds/milliseconds mix-up is rejected instead of expiring on arrival. A united
    value is unambiguous, so it only has to be positive.
    """
    text = str(value).strip().lower()
    if not text:
        raise ValueError("timeout must not be empty")

    number, multiplier, united = text, 1, False
    for suffix, factor in _TIMEOUT_SUFFIXES_MS:
        if text.endswith(suffix):
            number, multiplier, united = text[: -len(suffix)].strip(), factor, True
            break

    try:
        amount = int(number)
    except ValueError:
        raise ValueError(
            f"Invalid timeout {value!r}. Use milliseconds (600000) or a unit (600s, 10m)."
        ) from None

    if amount <= 0:
        raise ValueError("timeout must be positive")

    total = amount * multiplier
    if not united and total < MIN_BARE_TIMEOUT_MS:
        raise ValueError(
            f"Timeout {value!r} is {_plural(total, 'millisecond')}, which expires before "
            f"any backend replies. A bare number is milliseconds: write '{amount}s' for "
            f"{_plural(amount, 'second')}, or {amount * 1000}. Write '{amount}ms' to keep "
            "the short deadline."
        )
    return total


SUPPORTED_CLIS = (
    "codex",
    "claude",
    "cursor-agent",
    "glm",
    "kimi",
    "grok",
    "antigravity",
    "gemini",
    "opencode",
    "command-code",
)
SUPPORTED_CLIS_HELP = ", ".join(SUPPORTED_CLIS)


def format_concatenated_prompt(system_context: str, prompt: str) -> str:
    return f"[System Context]\n{system_context}\n\n[User Prompt]\n{prompt}"
