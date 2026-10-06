"""Signal 2: stylometric heuristics (pure Python, no network).

Measures statistical *uniformity*. AI prose tends to have even sentence lengths,
sparse expressive punctuation, and consistent clause complexity; human writing
is burstier on all three. Vocabulary diversity runs the other way on the
calibration set: current models avoid repeating words, while human writers
repeat for rhythm and emphasis, so a *higher* moving-average type-token ratio
is the AI-like end (see planning.md, Decision log, 2026-10-06).

Contract (see planning.md, "Signal 2"):
  - four features, each mapped to a sub-score in [0, 1] (1 = AI-like) by linear
    interpolation between an AI-like end and a human-like end
  - p_stylo = 0.35*s_cv + 0.30*s_mattr + 0.20*s_punct + 0.15*s_clause
  - reliable = False when < 40 words or < 3 sentences
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field

MIN_RELIABLE_WORDS = 40
MIN_RELIABLE_SENTENCES = 3
MATTR_WINDOW = 50
# Below this many words the type-token ratio is inflated for everyone, so the
# MATTR sub-score is damped toward neutral in proportion to length.
MATTR_FULL_WEIGHT_WORDS = 100

# (ai_like_end, human_like_end) for each feature. Values at or beyond the AI end
# score 1.0; at or beyond the human end score 0.0. The order of the pair gives
# the direction. Tuned on calibration/{human,ai}.json, 2026-10-06.
REFERENCE_RANGES: dict[str, tuple[float, float]] = {
    "sentence_len_cv": (0.25, 0.70),          # low variance  -> AI
    "mattr": (0.90, 0.72),                    # HIGH diversity -> AI (flipped)
    "punct_per_100w": (1.0, 6.0),             # sparse punctuation -> AI
    "clause_markers_per_sent": (0.50, 2.00),  # steady clause complexity -> AI
}

WEIGHTS: dict[str, float] = {
    "sentence_len_cv": 0.35,
    "mattr": 0.30,
    "punct_per_100w": 0.20,
    "clause_markers_per_sent": 0.15,
}

# Sentence boundaries: terminal punctuation runs, or a line break (so verse
# lines count as units too).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\"'”’)]*\s+|\n+")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
# "Expressive" punctuation only; ordinary periods and commas are excluded.
_EXPRESSIVE_PUNCT = re.compile(r"[!?;:—–…()\"“”]|\.\.\.")
_CLAUSE_PUNCT = re.compile(r"[,;]")
_CLAUSE_WORDS = re.compile(r"\b(which|that|because|although|while|when)\b", re.IGNORECASE)


@dataclass
class StylometryResult:
    ai_probability: float
    reliable: bool
    word_count: int
    sentence_count: int
    features: dict[str, float] = field(default_factory=dict)
    sub_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if p and _WORD.search(p)]


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _mattr(words: list[str], window: int = MATTR_WINDOW) -> float:
    """Moving-average type-token ratio; plain TTR when shorter than one window."""
    n = len(words)
    if n == 0:
        return 0.0
    if n <= window:
        return len(set(words)) / n
    total = 0.0
    for i in range(n - window + 1):
        total += len(set(words[i : i + window])) / window
    return total / (n - window + 1)


def _sub_score(feature: str, value: float) -> float:
    """Linear map from the human-like end (0.0) to the AI-like end (1.0), clamped.
    Works whichever end is numerically larger."""
    ai_end, human_end = REFERENCE_RANGES[feature]
    s = (value - human_end) / (ai_end - human_end)
    return min(1.0, max(0.0, s))


def features_for(text: str) -> tuple[dict[str, float], int, int]:
    sentences = _sentences(text)
    words = _words(text)
    n_words, n_sent = len(words), len(sentences)

    lens = [len(_words(s)) for s in sentences]
    if len(lens) >= 2 and statistics.mean(lens) > 0:
        cv = statistics.pstdev(lens) / statistics.mean(lens)
    else:
        cv = 0.0

    punct = len(_EXPRESSIVE_PUNCT.findall(text))
    punct_per_100w = (punct / n_words * 100.0) if n_words else 0.0

    clause_counts = [
        len(_CLAUSE_PUNCT.findall(s)) + len(_CLAUSE_WORDS.findall(s)) for s in sentences
    ]
    clause_std = statistics.pstdev(clause_counts) if len(clause_counts) >= 2 else 0.0

    feats = {
        "sentence_len_cv": round(cv, 4),
        "mattr": round(_mattr(words), 4),
        "punct_per_100w": round(punct_per_100w, 4),
        "clause_markers_per_sent": round(clause_std, 4),
    }
    return feats, n_words, n_sent


def score(text: str) -> StylometryResult:
    """Compute the stylometric AI-likeness of ``text``. Pure; never raises on odd input."""
    feats, n_words, n_sent = features_for(text or "")
    subs = {k: _sub_score(k, v) for k, v in feats.items()}

    # Damp the diversity sub-score toward neutral on short texts.
    damp = min(1.0, n_words / MATTR_FULL_WEIGHT_WORDS)
    subs["mattr"] = 0.5 + (subs["mattr"] - 0.5) * damp
    subs = {k: round(v, 4) for k, v in subs.items()}

    p = sum(WEIGHTS[k] * subs[k] for k in WEIGHTS)
    reliable = n_words >= MIN_RELIABLE_WORDS and n_sent >= MIN_RELIABLE_SENTENCES
    return StylometryResult(
        ai_probability=round(min(1.0, max(0.0, p)), 4),
        reliable=reliable,
        word_count=n_words,
        sentence_count=n_sent,
        features=feats,
        sub_scores=subs,
    )
