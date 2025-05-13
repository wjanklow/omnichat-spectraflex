# guardrails.py
"""
Toxic-content check, simple keyword block, and PII scrubber.
• toxic_or_blocked(text) → bool
• scrub(text) → str           (redacts emails / phone numbers)
You can refine later; this keeps the import errors away.
"""

import re
from typing import Set

try:
    import openai
except ImportError:  # allow import even if OpenAI not installed yet
    openai = None

# quick keyword block-list (expand as needed)
_BAD_TOPICS: Set[str] = {
    "dosage", "prescription", "lawsuit", "bomb", "weapon"
}

_PII_RE = re.compile(
    r"[\w\.-]+@[\w\.-]+|"          # emails
    r"\+?\d[\d\s\-\(\)]{7,}"       # phone numbers
)

def toxic_or_blocked(msg: str) -> bool:
    """Return True if message is toxic, illegal, or off-limits."""
    # basic keyword filter
    if any(word in msg.lower() for word in _BAD_TOPICS):
        return True

    # optional OpenAI moderation (skip if key not set)
    if openai and openai.api_key:
        try:
            flagged = openai.moderations.create(input=msg).results[0].flagged
            if flagged:
                return True
        except Exception:  # network / quota errors -> just skip
            pass

    return False

def scrub(text: str) -> str:
    """Redact obvious PII (email / phone) from text."""
    return _PII_RE.sub("[redacted]", text)
