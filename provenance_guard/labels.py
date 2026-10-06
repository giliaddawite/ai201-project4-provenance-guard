"""Transparency labels and confidence bands.

The three label strings below are the canonical copies. planning.md and the
README quote them verbatim; if you change them here, change them there too.
"""

from __future__ import annotations

AI_THRESHOLD = 0.75      # ai_probability >= this  -> "ai"  (lowered from 0.80 after calibration, see planning.md)
HUMAN_THRESHOLD = 0.20   # ai_probability <= this  -> "human"

UNDER_REVIEW_SUFFIX = "The creator has appealed this label and it is under review."

LABELS: dict[str, dict[str, str]] = {
    "ai": {
        "title": "Likely made with AI",
        "body": (
            "Our checks found strong signs that this text was produced with an AI "
            "writing tool rather than written by a person. The creator has not "
            "confirmed this, and may appeal if we got it wrong. Please keep that in "
            "mind as you read."
        ),
    },
    "human": {
        "title": "Likely written by a person",
        "body": (
            "Our checks found strong signs that a person wrote this text themselves. "
            "We found no clear indication of an AI writing tool. No check is perfect, "
            "but this piece looks like original human work."
        ),
    },
    "uncertain": {
        "title": "Unclear origin",
        "body": (
            "We could not tell whether this text was written by a person or produced "
            "with an AI writing tool. Our checks disagreed or did not find strong "
            "evidence either way. Please read it with that in mind. This is not an "
            "accusation."
        ),
    },
}


def band(ai_probability: float) -> str:
    """Map a probability to one of 'ai', 'human', 'uncertain'."""
    if ai_probability >= AI_THRESHOLD:
        return "ai"
    if ai_probability <= HUMAN_THRESHOLD:
        return "human"
    return "uncertain"


def label_for(ai_probability: float, under_review: bool = False) -> dict[str, str]:
    """Return {variant, title, body} for display to a reader."""
    variant = band(ai_probability)
    text = LABELS[variant]
    body = text["body"]
    if under_review:
        body = f"{body} {UNDER_REVIEW_SUFFIX}"
    return {"variant": variant, "title": text["title"], "body": body}
