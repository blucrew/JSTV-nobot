"""Detect simple-refusal 'no' messages while ignoring natural usage in longer thoughts."""
import re

# Strict whitelist of phrases that count as a standalone refusal after normalization.
_WHITELIST: set[str] = {
    "no",
    "nope",
    "nah",
    "naw",
    "nuh uh",
    "nuhuh",
    "no way",
    "hell no",
    "heck no",
    "absolutely not",
    "no thanks",
    "no thank you",
    "nada",
    "negative",
    "not a chance",
    "no sir",
    "no ma'am",
    "no maam",
    "no siree",
    "definitely not",
}

_TRAILING_PUNCT = re.compile(r"[.!?,;:~\s]+$")
_LEADING_PUNCT = re.compile(r"^[\s.!?,;:~]+")
_REPEATED_CHAR = re.compile(r"(.)\1{2,}")  # 3+ of same char → 1


def _normalize(text: str) -> str:
    t = text.strip().lower()
    t = _LEADING_PUNCT.sub("", t)
    t = _TRAILING_PUNCT.sub("", t)
    # collapse extended vowels/consonants: "noooo" → "no", "nahhh" → "nah"
    t = _REPEATED_CHAR.sub(r"\1", t)
    # collapse internal whitespace
    t = re.sub(r"\s+", " ", t)
    return t


def is_simple_no(text: str) -> bool:
    """True if the text reads as a standalone refusal.

    Returns False for natural usage inside longer thoughts ('I don't know', 'no
    problem', 'there's no way to fix that properly', etc.) because those fail
    the whitelist or the length cap.
    """
    if not text:
        return False
    t = _normalize(text)
    if not t:
        return False
    # Hard caps so something like "no, I already tried that and it didn't work"
    # never matches even if it starts with 'no'.
    if len(t) > 20 or len(t.split()) > 3:
        return False
    return t in _WHITELIST
