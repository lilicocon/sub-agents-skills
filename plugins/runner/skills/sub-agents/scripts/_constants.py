from __future__ import annotations

DEFAULT_TIMEOUT_MS = 600000
# A timeout below this is almost always seconds typed where milliseconds are read.
MIN_TIMEOUT_MS = 1000
_TIMEOUT_SUFFIXES_MS = (("ms", 1), ("s", 1000), ("m", 60000))


def parse_timeout_ms(value: str) -> int:
    """Read a timeout in milliseconds, accepting an explicit ``ms``/``s``/``m`` unit.

    A bare number stays milliseconds for compatibility, but a value small enough to
    be a seconds/milliseconds mix-up is rejected instead of expiring immediately.
    """
    text = str(value).strip().lower()
    if not text:
        raise ValueError("timeout must not be empty")

    number, multiplier = text, 1
    for suffix, factor in _TIMEOUT_SUFFIXES_MS:
        if text.endswith(suffix):
            number, multiplier = text[: -len(suffix)].strip(), factor
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
    if total < MIN_TIMEOUT_MS:
        raise ValueError(
            f"Timeout {value!r} is {total} ms, which expires before any backend replies. "
            f"Timeouts are milliseconds: write '{amount}s' or {amount * 1000} "
            f"for {amount} seconds."
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
