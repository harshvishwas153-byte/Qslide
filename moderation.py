import os
import re
import unicodedata


class ModerationError(ValueError):
    pass


DEFAULT_BLOCKED_TERMS = (
    "asshole",
    "bastard",
    "bitch",
    "bullshit",
    "chutiya",
    "dick",
    "fuck",
    "fucker",
    "fucking",
    "harami",
    "madarchod",
    "motherfucker",
    "randi",
    "shit",
    "slut",
    "whore",
)

TARGETED_ABUSE_PATTERNS = (
    r"\bgo\s+to\s+hell\b",
    r"\bkill\s+yourself\b",
)

LEETSPEAK_TRANSLATION = str.maketrans({
    "@": "a",
    "$": "s",
    "0": "o",
    "1": "i",
    "!": "i",
    "3": "e",
    "4": "a",
    "5": "s",
    "7": "t",
})


def _extra_blocked_terms():
    raw_terms = os.environ.get("BLOCKED_CONTENT_TERMS", "")
    return tuple(term.strip().casefold() for term in raw_terms.split(",") if term.strip())


def _normalize_text(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.translate(LEETSPEAK_TRANSLATION)
    text = re.sub(r"(.)\1{2,}", r"\1", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _iter_text(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_text(nested)
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_text(nested)
    elif value is not None:
        yield str(value)


def find_abusive_term(value):
    normalized_values = [_normalize_text(text) for text in _iter_text(value)]
    blocked_terms = set(DEFAULT_BLOCKED_TERMS) | set(_extra_blocked_terms())

    for normalized in normalized_values:
        if not normalized:
            continue
        for pattern in TARGETED_ABUSE_PATTERNS:
            if re.search(pattern, normalized):
                return "abusive phrase"
        for term in blocked_terms:
            normalized_term = _normalize_text(term)
            if normalized_term and re.search(rf"\b{re.escape(normalized_term)}\b", normalized):
                return normalized_term
    return None


def validate_no_abusive_content(value, label="content"):
    blocked_term = find_abusive_term(value)
    if blocked_term:
        raise ModerationError(
            f"{label} contains abusive or offensive language. Please remove it and try again."
        )
